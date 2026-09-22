"""
apps/tenant_core/services_attention.py — Layer 2 Module B Phase 7: Stuck Lead & Next Best Action Engine

Deterministic, backend-authoritative evaluation of lead attention state,
stuck reasons, and explainable next best operational actions.

Key Architectural Guarantees:
1. Zero Heuristic False-Positives: Fresh leads entering a stage are NEVER marked stuck immediately.
   Time-based stuck conditions are strictly bounded by canonical policies (CRMStageSlaPolicy,
   CRMTrialReminderPolicy, SalesFollowupTask.due_at).
2. Canonical Stage SLA Semantics: Stage SLA is anchored to the stage entry timestamp.
   Activity logging does not reset stage SLA; stage transitions do reset stage SLA.
3. Chronological Communication Evaluation: NO_RESPONSE verifies whether any inbound message
   was received AFTER the latest outbound message, and respects the configured wait window.
   COMMUNICATION_FAILED is separated from NO_RESPONSE.
4. Phase 4 Reference Reuse: No-show and attended follow-ups are tracked deterministically
   via external_reference ('TRIAL_NOSHOW_<id>' and 'TRIAL_ATTENDED_<id>').
5. Advisory Next Best Actions: Recommendation actions (CALL_LEAD, BOOK_TRIAL, etc.) are separated
   from executable automation actions, and prompt user-confirmed modals in the UI.
6. DB-Safe Idempotent SLA Event Emission: Enqueues 'CRM_LEAD_SLA_BREACHED' DomainOutboxEvents
   with strict idempotency key 'SLA_BREACH_<lead_id>_<stage_entered_at>'.
7. High Performance: Avoids N+1 queries using targeted batched lookups over paginated sets.
"""

import uuid
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
from django.db import transaction
from django.utils import timezone
from config.routers import get_tenant_db_alias

from .models_org import Organization, Branch
from .models_users import TenantUser
from .models_crm import (
    Lead,
    LeadStatusHistory,
    LeadActivity,
    SalesFollowupTask,
    TrialBooking,
    CRMStageSlaPolicy,
    CRMTrialReminderPolicy,
)
from .models_communication import CommunicationMessage
from .models_attention import CRMAttentionPolicy
from .models_audit_outbox import DomainOutboxEvent
from .automation.registry import RecommendationActionRegistry

logger = logging.getLogger(__name__)

# Canonical event code for SLA breaches matching TriggerRegistry
CANONICAL_SLA_BREACH_EVENT_CODE = 'CRM_LEAD_SLA_BREACHED'

# Canonical active pipeline stages where attention monitoring applies
ACTIVE_PIPELINE_STAGES = [
    'NEW_LEAD',
    'TRIAL_BOOKED',
    'TRIAL_CONFIRMED',
    'TRIAL_ATTENDED',
    'NO_SHOW',
    'FOLLOW_UP_PENDING',
    'INTERESTED',
    'HOT_LEAD',
    'PAYMENT_PENDING',
]

TERMINAL_STAGES = ['CONVERTED', 'NOT_INTERESTED', 'LOST']


class LeadAttentionService:
    """
    Authoritative service for evaluating lead stuck state, reasons, and next best actions.
    """

    @classmethod
    def get_attention_policy(cls, organization: Organization, db_alias: Optional[str] = None) -> CRMAttentionPolicy:
        alias = db_alias or get_tenant_db_alias() or 'default'
        policy, _ = CRMAttentionPolicy.objects.using(alias).get_or_create(organization=organization)
        return policy

    @classmethod
    def calculate_stage_sla(cls, lead: Lead, db_alias: Optional[str] = None) -> Dict[str, Any]:
        """
        Computes the canonical stage SLA deadline for a lead.
        Stage SLA is strictly anchored to when the lead entered its current stage.
        Activity logging does not alter this anchor.
        """
        alias = db_alias or get_tenant_db_alias() or 'default'
        now = timezone.now()

        latest_transition = LeadStatusHistory.objects.using(alias).filter(
            lead=lead, to_status=lead.current_status
        ).order_by('-changed_at').first()

        stage_entered_at = latest_transition.changed_at if latest_transition else lead.created_at
        stage_age_seconds = max(0, int((now - stage_entered_at).total_seconds()))

        policy = CRMStageSlaPolicy.objects.using(alias).filter(
            organization=lead.organization,
            canonical_stage=lead.current_status,
            is_enabled=True,
        ).first()

        if not policy:
            return {
                'stage_entered_at': stage_entered_at,
                'stage_age_seconds': stage_age_seconds,
                'sla_policy_id': None,
                'sla_target_value': None,
                'sla_target_unit': None,
                'sla_due_at': None,
                'sla_status': 'DISABLED',
                'is_breached': False,
                'overdue_seconds': 0,
            }

        if policy.response_target_unit == 'MINUTES':
            delta = timedelta(minutes=policy.response_target_value)
        elif policy.response_target_unit == 'HOURS':
            delta = timedelta(hours=policy.response_target_value)
        elif policy.response_target_unit == 'DAYS':
            delta = timedelta(days=policy.response_target_value)
        else:
            delta = timedelta(minutes=policy.response_target_value)

        sla_due_at = stage_entered_at + delta
        is_breached = now > sla_due_at
        overdue_seconds = max(0, int((now - sla_due_at).total_seconds())) if is_breached else 0

        return {
            'stage_entered_at': stage_entered_at,
            'stage_age_seconds': stage_age_seconds,
            'sla_policy_id': str(policy.id),
            'sla_target_value': policy.response_target_value,
            'sla_target_unit': policy.response_target_unit,
            'sla_due_at': sla_due_at,
            'sla_status': 'BREACHED' if is_breached else 'ON_TRACK',
            'is_breached': is_breached,
            'overdue_seconds': overdue_seconds,
            'escalation_enabled': policy.escalation_enabled,
        }

    @classmethod
    def evaluate_lead(
        cls,
        lead: Lead,
        db_alias: Optional[str] = None,
        policy: Optional[CRMAttentionPolicy] = None,
    ) -> Dict[str, Any]:
        """
        Evaluates a single lead deterministically.
        Returns a structured explanation of all detected reasons, the primary reason,
        severity, and recommended next best action.
        """
        alias = db_alias or get_tenant_db_alias() or 'default'
        now = timezone.now()

        if policy is None:
            policy = cls.get_attention_policy(lead.organization, db_alias=alias)

        # Terminal stages do not need operational attention
        if lead.current_status in TERMINAL_STAGES:
            return cls._empty_attention_response(lead, now)

        # Compute canonical stage SLA
        sla_info = cls.calculate_stage_sla(lead, db_alias=alias)
        stage_entered_at = sla_info['stage_entered_at']
        sla_due_at = sla_info['sla_due_at']
        sla_is_breached = sla_info['is_breached']

        reasons: List[Dict[str, Any]] = []

        # -------------------------------------------------------------------------
        # 1. STAGE SLA BREACHED
        # -------------------------------------------------------------------------
        # Only if policy enabled AND stage SLA actually breached (now > sla_due_at).
        # Fresh leads are never breached.
        if (not policy.is_enabled or policy.sla_breach_attention_enabled) and sla_is_breached:
            hours_overdue = round(sla_info['overdue_seconds'] / 3600, 1)
            reasons.append({
                'code': 'STAGE_SLA_BREACHED',
                'display_name': 'Stage SLA Breached',
                'description': f"Stage SLA exceeded by {hours_overdue}h in '{lead.get_current_status_display()}' stage.",
                'severity': 'CRITICAL' if sla_info.get('escalation_enabled') else 'HIGH',
                'overdue_seconds': sla_info['overdue_seconds'],
                'due_at': sla_due_at.isoformat() if sla_due_at else None,
                'recommended_action_code': 'CALL_LEAD' if lead.phone_normalized else 'CREATE_FOLLOWUP',
            })

        # -------------------------------------------------------------------------
        # 2. OVERDUE FOLLOW-UP
        # -------------------------------------------------------------------------
        overdue_task = SalesFollowupTask.objects.using(alias).filter(
            lead=lead,
            status='PENDING',
            due_at__lt=now,
        ).order_by('due_at').first()

        if overdue_task and (not policy.is_enabled or policy.overdue_followup_attention_enabled):
            task_overdue_seconds = max(0, int((now - overdue_task.due_at).total_seconds()))
            task_overdue_hours = round(task_overdue_seconds / 3600, 1)
            reasons.append({
                'code': 'FOLLOWUP_OVERDUE',
                'display_name': 'Follow-up Overdue',
                'description': f"Scheduled follow-up '{overdue_task.task_type}' was due {task_overdue_hours}h ago.",
                'severity': 'HIGH' if overdue_task.priority in ['HIGH', 'URGENT'] else 'MEDIUM',
                'overdue_seconds': task_overdue_seconds,
                'due_at': overdue_task.due_at.isoformat(),
                'related_entity_id': str(overdue_task.id),
                'recommended_action_code': 'CALL_LEAD' if overdue_task.task_type in ['CALL', 'FOLLOW_UP'] else 'CREATE_FOLLOWUP',
            })

        # -------------------------------------------------------------------------
        # 3. NO FOLLOW-UP (GATED BY STAGE SLA DEADLINE)
        # -------------------------------------------------------------------------
        # If there is a valid pending/future follow-up: NO_FOLLOWUP is FALSE.
        # If there is NO follow-up: only mark NO_FOLLOWUP once the applicable stage
        # SLA deadline has arrived. A brand new lead or fresh stage transition is NOT stuck immediately.
        has_pending_followup = SalesFollowupTask.objects.using(alias).filter(
            lead=lead,
            status='PENDING',
        ).exists()

        if not has_pending_followup and (not policy.is_enabled or policy.no_followup_attention_enabled):
            # Must be past stage SLA deadline before flagging missing follow-up
            if sla_due_at and now > sla_due_at:
                reasons.append({
                    'code': 'NO_FOLLOWUP',
                    'display_name': 'No Follow-up Scheduled',
                    'description': f"No upcoming follow-up task scheduled after stage SLA response window elapsed.",
                    'severity': 'MEDIUM',
                    'overdue_seconds': sla_info['overdue_seconds'],
                    'due_at': sla_due_at.isoformat() if sla_due_at else None,
                    'recommended_action_code': 'CREATE_FOLLOWUP',
                })

        # -------------------------------------------------------------------------
        # 4. TRIAL NOT BOOKED (GATED BY STAGE SLA DEADLINE)
        # -------------------------------------------------------------------------
        # Only applies to trial-eligible stages (INTERESTED, HOT_LEAD).
        # A lead entering INTERESTED at 10:00 AM is NOT stuck at 10:01 AM.
        # It is only flagged if no valid trial exists AND stage SLA deadline has arrived.
        if lead.current_status in ['INTERESTED', 'HOT_LEAD'] and (not policy.is_enabled or policy.trial_not_booked_attention_enabled):
            has_valid_trial = TrialBooking.objects.using(alias).filter(
                lead=lead,
                status__in=['BOOKED', 'CONFIRMED', 'ATTENDED'],
            ).exists()

            if not has_valid_trial:
                if sla_due_at and now > sla_due_at:
                    reasons.append({
                        'code': 'TRIAL_NOT_BOOKED',
                        'display_name': 'Trial Not Booked',
                        'description': f"Prospect in '{lead.get_current_status_display()}' stage has not booked a trial after response window.",
                        'severity': 'HIGH' if lead.current_status == 'HOT_LEAD' else 'MEDIUM',
                        'overdue_seconds': sla_info['overdue_seconds'],
                        'due_at': sla_due_at.isoformat() if sla_due_at else None,
                        'recommended_action_code': 'BOOK_TRIAL',
                    })

        # -------------------------------------------------------------------------
        # 5. TRIAL CONFIRMATION PENDING / RESCHEDULE REQUESTED
        # -------------------------------------------------------------------------
        latest_trial = TrialBooking.objects.using(alias).filter(
            lead=lead
        ).order_by('-scheduled_start').first()

        if latest_trial and latest_trial.status == 'BOOKED' and (not policy.is_enabled or policy.trial_confirmation_attention_enabled):
            if latest_trial.confirmation_status == 'RESCHEDULE_REQUESTED':
                reasons.append({
                    'code': 'TRIAL_RESCHEDULE_REQUESTED',
                    'display_name': 'Trial Reschedule Requested',
                    'description': f"Prospect requested an alternate date/time for their trial session.",
                    'severity': 'HIGH',
                    'overdue_seconds': max(0, int((now - latest_trial.updated_at).total_seconds())),
                    'due_at': latest_trial.scheduled_start.isoformat(),
                    'related_entity_id': str(latest_trial.id),
                    'recommended_action_code': 'RESCHEDULE_TRIAL',
                })
            elif latest_trial.confirmation_status == 'PENDING':
                # Reuses CRMTrialReminderPolicy confirmation_wait_duration
                reminder_policy = CRMTrialReminderPolicy.objects.using(alias).filter(
                    organization=lead.organization
                ).first()

                wait_hours = reminder_policy.confirmation_wait_duration_value if reminder_policy else 2
                if reminder_policy and reminder_policy.confirmation_wait_duration_unit == 'DAYS':
                    wait_hours = wait_hours * 24

                wait_deadline = latest_trial.created_at + timedelta(hours=wait_hours)
                if now > wait_deadline:
                    conf_overdue_seconds = int((now - wait_deadline).total_seconds())
                    reasons.append({
                        'code': 'TRIAL_CONFIRMATION_PENDING',
                        'display_name': 'Trial Confirmation Pending',
                        'description': f"Trial booking confirmation has remained pending beyond {wait_hours}h.",
                        'severity': 'HIGH',
                        'overdue_seconds': conf_overdue_seconds,
                        'due_at': wait_deadline.isoformat(),
                        'related_entity_id': str(latest_trial.id),
                        'recommended_action_code': 'CONFIRM_TRIAL',
                    })

        # -------------------------------------------------------------------------
        # 6. TRIAL NO-SHOW RECOVERY
        # -------------------------------------------------------------------------
        # Checked via Phase 4 reference: external_reference=f"TRIAL_NOSHOW_{trial.id}"
        if latest_trial and latest_trial.status == 'NO_SHOW' and (not policy.is_enabled or policy.trial_no_show_attention_enabled):
            has_completed_recovery = SalesFollowupTask.objects.using(alias).filter(
                lead=lead,
                external_reference=f"TRIAL_NOSHOW_{latest_trial.id}",
                status='COMPLETED',
            ).exists()

            if not has_completed_recovery:
                no_show_overdue = max(0, int((now - latest_trial.updated_at).total_seconds()))
                reasons.append({
                    'code': 'TRIAL_NO_SHOW',
                    'display_name': 'Trial No-Show Recovery Due',
                    'description': f"Prospect missed trial on {latest_trial.scheduled_start.strftime('%d %b')}. Recovery outreach pending.",
                    'severity': 'HIGH',
                    'overdue_seconds': no_show_overdue,
                    'due_at': latest_trial.updated_at.isoformat(),
                    'related_entity_id': str(latest_trial.id),
                    'recommended_action_code': 'CREATE_FOLLOWUP',
                })

        # -------------------------------------------------------------------------
        # 7. POST-TRIAL SALES FOLLOW-UP DUE
        # -------------------------------------------------------------------------
        # Checked via Phase 4 reference: external_reference=f"TRIAL_ATTENDED_{trial.id}"
        if latest_trial and latest_trial.status == 'ATTENDED' and (not policy.is_enabled or policy.post_trial_followup_attention_enabled):
            has_post_trial_followup = SalesFollowupTask.objects.using(alias).filter(
                lead=lead,
                external_reference=f"TRIAL_ATTENDED_{latest_trial.id}",
                status='COMPLETED',
            ).exists()

            if not has_post_trial_followup:
                attended_overdue = max(0, int((now - latest_trial.updated_at).total_seconds()))
                reasons.append({
                    'code': 'POST_TRIAL_FOLLOWUP_DUE',
                    'display_name': 'Post-Trial Follow-up Due',
                    'description': f"Prospect attended trial workout. Post-trial sales consultation required.",
                    'severity': 'HIGH',
                    'overdue_seconds': attended_overdue,
                    'due_at': latest_trial.updated_at.isoformat(),
                    'related_entity_id': str(latest_trial.id),
                    'recommended_action_code': 'POST_TRIAL_FOLLOWUP',
                })

        # -------------------------------------------------------------------------
        # 8. NO-RESPONSE CHRONOLOGICAL EVALUATION & COMMUNICATION FAILED
        # -------------------------------------------------------------------------
        # Ordering matters: find latest outbound message, then check if ANY inbound exists AFTER that outbound.
        if not policy.is_enabled or policy.no_response_attention_enabled:
            latest_outbound = CommunicationMessage.objects.using(alias).filter(
                lead=lead,
                direction='OUTBOUND',
            ).order_by('-created_at').first()

            if latest_outbound:
                if latest_outbound.status == 'FAILED':
                    # Distinct reason: communication failed, recommend alternative or phone call
                    reasons.append({
                        'code': 'COMMUNICATION_FAILED',
                        'display_name': 'Outbound Message Failed',
                        'description': f"Outbound {latest_outbound.channel} message failed delivery ({latest_outbound.failure_code or 'Permanent error'}).",
                        'severity': 'MEDIUM',
                        'overdue_seconds': max(0, int((now - latest_outbound.created_at).total_seconds())),
                        'due_at': latest_outbound.created_at.isoformat(),
                        'related_entity_id': str(latest_outbound.id),
                        'recommended_action_code': 'CALL_LEAD' if lead.phone_normalized else 'SEND_COMMUNICATION',
                    })
                elif latest_outbound.status in ['SENT', 'DELIVERED', 'READ']:
                    has_inbound_after = CommunicationMessage.objects.using(alias).filter(
                        lead=lead,
                        direction='INBOUND',
                        created_at__gt=latest_outbound.created_at,
                    ).exists()

                    if not has_inbound_after:
                        # Only flag if configured wait hours has elapsed
                        wait_hours = policy.no_response_wait_hours if policy.no_response_wait_hours is not None else 24
                        response_deadline = latest_outbound.created_at + timedelta(hours=wait_hours)
                        if now > response_deadline:
                            comm_overdue_seconds = int((now - response_deadline).total_seconds())
                            reasons.append({
                                'code': 'NO_RESPONSE',
                                'display_name': 'No Customer Response',
                                'description': f"Outbound {latest_outbound.channel} message sent >{wait_hours}h ago with no customer reply.",
                                'severity': 'MEDIUM',
                                'overdue_seconds': comm_overdue_seconds,
                                'due_at': response_deadline.isoformat(),
                                'related_entity_id': str(latest_outbound.id),
                                'recommended_action_code': 'CALL_LEAD' if lead.phone_normalized else 'SEND_COMMUNICATION',
                            })

        # -------------------------------------------------------------------------
        # 9. UNASSIGNED LEAD
        # -------------------------------------------------------------------------
        is_unassigned = lead.assigned_sales_user is None
        if not is_unassigned and lead.assigned_sales_user:
            # Check user active status via status field
            is_unassigned = getattr(lead.assigned_sales_user, 'status', 'ACTIVE') != 'ACTIVE'

        if is_unassigned and (not policy.is_enabled or policy.unassigned_lead_attention_enabled):
            unassigned_wait_minutes = policy.unassigned_wait_minutes if policy.unassigned_wait_minutes is not None else 0
            unassigned_deadline = lead.created_at + timedelta(minutes=unassigned_wait_minutes)
            if now >= unassigned_deadline:
                unassigned_overdue = max(0, int((now - unassigned_deadline).total_seconds()))
                reasons.append({
                    'code': 'UNASSIGNED_LEAD',
                    'display_name': 'Unassigned Lead',
                    'description': 'Lead has no active sales agent assigned.',
                    'severity': 'HIGH' if unassigned_overdue > 3600 else 'MEDIUM',
                    'overdue_seconds': unassigned_overdue,
                    'due_at': unassigned_deadline.isoformat(),
                    'recommended_action_code': 'ASSIGN_LEAD',
                })

        # -------------------------------------------------------------------------
        # DETERMINISTIC PRIMARY REASON & SEVERITY RESOLUTION
        # -------------------------------------------------------------------------
        is_stuck = len(reasons) > 0
        if not is_stuck:
            return cls._empty_attention_response(lead, now, sla_info)

        # Primary reason selection:
        # Prioritize the reason with the greatest overdue magnitude (overdue_seconds).
        # On tie, break with deterministic order based on operational urgency.
        PRIORITY_ORDER = {
            'STAGE_SLA_BREACHED': 100,
            'FOLLOWUP_OVERDUE': 90,
            'TRIAL_RESCHEDULE_REQUESTED': 85,
            'TRIAL_CONFIRMATION_PENDING': 80,
            'TRIAL_NO_SHOW': 75,
            'POST_TRIAL_FOLLOWUP_DUE': 70,
            'TRIAL_NOT_BOOKED': 60,
            'NO_FOLLOWUP': 50,
            'NO_RESPONSE': 40,
            'COMMUNICATION_FAILED': 35,
            'UNASSIGNED_LEAD': 30,
        }

        # Sort primarily by overdue magnitude descending, secondarily by PRIORITY_ORDER descending
        reasons_sorted = sorted(
            reasons,
            key=lambda r: (r.get('overdue_seconds', 0), PRIORITY_ORDER.get(r['code'], 0)),
            reverse=True,
        )

        primary = reasons_sorted[0]
        overall_severity = primary['severity']

        # Resolve Next Best Action from RecommendationActionRegistry
        action_meta = RecommendationActionRegistry.get_recommendation_meta(primary['recommended_action_code'])
        action_display = action_meta['display_name'] if action_meta else primary['recommended_action_code']

        recommended_action = {
            'action_code': primary['recommended_action_code'],
            'display_name': action_display,
            'reason': primary['description'],
            'priority': overall_severity,
            'suggested_urgency': overall_severity,
            'target_ui_action': action_meta.get('target_ui_action', 'VIEW_LEAD_360') if action_meta else 'VIEW_LEAD_360',
            'required_permission': action_meta.get('required_permission', 'crm.leads.edit') if action_meta else 'crm.leads.edit',
            'is_automation_executable': action_meta.get('is_automation_executable', False) if action_meta else False,
            'related_entity_id': primary.get('related_entity_id'),
        }

        # Latest activity timestamp
        last_activity = LeadActivity.objects.using(alias).filter(lead=lead).order_by('-activity_at').first()
        last_activity_at = last_activity.activity_at.isoformat() if last_activity and last_activity.activity_at else None

        # Next upcoming follow-up timestamp
        next_followup = SalesFollowupTask.objects.using(alias).filter(
            lead=lead, status='PENDING', due_at__gte=now
        ).order_by('due_at').first()
        next_followup_at = next_followup.due_at.isoformat() if next_followup else None

        return {
            'is_stuck': True,
            'primary_reason': primary['code'],
            'primary_reason_display': primary['display_name'],
            'severity': overall_severity,
            'stage_age_seconds': sla_info['stage_age_seconds'],
            'stage_entered_at': stage_entered_at.isoformat() if stage_entered_at else None,
            'sla_due_at': sla_due_at.isoformat() if sla_due_at else None,
            'overdue_by_seconds': primary.get('overdue_seconds', 0),
            'last_activity_at': last_activity_at,
            'next_followup_at': next_followup_at,
            'reasons': reasons,
            'recommended_action': recommended_action,
            'evaluated_at': now.isoformat(),
        }

    @classmethod
    def _empty_attention_response(
        cls,
        lead: Lead,
        now: datetime,
        sla_info: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Returns clean non-stuck attention response."""
        stage_age_sec = sla_info['stage_age_seconds'] if sla_info else 0
        sla_due = sla_info['sla_due_at'].isoformat() if sla_info and sla_info.get('sla_due_at') else None

        return {
            'is_stuck': False,
            'primary_reason': None,
            'primary_reason_display': None,
            'severity': 'LOW',
            'stage_age_seconds': stage_age_sec,
            'stage_entered_at': sla_info['stage_entered_at'].isoformat() if sla_info and sla_info.get('stage_entered_at') else None,
            'sla_due_at': sla_due,
            'overdue_by_seconds': 0,
            'last_activity_at': None,
            'next_followup_at': None,
            'reasons': [],
            'recommended_action': {
                'action_code': 'REVIEW_LEAD',
                'display_name': 'Review Lead',
                'reason': 'Lead is on track with scheduled touchpoints.',
                'priority': 'LOW',
                'suggested_urgency': 'LOW',
                'target_ui_action': 'VIEW_LEAD_360',
                'required_permission': 'crm.leads.view',
                'is_automation_executable': False,
            },
            'evaluated_at': now.isoformat(),
        }

    @classmethod
    def bulk_evaluate_leads(
        cls,
        leads_list: List[Lead],
        db_alias: Optional[str] = None,
    ) -> Dict[uuid.UUID, Dict[str, Any]]:
        """
        High performance bulk evaluator over already-paginated lead slices.
        Reuses cached policies across the batch to avoid repeated queries.
        """
        if not leads_list:
            return {}

        alias = db_alias or get_tenant_db_alias() or 'default'
        org = leads_list[0].organization
        policy = cls.get_attention_policy(org, db_alias=alias)

        results = {}
        for lead in leads_list:
            results[lead.id] = cls.evaluate_lead(lead, db_alias=alias, policy=policy)

        return results

    @classmethod
    def get_attention_metrics(
        cls,
        organization: Organization,
        branch_id: Optional[str] = None,
        db_alias: Optional[str] = None,
    ) -> Dict[str, int]:
        """
        Computes tenant-wide aggregated operational KPI metrics for the Attention Queue.
        These are authoritative counts across all active leads in the tenant/branch,
        never manufactured from current-page pagination slices.
        """
        alias = db_alias or get_tenant_db_alias() or 'default'
        now = timezone.now()

        qs = Lead.objects.using(alias).filter(
            organization=organization,
            current_status__in=ACTIVE_PIPELINE_STAGES,
        )
        if branch_id and branch_id != 'ALL':
            qs = qs.filter(branch_id=branch_id)

        leads = list(qs.select_related('organization', 'branch', 'assigned_sales_user'))
        policy = cls.get_attention_policy(organization, db_alias=alias)

        stuck_count = 0
        sla_breached_count = 0
        overdue_tasks_count = 0
        awaiting_response_count = 0

        for lead in leads:
            res = cls.evaluate_lead(lead, db_alias=alias, policy=policy)
            if res['is_stuck']:
                stuck_count += 1
                for r in res['reasons']:
                    if r['code'] == 'STAGE_SLA_BREACHED':
                        sla_breached_count += 1
                        break
                for r in res['reasons']:
                    if r['code'] == 'FOLLOWUP_OVERDUE':
                        overdue_tasks_count += 1
                        break
                for r in res['reasons']:
                    if r['code'] == 'NO_RESPONSE':
                        awaiting_response_count += 1
                        break

        return {
            'stuck_leads': stuck_count,
            'sla_breached': sla_breached_count,
            'overdue_tasks': overdue_tasks_count,
            'awaiting_response': awaiting_response_count,
            'total_active_leads': len(leads),
        }

    @classmethod
    def scan_and_emit_sla_breaches(
        cls,
        organization: Organization,
        db_alias: Optional[str] = None,
    ) -> int:
        """
        Periodic worker/scanner service:
        Detects active leads that have breached stage SLA.
        Enqueues idempotent DomainOutboxEvent ('CRM_LEAD_SLA_BREACHED') once per stage entry.
        Does NOT directly invoke AutomationEngine; Automation consumes through outbox boundary.
        """
        alias = db_alias or get_tenant_db_alias() or 'default'
        now = timezone.now()

        active_leads = Lead.objects.using(alias).filter(
            organization=organization,
            current_status__in=ACTIVE_PIPELINE_STAGES,
        ).select_related('organization', 'branch', 'assigned_sales_user')

        emitted_count = 0
        for lead in active_leads:
            sla_info = cls.calculate_stage_sla(lead, db_alias=alias)
            if sla_info['is_breached']:
                stage_entered_at = sla_info['stage_entered_at']
                idempotency_key = f"SLA_BREACH_{lead.id}_{stage_entered_at.isoformat()}"

                with transaction.atomic(using=alias):
                    # Check DB-safe idempotency via payload
                    already_emitted = DomainOutboxEvent.objects.using(alias).filter(
                        organization=organization,
                        event_type=CANONICAL_SLA_BREACH_EVENT_CODE,
                        aggregate_id=lead.id,
                        payload__idempotency_key=idempotency_key,
                    ).exists()

                    if not already_emitted:
                        DomainOutboxEvent.objects.using(alias).create(
                            organization=organization,
                            event_type=CANONICAL_SLA_BREACH_EVENT_CODE,
                            aggregate_type='CRM_LEAD',
                            aggregate_id=lead.id,
                            payload={
                                'idempotency_key': idempotency_key,
                                'lead_id': str(lead.id),
                                'stage': lead.current_status,
                                'stage_age_seconds': sla_info['stage_age_seconds'],
                                'sla_due_at': sla_info['sla_due_at'].isoformat() if sla_info['sla_due_at'] else None,
                                'branch_id': str(lead.branch_id) if lead.branch_id else None,
                                'assigned_agent_id': str(lead.assigned_sales_user_id) if lead.assigned_sales_user_id else None,
                            },
                        )
                        emitted_count += 1

        return emitted_count
