"""
apps/tenant_core/services_crm.py — Business Services for Layer 2 Module B: CRM, Leads & Trials

Workflows:
- create_lead: Idempotent/atomic lead intake, status history, initial assignment.
- transition_lead_status: State machine transitions, reason tracking, outbox event.
- assign_lead: Sales and trainer assignment with historical tracking.
- book_trial: Atomic trial booking, trainer schedule validation, lead state sync.
- transition_trial_status: Trial lifecycle (BOOKED -> CONFIRMED -> ATTENDED / NO_SHOW).
- submit_intake_form: Atomic submission with protected sensitive health data handling.
"""

import uuid
import re
import logging
from datetime import datetime, timedelta, date
from typing import Optional, Dict, Any, List, Tuple
from django.db import transaction, models
from django.db.models import Q as models_Q, F as models_F, Q
from django.utils import timezone
from django.core.exceptions import ValidationError
from config.routers import get_tenant_db_alias

from .models_org import Organization, Branch
from .models_users import TenantUser
from .models_workforce import UserProfile, TrainerProfile
from .models_crm import (
    LeadSource,
    Lead,
    LeadAttribution,
    LeadStatusHistory,
    LeadAssignment,
    LeadNote,
    LeadActivity,
    IntakeForm,
    IntakeQuestion,
    IntakeSubmission,
    IntakeAnswer,
    TrialBooking,
    TrialStatusHistory,
    LeadConversion,
    SalesFollowupTask,
    LeadCommercialProfile,
    CRMStageSlaPolicy,
    CRMTrialReminderPolicy,
)
from .services_workforce import TrainerAvailabilityService
from .services_reliability import record_business_audit, enqueue_outbox_event

logger = logging.getLogger(__name__)

# Valid transitions state machine
VALID_LEAD_TRANSITIONS = {
    'NEW_LEAD': ['TRIAL_BOOKED', 'FOLLOW_UP_PENDING', 'INTERESTED', 'HOT_LEAD', 'NOT_INTERESTED', 'LOST'],
    'TRIAL_BOOKED': ['TRIAL_CONFIRMED', 'TRIAL_ATTENDED', 'NO_SHOW', 'CANCELLED', 'FOLLOW_UP_PENDING', 'LOST'],
    'TRIAL_CONFIRMED': ['TRIAL_ATTENDED', 'NO_SHOW', 'TRIAL_BOOKED', 'FOLLOW_UP_PENDING', 'LOST'],
    'TRIAL_ATTENDED': ['INTERESTED', 'HOT_LEAD', 'PAYMENT_PENDING', 'CONVERTED', 'FOLLOW_UP_PENDING', 'NOT_INTERESTED', 'LOST'],
    'NO_SHOW': ['TRIAL_BOOKED', 'FOLLOW_UP_PENDING', 'NOT_INTERESTED', 'LOST'],
    'FOLLOW_UP_PENDING': ['TRIAL_BOOKED', 'INTERESTED', 'HOT_LEAD', 'PAYMENT_PENDING', 'CONVERTED', 'NOT_INTERESTED', 'LOST'],
    'INTERESTED': ['TRIAL_BOOKED', 'HOT_LEAD', 'PAYMENT_PENDING', 'CONVERTED', 'NOT_INTERESTED', 'LOST'],
    'HOT_LEAD': ['PAYMENT_PENDING', 'CONVERTED', 'TRIAL_BOOKED', 'NOT_INTERESTED', 'LOST'],
    'PAYMENT_PENDING': ['CONVERTED', 'HOT_LEAD', 'LOST'],
    'CONVERTED': [],  # Terminal converted state
    'NOT_INTERESTED': ['NEW_LEAD', 'INTERESTED'],  # Can be reactivated
    'LOST': ['NEW_LEAD', 'INTERESTED'],  # Can be reactivated
}

GMAIL_REGEX = re.compile(r'^[a-zA-Z0-9._%+-]+@gmail\.com$', re.IGNORECASE)


def validate_lead_email(email: Optional[str], required: bool = False) -> Optional[str]:
    """
    Validates that a lead email is a syntactically valid @gmail.com address.
    Rules:
    - Domain must be exactly gmail.com (case-insensitive)
    - Rejects other domains (yahoo, outlook, company, etc.)
    - Rejects malformed addresses (e.g. rahul@gmail, @gmail.com, rahul@gmail.co, user@gmail.com.fake.com)
    - Strips whitespace. Rejects internal spaces.
    - Returns normalized lowercased email string.
    """
    if not email:
        if required:
            raise ValidationError('Please enter a valid Gmail address.')
        return None

    cleaned = str(email).strip()
    if not cleaned:
        if required:
            raise ValidationError('Please enter a valid Gmail address.')
        return None

    if ' ' in cleaned:
        raise ValidationError('Please enter a valid Gmail address.')

    if not GMAIL_REGEX.match(cleaned):
        raise ValidationError('Please enter a valid Gmail address.')

    return cleaned.lower()


def validate_lead_phone(phone: Optional[str], required: bool = True) -> Optional[str]:
    """
    Validates and normalizes an Indian mobile phone number to canonical E.164 (+91XXXXXXXXXX).
    Rules:
    - National number must consist of EXACTLY 10 numeric digits.
    - Strips spaces, hyphens, and standard country prefixes (+91, 91 if 12 digits, 0 if 11 digits).
    - Rejects < 10 digits, > 10 digits, and non-numeric characters.
    - Returns canonical '+91' + 10 digits.
    """
    if not phone or not str(phone).strip():
        if required:
            raise ValidationError('Please enter a valid 10-digit mobile number.')
        return None

    raw = str(phone).strip()
    cleaned = raw.replace(' ', '').replace('-', '').replace('(', '').replace(')', '')

    # Reject if non-numeric except leading +
    check_str = cleaned[1:] if cleaned.startswith('+') else cleaned
    if not check_str.isdigit():
        raise ValidationError('Please enter a valid 10-digit mobile number.')

    if cleaned.startswith('+91'):
        national = cleaned[3:]
    elif cleaned.startswith('91') and len(cleaned) == 12:
        national = cleaned[2:]
    elif cleaned.startswith('0') and len(cleaned) == 11:
        national = cleaned[1:]
    else:
        national = cleaned

    if not re.match(r'^[0-9]{10}$', national):
        raise ValidationError('Please enter a valid 10-digit mobile number.')

    return f"+91{national}"


class CRMLeadService:
    """
    Core business service managing Lead lifecycle, trial sessions, and assignments.
    """

    @classmethod
    def create_lead(
        cls,
        organization: Organization,
        first_name: str,
        last_name: str,
        phone: Optional[str] = None,
        email: Optional[str] = None,
        branch: Optional[Branch] = None,
        lead_source: Optional[LeadSource] = None,
        assigned_sales_user: Optional[TenantUser] = None,
        actor_user: Optional[TenantUser] = None,
        extra_fields: Optional[Dict[str, Any]] = None,
        attribution_data: Optional[Dict[str, Any]] = None,
        db_alias: Optional[str] = None,
    ) -> Lead:
        alias = db_alias or get_tenant_db_alias() or 'default'

        with transaction.atomic(using=alias):
            extra = dict(extra_fields or {})
            billing_name = extra.pop('billing_name', None)
            gst_number = extra.pop('gst_number', None)
            pan_number = extra.pop('pan_number', None)
            attribution_from_extra = extra.pop('attribution', None)

            if email:
                email = validate_lead_email(email, required=False)
            if phone:
                phone = validate_lead_phone(phone, required=False)

            lead = Lead.objects.using(alias).create(
                organization=organization,
                branch=branch,
                lead_source=lead_source,
                first_name=first_name,
                last_name=last_name,
                phone_normalized=phone,
                email_normalized=email,
                current_status='NEW_LEAD',
                assigned_sales_user=assigned_sales_user,
                **extra,
            )

            # Create commercial profile if commercial fields provided
            if billing_name or gst_number or pan_number:
                LeadCommercialProfile.objects.using(alias).create(
                    lead=lead,
                    billing_name=billing_name,
                    gst_number=gst_number,
                    pan_number=pan_number,
                )

            # Record initial status
            LeadStatusHistory.objects.using(alias).create(
                lead=lead,
                from_status=None,
                to_status='NEW_LEAD',
                reason_code='LEAD_CREATION',
                reason_text='Initial lead intake into system',
                changed_by_user=actor_user,
            )

            # Record initial assignment if specified
            if assigned_sales_user:
                LeadAssignment.objects.using(alias).create(
                    lead=lead,
                    assigned_to_user=assigned_sales_user,
                    assigned_by_user=actor_user,
                    assignment_type='SALES',
                    status='ACTIVE',
                )

            # Record initial marketing attribution if supplied and has actual data
            attr_payload = attribution_data or attribution_from_extra
            if attr_payload and isinstance(attr_payload, dict):
                has_meaningful_data = any(
                    bool(str(v).strip()) for k, v in attr_payload.items()
                    if k not in ('touch_type', 'raw_metadata') and v is not None
                )
                if has_meaningful_data:
                    cls.record_lead_attribution(
                        lead=lead,
                        attribution_data=attr_payload,
                        actor_user=actor_user,
                        db_alias=alias,
                    )

            # Audit & Outbox
            record_business_audit(
                organization=organization,
                branch=branch,
                actor_type='EMPLOYEE' if actor_user else 'SYSTEM',
                actor_user=actor_user,
                module='crm',
                action_code='LEAD_CREATE',
                entity_type='Lead',
                entity_id=lead.id,
                event_description=f"Created lead {lead.first_name} {lead.last_name}",
                after_data={'lead_id': str(lead.id), 'status': lead.current_status},
                db_alias=alias,
            )

            enqueue_outbox_event(
                organization=organization,
                event_type='LEAD_CREATED',
                aggregate_type='Lead',
                aggregate_id=lead.id,
                payload={'lead_id': str(lead.id), 'branch_id': str(branch.id) if branch else None},
                db_alias=alias,
            )

            return lead

    @classmethod
    def record_lead_attribution(
        cls,
        lead: Lead,
        attribution_data: Dict[str, Any],
        actor_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> LeadAttribution:
        alias = db_alias or get_tenant_db_alias() or 'default'
        data = dict(attribution_data)
        platform = data.get('platform')
        external_lead_id = data.get('external_lead_id')

        # Idempotency check for external capture
        if platform and external_lead_id:
            existing_attr = LeadAttribution.objects.using(alias).filter(
                organization=lead.organization,
                platform=platform,
                external_lead_id=external_lead_id,
            ).first()
            if existing_attr:
                logger.info(
                    "Idempotent attribution hit for org=%s platform=%s external_lead_id=%s",
                    lead.organization_id, platform, external_lead_id,
                )
                return existing_attr

        # Resolve lead_source if passed by ID or object
        source_val = data.get('lead_source') or data.get('lead_source_id')
        lead_source = None
        if isinstance(source_val, LeadSource):
            lead_source = source_val
        elif source_val:
            lead_source = LeadSource.objects.using(alias).filter(
                organization=lead.organization, id=source_val
            ).first()

        # Check existing touches to determine touch_type
        existing_touches_count = LeadAttribution.objects.using(alias).filter(lead=lead).count()
        touch_type = data.get('touch_type')
        if not touch_type:
            touch_type = 'FIRST_TOUCH' if existing_touches_count == 0 else 'ASSISTED_TOUCH'

        attribution = LeadAttribution.objects.using(alias).create(
            organization=lead.organization,
            lead=lead,
            lead_source=lead_source or lead.lead_source,
            touch_type=touch_type,
            platform=platform or (lead_source.source_type if lead_source else None),
            campaign_name=data.get('campaign_name'),
            campaign_external_id=data.get('campaign_external_id'),
            ad_set_name=data.get('ad_set_name'),
            ad_set_external_id=data.get('ad_set_external_id'),
            ad_name=data.get('ad_name'),
            ad_external_id=data.get('ad_external_id'),
            form_name=data.get('form_name'),
            form_external_id=data.get('form_external_id'),
            external_lead_id=external_lead_id,
            utm_source=data.get('utm_source'),
            utm_medium=data.get('utm_medium'),
            utm_campaign=data.get('utm_campaign'),
            utm_term=data.get('utm_term'),
            utm_content=data.get('utm_content'),
            landing_page_url=data.get('landing_page_url'),
            referrer_url=data.get('referrer_url'),
            capture_method=data.get('capture_method') or 'MANUAL',
            captured_at=data.get('captured_at') or timezone.now(),
            raw_metadata=data.get('raw_metadata') or {},
        )

        # Update Lead mirror fields
        update_fields = []
        source_display = platform or (lead_source.name if lead_source else 'Direct')
        if not lead.first_touch_source:
            lead.first_touch_source = source_display
            update_fields.append('first_touch_source')
        lead.latest_touch_source = source_display
        update_fields.append('latest_touch_source')
        if data.get('campaign_name') and not lead.campaign_reference:
            lead.campaign_reference = data.get('campaign_name')
            update_fields.append('campaign_reference')
        if update_fields:
            lead.save(using=alias, update_fields=update_fields)

        # Audit event
        record_business_audit(
            organization=lead.organization,
            branch=lead.branch,
            actor_type='EMPLOYEE' if actor_user else 'SYSTEM',
            actor_user=actor_user,
            module='crm',
            action_code='CRM_LEAD_ATTRIBUTION_CAPTURED',
            entity_type='LeadAttribution',
            entity_id=attribution.id,
            event_description=f"Attribution touch [{attribution.touch_type}] recorded for lead {lead.first_name} {lead.last_name}",
            metadata={
                'lead_id': str(lead.id),
                'touch_type': attribution.touch_type,
                'platform': attribution.platform,
                'campaign_name': attribution.campaign_name,
                'utm_source': attribution.utm_source,
                'utm_campaign': attribution.utm_campaign,
            },
            db_alias=alias,
        )

        return attribution

    @classmethod
    def calculate_lead_sla(cls, lead: Lead, db_alias: Optional[str] = None) -> Dict[str, Any]:
        alias = db_alias or get_tenant_db_alias() or 'default'
        now = timezone.now()

        # When was the current stage entered?
        latest_transition = LeadStatusHistory.objects.using(alias).filter(
            lead=lead, to_status=lead.current_status
        ).order_by('-changed_at').first()
        stage_entered_at = latest_transition.changed_at if latest_transition else lead.created_at
        stage_age_seconds = max(0, int((now - stage_entered_at).total_seconds()))

        # Look up tenant's SLA policy for this canonical stage
        policy = CRMStageSlaPolicy.objects.using(alias).filter(
            organization=lead.organization,
            canonical_stage=lead.current_status,
            is_enabled=True,
        ).first()

        if not policy:
            return {
                'stage_entered_at': stage_entered_at.isoformat(),
                'stage_age_seconds': stage_age_seconds,
                'sla_policy_id': None,
                'sla_target_value': None,
                'sla_target_unit': None,
                'sla_due_at': None,
                'sla_status': 'DISABLED',
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
        sla_status = 'BREACHED' if now > sla_due_at else 'ON_TRACK'

        return {
            'stage_entered_at': stage_entered_at.isoformat(),
            'stage_age_seconds': stage_age_seconds,
            'sla_policy_id': str(policy.id),
            'sla_target_value': policy.response_target_value,
            'sla_target_unit': policy.response_target_unit,
            'sla_due_at': sla_due_at.isoformat(),
            'sla_status': sla_status,
        }

    @classmethod
    def get_lead_timeline(
        cls,
        lead: Lead,
        limit: int = 50,
        db_alias: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        alias = db_alias or get_tenant_db_alias() or 'default'
        events = []

        # 1. Lead Created
        events.append({
            'id': f"lead-created-{lead.id}",
            'event_type': 'LEAD_CREATED',
            'occurred_at': lead.created_at.isoformat(),
            'title': 'Lead Created',
            'description': f"Lead profile created ({lead.first_name} {lead.last_name})",
            'actor': 'System' if not lead.referred_by_user else str(lead.referred_by_user),
            'channel': lead.first_touch_source or 'DIRECT',
            'metadata': {
                'status': lead.current_status,
                'branch_id': str(lead.branch_id) if lead.branch_id else None,
                'source': lead.lead_source.name if lead.lead_source else None,
            },
        })

        # 2. Status Transitions
        for sh in LeadStatusHistory.objects.using(alias).filter(lead=lead).select_related('changed_by_user'):
            actor_name = f"{sh.changed_by_user.first_name} {sh.changed_by_user.last_name}".strip() if sh.changed_by_user else 'System'
            events.append({
                'id': f"status-{sh.id}",
                'event_type': 'STATUS_CHANGE',
                'occurred_at': sh.changed_at.isoformat(),
                'title': f"Status changed to {sh.to_status.replace('_', ' ').title()}",
                'description': sh.reason_text or f"Transitioned from {sh.from_status or 'None'}",
                'actor': actor_name,
                'channel': 'CRM',
                'metadata': {
                    'from_status': sh.from_status,
                    'to_status': sh.to_status,
                    'reason_code': sh.reason_code,
                },
            })

        # 3. Assignments
        for la in LeadAssignment.objects.using(alias).filter(lead=lead).select_related('assigned_to_user', 'assigned_by_user'):
            target_name = f"{la.assigned_to_user.first_name} {la.assigned_to_user.last_name}".strip() if la.assigned_to_user else 'Agent'
            by_name = f"{la.assigned_by_user.first_name} {la.assigned_by_user.last_name}".strip() if la.assigned_by_user else 'System'
            events.append({
                'id': f"assign-{la.id}",
                'event_type': 'ASSIGNMENT',
                'occurred_at': la.assigned_at.isoformat(),
                'title': f"Assigned to {target_name}",
                'description': f"Role: {la.assignment_type} assignment",
                'actor': by_name,
                'channel': 'CRM',
                'metadata': {
                    'assignment_type': la.assignment_type,
                    'assigned_to_id': str(la.assigned_to_user_id) if la.assigned_to_user_id else None,
                },
            })

        # 4. Attributions
        for attr in LeadAttribution.objects.using(alias).filter(lead=lead):
            events.append({
                'id': f"attr-{attr.id}",
                'event_type': 'ATTRIBUTION_CAPTURED',
                'occurred_at': attr.captured_at.isoformat(),
                'title': f"Attribution: {attr.touch_type.replace('_', ' ').title()}",
                'description': f"{attr.platform or 'Digital'} • Campaign: {attr.campaign_name or 'Direct'}",
                'actor': 'Marketing Engine',
                'channel': attr.platform or 'WEB',
                'metadata': {
                    'touch_type': attr.touch_type,
                    'platform': attr.platform,
                    'campaign_name': attr.campaign_name,
                    'ad_name': attr.ad_name,
                    'utm_source': attr.utm_source,
                    'utm_medium': attr.utm_medium,
                    'utm_campaign': attr.utm_campaign,
                    'landing_page_url': attr.landing_page_url,
                },
            })

        # 5. Trial Bookings & Status History
        for tb in TrialBooking.objects.using(alias).filter(lead=lead).select_related('assigned_trainer_profile', 'branch'):
            events.append({
                'id': f"trial-{tb.id}",
                'event_type': 'TRIAL_BOOKED',
                'occurred_at': tb.created_at.isoformat(),
                'title': "Trial Session Booked",
                'description': f"Scheduled for {tb.scheduled_start.strftime('%d %b %Y %H:%M')}",
                'actor': 'Staff',
                'channel': tb.booking_source or 'FRONT_DESK',
                'metadata': {
                    'trial_id': str(tb.id),
                    'status': tb.status,
                    'confirmation_status': tb.confirmation_status,
                    'branch': tb.branch.name if tb.branch else None,
                    'start': tb.scheduled_start.isoformat(),
                },
            })
            for tsh in TrialStatusHistory.objects.using(alias).filter(trial_booking=tb).select_related('changed_by_user'):
                t_actor = f"{tsh.changed_by_user.first_name} {tsh.changed_by_user.last_name}".strip() if tsh.changed_by_user else 'System'
                ev_type = 'TRIAL_STATUS_CHANGE'
                title = f"Trial Status: {tsh.to_status}"
                if tsh.to_status == 'ATTENDED':
                    ev_type = 'TRIAL_ATTENDED'
                    title = 'Trial Attended'
                elif tsh.to_status == 'NO_SHOW':
                    ev_type = 'TRIAL_NO_SHOW'
                    title = 'Trial No Show'
                elif tsh.to_status == 'CANCELLED':
                    ev_type = 'TRIAL_CANCELLED'
                    title = 'Trial Cancelled'
                elif tsh.to_status == 'RESCHEDULED':
                    ev_type = 'TRIAL_RESCHEDULED'
                    title = 'Trial Rescheduled'
                elif tsh.to_status == 'CONFIRMED' or (tsh.reason_code and 'CONFIRMED' in tsh.reason_code):
                    ev_type = 'TRIAL_CONFIRMED'
                    title = 'Trial Confirmed'
                elif tsh.reason_code == 'RESCHEDULE_REQUESTED':
                    ev_type = 'TRIAL_RESCHEDULE_REQUESTED'
                    title = 'Trial Reschedule Requested'

                events.append({
                    'id': f"trial-status-{tsh.id}",
                    'event_type': ev_type,
                    'occurred_at': tsh.changed_at.isoformat(),
                    'title': title,
                    'description': tsh.reason_code or f"Updated from {tsh.from_status}",
                    'actor': t_actor,
                    'channel': 'OPS',
                    'metadata': {
                        'from_status': tsh.from_status,
                        'to_status': tsh.to_status,
                        'reason_code': tsh.reason_code,
                    },
                })

        # 6. Activities
        for act in LeadActivity.objects.using(alias).filter(lead=lead).select_related('performed_by_user'):
            act_user = f"{act.performed_by_user.first_name} {act.performed_by_user.last_name}".strip() if act.performed_by_user else 'Staff'
            events.append({
                'id': f"act-{act.id}",
                'event_type': 'LEAD_ACTIVITY',
                'occurred_at': act.activity_at.isoformat(),
                'title': f"{act.activity_type.replace('_', ' ').title()}",
                'description': act.outcome or act.notes or '',
                'actor': act_user,
                'channel': act.activity_type,
                'metadata': {'activity_type': act.activity_type, 'outcome': act.outcome, 'notes': act.notes},
            })

        # 7. Notes
        for ln in LeadNote.objects.using(alias).filter(lead=lead).select_related('created_by_user'):
            note_user = f"{ln.created_by_user.first_name} {ln.created_by_user.last_name}".strip() if ln.created_by_user else 'Staff'
            events.append({
                'id': f"note-{ln.id}",
                'event_type': 'LEAD_NOTE',
                'occurred_at': ln.created_at.isoformat(),
                'title': 'Note Added',
                'description': getattr(ln, 'note_text', getattr(ln, 'content', '')),
                'actor': note_user,
                'channel': 'NOTE',
                'metadata': {'is_pinned': getattr(ln, 'is_pinned', False)},
            })

        # 8. Follow-up Tasks
        for task in SalesFollowupTask.objects.using(alias).filter(lead=lead).select_related('assigned_to_user'):
            assigned_name = f"{task.assigned_to_user.first_name} {task.assigned_to_user.last_name}".strip() if task.assigned_to_user else 'Unassigned'
            events.append({
                'id': f"task-{task.id}",
                'event_type': 'FOLLOWUP_TASK',
                'occurred_at': task.created_at.isoformat(),
                'title': f"Follow-up Scheduled: {task.task_type.replace('_', ' ').title()}",
                'description': f"Due at {task.due_at.strftime('%d %b %Y %H:%M')} • Assigned to {assigned_name}",
                'actor': assigned_name,
                'channel': 'TASK',
                'metadata': {
                    'task_type': task.task_type,
                    'priority': task.priority,
                    'status': task.status,
                    'due_at': task.due_at.isoformat(),
                },
            })
            if task.status == 'COMPLETED':
                events.append({
                    'id': f"task-comp-{task.id}",
                    'event_type': 'FOLLOWUP_COMPLETED',
                    'occurred_at': (task.updated_at or task.created_at).isoformat(),
                    'title': f"Follow-up Completed: {task.task_type.replace('_', ' ').title()}",
                    'description': task.outcome or f"Follow-up completed by {assigned_name}",
                    'actor': assigned_name,
                    'channel': 'TASK',
                    'metadata': {
                        'task_type': task.task_type,
                        'outcome': task.outcome,
                        'completed_at': (task.updated_at or task.created_at).isoformat(),
                    },
                })

        # 9. Communications (Outbound & Inbound)
        from .models_communication import CommunicationMessage
        for comm in CommunicationMessage.objects.using(alias).filter(lead=lead).select_related('created_by_user'):
            actor_name = 'Customer' if comm.direction == 'INBOUND' else (
                f"{comm.created_by_user.first_name} {comm.created_by_user.last_name}".strip() if comm.created_by_user else 'System'
            )
            title = f"{comm.channel.title()} ({comm.direction.title()}): {comm.status.title()}"
            events.append({
                'id': f"comm-{comm.id}",
                'event_type': f"COMMUNICATION_{comm.direction}",
                'occurred_at': (comm.sent_at or comm.received_at or comm.created_at).isoformat(),
                'title': title,
                'description': comm.body_snapshot[:120] + ('...' if len(comm.body_snapshot) > 120 else ''),
                'actor': actor_name,
                'channel': comm.channel,
                'metadata': {
                    'communication_id': str(comm.id),
                    'channel': comm.channel,
                    'direction': comm.direction,
                    'status': comm.status,
                    'purpose': comm.purpose,
                    'recipient': comm.recipient,
                    'sender': comm.sender,
                },
            })

        # Sort descending by occurred_at, with deterministic secondary sort on ID
        events.sort(key=lambda e: (e['occurred_at'], e['id']), reverse=True)
        return events[:limit]

    @classmethod
    def update_lead(
        cls,
        lead: Lead,
        data: Dict[str, Any],
        actor_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> Lead:
        """
        Atomically updates a Lead and optional LeadCommercialProfile,
        tracking assignment changes and logging audit events.
        """
        alias = db_alias or get_tenant_db_alias() or 'default'

        with transaction.atomic(using=alias):
            fields_to_update = dict(data)
            billing_name = fields_to_update.pop('billing_name', None)
            gst_number = fields_to_update.pop('gst_number', None)
            pan_number = fields_to_update.pop('pan_number', None)

            if 'email_normalized' in fields_to_update and fields_to_update['email_normalized']:
                fields_to_update['email_normalized'] = validate_lead_email(fields_to_update['email_normalized'])
            if 'email' in fields_to_update and fields_to_update['email']:
                fields_to_update['email_normalized'] = validate_lead_email(fields_to_update.pop('email'))
            if 'phone_normalized' in fields_to_update and fields_to_update['phone_normalized']:
                fields_to_update['phone_normalized'] = validate_lead_phone(fields_to_update['phone_normalized'])
            if 'phone' in fields_to_update and fields_to_update['phone']:
                fields_to_update['phone_normalized'] = validate_lead_phone(fields_to_update.pop('phone'))

            assigned_sales_user = fields_to_update.get('assigned_sales_user')
            if assigned_sales_user is not None and assigned_sales_user != lead.assigned_sales_user:
                # Update assignment history
                LeadAssignment.objects.using(alias).filter(
                    lead=lead, assignment_type='SALES', status='ACTIVE'
                ).update(status='INACTIVE', unassigned_at=timezone.now())
                if assigned_sales_user:
                    LeadAssignment.objects.using(alias).create(
                        lead=lead,
                        assigned_to_user=assigned_sales_user,
                        assigned_by_user=actor_user,
                        assignment_type='SALES',
                        status='ACTIVE',
                    )

            if fields_to_update.get('current_status') == 'CONVERTED' and lead.current_status != 'CONVERTED':
                from .models_crm import LeadConversion
                if not LeadConversion.objects.using(alias).filter(lead=lead).exists():
                    raise ValidationError(
                        "Manual update to CONVERTED is forbidden. "
                        "Leads may only be converted through the commercial conversion flow."
                    )

            for field, val in fields_to_update.items():
                if hasattr(lead, field):
                    setattr(lead, field, val)
            lead.save(using=alias)

            # Commercial profile
            if billing_name is not None or gst_number is not None or pan_number is not None:
                profile, _ = LeadCommercialProfile.objects.using(alias).get_or_create(lead=lead)
                if billing_name is not None:
                    profile.billing_name = billing_name
                if gst_number is not None:
                    profile.gst_number = gst_number
                if pan_number is not None:
                    profile.pan_number = pan_number
                profile.save(using=alias)

            record_business_audit(
                organization=lead.organization,
                branch=lead.branch,
                actor_type='EMPLOYEE' if actor_user else 'SYSTEM',
                actor_user=actor_user,
                module='crm',
                action_code='LEAD_UPDATED',
                entity_type='Lead',
                entity_id=lead.id,
                event_description=f"Updated lead {lead.first_name} {lead.last_name}",
                after_data={'lead_id': str(lead.id), 'status': lead.current_status},
                db_alias=alias,
            )

            enqueue_outbox_event(
                organization=lead.organization,
                event_type='LEAD_UPDATED',
                aggregate_type='Lead',
                aggregate_id=lead.id,
                payload={'lead_id': str(lead.id), 'branch_id': str(lead.branch_id) if lead.branch_id else None},
                db_alias=alias,
            )

            return lead

    @classmethod
    def transition_lead_status(
        cls,
        lead: Lead,
        new_status: str,
        reason_code: Optional[str] = None,
        reason_text: Optional[str] = None,
        actor_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> Lead:
        alias = db_alias or get_tenant_db_alias() or 'default'

        with transaction.atomic(using=alias):
            old_status = lead.current_status
            if old_status == new_status:
                return lead

            if new_status == 'CONVERTED':
                from .models_crm import LeadConversion
                if not LeadConversion.objects.using(alias).filter(lead=lead).exists():
                    raise ValidationError(
                        "Manual transition to CONVERTED is forbidden. "
                        "Leads may only be converted through the commercial conversion flow."
                    )

            if new_status == 'TRIAL_BOOKED':
                from .models_crm import TrialBooking
                has_active = TrialBooking.objects.using(alias).filter(
                    lead=lead,
                    status__in=['BOOKED', 'CONFIRMED', 'ATTENDED'],
                ).exclude(confirmation_status__in=['CANCELLED', 'DECLINED']).exists()
                if not has_active:
                    raise ValidationError("Book a real trial session to move this lead to Trial Booked.")

            if new_status == 'TRIAL_CONFIRMED':
                from .models_crm import TrialBooking
                has_confirmed = TrialBooking.objects.using(alias).filter(
                    lead=lead,
                ).filter(
                    models.Q(status='CONFIRMED') | models.Q(confirmation_status='CONFIRMED')
                ).exists()
                if not has_confirmed:
                    raise ValidationError("Trial must be confirmed through the trial management lifecycle.")

            if new_status == 'TRIAL_ATTENDED':
                from .models_crm import TrialBooking
                if not TrialBooking.objects.using(alias).filter(lead=lead, status='ATTENDED').exists():
                    raise ValidationError("Trial attendance must be verified through the trial check-in lifecycle.")

            if new_status == 'NO_SHOW':
                from .models_crm import TrialBooking
                if not TrialBooking.objects.using(alias).filter(lead=lead, status='NO_SHOW').exists():
                    raise ValidationError("Trial no-show must be recorded through the trial management lifecycle.")

            lead.current_status = new_status
            lead.save(using=alias, update_fields=['current_status', 'updated_at'])

            LeadStatusHistory.objects.using(alias).create(
                lead=lead,
                from_status=old_status,
                to_status=new_status,
                reason_code=reason_code,
                reason_text=reason_text,
                changed_by_user=actor_user,
            )

            record_business_audit(
                organization=lead.organization,
                branch=lead.branch,
                actor_type='EMPLOYEE' if actor_user else 'SYSTEM',
                actor_user=actor_user,
                module='crm',
                action_code='LEAD_STATUS_TRANSITION',
                entity_type='Lead',
                entity_id=lead.id,
                event_description=f"Lead {lead.id} transitioned from {old_status} to {new_status}",
                before_data={'status': old_status},
                after_data={'status': new_status, 'reason': reason_code},
                db_alias=alias,
            )

            enqueue_outbox_event(
                organization=lead.organization,
                event_type='LEAD_STATUS_CHANGED',
                aggregate_type='Lead',
                aggregate_id=lead.id,
                payload={'lead_id': str(lead.id), 'old_status': old_status, 'new_status': new_status},
                db_alias=alias,
            )

            return lead

    @classmethod
    def assign_lead(
        cls,
        lead: Lead,
        assigned_to_user: TenantUser,
        assignment_type: str = 'SALES',
        actor_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> LeadAssignment:
        alias = db_alias or get_tenant_db_alias() or 'default'

        with transaction.atomic(using=alias):
            # Deactivate previous active assignment of same type
            LeadAssignment.objects.using(alias).filter(
                lead=lead,
                assignment_type=assignment_type,
                status='ACTIVE',
            ).update(status='INACTIVE', unassigned_at=timezone.now())

            new_assign = LeadAssignment.objects.using(alias).create(
                lead=lead,
                assigned_to_user=assigned_to_user,
                assigned_by_user=actor_user,
                assignment_type=assignment_type,
                status='ACTIVE',
            )

            if assignment_type == 'SALES':
                lead.assigned_sales_user = assigned_to_user
                lead.save(using=alias, update_fields=['assigned_sales_user', 'updated_at'])
            elif assignment_type == 'TRAINER':
                lead.assigned_trainer_user = assigned_to_user
                lead.save(using=alias, update_fields=['assigned_trainer_user', 'updated_at'])

            return new_assign

    @classmethod
    def resolve_trial_booking_policy(
        cls,
        lead: Optional[Lead],
        branch: Branch,
        class_template: Optional[Any] = None,
        occurrence: Optional[Any] = None,
        db_alias: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        alias = db_alias or get_tenant_db_alias() or 'default'
        from .models_bookings import BookingPolicySet

        org = lead.organization if lead else (branch.organization if branch else None)
        policies = BookingPolicySet.objects.using(alias).filter(
            organization=org,
            status='ACTIVE',
        ) if org else BookingPolicySet.objects.none()
        policy = None
        if occurrence:
            policy = policies.filter(occurrence=occurrence).first()
        if not policy and class_template:
            policy = policies.filter(class_template=class_template, branch=branch).first()
            if not policy:
                policy = policies.filter(class_template=class_template, branch__isnull=True).first()
        if not policy:
            policy = policies.filter(branch=branch, class_template__isnull=True, occurrence__isnull=True).first()
        if not policy:
            policy = policies.filter(branch__isnull=True, class_template__isnull=True, occurrence__isnull=True).first()

        if policy:
            if not policy.allow_trial:
                return False, "Trials are not permitted by booking policy for this session."
            if policy.max_trial_bookings and lead:
                used_trials = TrialBooking.objects.using(alias).filter(lead=lead).exclude(status__in=['CANCELLED', 'RESCHEDULED']).count()
                if used_trials >= policy.max_trial_bookings:
                    return False, f"Maximum trial limit reached: maximum of {policy.max_trial_bookings} exceeded for this lead."
            if occurrence:
                diff_minutes = int((occurrence.start_at - timezone.now()).total_seconds() / 60)
                if policy.booking_open_minutes_before and diff_minutes > policy.booking_open_minutes_before:
                    return False, f"Booking is not yet open for this session (opens {policy.booking_open_minutes_before} minutes before start)."
                if policy.booking_close_minutes_before and diff_minutes < policy.booking_close_minutes_before:
                    return False, f"Booking has closed for this session ({policy.booking_close_minutes_before} minutes before start cutoff)."

        return True, None

    @classmethod
    def get_available_trial_slots(
        cls,
        branch_id: str,
        lead_id: Optional[str] = None,
        date_from: Optional[Any] = None,
        date_to: Optional[Any] = None,
        class_template_id: Optional[str] = None,
        program_id: Optional[str] = None,
        db_alias: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        alias = db_alias or get_tenant_db_alias() or 'default'
        from .models_classes import ClassOccurrence, ClassBranchAvailability
        from .models_bookings import Booking

        now = timezone.now()
        qs = ClassOccurrence.objects.using(alias).filter(
            branch_id=branch_id,
            status__in=['SCHEDULED', 'OPEN'],
            start_at__gt=now,
            class_template__status='ACTIVE',
            class_template__allow_trial=True,
        ).select_related(
            'class_template',
            'class_template__program',
            'branch'
        ).prefetch_related(
            'trainer_assignments__trainer_profile__employee_profile__user_profile__user'
        )

        if date_from:
            qs = qs.filter(occurrence_date__gte=date_from)
        if date_to:
            qs = qs.filter(occurrence_date__lte=date_to)
        if class_template_id:
            qs = qs.filter(class_template_id=class_template_id)
        if program_id:
            qs = qs.filter(class_template__program_id=program_id)

        lead = Lead.objects.using(alias).filter(id=lead_id).first() if lead_id else None

        slots = []
        for occ in qs.order_by('start_at')[:100]:
            # Branch availability check
            branch_avail = ClassBranchAvailability.objects.using(alias).filter(
                class_template=occ.class_template,
                branch=occ.branch,
            ).first()
            if branch_avail and branch_avail.status == 'DISABLED':
                continue

            effective_capacity = (
                occ.capacity
                or (branch_avail.capacity_override if branch_avail and branch_avail.capacity_override else None)
                or occ.class_template.default_capacity
            )
            effective_trial_cap = (
                occ.trial_capacity
                or (branch_avail.trial_capacity_override if branch_avail and branch_avail.trial_capacity_override else None)
                or occ.class_template.default_trial_capacity
            )

            # Semantics: If effective_trial_cap <= 0, trials are disabled for this session
            if effective_trial_cap <= 0:
                continue

            policy_allowed, policy_message = cls.resolve_trial_booking_policy(
                lead=lead,
                branch=occ.branch,
                class_template=occ.class_template,
                occurrence=occ,
                db_alias=alias,
            )
            if not policy_allowed and lead:
                continue

            # Calculate total occupancy
            regular_bookings_count = Booking.objects.using(alias).filter(
                occurrence=occ,
                status__in=['CONFIRMED', 'RESERVED', 'COMPLETED'],
            ).count()
            trial_bookings_count = TrialBooking.objects.using(alias).filter(
                class_occurrence_id=occ.id,
                status__in=['BOOKED', 'CONFIRMED', 'ATTENDED'],
            ).exclude(confirmation_status__in=['CANCELLED', 'DECLINED']).count()

            total_occupied = regular_bookings_count + trial_bookings_count
            remaining_capacity = max(0, effective_capacity - total_occupied)
            remaining_trial_capacity = max(0, effective_trial_cap - trial_bookings_count)

            # CRITICAL TRIAL CAPACITY RULE:
            # Must have BOTH overall capacity remaining AND trial capacity remaining!
            if remaining_capacity <= 0 or remaining_trial_capacity <= 0:
                continue

            # Duplicate booking check: if this lead already has an active trial for this session, skip
            if lead and TrialBooking.objects.using(alias).filter(
                lead=lead,
                class_occurrence_id=occ.id,
                status__in=['BOOKED', 'CONFIRMED', 'ATTENDED'],
            ).exclude(confirmation_status__in=['CANCELLED', 'DECLINED']).exists():
                continue

            # Trainer info
            trainer_assignment = occ.trainer_assignments.filter(status='ACTIVE').first() or occ.trainer_assignments.first()
            trainer_id = None
            trainer_name = 'Unassigned'
            if trainer_assignment and trainer_assignment.trainer_profile:
                tp = trainer_assignment.trainer_profile
                trainer_id = str(tp.id)
                emp = getattr(tp, 'employee_profile', None)
                up = getattr(emp, 'user_profile', None) if emp else None
                u = getattr(up, 'user', None) if up else None
                if u:
                    trainer_name = f"{u.first_name} {u.last_name}".strip()
                elif emp:
                    trainer_name = f"{emp.first_name_snapshot or ''} {emp.last_name_snapshot or ''}".strip() or emp.employee_code
                else:
                    trainer_name = tp.trainer_code

            slots.append({
                'occurrence_id': str(occ.id),
                'class_template_id': str(occ.class_template_id),
                'class_name': occ.class_template.name,
                'program_id': str(occ.class_template.program_id) if occ.class_template.program_id else None,
                'program_name': occ.class_template.program.name if occ.class_template.program else None,
                'branch_id': str(occ.branch_id),
                'branch_name': occ.branch.name,
                'occurrence_date': occ.occurrence_date.isoformat(),
                'start_at': occ.start_at.isoformat(),
                'end_at': occ.end_at.isoformat(),
                'start_time': occ.start_at.strftime('%H:%M'),
                'end_time': occ.end_at.strftime('%H:%M'),
                'delivery_mode': occ.delivery_mode,
                'booking_capacity': effective_capacity,
                'capacity': effective_capacity,
                'total_capacity': effective_capacity,
                'booked_count': total_occupied,
                'total_booked': total_occupied,
                'remaining_capacity': remaining_capacity,
                'available_spots': remaining_capacity,
                'trial_capacity': effective_trial_cap,
                'trial_booked': trial_bookings_count,
                'trial_booked_count': trial_bookings_count,
                'remaining_trial_capacity': remaining_trial_capacity,
                'available_trial_spots': min(remaining_capacity, remaining_trial_capacity),
                'policy_allowed': policy_allowed,
                'policy_message': policy_message if not policy_allowed else '',
                'is_available': policy_allowed and remaining_capacity > 0 and remaining_trial_capacity > 0,
                'trainer_id': trainer_id,
                'trainer_name': trainer_name,
            })

        return slots

    @classmethod
    def book_trial(
        cls,
        lead: Optional[Lead] = None,
        branch: Optional[Branch] = None,
        scheduled_start: Optional[datetime] = None,
        scheduled_end: Optional[datetime] = None,
        class_occurrence_id: Optional[Any] = None,
        assigned_trainer: Optional[TrainerProfile] = None,
        trial_type: str = 'GROUP_CLASS',
        booking_source: str = 'WEB',
        notes: Optional[str] = None,
        actor_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
        **kwargs,
    ) -> TrialBooking:
        alias = kwargs.get('tenant_alias') or db_alias or get_tenant_db_alias() or 'default'
        if not lead and 'lead_id' in kwargs:
            lead = Lead.objects.using(alias).get(id=kwargs['lead_id'])
        if not branch and 'branch_id' in kwargs:
            branch = Branch.objects.using(alias).get(id=kwargs['branch_id'])
        if not actor_user and 'performed_by_user' in kwargs:
            actor_user = kwargs['performed_by_user']
        if not class_occurrence_id and 'class_occurrence_id' in kwargs:
            class_occurrence_id = kwargs['class_occurrence_id']

        if not class_occurrence_id:
            raise ValidationError("A valid class occurrence must be selected to book a trial.")

        from .models_classes import ClassOccurrence, ClassBranchAvailability
        from .models_bookings import Booking

        with transaction.atomic(using=alias):
            # Organization isolation
            if lead.organization_id != branch.organization_id:
                raise ValidationError("Lead and Branch belong to different organizations.")

            if lead.current_status == 'CONVERTED':
                raise ValidationError("Converted leads are members and cannot book prospect trials. Use member booking.")

            try:
                occ = ClassOccurrence.objects.using(alias).select_for_update().get(id=class_occurrence_id)
            except ClassOccurrence.DoesNotExist:
                raise ValidationError("Class occurrence does not exist.")

            if occ.branch_id != branch.id:
                raise ValidationError("Class occurrence does not belong to the selected branch.")

            if occ.status not in ['SCHEDULED', 'OPEN']:
                raise ValidationError(f"Class occurrence is not open for booking (status: {occ.status}).")

            if occ.start_at <= timezone.now():
                raise ValidationError("Cannot book a trial for a past class occurrence.")

            if occ.class_template.status != 'ACTIVE':
                raise ValidationError("Class template is not active.")

            if not occ.class_template.allow_trial:
                raise ValidationError("Trials are not permitted for this class.")

            branch_avail = ClassBranchAvailability.objects.using(alias).filter(
                class_template=occ.class_template,
                branch=occ.branch,
            ).first()
            if branch_avail and branch_avail.status == 'DISABLED':
                raise ValidationError("This class is disabled at the selected branch.")

            effective_capacity = (
                occ.capacity
                or (branch_avail.capacity_override if branch_avail and branch_avail.capacity_override else None)
                or occ.class_template.default_capacity
            )
            effective_trial_cap = (
                occ.trial_capacity
                or (branch_avail.trial_capacity_override if branch_avail and branch_avail.trial_capacity_override else None)
                or occ.class_template.default_trial_capacity
            )

            if effective_trial_cap <= 0:
                raise ValidationError("Trial bookings are not enabled for this session (trial capacity is 0).")

            # Duplicate booking check: lead already has active trial for this occurrence
            # Booking Policy Check
            allowed, reason = cls.resolve_trial_booking_policy(
                lead=lead,
                branch=branch,
                class_template=occ.class_template,
                occurrence=occ,
                db_alias=alias,
            )
            if not allowed:
                raise ValidationError(reason or "Booking policy does not allow trial for this session.")

            # Duplicate booking check: lead already has active trial for this occurrence
            existing_active = TrialBooking.objects.using(alias).filter(
                lead=lead,
                class_occurrence_id=occ.id,
                status__in=['BOOKED', 'CONFIRMED', 'ATTENDED'],
            ).exclude(confirmation_status__in=['CANCELLED', 'DECLINED']).exists()
            if existing_active:
                raise ValidationError("Lead already has an active trial booked for this session.")

            # Concurrency-safe capacity re-validation under lock
            regular_booked = Booking.objects.using(alias).filter(
                occurrence=occ,
                status__in=['CONFIRMED', 'RESERVED', 'COMPLETED'],
            ).count()
            trial_booked = TrialBooking.objects.using(alias).filter(
                class_occurrence_id=occ.id,
                status__in=['BOOKED', 'CONFIRMED', 'ATTENDED'],
            ).exclude(confirmation_status__in=['CANCELLED', 'DECLINED']).count()

            if regular_booked + trial_booked >= effective_capacity:
                raise ValidationError("Total occurrence capacity reached for this session. This trial session is now full. Please select another session.")

            if effective_trial_cap > 0 and trial_booked >= effective_trial_cap:
                raise ValidationError("Trial capacity reached for this session. This trial session is now full. Please select another session.")

            scheduled_start = occ.start_at
            scheduled_end = occ.end_at
            if not assigned_trainer:
                ta = occ.trainer_assignments.filter(status='ACTIVE').first() or occ.trainer_assignments.first()
                if ta and ta.trainer_profile:
                    assigned_trainer = ta.trainer_profile

            trial = TrialBooking.objects.using(alias).create(
                lead=lead,
                branch=branch,
                class_occurrence_id=occ.id if occ else None,
                assigned_trainer_profile=assigned_trainer,
                trial_type=trial_type,
                scheduled_start=scheduled_start,
                scheduled_end=scheduled_end,
                status='BOOKED',
                confirmation_status='PENDING',
                booking_source=booking_source,
                notes=notes,
                created_by_user=actor_user,
            )

            TrialStatusHistory.objects.using(alias).create(
                trial_booking=trial,
                from_status=None,
                to_status='BOOKED',
                reason_code='INITIAL_BOOKING',
                changed_by_user=actor_user,
            )

            # Sync lead status
            cls.transition_lead_status(
                lead=lead,
                new_status='TRIAL_BOOKED',
                reason_code='TRIAL_SCHEDULED',
                reason_text=f"Trial booked for {scheduled_start}",
                actor_user=actor_user,
                db_alias=alias,
            )

            record_business_audit(
                organization=lead.organization,
                branch=branch,
                actor_type='EMPLOYEE' if actor_user else 'SYSTEM',
                actor_user=actor_user,
                module='crm',
                action_code='CRM_TRIAL_BOOKED',
                entity_type='TrialBooking',
                entity_id=trial.id,
                event_description=f"Booked trial for {lead.first_name} {lead.last_name} at {branch.name}",
                after_data={
                    'trial_id': str(trial.id),
                    'lead_id': str(lead.id),
                    'branch_id': str(branch.id),
                    'scheduled_start': scheduled_start.isoformat(),
                    'class_occurrence_id': str(occ.id) if occ else None,
                },
                db_alias=alias,
            )

            enqueue_outbox_event(
                organization=lead.organization,
                event_type='CRM_TRIAL_BOOKED',
                aggregate_type='TrialBooking',
                aggregate_id=str(trial.id),
                payload={
                    'trial_id': str(trial.id),
                    'lead_id': str(lead.id),
                    'branch_id': str(branch.id),
                    'scheduled_start': scheduled_start.isoformat(),
                },
                db_alias=alias,
            )

            return trial

    @classmethod
    def confirm_trial(
        cls,
        trial: TrialBooking,
        channel: str = 'MANUAL',
        notes: Optional[str] = None,
        actor_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> TrialBooking:
        alias = db_alias or get_tenant_db_alias() or 'default'
        with transaction.atomic(using=alias):
            trial.confirmation_status = 'CONFIRMED'
            trial.confirmation_channel = channel
            trial.confirmed_at = timezone.now()
            if notes:
                trial.notes = f"{trial.notes}\n{notes}".strip() if trial.notes else notes
            trial.save(using=alias, update_fields=['confirmation_status', 'confirmation_channel', 'confirmed_at', 'notes', 'updated_at'])

            TrialStatusHistory.objects.using(alias).create(
                trial_booking=trial,
                from_status=trial.status,
                to_status=trial.status,
                reason_code=f"CONFIRMED_VIA_{channel}",
                changed_by_user=actor_user,
            )

            cls.transition_lead_status(
                lead=trial.lead,
                new_status='TRIAL_CONFIRMED',
                reason_code='TRIAL_CONFIRMED',
                reason_text=f"Trial confirmed via {channel}",
                actor_user=actor_user,
                db_alias=alias,
            )

            record_business_audit(
                organization=trial.lead.organization,
                branch=trial.branch,
                actor_type='EMPLOYEE' if actor_user else 'SYSTEM',
                actor_user=actor_user,
                module='crm',
                action_code='CRM_TRIAL_CONFIRMED',
                entity_type='TrialBooking',
                entity_id=trial.id,
                event_description=f"Confirmed trial {trial.id} via {channel}",
                after_data={'trial_id': str(trial.id), 'confirmation_channel': channel},
                db_alias=alias,
            )

            enqueue_outbox_event(
                organization=trial.lead.organization,
                event_type='CRM_TRIAL_CONFIRMED',
                aggregate_type='TrialBooking',
                aggregate_id=str(trial.id),
                payload={'trial_id': str(trial.id), 'channel': channel},
                db_alias=alias,
            )

            return trial

    @classmethod
    def request_reschedule_trial(
        cls,
        trial: TrialBooking,
        reason: Optional[str] = None,
        actor_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> TrialBooking:
        alias = db_alias or get_tenant_db_alias() or 'default'
        with transaction.atomic(using=alias):
            trial.confirmation_status = 'RESCHEDULE_REQUESTED'
            if reason:
                trial.cancellation_reason = reason
            trial.save(using=alias, update_fields=['confirmation_status', 'cancellation_reason', 'updated_at'])

            TrialStatusHistory.objects.using(alias).create(
                trial_booking=trial,
                from_status=trial.status,
                to_status=trial.status,
                reason_code='RESCHEDULE_REQUESTED',
                changed_by_user=actor_user,
            )

            record_business_audit(
                organization=trial.lead.organization,
                branch=trial.branch,
                actor_type='EMPLOYEE' if actor_user else 'SYSTEM',
                actor_user=actor_user,
                module='crm',
                action_code='CRM_TRIAL_RESCHEDULE_REQUESTED',
                entity_type='TrialBooking',
                entity_id=trial.id,
                event_description=f"Reschedule requested for trial {trial.id}: {reason or ''}",
                db_alias=alias,
            )
            return trial

    @classmethod
    def reschedule_trial(
        cls,
        trial: TrialBooking,
        new_scheduled_start: Optional[datetime] = None,
        new_scheduled_end: Optional[datetime] = None,
        new_class_occurrence_id: Optional[uuid.UUID] = None,
        reason: Optional[str] = None,
        actor_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> TrialBooking:
        alias = db_alias or get_tenant_db_alias() or 'default'
        from .models_classes import ClassOccurrence, ClassBranchAvailability
        from .models_bookings import Booking, BookingPolicySet

        with transaction.atomic(using=alias):
            # Re-lock trial record
            trial = TrialBooking.objects.using(alias).select_for_update().get(id=trial.id)

            if trial.status in ['ATTENDED', 'CANCELLED']:
                raise ValidationError(f"Cannot reschedule trial in '{trial.status}' status.")

            # Max reschedules check
            reschedule_count = TrialStatusHistory.objects.using(alias).filter(
                trial_booking=trial, to_status='RESCHEDULED'
            ).count()
            bp = BookingPolicySet.objects.using(alias).filter(organization=trial.lead.organization, status='ACTIVE').first()
            if bp and bp.max_reschedules and reschedule_count >= bp.max_reschedules:
                raise ValidationError(f"Maximum reschedules ({bp.max_reschedules}) exceeded for this trial.")

            if not new_class_occurrence_id:
                raise ValidationError("new_class_occurrence_id is required to reschedule trial to an authoritative session.")

            try:
                new_occ = ClassOccurrence.objects.using(alias).select_for_update().get(id=new_class_occurrence_id)
            except ClassOccurrence.DoesNotExist:
                raise ValidationError("New class occurrence does not exist.")

            if new_occ.branch_id != trial.branch_id:
                raise ValidationError("New class occurrence does not belong to the trial's branch.")

            if new_occ.status not in ['SCHEDULED', 'OPEN']:
                raise ValidationError(f"Class occurrence is not open for booking (status: {new_occ.status}).")

            if new_occ.start_at <= timezone.now():
                raise ValidationError("Cannot reschedule to a past class occurrence.")

            if new_occ.class_template.status != 'ACTIVE':
                raise ValidationError("Class template is not active.")

            if not new_occ.class_template.allow_trial:
                raise ValidationError("Trials are not permitted for this class.")

            branch_avail = ClassBranchAvailability.objects.using(alias).filter(
                class_template=new_occ.class_template,
                branch=new_occ.branch,
            ).first()
            if branch_avail and branch_avail.status == 'DISABLED':
                raise ValidationError("This class is disabled at the selected branch.")

            effective_capacity = (
                new_occ.capacity
                if new_occ.capacity is not None
                else (branch_avail.capacity_override if branch_avail and branch_avail.capacity_override is not None else new_occ.class_template.default_capacity)
            )
            effective_trial_cap = (
                new_occ.trial_capacity
                if new_occ.trial_capacity is not None
                else (branch_avail.trial_capacity_override if branch_avail and branch_avail.trial_capacity_override is not None else new_occ.class_template.default_trial_capacity)
            )

            if effective_trial_cap <= 0 or effective_capacity <= 0:
                raise ValidationError("Trial capacity reached for this session. This trial session is now full. Please select another session.")

            # Concurrency-safe capacity re-validation under lock
            reg_count = Booking.objects.using(alias).filter(
                occurrence=new_occ,
                status__in=['CONFIRMED', 'RESERVED', 'COMPLETED'],
            ).count()
            tr_count = TrialBooking.objects.using(alias).filter(
                class_occurrence_id=new_occ.id,
                status__in=['BOOKED', 'CONFIRMED', 'ATTENDED'],
            ).exclude(confirmation_status__in=['CANCELLED', 'DECLINED']).count()

            # If target occurrence is the same as current occurrence, don't double count self
            if trial.class_occurrence_id == new_occ.id:
                tr_count = max(0, tr_count - 1)

            if reg_count + tr_count >= effective_capacity:
                raise ValidationError("Total occurrence capacity reached for this session. This trial session is now full. Please select another session.")
            if effective_trial_cap > 0 and tr_count >= effective_trial_cap:
                raise ValidationError("Trial capacity reached for this session. This trial session is now full. Please select another session.")

            assigned_trainer = None
            ta = new_occ.trainer_assignments.filter(status='ACTIVE').first() or new_occ.trainer_assignments.first()
            if ta and ta.trainer_profile:
                assigned_trainer = ta.trainer_profile

            old_occ_id = trial.class_occurrence_id
            old_start = trial.scheduled_start
            old_end = trial.scheduled_end
            old_status = trial.status

            # Atomic capacity transfer: update single canonical TrialBooking in-place
            trial.class_occurrence_id = new_occ.id
            trial.scheduled_start = new_occ.start_at
            trial.scheduled_end = new_occ.end_at
            trial.assigned_trainer_profile = assigned_trainer
            trial.status = 'BOOKED'
            trial.confirmation_status = 'PENDING'
            trial.cancellation_reason = None
            trial.save(using=alias, update_fields=[
                'class_occurrence_id', 'scheduled_start', 'scheduled_end',
                'assigned_trainer_profile', 'status', 'confirmation_status',
                'cancellation_reason', 'updated_at'
            ])

            # Write status transition history
            TrialStatusHistory.objects.using(alias).create(
                trial_booking=trial,
                from_status=old_status,
                to_status='RESCHEDULED',
                reason_code=reason or f"RESCHEDULED_FROM_{old_start}_TO_{new_occ.start_at}",
                changed_by_user=actor_user,
            )
            TrialStatusHistory.objects.using(alias).create(
                trial_booking=trial,
                from_status='RESCHEDULED',
                to_status='BOOKED',
                reason_code='RESCHEDULED_SLOT_BOOKED',
                changed_by_user=actor_user,
            )

            # Sync lead status to TRIAL_BOOKED
            cls.transition_lead_status(
                lead=trial.lead,
                new_status='TRIAL_BOOKED',
                reason_code='TRIAL_RESCHEDULED',
                reason_text=f"Rescheduled from {old_start} to {new_occ.start_at}",
                actor_user=actor_user,
                db_alias=alias,
            )

            record_business_audit(
                organization=trial.lead.organization,
                branch=trial.branch,
                actor_type='EMPLOYEE' if actor_user else 'SYSTEM',
                actor_user=actor_user,
                module='crm',
                action_code='CRM_TRIAL_RESCHEDULED',
                entity_type='TrialBooking',
                entity_id=trial.id,
                event_description=f"Rescheduled trial {trial.id} to new occurrence {new_occ.id} at {new_occ.start_at}",
                after_data={
                    'trial_id': str(trial.id),
                    'old_occurrence_id': str(old_occ_id) if old_occ_id else None,
                    'new_occurrence_id': str(new_occ.id),
                    'old_start': old_start.isoformat() if old_start else None,
                    'new_start': new_occ.start_at.isoformat(),
                },
                db_alias=alias,
            )

            enqueue_outbox_event(
                organization=trial.lead.organization,
                event_type='CRM_TRIAL_RESCHEDULED',
                aggregate_type='TrialBooking',
                aggregate_id=str(trial.id),
                payload={
                    'trial_id': str(trial.id),
                    'old_occurrence_id': str(old_occ_id) if old_occ_id else None,
                    'new_occurrence_id': str(new_occ.id),
                },
                db_alias=alias,
            )

            return trial

    @classmethod
    def cancel_trial(
        cls,
        trial: TrialBooking,
        reason: Optional[str] = None,
        actor_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> TrialBooking:
        alias = db_alias or get_tenant_db_alias() or 'default'
        with transaction.atomic(using=alias):
            if trial.status in ['ATTENDED', 'CANCELLED']:
                raise ValidationError(f"Cannot cancel trial in '{trial.status}' status.")

            old_status = trial.status
            trial.status = 'CANCELLED'
            trial.confirmation_status = 'CANCELLED'
            trial.cancellation_reason = reason
            trial.save(using=alias, update_fields=['status', 'confirmation_status', 'cancellation_reason', 'updated_at'])

            TrialStatusHistory.objects.using(alias).create(
                trial_booking=trial,
                from_status=old_status,
                to_status='CANCELLED',
                reason_code=reason or 'MANUAL_CANCELLATION',
                changed_by_user=actor_user,
            )

            # Check if lead has any other active trial bookings
            has_other_active = TrialBooking.objects.using(alias).filter(
                lead=trial.lead,
                status__in=['BOOKED', 'CONFIRMED', 'ATTENDED'],
            ).exclude(id=trial.id).exists()

            if not has_other_active and trial.lead.current_status in ['TRIAL_BOOKED', 'TRIAL_CONFIRMED']:
                cls.transition_lead_status(
                    lead=trial.lead,
                    new_status='FOLLOW_UP_PENDING',
                    reason_code='TRIAL_CANCELLED',
                    reason_text=f"Trial cancelled: {reason or 'Customer request'}",
                    actor_user=actor_user,
                    db_alias=alias,
                )

            record_business_audit(
                organization=trial.lead.organization,
                branch=trial.branch,
                actor_type='EMPLOYEE' if actor_user else 'SYSTEM',
                actor_user=actor_user,
                module='crm',
                action_code='CRM_TRIAL_CANCELLED',
                entity_type='TrialBooking',
                entity_id=trial.id,
                event_description=f"Cancelled trial {trial.id}: {reason or ''}",
                db_alias=alias,
            )

            enqueue_outbox_event(
                organization=trial.lead.organization,
                event_type='CRM_TRIAL_CANCELLED',
                aggregate_type='TrialBooking',
                aggregate_id=str(trial.id),
                payload={'trial_id': str(trial.id), 'reason': reason},
                db_alias=alias,
            )

            return trial

    @classmethod
    def mark_trial_attended(
        cls,
        trial: TrialBooking,
        notes: Optional[str] = None,
        actor_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> TrialBooking:
        alias = db_alias or get_tenant_db_alias() or 'default'
        from .models_crm import CRMTrialReminderPolicy
        from .models_crm import SalesFollowupTask

        with transaction.atomic(using=alias):
            if trial.status in ['ATTENDED', 'NO_SHOW', 'CANCELLED', 'RESCHEDULED']:
                raise ValidationError(f"Cannot mark trial attended from '{trial.status}' status.")

            old_status = trial.status
            trial.status = 'ATTENDED'
            trial.confirmation_status = 'CONFIRMED'
            if notes:
                trial.notes = f"{trial.notes}\n{notes}".strip() if trial.notes else notes
            trial.save(using=alias, update_fields=['status', 'confirmation_status', 'notes', 'updated_at'])

            TrialStatusHistory.objects.using(alias).create(
                trial_booking=trial,
                from_status=old_status,
                to_status='ATTENDED',
                reason_code='SESSION_ATTENDED',
                changed_by_user=actor_user,
            )

            cls.transition_lead_status(
                lead=trial.lead,
                new_status='TRIAL_ATTENDED',
                reason_code='TRIAL_ATTENDED',
                actor_user=actor_user,
                db_alias=alias,
            )

            record_business_audit(
                organization=trial.lead.organization,
                branch=trial.branch,
                actor_type='EMPLOYEE' if actor_user else 'SYSTEM',
                actor_user=actor_user,
                module='crm',
                action_code='CRM_TRIAL_ATTENDED',
                entity_type='TrialBooking',
                entity_id=trial.id,
                event_description=f"Marked trial {trial.id} as ATTENDED",
                db_alias=alias,
            )

            enqueue_outbox_event(
                organization=trial.lead.organization,
                event_type='CRM_TRIAL_ATTENDED',
                aggregate_type='TrialBooking',
                aggregate_id=str(trial.id),
                payload={'trial_id': str(trial.id), 'lead_id': str(trial.lead_id)},
                db_alias=alias,
            )

            # Idempotent post-attended follow-up task
            policy = CRMTrialReminderPolicy.objects.using(alias).filter(organization=trial.lead.organization).first()
            if policy and policy.post_attended_followup_enabled:
                delay_val = policy.post_attended_followup_delay_value or 2
                delay_unit = policy.post_attended_followup_delay_unit or 'HOURS'
                multiplier = 60 if delay_unit == 'HOURS' else 1440 if delay_unit == 'DAYS' else 1
                due_at = timezone.now() + timedelta(minutes=delay_val * multiplier)

                idempotency_ref = f"TRIAL_ATTENDED_{trial.id}"
                task_exists = SalesFollowupTask.objects.using(alias).filter(external_reference=idempotency_ref).exists()
                if not task_exists:
                    agent = trial.lead.assigned_sales_user or actor_user
                    SalesFollowupTask.objects.using(alias).create(
                        lead=trial.lead,
                        task_type='TRIAL_FOLLOWUP',
                        priority='HIGH',
                        due_at=due_at,
                        status='PENDING',
                        assigned_to_user=agent,
                        created_by_user=actor_user,
                        external_reference=idempotency_ref,
                        outcome=f"Post-trial follow-up for attended session on {trial.scheduled_start.strftime('%Y-%m-%d')}",
                    )

            return trial

    @classmethod
    def mark_trial_no_show(
        cls,
        trial: TrialBooking,
        notes: Optional[str] = None,
        actor_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> TrialBooking:
        alias = db_alias or get_tenant_db_alias() or 'default'
        from .models_crm import CRMTrialReminderPolicy
        from .models_crm import SalesFollowupTask

        with transaction.atomic(using=alias):
            if trial.status in ['ATTENDED', 'NO_SHOW', 'CANCELLED', 'RESCHEDULED']:
                raise ValidationError(f"Cannot mark trial no-show from '{trial.status}' status.")

            old_status = trial.status
            trial.status = 'NO_SHOW'
            if notes:
                trial.notes = f"{trial.notes}\n{notes}".strip() if trial.notes else notes
            trial.save(using=alias, update_fields=['status', 'notes', 'updated_at'])

            TrialStatusHistory.objects.using(alias).create(
                trial_booking=trial,
                from_status=old_status,
                to_status='NO_SHOW',
                reason_code='SESSION_NO_SHOW',
                changed_by_user=actor_user,
            )

            cls.transition_lead_status(
                lead=trial.lead,
                new_status='NO_SHOW',
                reason_code='TRIAL_NO_SHOW',
                actor_user=actor_user,
                db_alias=alias,
            )

            record_business_audit(
                organization=trial.lead.organization,
                branch=trial.branch,
                actor_type='EMPLOYEE' if actor_user else 'SYSTEM',
                actor_user=actor_user,
                module='crm',
                action_code='CRM_TRIAL_NO_SHOW',
                entity_type='TrialBooking',
                entity_id=trial.id,
                event_description=f"Marked trial {trial.id} as NO_SHOW",
                db_alias=alias,
            )

            enqueue_outbox_event(
                organization=trial.lead.organization,
                event_type='CRM_TRIAL_NO_SHOW',
                aggregate_type='TrialBooking',
                aggregate_id=str(trial.id),
                payload={'trial_id': str(trial.id), 'lead_id': str(trial.lead_id)},
                db_alias=alias,
            )

            # Idempotent no-show recovery follow-up task
            policy = CRMTrialReminderPolicy.objects.using(alias).filter(organization=trial.lead.organization).first()
            if policy and policy.no_show_followup_enabled:
                delay_val = policy.no_show_followup_delay_value or 30
                delay_unit = policy.no_show_followup_delay_unit or 'MINUTES'
                multiplier = 60 if delay_unit == 'HOURS' else 1440 if delay_unit == 'DAYS' else 1
                due_at = timezone.now() + timedelta(minutes=delay_val * multiplier)

                idempotency_ref = f"TRIAL_NOSHOW_{trial.id}"
                task_exists = SalesFollowupTask.objects.using(alias).filter(external_reference=idempotency_ref).exists()
                if not task_exists:
                    agent = trial.lead.assigned_sales_user or actor_user
                    SalesFollowupTask.objects.using(alias).create(
                        lead=trial.lead,
                        task_type='NO_SHOW_RECOVERY',
                        priority='URGENT',
                        due_at=due_at,
                        status='PENDING',
                        assigned_to_user=agent,
                        created_by_user=actor_user,
                        external_reference=idempotency_ref,
                        outcome=f"Recovery follow-up for no-show on {trial.scheduled_start.strftime('%Y-%m-%d')}",
                    )

            return trial

    @classmethod
    def calculate_trial_reminder_schedule(
        cls,
        trial: TrialBooking,
        db_alias: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        alias = db_alias or get_tenant_db_alias() or 'default'
        from .models_crm import CRMTrialReminderPolicy

        policy = CRMTrialReminderPolicy.objects.using(alias).filter(organization=trial.lead.organization).first()
        schedule = []
        now = timezone.now()

        # Immediate confirmation point
        schedule.append({
            'id': f"rem-{trial.id}-immediate",
            'type': 'IMMEDIATE_CONFIRMATION',
            'offset_value': 0,
            'offset_unit': 'MINUTES',
            'channel': getattr(policy, 'default_channel', 'WHATSAPP') or 'WHATSAPP',
            'scheduled_at': (trial.created_at or now).isoformat(),
            'status': 'PAST',
        })

        if policy and policy.reminder_offsets:
            for idx, item in enumerate(policy.reminder_offsets):
                if isinstance(item, dict):
                    val = item.get('value')
                    unit = item.get('unit', 'HOURS')
                    ch = item.get('channel', 'WHATSAPP')
                else:
                    val = int(item)
                    unit = 'MINUTES'
                    ch = getattr(policy, 'default_channel', 'WHATSAPP') or 'WHATSAPP'

                if val is None:
                    continue

                mins = val * 60 if unit == 'HOURS' else val * 1440 if unit == 'DAYS' else val
                target_dt = trial.scheduled_start - timedelta(minutes=mins)
                is_past = now >= target_dt

                schedule.append({
                    'id': f"rem-{trial.id}-{idx}",
                    'type': 'REMINDER',
                    'offset_value': val,
                    'offset_unit': unit,
                    'channel': ch,
                    'scheduled_at': target_dt.isoformat(),
                    'status': 'PAST' if is_past else 'PENDING',
                })

        schedule.sort(key=lambda s: s['scheduled_at'])
        return schedule

    @classmethod
    def transition_trial_status(
        cls,
        trial: TrialBooking,
        new_status: str,
        reason_code: Optional[str] = None,
        actor_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> TrialBooking:
        alias = db_alias or get_tenant_db_alias() or 'default'

        if new_status == 'ATTENDED':
            return cls.mark_trial_attended(trial, notes=reason_code, actor_user=actor_user, db_alias=alias)
        elif new_status == 'NO_SHOW':
            return cls.mark_trial_no_show(trial, notes=reason_code, actor_user=actor_user, db_alias=alias)
        elif new_status == 'CANCELLED':
            return cls.cancel_trial(trial, reason=reason_code, actor_user=actor_user, db_alias=alias)

        with transaction.atomic(using=alias):
            old_status = trial.status
            if old_status == new_status:
                return trial

            trial.status = new_status
            trial.save(using=alias, update_fields=['status', 'updated_at'])

            TrialStatusHistory.objects.using(alias).create(
                trial_booking=trial,
                from_status=old_status,
                to_status=new_status,
                reason_code=reason_code,
                changed_by_user=actor_user,
            )

            lead_status_map = {
                'ATTENDED': 'TRIAL_ATTENDED',
                'NO_SHOW': 'NO_SHOW',
                'CONFIRMED': 'TRIAL_CONFIRMED',
            }
            if new_status in lead_status_map:
                cls.transition_lead_status(
                    lead=trial.lead,
                    new_status=lead_status_map[new_status],
                    reason_code=f"TRIAL_{new_status}",
                    actor_user=actor_user,
                    db_alias=alias,
                )

            return trial

    @classmethod
    def submit_intake_form(
        cls,
        intake_form: IntakeForm,
        answers: List[Dict[str, Any]],
        lead: Optional[Lead] = None,
        user_profile: Optional[UserProfile] = None,
        submitted_by_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> IntakeSubmission:
        alias = db_alias or get_tenant_db_alias() or 'default'

        with transaction.atomic(using=alias):
            submission = IntakeSubmission.objects.using(alias).create(
                intake_form=intake_form,
                lead=lead,
                user_profile=user_profile,
                submitted_by_user=submitted_by_user,
            )

            for item in answers:
                question_id = item.get('question_id')
                q = IntakeQuestion.objects.using(alias).get(id=question_id)
                IntakeAnswer.objects.using(alias).create(
                    submission=submission,
                    question=q,
                    text_value=item.get('text_value'),
                    numeric_value=item.get('numeric_value'),
                    boolean_value=item.get('boolean_value'),
                    date_value=item.get('date_value'),
                    json_value=item.get('json_value', {}),
                )

            return submission

    @classmethod
    def record_lead_activity(
        cls,
        lead: Lead,
        activity_type: str,
        outcome: Optional[str] = None,
        notes: Optional[str] = None,
        performed_by_user: Optional[TenantUser] = None,
        activity_at: Optional[datetime] = None,
        duration_minutes: Optional[int] = None,
        external_reference: Optional[str] = None,
        actor_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> LeadActivity:
        alias = db_alias or get_tenant_db_alias() or 'default'

        with transaction.atomic(using=alias):
            act_time = activity_at or timezone.now()
            performer = performed_by_user or actor_user
            act_notes = notes
            if duration_minutes:
                dur_text = f"Duration: {duration_minutes} mins"
                act_notes = f"{act_notes} ({dur_text})" if act_notes else dur_text

            activity = LeadActivity.objects.using(alias).create(
                lead=lead,
                activity_type=activity_type,
                outcome=outcome,
                notes=act_notes,
                performed_by_user=performer,
                activity_at=act_time,
                external_reference=external_reference,
            )

            record_business_audit(
                organization=lead.organization,
                branch=lead.branch,
                actor_type='EMPLOYEE' if actor_user else 'SYSTEM',
                actor_user=actor_user,
                module='crm',
                action_code='CRM_LEAD_ACTIVITY_LOGGED',
                entity_type='LeadActivity',
                entity_id=activity.id,
                event_description=f"Recorded {activity_type} activity on {lead.first_name} {lead.last_name}",
                after_data={
                    'activity_id': str(activity.id),
                    'lead_id': str(lead.id),
                    'activity_type': activity_type,
                    'outcome': outcome,
                },
                db_alias=alias,
            )

            return activity

    @classmethod
    def create_followup_task(
        cls,
        lead: Lead,
        task_type: str = 'CALL',
        due_at: Optional[datetime] = None,
        priority: str = 'NORMAL',
        assigned_to_user: Optional[TenantUser] = None,
        outcome: Optional[str] = None,
        created_by_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> SalesFollowupTask:
        alias = db_alias or get_tenant_db_alias() or 'default'
        assignee = assigned_to_user or lead.assigned_sales_user or created_by_user
        if not assignee:
            raise ValueError("An assigned salesperson or creator is required to create a follow-up task.")
        if priority == 'MEDIUM':
            priority = 'NORMAL'
        effective_due = due_at or (timezone.now() + timedelta(days=1))

        with transaction.atomic(using=alias):
            task = SalesFollowupTask.objects.using(alias).create(
                lead=lead,
                assigned_to_user=assignee,
                task_type=task_type,
                due_at=effective_due,
                priority=priority,
                outcome=outcome,
                status='PENDING',
                created_by_user=created_by_user or assignee,
            )

            record_business_audit(
                organization=lead.organization,
                branch=lead.branch,
                actor_type='EMPLOYEE' if created_by_user else 'SYSTEM',
                actor_user=created_by_user,
                module='crm',
                action_code='CRM_FOLLOWUP_TASK_CREATED',
                entity_type='SalesFollowupTask',
                entity_id=task.id,
                event_description=f"Created {task_type} follow-up task for {lead.first_name} {lead.last_name}",
                after_data={
                    'task_id': str(task.id),
                    'lead_id': str(lead.id),
                    'task_type': task_type,
                    'due_at': due_at.isoformat() if hasattr(due_at, 'isoformat') else str(due_at),
                    'assigned_to_user_id': str(assigned_to_user.id),
                },
                db_alias=alias,
            )

            return task

    @classmethod
    def complete_followup_task(
        cls,
        task: SalesFollowupTask,
        outcome: Optional[str] = None,
        next_followup_at: Optional[datetime] = None,
        log_activity: bool = False,
        actor_user: Optional[TenantUser] = None,
        completed_by_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> SalesFollowupTask:
        alias = db_alias or get_tenant_db_alias() or 'default'
        effective_actor = completed_by_user or actor_user

        with transaction.atomic(using=alias):
            task.status = 'COMPLETED'
            if outcome:
                task.outcome = outcome
            if next_followup_at:
                task.next_followup_at = next_followup_at
            task.save(using=alias, update_fields=['status', 'outcome', 'next_followup_at', 'updated_at'])

            if log_activity and task.lead:
                act_type = task.task_type if task.task_type in dict(LeadActivity.ACTIVITY_TYPES) else 'FOLLOW_UP'
                cls.record_lead_activity(
                    lead=task.lead,
                    activity_type=act_type,
                    outcome=outcome or f"Completed {task.task_type.replace('_', ' ').lower()}",
                    notes=f"Completed follow-up task: {task.task_type}",
                    performed_by_user=effective_actor or task.assigned_to_user,
                    activity_at=timezone.now(),
                    actor_user=effective_actor,
                    db_alias=alias,
                )

            record_business_audit(
                organization=task.lead.organization if task.lead else None,
                branch=task.lead.branch if task.lead else None,
                actor_type='EMPLOYEE' if effective_actor else 'SYSTEM',
                actor_user=effective_actor,
                module='crm',
                action_code='CRM_FOLLOWUP_TASK_COMPLETED',
                entity_type='SalesFollowupTask',
                entity_id=task.id,
                event_description=f"Completed follow-up task {task.task_type}",
                after_data={'task_id': str(task.id), 'status': 'COMPLETED', 'outcome': outcome},
                db_alias=alias,
            )

            return task

    @classmethod
    def reschedule_followup_task(
        cls,
        task: SalesFollowupTask,
        new_due_at: datetime,
        reason: Optional[str] = None,
        actor_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> SalesFollowupTask:
        alias = db_alias or get_tenant_db_alias() or 'default'

        with transaction.atomic(using=alias):
            old_due = task.due_at
            task.due_at = new_due_at
            if task.status in ('COMPLETED', 'CANCELLED'):
                task.status = 'PENDING'
            if reason:
                existing_outcome = f"{task.outcome}\n" if task.outcome else ""
                task.outcome = f"{existing_outcome}Rescheduled from {old_due.strftime('%Y-%m-%d %H:%M')}: {reason}".strip()
            task.save(using=alias, update_fields=['due_at', 'status', 'outcome', 'updated_at'])

            record_business_audit(
                organization=task.lead.organization if task.lead else None,
                branch=task.lead.branch if task.lead else None,
                actor_type='EMPLOYEE' if actor_user else 'SYSTEM',
                actor_user=actor_user,
                module='crm',
                action_code='CRM_FOLLOWUP_TASK_RESCHEDULED',
                entity_type='SalesFollowupTask',
                entity_id=task.id,
                event_description=f"Rescheduled follow-up task {task.task_type} to {new_due_at}",
                after_data={
                    'task_id': str(task.id),
                    'new_due_at': new_due_at.isoformat() if hasattr(new_due_at, 'isoformat') else str(new_due_at),
                    'reason': reason,
                },
                db_alias=alias,
            )

            return task

    @classmethod
    def cancel_followup_task(
        cls,
        task: SalesFollowupTask,
        reason: Optional[str] = None,
        actor_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> SalesFollowupTask:
        alias = db_alias or get_tenant_db_alias() or 'default'

        with transaction.atomic(using=alias):
            task.status = 'CANCELLED'
            if reason:
                existing_outcome = f"{task.outcome}\n" if task.outcome else ""
                task.outcome = f"{existing_outcome}Cancelled: {reason}".strip()
            task.save(using=alias, update_fields=['status', 'outcome', 'updated_at'])

            record_business_audit(
                organization=task.lead.organization if task.lead else None,
                branch=task.lead.branch if task.lead else None,
                actor_type='EMPLOYEE' if actor_user else 'SYSTEM',
                actor_user=actor_user,
                module='crm',
                action_code='CRM_FOLLOWUP_TASK_CANCELLED',
                entity_type='SalesFollowupTask',
                entity_id=task.id,
                event_description=f"Cancelled follow-up task {task.task_type}",
                after_data={'task_id': str(task.id), 'status': 'CANCELLED', 'reason': reason},
                db_alias=alias,
            )

            return task


# ===========================================================================
# CRM PHASE 8 — LEAD → MEMBER CONVERSION SERVICE
# ===========================================================================

class IdentityConflictError(Exception):
    """Raised when phone and email resolve to two different existing members."""
    pass


class LeadAlreadyConvertedError(Exception):
    """Raised when a lead is already in CONVERTED terminal state."""
    pass


def check_payment_recording_permission(user, branch_id=None, request=None, db_alias=None) -> Tuple[bool, str]:
    """
    Separates conversion preparation (crm.leads.convert) from payment recording authority.
    Validates whether the user holds the canonical permission to record offline payments:
    - finance.payments.create (canonical payment collection permission)
    - core.settings.edit (commerce settings compatibility)
    - Superuser / platform admin override
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return False, "User is not authenticated."
    if getattr(user, 'is_superuser', False):
        return True, "Platform admin override"

    from .rbac_engine import RBACAuthorizationEngine
    alias = db_alias or get_tenant_db_alias() or 'default'

    # 1. Primary canonical: finance.payments.create
    allowed_fin, reason_fin, _ = RBACAuthorizationEngine.evaluate(
        user=user,
        required_permission='finance.payments.create',
        branch_id=branch_id,
        request=request,
    )
    if allowed_fin:
        return True, reason_fin

    # 2. Compatibility fallback: core.settings.edit
    allowed_set, reason_set, _ = RBACAuthorizationEngine.evaluate(
        user=user,
        required_permission='core.settings.edit',
        branch_id=branch_id,
        request=request,
    )
    if allowed_set:
        return True, reason_set

    return False, "User lacks payment confirmation authority (finance.payments.create)."


class LeadConversionService:
    """
    Orchestrates the complete commercial conversion of a CRM Lead into an active Member.

    Two-phase architecture:
      Phase 1 — Quote (read-only): validate catalog + compute pricing preview.
      Phase 2 — Commit: order creation, payment recording, membership activation,
                        lead status transition, idempotency record.

    All writes in Phase 2 are protected by execute_idempotent_operation().
    Payment is recorded as an offline/manual SUCCESS transaction (no external gateway).
    Membership is activated only after the order reaches PAID status.
    """

    # ------------------------------------------------------------------
    # IDENTITY RESOLUTION
    # ------------------------------------------------------------------

    @staticmethod
    def resolve_or_create_member_identity(
        lead: Lead,
        branch,
        actor_user=None,
        db_alias: Optional[str] = None,
    ):
        """
        Resolve (or create) a TenantUser + UserProfile for the lead.

        Conflict resolution rules:
          A. phone + email → same user → reuse
          B. phone match only → reuse
          C. email match only → reuse
          D. phone → User A, email → User B → raise IdentityConflictError
          E. no match → create TenantUser(status=INVITED, is_login_allowed=True,
                          password_hash='') + UserProfile

        Returns: (user_profile, created_flag)
        """
        from .models_users import TenantUser, UserBranch
        from .models_workforce import UserProfile

        alias = db_alias or get_tenant_db_alias() or 'default'
        org = lead.organization

        phone = lead.phone_normalized
        email = lead.email_normalized

        phone_user = None
        email_user = None

        if phone:
            phone_user = TenantUser.objects.using(alias).filter(
                organization=org,
                phone=phone,
            ).select_related('profile').first()

        if email:
            email_user = TenantUser.objects.using(alias).filter(
                organization=org,
                email__iexact=email,
            ).select_related('profile').first()

        # Conflict detection
        if phone_user and email_user and phone_user.pk != email_user.pk:
            raise IdentityConflictError(
                f"Phone {phone} belongs to user {phone_user.id} but email {email} belongs to "
                f"user {email_user.id}. Cannot auto-merge. Manual identity resolution required."
            )

        # Reuse existing
        existing_user = phone_user or email_user
        if existing_user:
            try:
                profile = existing_user.profile
            except UserProfile.DoesNotExist:
                profile = UserProfile.objects.using(alias).create(
                    user=existing_user,
                    first_name_snapshot=existing_user.first_name,
                    last_name_snapshot=existing_user.last_name,
                    preferred_branch=branch,
                    joining_date=timezone.now().date(),
                    member_type='MEMBER',
                    member_status='ACTIVE',
                    acquisition_source='CRM_CONVERSION',
                )
            return profile, False

        # Create new identity: INVITED status, no password stored
        # Build a safe unique email — lead may have no email (use phone-based placeholder)
        if email:
            user_email = email
        elif phone:
            # Derive a placeholder email for identity storage; not for login
            safe_phone = phone.replace('+', '').replace(' ', '')
            user_email = f"member.{safe_phone}@{org.id.hex[:8]}.internal"
        else:
            raise ValidationError("Lead must have either a phone or email to create a member account.")

        with transaction.atomic(using=alias):
            new_user = TenantUser(
                organization=org,
                email=user_email,
                phone=phone or '',
                first_name=lead.first_name,
                last_name=lead.last_name,
                display_name=f"{lead.first_name} {lead.last_name}".strip(),
                user_type='MEMBER',
                status='INVITED',
                is_login_allowed=True,
                home_branch=branch,
            )
            # password_hash set to unusable string — invite/activation flow sets password later
            new_user.password_hash = '!unusable'
            new_user.save(using=alias)

            # Assign to branch
            UserBranch.objects.using(alias).create(
                user=new_user,
                branch=branch,
                scope_type='HOME',
                is_primary=True,
                status='ACTIVE',
                is_active=True,
            )

            profile = UserProfile.objects.using(alias).create(
                user=new_user,
                first_name_snapshot=lead.first_name,
                last_name_snapshot=lead.last_name,
                preferred_branch=branch,
                joining_date=timezone.now().date(),
                member_type='MEMBER',
                member_status='ACTIVE',
                acquisition_source='CRM_CONVERSION',
            )

        return profile, True

    # ------------------------------------------------------------------
    # CATALOG VALIDATION HELPERS
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_purchasable_version(package_version_id: str, branch, db_alias: str):
        """
        Validate that a PackageVersion is purchasable at the given branch.

        Purchasable criteria:
          - PackageVersion.status == 'ACTIVE'
          - Package.status == 'ACTIVE'
          - Program.status == 'ACTIVE'
          - PackageBranchAvailability(package, branch).status == 'ENABLED' (if any row exists)
          - At least one active PackagePrice effective today

        Returns: (package_version, package_price, program)
        Raises: ValidationError if any condition fails.
        """
        from .models_catalog import PackageVersion, PackageBranchAvailability, PackagePrice

        now = timezone.now()

        try:
            pv = PackageVersion.objects.using(db_alias).select_related(
                'package', 'package__program'
            ).get(id=package_version_id)
        except PackageVersion.DoesNotExist:
            raise ValidationError(f"PackageVersion {package_version_id} not found.")

        if pv.status != 'ACTIVE':
            raise ValidationError(
                f"PackageVersion '{pv.name_snapshot}' (v{pv.version_number}) is {pv.status} and cannot be purchased."
            )
        if pv.effective_from and now < pv.effective_from:
            raise ValidationError(f"PackageVersion '{pv.name_snapshot}' is not yet effective.")
        if pv.effective_until and now > pv.effective_until:
            raise ValidationError(f"PackageVersion '{pv.name_snapshot}' has expired.")

        pkg = pv.package
        if pkg.status not in ('ACTIVE',):
            raise ValidationError(f"Package '{pkg.name}' is {pkg.status} and not available for purchase.")

        program = pkg.program
        if program.status not in ('ACTIVE',):
            raise ValidationError(f"Program '{program.name}' is {program.status} and not available for purchase.")

        # Branch availability check
        all_branch_avail = PackageBranchAvailability.objects.using(db_alias).filter(package=pkg)
        avail_qs = all_branch_avail.filter(branch=branch)
        if avail_qs.exists():
            avail = avail_qs.first()
            if avail.status != 'ENABLED':
                raise ValidationError(
                    f"Package '{pkg.name}' is not available for sale at branch '{branch.name}'."
                )
            # Check date bounds
            if avail.available_from and now < avail.available_from:
                raise ValidationError(f"Package '{pkg.name}' is not yet available at '{branch.name}'.")
            if avail.available_until and now > avail.available_until:
                raise ValidationError(f"Package '{pkg.name}' availability has expired at '{branch.name}'.")
        elif all_branch_avail.exists():
            raise ValidationError(
                f"Package '{pkg.name}' is not available for sale at branch '{branch.name}'."
            )

        # Price resolution: prefer branch-specific, then global
        price_qs = PackagePrice.objects.using(db_alias).filter(
            package_version=pv,
            status='ACTIVE',
            effective_from__lte=now,
        ).filter(
            models_Q(effective_until__isnull=True) | models_Q(effective_until__gte=now)
        ).order_by(
            models_F('branch').desc(nulls_last=True),  # branch-specific first
        )

        # Try branch-specific price first, then fall back to null-branch (global)
        branch_price = price_qs.filter(branch=branch).first()
        global_price = price_qs.filter(branch__isnull=True).first()
        package_price = branch_price or global_price

        if not package_price:
            raise ValidationError(
                f"No active price found for '{pv.name_snapshot}' at '{branch.name}'. "
                "Please configure pricing before attempting conversion."
            )

        return pv, package_price, program

    # ------------------------------------------------------------------
    # ELIGIBILITY CHECK
    # ------------------------------------------------------------------

    @classmethod
    def get_conversion_eligibility(
        cls,
        lead: Lead,
        db_alias: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Returns backend-driven eligibility for the conversion wizard button.
        Frontend must not decide eligibility purely on current_status.
        """
        alias = db_alias or get_tenant_db_alias() or 'default'

        if lead.current_status == 'CONVERTED':
            return {
                'eligible': False,
                'reason': 'already_converted',
                'message': 'This lead has already been converted to a member.',
                'existing_conversions': list(
                    LeadConversion.objects.using(alias).filter(lead=lead).values(
                        'id', 'converted_at', 'order_id', 'membership_id'
                    )
                ),
            }

        if not lead.branch_id:
            return {
                'eligible': False,
                'reason': 'no_branch',
                'message': 'Lead must be assigned to a branch before conversion.',
            }

        return {
            'eligible': True,
            'reason': 'eligible',
            'message': 'Lead is eligible for conversion.',
        }

    # ------------------------------------------------------------------
    # QUOTE (READ-ONLY)
    # ------------------------------------------------------------------

    @classmethod
    def get_conversion_quote(
        cls,
        lead: Lead,
        package_version_id: str,
        branch_id: str,
        coupon_code: Optional[str] = None,
        db_alias: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Pure read-only pricing preview. No DB writes.
        Validates catalog, resolves price, previews discount.

        Does NOT:
        - Create coupon redemption
        - Write any ledger entry
        - Create an order
        """
        from decimal import Decimal
        from .models_org import Branch

        alias = db_alias or get_tenant_db_alias() or 'default'

        try:
            branch = Branch.objects.using(alias).get(id=branch_id)
        except Branch.DoesNotExist:
            raise ValidationError(f"Branch {branch_id} not found.")

        pv, package_price, program = cls._resolve_purchasable_version(
            package_version_id, branch, alias
        )

        base_price = package_price.base_price
        tax_pct = package_price.tax_percent
        prices_include_tax = package_price.prices_include_tax

        if prices_include_tax:
            subtotal = base_price
            tax_amount = Decimal('0.00')
        else:
            subtotal = base_price
            tax_amount = (subtotal * tax_pct / Decimal('100')).quantize(Decimal('0.01'))

        discount_amount = Decimal('0.00')
        coupon_preview = None

        if coupon_code and coupon_code.strip():
            # Preview only — no redemption write
            from .services_discounts import DiscountCouponEngineService
            from .models_workforce import UserProfile

            # Try to find existing profile for coupon user-limit checks; OK if absent
            user_profile = None
            try:
                from .models_users import TenantUser
                matching_user = TenantUser.objects.using(alias).filter(
                    organization=lead.organization
                ).filter(
                    models_Q(phone=lead.phone_normalized) if lead.phone_normalized else models_Q()
                ).first()
                if matching_user:
                    user_profile = UserProfile.objects.using(alias).filter(user=matching_user).first()
            except Exception:
                pass

            val = DiscountCouponEngineService.validate_coupon(
                code_str=coupon_code,
                user_profile=user_profile,
                order_subtotal=subtotal,
                branch=branch,
                package=pv.package,
                db_alias=alias,
            )
            coupon_preview = val
            if val['is_valid']:
                discount_amount = Decimal(val['discount_amount'])

        net_after_discount = max(Decimal('0.00'), subtotal - discount_amount)
        if not prices_include_tax:
            tax_on_discounted = (net_after_discount * tax_pct / Decimal('100')).quantize(Decimal('0.01'))
        else:
            tax_on_discounted = Decimal('0.00')

        total_payable = net_after_discount + tax_on_discounted

        from .models_catalog import PackageEntitlementDefinition
        entitlements = list(
            PackageEntitlementDefinition.objects.using(alias).filter(
                package_version=pv, status='ACTIVE'
            ).values('entitlement_type', 'allocated_units', 'is_unlimited')
        )

        from .models_commerce import PaymentTransaction
        from .models_infra import Integration

        all_provider_choices = [c[0] for c in PaymentTransaction.PROVIDER_CHOICES]
        offline_providers = ['CASH', 'BANK_TRANSFER', 'OTHER']
        gateway_providers = [c for c in all_provider_choices if c not in offline_providers]

        available_providers = [p for p in offline_providers if p in all_provider_choices]
        try:
            active_gateways = set(
                Integration.objects.using(alias)
                .filter(integration_type='PAYMENT', status='ACTIVE')
                .values_list('provider', flat=True)
            )
            active_gateways_upper = {g.strip().upper() for g in active_gateways if g}
            for gw in gateway_providers:
                if gw in active_gateways_upper:
                    available_providers.append(gw)
        except Exception:
            pass

        return {
            'program': {'id': str(program.id), 'name': program.name, 'code': program.code},
            'package': {'id': str(pv.package.id), 'name': pv.package.name, 'code': pv.package.code},
            'package_version': {
                'id': str(pv.id),
                'version_number': pv.version_number,
                'name': pv.name_snapshot,
                'duration_value': pv.duration_value,
                'duration_unit': pv.duration_unit,
                'total_days': pv.total_days,
                'status': pv.status,
            },
            'package_price': {
                'id': str(package_price.id),
                'base_price': str(base_price),
                'tax_percent': str(tax_pct),
                'prices_include_tax': prices_include_tax,
                'currency': package_price.currency,
                'effective_from': package_price.effective_from.isoformat(),
                'effective_until': package_price.effective_until.isoformat() if package_price.effective_until else None,
            },
            'pricing': {
                'subtotal': str(subtotal),
                'discount_amount': str(discount_amount),
                'tax_amount': str(tax_on_discounted),
                'total_payable': str(total_payable),
                'currency': package_price.currency,
            },
            'coupon': coupon_preview,
            'entitlements': entitlements,
            'payment_providers': available_providers,
        }

    # ------------------------------------------------------------------
    # EXECUTE CONVERSION (COMMIT)
    # ------------------------------------------------------------------

    @classmethod
    def execute_conversion(
        cls,
        lead: Lead,
        package_version_id: str,
        branch_id: str,
        payment_provider: str,
        payment_amount: 'Decimal',
        payment_method: Optional[str] = None,
        coupon_code: Optional[str] = None,
        start_date=None,
        actor_user=None,
        idempotency_key: Optional[str] = None,
        db_alias: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Execute the full Lead → Member conversion.

        Two-phase approach:
          Phase 1 — Validate + prepare (outside long transaction):
            - Catalog validation
            - Identity resolution
            - Order creation (leaves order in PENDING_PAYMENT)

          Phase 2 — Atomic finalization (focused transaction):
            - select_for_update on Lead + Order
            - Payment recording
            - Membership activation (MembershipLifecycleService)
            - LeadConversion record
            - Lead status → CONVERTED
            - Audit + Outbox

        Protected by IdempotencyRecord keyed on
        (org, 'LEAD_CONVERSION', idempotency_key) to survive double-click,
        retries, and worker restarts.

        Payment is offline/manual (PaymentTransaction.provider must be a valid
        PROVIDER_CHOICES value). The backend records the operator-confirmed
        payment as SUCCESS — no external gateway is contacted.
        """
        from decimal import Decimal as _D
        from datetime import date as _date
        from .models_org import Branch
        from .models_catalog import PackageVersion
        from .models_commerce import Order, PaymentTransaction, MemberInvoice
        from .models_memberships import Membership
        from .services_commerce import CommerceService
        from .services_memberships import MembershipLifecycleService
        from .services_reliability import execute_idempotent_operation, IdempotencyConflictError

        alias = db_alias or get_tenant_db_alias() or 'default'
        org = lead.organization

        # ── Validate payment provider (do this before idempotency) ────
        valid_providers = [c[0] for c in PaymentTransaction.PROVIDER_CHOICES]
        if payment_provider not in valid_providers:
            raise ValidationError(
                f"Invalid payment provider '{payment_provider}'. "
                f"Valid choices: {valid_providers}"
            )

        amount = _D(str(payment_amount))
        if amount <= _D('0.00'):
            raise ValidationError("Payment amount must be greater than zero.")

        # ── Payment recording authority enforcement ────────────────────
        if actor_user and not getattr(actor_user, 'is_superuser', False):
            has_pay_perm, pay_reason = check_payment_recording_permission(
                actor_user, branch_id=str(branch_id), db_alias=alias
            )
            if not has_pay_perm:
                from rest_framework.exceptions import PermissionDenied
                raise PermissionDenied(
                    "Permission denied: payment confirmation permission (finance.payments.create) required to record payment."
                )

        # ── Stable idempotency key for this conversion attempt ──────────
        attempt_key = idempotency_key or f"lead-convert-{lead.id}-{uuid.uuid4().hex[:12]}"
        idem_key = f"lead_conversion:{lead.id}:{attempt_key}"

        def _do_conversion():
            # ── Guard: already converted (terminal state) ───────────────
            if lead.current_status == 'CONVERTED':
                raise LeadAlreadyConvertedError(
                    f"Lead {lead.id} is already CONVERTED. "
                    "If this is a renewal, create a new order via the membership service."
                )

            # ── Phase 1: Catalog + Identity ─────────────────────────────
            branch = Branch.objects.using(alias).select_for_update().get(id=branch_id)

            pv, package_price, program = cls._resolve_purchasable_version(
                package_version_id, branch, alias
            )
            pkg = pv.package

            # ── Identity resolution ─────────────────────────────────────
            user_profile, identity_created = cls.resolve_or_create_member_identity(
                lead=lead,
                branch=branch,
                actor_user=actor_user,
                db_alias=alias,
            )

            # ── Build order items_data ──────────────────────────────────
            unit_price = package_price.base_price
            tax_pct = package_price.tax_percent if not package_price.prices_include_tax else _D('0.000')

            items_data = [{
                'item_type': 'PACKAGE',
                'package_id': str(pkg.id),
                'package_version_id': str(pv.id),
                'package_price_id': str(package_price.id),
                'item_name_snapshot': f"{program.name} — {pkg.name} (v{pv.version_number})",
                'quantity': '1.00',
                'unit_price': str(unit_price),
                'tax_percent': str(tax_pct),
                'discount_amount': '0.00',
            }]

            # ── Check for existing unfinalized order from interrupted conversion attempt ──
            existing_candidates = Order.objects.using(alias).filter(
                lead=lead,
                items__package_version_id=str(pv.id),
                status__in=['PENDING_PAYMENT', 'PAID'],
            ).order_by('-created_at')

            existing_order = None
            for cand in existing_candidates:
                if cand.status == 'PENDING_PAYMENT':
                    existing_order = cand
                    break
                elif cand.status == 'PAID':
                    # Only reuse paid order if membership was NOT yet activated (recovery scenario)
                    if not Membership.objects.using(alias).filter(source_order=cand).exists():
                        existing_order = cand
                        break

            if existing_order:
                order = existing_order
            else:
                order = CommerceService.create_order(
                    branch=branch,
                    items_data=items_data,
                    user_profile=user_profile,
                    lead=lead,
                    sold_by=actor_user,
                    order_type='NEW_MEMBERSHIP',
                    source='SALES',
                    notes=f"CRM Lead conversion: {lead.first_name} {lead.last_name}",
                    created_by=actor_user,
                    db_alias=alias,
                )

            order_item = order.items.using(alias).filter(item_type='PACKAGE').first()

            # ── Optional coupon redemption BEFORE payment (if order pending) ──
            if order.status == 'PENDING_PAYMENT' and coupon_code and coupon_code.strip():
                from .services_discounts import DiscountCouponEngineService
                try:
                    DiscountCouponEngineService.redeem_coupon(
                        order=order,
                        code_str=coupon_code,
                        user_profile=user_profile,
                        created_by_user=actor_user,
                        db_alias=alias,
                    )
                    order.refresh_from_db(using=alias)
                except ValidationError as e:
                    raise ValidationError(f"Coupon error: {e.message if hasattr(e, 'message') else str(e)}")

            # Validate submitted amount vs authoritative order total
            if amount < order.total_amount:
                raise ValidationError(
                    f"Payment amount {amount} is less than required total {order.total_amount}. "
                    "Full payment required before membership activation."
                )

            # ── Idempotency key for the payment txn ────────────────────
            payment_idem_key = f"payment-{idem_key}"

            # ── Phase 2: Atomic finalization ────────────────────────────
            with transaction.atomic(using=alias):
                # Lock the order and lead
                order = Order.objects.using(alias).select_for_update().get(id=order.id)
                lead_locked = Lead.objects.using(alias).select_for_update().get(id=lead.id)

                if lead_locked.current_status == 'CONVERTED':
                    raise LeadAlreadyConvertedError("Lead was converted concurrently.")

                if order.status == 'PAID':
                    # Post-payment recovery: payment was already completed, retrieve records
                    txn = PaymentTransaction.objects.using(alias).filter(order=order, status='SUCCESS').first()
                    invoice = MemberInvoice.objects.using(alias).filter(order=order).first()
                else:
                    # Record payment — records SUCCESS, marks order PAID if fully covered
                    txn, invoice = CommerceService.record_payment(
                        order_id=str(order.id),
                        amount=amount,
                        provider=payment_provider,
                        payment_method=payment_method or payment_provider,
                        idempotency_key=payment_idem_key,
                        actor=actor_user,
                        db_alias=alias,
                    )

                # Verify order reached PAID before activating membership
                order.refresh_from_db(using=alias)
                if order.status != 'PAID':
                    raise ValidationError(
                        f"Order is {order.status} after payment of {amount}. "
                        f"Order total is {order.total_amount}. Full payment required."
                    )

                # Activate membership
                if isinstance(start_date, str) and start_date:
                    from datetime import date as _d
                    start = _d.fromisoformat(start_date)
                elif isinstance(start_date, date):
                    start = start_date
                else:
                    start = timezone.now().date()

                membership = MembershipLifecycleService.activate_membership_from_order(
                    order=order,
                    order_item=order_item,
                    start_date=start,
                    db_alias=alias,
                    created_by_user=actor_user,
                )

                # Create LeadConversion record
                conversion = LeadConversion.objects.using(alias).create(
                    lead=lead_locked,
                    user_profile=user_profile,
                    converted_at=timezone.now(),
                    converted_by_user=actor_user,
                    order_id=order.id,
                    membership_id=membership.id,
                    package_id=pkg.id,
                    package_version_id=pv.id,
                    conversion_source='CRM_WIZARD',
                )

                # Transition Lead to CONVERTED (terminal)
                CRMLeadService.transition_lead_status(
                    lead=lead_locked,
                    new_status='CONVERTED',
                    reason_code='LEAD_CONVERTED',
                    reason_text=f"Converted via membership purchase. Order: {order.order_number}",
                    actor_user=actor_user,
                    db_alias=alias,
                )

                # Audit
                record_business_audit(
                    organization=org,
                    branch=branch,
                    actor_type='EMPLOYEE' if actor_user else 'SYSTEM',
                    actor_user=actor_user,
                    module='crm',
                    action_code='LEAD_CONVERTED',
                    entity_type='LeadConversion',
                    entity_id=conversion.id,
                    event_description=(
                        f"Lead {lead.first_name} {lead.last_name} converted. "
                        f"Order: {order.order_number}. Membership: {membership.membership_number}"
                    ),
                    after_data={
                        'lead_id': str(lead.id),
                        'order_id': str(order.id),
                        'membership_id': str(membership.id),
                        'conversion_id': str(conversion.id),
                    },
                    db_alias=alias,
                )

                enqueue_outbox_event(
                    organization=org,
                    event_type='crm.lead.converted',
                    aggregate_type='LeadConversion',
                    aggregate_id=conversion.id,
                    payload={
                        'lead_id': str(lead.id),
                        'conversion_id': str(conversion.id),
                        'order_id': str(order.id),
                        'order_number': order.order_number,
                        'membership_id': str(membership.id),
                        'membership_number': membership.membership_number,
                        'user_profile_id': str(user_profile.id),
                        'package_id': str(pkg.id),
                        'package_version_id': str(pv.id),
                        'identity_created': identity_created,
                    },
                    db_alias=alias,
                )

            return {
                'conversion_id': str(conversion.id),
                'lead_id': str(lead.id),
                'lead_status': 'CONVERTED',
                'order_id': str(order.id),
                'order_number': order.order_number,
                'order_status': order.status,
                'invoice_id': str(invoice.id) if invoice else None,
                'invoice_number': invoice.invoice_number if invoice else None,
                'membership_id': str(membership.id),
                'membership_number': membership.membership_number,
                'user_profile_id': str(user_profile.id),
                'identity_created': identity_created,
                'converted_at': conversion.converted_at.isoformat(),
            }

        # ── Wrap with idempotency guard ─────────────────────────────────
        result = execute_idempotent_operation(
            organization=org,
            operation_type='LEAD_CONVERSION',
            idempotency_key=idem_key,
            operation_func=_do_conversion,
            request_data={
                'lead_id': str(lead.id),
                'package_version_id': str(package_version_id),
                'branch_id': str(branch_id),
                'payment_provider': str(payment_provider),
                'payment_amount': str(amount),
            },
            actor_user=actor_user,
            ttl_seconds=86400,
            db_alias=alias,
        )

        return result
