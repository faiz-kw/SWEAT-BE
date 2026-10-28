"""
backend/apps/tenant_core/services_lead_recycling.py — Stale Lead Revival & SLA Dead Lead Recycling Engine

Authoritative, concurrency-safe recycling service supporting:
1. SLA Breach Recycling: Leads uncontacted for > 4 hours automatically revoked from dormant reps
   and reassigned to the next available on-duty sales rep.
2. Dormant Lead Revival: Leads untouched for > 7 days without pending follow-ups re-queued and
   re-routed for fresh revival outreach.
3. Complete Auditability: Every recycling action logs a verified timeline activity, updates
   the canonical LeadAssignment history, and dispatches outbox notifications.
"""

import logging
from typing import Optional, Dict, Any, List
from datetime import timedelta
from django.db import transaction, models
from django.utils import timezone

from .context import get_tenant_db_alias
from .models_org import Organization
from .models_users import TenantUser
from .models_crm import Lead, LeadActivity, LeadAssignment, CallSession, SalesFollowupTask
from .services_lead_assignment import LeadAutoAssignmentService, LeadAssignmentExecutionService
from .services_reliability import record_business_audit, enqueue_outbox_event

logger = logging.getLogger(__name__)

TERMINAL_LEAD_STATUSES = ('CONVERTED', 'NOT_INTERESTED', 'LOST', 'INVALID')


class LeadRecyclingService:
    """
    Authoritative service governing automated lead recycling and revival.
    """

    @classmethod
    def recycle_uncontacted_sla_breached_leads(
        cls,
        organization: Organization,
        sla_hours: int = 4,
        db_alias: Optional[str] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Identifies open leads assigned to a sales rep more than `sla_hours` ago
        that have received zero calls or contact activities, and reassigns them
        to the next available active sales rep.
        """
        alias = db_alias or get_tenant_db_alias() or 'default'
        cutoff_time = timezone.now() - timedelta(hours=sla_hours)

        # Leads in NEW_LEAD status assigned to a rep, created/assigned before cutoff
        leads_qs = Lead.objects.using(alias).filter(
            organization=organization,
            current_status='NEW_LEAD',
            assigned_sales_user__isnull=False,
            created_at__lte=cutoff_time,
        ).exclude(
            current_status__in=TERMINAL_LEAD_STATUSES
        ).select_related('assigned_sales_user', 'branch', 'organization')

        reassigned_leads: List[Dict[str, Any]] = []

        for lead in leads_qs:
            # Check if any call or contact activity has actually occurred
            has_calls = CallSession.objects.using(alias).filter(
                lead=lead,
                status__in=['COMPLETED', 'IN_PROGRESS', 'RINGING'],
            ).exists()

            has_activities = LeadActivity.objects.using(alias).filter(
                lead=lead,
                activity_type__in=['CALL', 'NOTE', 'WHATSAPP', 'EMAIL', 'MEETING'],
            ).exists()

            if has_calls or has_activities or lead.last_contacted_at:
                continue  # Lead was contacted; SLA is not breached

            # Verify active assignment duration
            active_assign = LeadAssignment.objects.using(alias).filter(
                lead=lead,
                status='ACTIVE',
            ).order_by('-assigned_at').first()

            if active_assign and active_assign.assigned_at > cutoff_time:
                continue  # Assigned recently

            prev_rep = lead.assigned_sales_user
            prev_rep_name = f"{prev_rep.first_name} {prev_rep.last_name}".strip() if prev_rep else "Unassigned"

            if dry_run:
                reassigned_leads.append({
                    'lead_id': str(lead.id),
                    'lead_name': f"{lead.first_name} {lead.last_name}".strip(),
                    'previous_rep': prev_rep_name,
                    'reason': f'Uncontacted for > {sla_hours}h',
                })
                continue

            try:
                # Resolve next available rep (excluding current rep if possible)
                new_rep, strategy, reason = LeadAutoAssignmentService.resolve_assignee(
                    organization=organization,
                    branch=lead.branch,
                    db_alias=alias,
                )

                if not new_rep or (prev_rep and new_rep.id == prev_rep.id):
                    # No other available rep to rotate to
                    continue

                new_rep_name = f"{new_rep.first_name} {new_rep.last_name}".strip()

                with transaction.atomic(using=alias):
                    # 1. Reassign lead
                    LeadAssignmentExecutionService.execute_assignment(
                        lead=lead,
                        assigned_to_user=new_rep,
                        assignment_source='SYSTEM_RECYCLED',
                        assignment_strategy='SLA_BREACH_RECYCLE',
                        reason=f'SLA Breach: Uncontacted for > {sla_hours}h by {prev_rep_name}',
                        actor_user=None,
                        db_alias=alias,
                    )

                    # 2. Record timeline activity
                    LeadActivity.objects.using(alias).create(
                        lead=lead,
                        activity_type='STATUS_CHANGE',
                        outcome='SLA_BREACH_REASSIGNED',
                        notes=(
                            f"⚡ SLA Breach: Lead uncontacted for > {sla_hours} hours by {prev_rep_name}. "
                            f"Automatically recycled and reassigned to {new_rep_name}."
                        ),
                        performed_by_user=None,
                        activity_at=timezone.now(),
                    )

                    # 3. Emit audit event
                    record_business_audit(
                        organization=organization,
                        branch=lead.branch,
                        actor_user=None,
                        module='crm',
                        action_code='CRM_LEAD_SLA_RECYCLED',
                        entity_type='Lead',
                        entity_id=lead.id,
                        event_description=f"Lead {lead.first_name} {lead.last_name} recycled from {prev_rep_name} to {new_rep_name} (SLA Breach)",
                        after_data={'previous_rep_id': str(prev_rep.id) if prev_rep else None, 'new_rep_id': str(new_rep.id)},
                        db_alias=alias,
                    )

                    reassigned_leads.append({
                        'lead_id': str(lead.id),
                        'lead_name': f"{lead.first_name} {lead.last_name}".strip(),
                        'previous_rep': prev_rep_name,
                        'new_rep': new_rep_name,
                        'reason': 'SLA_BREACH_4H',
                    })

            except Exception as exc:
                logger.error("Failed to recycle SLA breached lead %s: %s", lead.id, exc, exc_info=True)

        return {
            'organization_id': str(organization.id),
            'sla_hours': sla_hours,
            'dry_run': dry_run,
            'reassigned_count': len(reassigned_leads),
            'reassigned_leads': reassigned_leads,
        }

    @classmethod
    def recycle_dormant_leads(
        cls,
        organization: Organization,
        dormant_days: int = 7,
        db_alias: Optional[str] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Identifies non-terminal leads that have had no calls or outreach activity
        for > `dormant_days` (default 7 days) and have no pending follow-up tasks,
        reassigning them to an available sales rep for fresh revival outreach.
        """
        alias = db_alias or get_tenant_db_alias() or 'default'
        cutoff_date = timezone.now() - timedelta(days=dormant_days)

        # Non-terminal leads where last contact was before cutoff (or never contacted and created before cutoff)
        leads_qs = Lead.objects.using(alias).filter(
            organization=organization,
        ).exclude(
            current_status__in=TERMINAL_LEAD_STATUSES
        ).filter(
            models.Q(last_contacted_at__lte=cutoff_date) |
            models.Q(last_contacted_at__isnull=True, created_at__lte=cutoff_date)
        ).select_related('assigned_sales_user', 'branch', 'organization')

        revived_leads: List[Dict[str, Any]] = []

        for lead in leads_qs:
            # Check if there is an active future follow-up task
            has_pending_followup = SalesFollowupTask.objects.using(alias).filter(
                lead=lead,
                status='PENDING',
                due_at__gte=timezone.now(),
            ).exists()

            if has_pending_followup:
                continue  # Rep already has a scheduled follow-up planned

            # Check if any recent activity occurred within dormant window
            has_recent_activity = LeadActivity.objects.using(alias).filter(
                lead=lead,
                activity_at__gte=cutoff_date,
            ).exists()

            if has_recent_activity:
                continue

            prev_rep = lead.assigned_sales_user
            prev_rep_name = f"{prev_rep.first_name} {prev_rep.last_name}".strip() if prev_rep else "Unassigned"

            if dry_run:
                revived_leads.append({
                    'lead_id': str(lead.id),
                    'lead_name': f"{lead.first_name} {lead.last_name}".strip(),
                    'previous_rep': prev_rep_name,
                    'reason': f'Untouched for > {dormant_days}d',
                })
                continue

            try:
                new_rep, strategy, reason = LeadAutoAssignmentService.resolve_assignee(
                    organization=organization,
                    branch=lead.branch,
                    db_alias=alias,
                )

                if not new_rep or (prev_rep and new_rep.id == prev_rep.id):
                    continue

                new_rep_name = f"{new_rep.first_name} {new_rep.last_name}".strip()

                with transaction.atomic(using=alias):
                    LeadAssignmentExecutionService.execute_assignment(
                        lead=lead,
                        assigned_to_user=new_rep,
                        assignment_source='SYSTEM_RECYCLED',
                        assignment_strategy='DORMANT_REVIVAL',
                        reason=f'Dormant Lead Revival: Untouched for > {dormant_days} days by {prev_rep_name}',
                        actor_user=None,
                        db_alias=alias,
                    )

                    LeadActivity.objects.using(alias).create(
                        lead=lead,
                        activity_type='STATUS_CHANGE',
                        outcome='DORMANT_REVIVED',
                        notes=(
                            f"🔄 Stale Lead Revival: Lead untouched for > {dormant_days} days. "
                            f"Auto-reassigned from {prev_rep_name} to {new_rep_name} for fresh re-engagement."
                        ),
                        performed_by_user=None,
                        activity_at=timezone.now(),
                    )

                    record_business_audit(
                        organization=organization,
                        branch=lead.branch,
                        actor_user=None,
                        module='crm',
                        action_code='CRM_LEAD_DORMANT_REVIVED',
                        entity_type='Lead',
                        entity_id=lead.id,
                        event_description=f"Lead {lead.first_name} {lead.last_name} revived and reassigned to {new_rep_name}",
                        after_data={'previous_rep_id': str(prev_rep.id) if prev_rep else None, 'new_rep_id': str(new_rep.id)},
                        db_alias=alias,
                    )

                    revived_leads.append({
                        'lead_id': str(lead.id),
                        'lead_name': f"{lead.first_name} {lead.last_name}".strip(),
                        'previous_rep': prev_rep_name,
                        'new_rep': new_rep_name,
                        'reason': f'DORMANT_{dormant_days}D',
                    })

            except Exception as exc:
                logger.error("Failed to revive dormant lead %s: %s", lead.id, exc, exc_info=True)

        return {
            'organization_id': str(organization.id),
            'dormant_days': dormant_days,
            'dry_run': dry_run,
            'revived_count': len(revived_leads),
            'revived_leads': revived_leads,
        }

    @classmethod
    def run_tenant_recycling(
        cls,
        organization: Organization,
        sla_hours: int = 4,
        dormant_days: int = 7,
        db_alias: Optional[str] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Executes both SLA breach recycling and dormant lead revival in one unified cycle.
        """
        alias = db_alias or get_tenant_db_alias() or 'default'
        sla_results = cls.recycle_uncontacted_sla_breached_leads(
            organization=organization,
            sla_hours=sla_hours,
            db_alias=alias,
            dry_run=dry_run,
        )
        dormant_results = cls.recycle_dormant_leads(
            organization=organization,
            dormant_days=dormant_days,
            db_alias=alias,
            dry_run=dry_run,
        )
        return {
            'organization_id': str(organization.id),
            'dry_run': dry_run,
            'sla_breached_reassigned': sla_results.get('reassigned_count', 0),
            'dormant_revived': dormant_results.get('revived_count', 0),
            'total_recycled': sla_results.get('reassigned_count', 0) + dormant_results.get('revived_count', 0),
            'sla_details': sla_results.get('reassigned_leads', []),
            'dormant_details': dormant_results.get('revived_leads', []),
        }

