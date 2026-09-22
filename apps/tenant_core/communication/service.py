"""
apps/tenant_core/communication/service.py — Core Omnichannel Communication Service.

Provides authoritative dispatch, template rendering, consent verification, out-of-order webhook
protection, durable reminder evaluation, and inbound message intake with Lead resolution.
"""

import logging
import uuid
import re
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, timedelta
from django.utils import timezone
from django.db import transaction, IntegrityError
from django.db.models import Q

from apps.tenant_core.models_org import Organization
from apps.tenant_core.models_crm import Lead, TrialBooking, SalesFollowupTask
from apps.tenant_core.models_govern import NotificationTemplate
from apps.tenant_core.models_communication import CommunicationMessage, CommunicationStatusEvent
from apps.tenant_core.models_users import TenantUser
from .registry import CommunicationProviderRegistry
from .adapters.base import ProviderStatusEvent, ProviderInboundMessage

logger = logging.getLogger(__name__)

STATUS_ORDER = {
    'QUEUED': 10,
    'SUBMITTED': 20,
    'SENT': 30,
    'DELIVERED': 40,
    'READ': 50,
    'RECEIVED': 60,
    'REPLIED': 70,
    'FAILED': 99,
    'CANCELLED': 99,
}


class ConsentViolationError(Exception):
    """Raised when marketing communication is attempted without valid consent or under DNC."""
    pass


class CommunicationService:
    """
    Authoritative communication service for tenant CRM operations.
    """

    @classmethod
    def render_template(
        cls,
        template: NotificationTemplate,
        context: Dict[str, Any],
    ) -> Tuple[str, str]:
        """
        Renders template subject and body using {{variable}} placeholders.
        """
        body = template.body or ''
        subject = template.subject or ''
        for key, val in context.items():
            placeholder = f"{{{{{key}}}}}"
            str_val = str(val) if val is not None else ''
            body = body.replace(placeholder, str_val)
            subject = subject.replace(placeholder, str_val)
        return subject, body

    @classmethod
    def verify_consent(cls, lead: Optional[Lead], channel: str, purpose: str) -> None:
        """
        Enforces privacy & consent rules.
        Transactional messages bypass marketing opt-ins.
        Marketing messages strictly require channel opt-in and respect DNC.
        """
        if purpose != 'MARKETING' or not lead:
            return

        if getattr(lead, 'do_not_contact', False):
            raise ConsentViolationError(f"Marketing communication rejected: Lead {lead.id} is marked Do Not Contact.")

        channel_upper = channel.upper()
        if channel_upper == 'WHATSAPP' and not getattr(lead, 'consent_whatsapp', False):
            raise ConsentViolationError(f"Marketing communication rejected: Lead {lead.id} has not consented to WhatsApp.")
        elif channel_upper == 'EMAIL' and not getattr(lead, 'consent_email', False):
            raise ConsentViolationError(f"Marketing communication rejected: Lead {lead.id} has not consented to Email.")
        elif channel_upper == 'SMS' and not getattr(lead, 'consent_sms', False):
            raise ConsentViolationError(f"Marketing communication rejected: Lead {lead.id} has not consented to SMS.")

    @classmethod
    def send_communication(
        cls,
        organization: Organization,
        channel: str,
        recipient: str,
        lead: Optional[Lead] = None,
        template: Optional[NotificationTemplate] = None,
        template_reference: str = '',
        subject: str = '',
        body: str = '',
        context_data: Optional[Dict[str, Any]] = None,
        purpose: str = 'TRANSACTIONAL',
        idempotency_key: Optional[str] = None,
        trigger_type: str = 'MANUAL',
        related_trial: Optional[TrialBooking] = None,
        related_followup: Optional[SalesFollowupTask] = None,
        created_by_user: Optional[TenantUser] = None,
        provider_preference: Optional[str] = None,
        in_reply_to: Optional[CommunicationMessage] = None,
    ) -> CommunicationMessage:
        """
        Dispatches outbound communication, verifies consent, renders template snapshot,
        persists immutable record, and calls provider adapter.
        Database idempotency guarantees at most one message per organization + idempotency_key.
        """
        # 1. Idempotency Check
        if idempotency_key:
            existing = CommunicationMessage.objects.filter(
                organization=organization,
                idempotency_key=idempotency_key,
            ).first()
            if existing:
                return existing

        # 2. Consent Check
        cls.verify_consent(lead, channel, purpose)

        # 3. Render content snapshot
        final_subject = subject
        final_body = body
        if template:
            tmpl_subject, tmpl_body = cls.render_template(template, context_data or {})
            if not final_subject:
                final_subject = tmpl_subject
            if not final_body:
                final_body = tmpl_body
            if not template_reference:
                template_reference = getattr(template, 'code', None) or getattr(template, 'name', None) or str(template.id)

        # 4. Resolve Provider Adapter
        adapter, integration = CommunicationProviderRegistry.resolve_for_tenant(
            channel=channel,
            provider_preference=provider_preference,
        )

        provider_name = adapter.provider_name if adapter else (provider_preference or 'UNCONFIGURED')
        sender_id = ''
        if integration and integration.configuration:
            sender_id = integration.configuration.get('sender_id') or integration.configuration.get('from_number') or ''

        # 5. Persist CommunicationMessage in QUEUED state
        msg = CommunicationMessage(
            organization=organization,
            lead=lead,
            resolution_status='RESOLVED' if lead else 'UNRESOLVED',
            sender_identifier=sender_id,
            channel=channel.upper(),
            direction='OUTBOUND',
            purpose=purpose,
            recipient=recipient,
            sender=sender_id,
            template=template,
            template_reference=template_reference,
            subject=final_subject,
            body_snapshot=final_body,
            provider=provider_name,
            status='QUEUED',
            idempotency_key=idempotency_key,
            trigger_type=trigger_type,
            related_trial=related_trial,
            related_followup=related_followup,
            in_reply_to=in_reply_to,
            created_by_user=created_by_user,
            queued_at=timezone.now(),
        )

        try:
            with transaction.atomic():
                msg.save()
        except IntegrityError:
            if idempotency_key:
                existing = CommunicationMessage.objects.filter(
                    organization=organization,
                    idempotency_key=idempotency_key,
                ).first()
                if existing:
                    return existing
            raise

        # 6. Execute Provider Dispatch (Graceful Fail-Safe)
        if not adapter or not adapter.is_configured():
            msg.status = 'FAILED'
            msg.failure_code = 'UNCONFIGURED_PROVIDER'
            msg.failure_reason = f"No active, configured provider adapter found for channel {channel}."
            msg.failed_at = timezone.now()
            msg.save(update_fields=['status', 'failure_code', 'failure_reason', 'failed_at'])
            return msg

        try:
            result = adapter.send(
                recipient=recipient,
                body=final_body,
                subject=final_subject,
                template_ref=template_reference,
                template_vars=context_data,
                sender_id=sender_id,
            )
            if result.success:
                msg.status = result.status
                msg.provider_message_id = result.provider_message_id
                if result.status == 'SENT':
                    msg.sent_at = timezone.now()
                msg.save(update_fields=['status', 'provider_message_id', 'sent_at'])
            else:
                msg.status = 'FAILED'
                msg.failure_code = result.error_code or 'DISPATCH_FAILED'
                msg.failure_reason = result.error_message or 'Provider returned failure status'
                msg.failed_at = timezone.now()
                msg.save(update_fields=['status', 'failure_code', 'failure_reason', 'failed_at'])
        except Exception as exc:
            logger.error("Exception during communication dispatch: %s", exc)
            msg.status = 'FAILED'
            msg.failure_code = 'PROVIDER_EXCEPTION'
            msg.failure_reason = str(exc)
            msg.failed_at = timezone.now()
            msg.save(update_fields=['status', 'failure_code', 'failure_reason', 'failed_at'])

        return msg

    @classmethod
    def record_status_event(
        cls,
        message: CommunicationMessage,
        to_status: str,
        provider_event_id: Optional[str] = None,
        occurred_at: Optional[datetime] = None,
        raw_metadata: Optional[Dict[str, Any]] = None,
        error_code: str = '',
        error_message: str = '',
    ) -> Optional[CommunicationStatusEvent]:
        """
        Appends an immutable CommunicationStatusEvent with database idempotency.
        Updates message status ONLY if the new status is a forward progression.
        Prevents out-of-order regression (e.g. READ -> DELIVERED).
        """
        occurred = occurred_at or timezone.now()
        safe_meta = raw_metadata or {}

        # 1. Database-enforced idempotency for status event
        if provider_event_id:
            existing = CommunicationStatusEvent.objects.filter(
                message=message,
                provider_event_id=provider_event_id,
            ).first()
            if existing:
                return existing

        event = CommunicationStatusEvent(
            message=message,
            provider_event_id=provider_event_id,
            from_status=message.status,
            to_status=to_status,
            occurred_at=occurred,
            raw_metadata=safe_meta,
        )
        try:
            with transaction.atomic():
                event.save()
        except IntegrityError:
            # Duplicate event caught by DB constraint
            if provider_event_id:
                return CommunicationStatusEvent.objects.filter(
                    message=message,
                    provider_event_id=provider_event_id,
                ).first()
            return None

        # 2. Out-of-Order Regression Protection
        current_rank = STATUS_ORDER.get(message.status, 0)
        new_rank = STATUS_ORDER.get(to_status, 0)

        # Allow update if new status is higher rank or if transitioning to FAILED
        should_update_message = False
        if to_status == 'FAILED' and message.status not in ('DELIVERED', 'READ', 'REPLIED'):
            should_update_message = True
        elif new_rank > current_rank and to_status != 'FAILED':
            should_update_message = True

        if should_update_message:
            message.status = to_status
            update_fields = ['status', 'updated_at']
            if to_status == 'SENT' and not message.sent_at:
                message.sent_at = occurred
                update_fields.append('sent_at')
            elif to_status == 'DELIVERED' and not message.delivered_at:
                message.delivered_at = occurred
                update_fields.append('delivered_at')
            elif to_status == 'READ' and not message.read_at:
                message.read_at = occurred
                update_fields.append('read_at')
            elif to_status == 'FAILED':
                message.failed_at = occurred
                message.failure_code = error_code
                message.failure_reason = error_message
                update_fields.extend(['failed_at', 'failure_code', 'failure_reason'])

            message.save(update_fields=update_fields)

        return event

    @classmethod
    def resolve_inbound_lead(
        cls,
        organization: Organization,
        sender_identifier: str,
    ) -> Tuple[Optional[Lead], str]:
        """
        Deterministic lead resolution:
        - 1 match         -> (lead, 'RESOLVED')
        - 0 matches       -> (None, 'UNRESOLVED')
        - >1 matches      -> (None, 'AMBIGUOUS')
        Never guesses or binds ambiguous matches.
        """
        db_alias = getattr(organization, '_state', None) and organization._state.db or 'default'
        clean_id = sender_identifier.strip()
        # Look up by phone_normalized or email_normalized
        query = Q(phone_normalized=clean_id) | Q(email_normalized__iexact=clean_id)
        if clean_id.startswith('+'):
            query |= Q(phone_normalized=clean_id[1:])
        elif clean_id.startswith('91') and len(clean_id) == 12:
            query |= Q(phone_normalized=clean_id[2:]) | Q(phone_normalized=f"+{clean_id}")
        else:
            query |= Q(phone_normalized=f"+{clean_id}") | Q(phone_normalized=f"+91{clean_id}")

        matches = list(Lead.objects.using(db_alias).filter(organization=organization).filter(query)[:3])
        if len(matches) == 1:
            return matches[0], 'RESOLVED'
        elif len(matches) > 1:
            return None, 'AMBIGUOUS'
        else:
            return None, 'UNRESOLVED'

    @classmethod
    def ingest_inbound_message(
        cls,
        organization: Organization,
        channel: str,
        sender_identifier: str,
        body: str,
        provider: str,
        provider_message_id: str,
        in_reply_to_provider_id: Optional[str] = None,
        raw_payload: Optional[Dict[str, Any]] = None,
    ) -> CommunicationMessage:
        """
        Persists a real inbound CommunicationMessage.
        Resolves sender to Lead (or marks UNRESOLVED / AMBIGUOUS).
        Links to prior outbound message if in_reply_to_provider_id is provided.
        """
        db_alias = getattr(organization, '_state', None) and organization._state.db or 'default'

        # Idempotency by provider + provider_message_id
        if provider_message_id:
            existing = CommunicationMessage.objects.using(db_alias).filter(
                organization=organization,
                provider=provider,
                provider_message_id=provider_message_id,
            ).first()
            if existing:
                return existing

        lead, res_status = cls.resolve_inbound_lead(organization, sender_identifier)

        in_reply_to_msg = None
        if in_reply_to_provider_id:
            in_reply_to_msg = CommunicationMessage.objects.using(db_alias).filter(
                organization=organization,
                provider_message_id=in_reply_to_provider_id,
            ).first()

        inbound = CommunicationMessage(
            organization=organization,
            lead=lead,
            resolution_status=res_status,
            sender_identifier=sender_identifier,
            raw_sender_data=raw_payload or {},
            channel=channel.upper(),
            direction='INBOUND',
            purpose='TRANSACTIONAL',
            recipient='',
            sender=sender_identifier,
            body_snapshot=body,
            provider=provider,
            provider_message_id=provider_message_id,
            status='RECEIVED',
            received_at=timezone.now(),
            in_reply_to=in_reply_to_msg,
            related_trial=in_reply_to_msg.related_trial if in_reply_to_msg else None,
            related_followup=in_reply_to_msg.related_followup if in_reply_to_msg else None,
        )
        inbound.save(using=db_alias)

        # If it is a reply to an outbound message, record replied timestamp
        if in_reply_to_msg:
            in_reply_to_msg.replied_at = timezone.now()
            if in_reply_to_msg.status in ('SENT', 'DELIVERED', 'READ'):
                in_reply_to_msg.status = 'REPLIED'
            in_reply_to_msg.save(using=db_alias, update_fields=['replied_at', 'status', 'updated_at'])

        return inbound

    @classmethod
    def send_trial_confirmation(
        cls,
        trial: TrialBooking,
        channel: str = 'WHATSAPP',
    ) -> Optional[CommunicationMessage]:
        """
        Decoupled side-effect helper: dispatches trial booking confirmation.
        MUST NEVER raise or fail the parent booking transaction!
        """
        try:
            lead = trial.lead
            recipient = lead.phone_normalized if channel.upper() in ('WHATSAPP', 'SMS') else lead.email_normalized
            if not recipient:
                return None

            dt_str = trial.scheduled_start.strftime('%b %d, %Y')
            tm_str = trial.scheduled_start.strftime('%I:%M %p')
            branch_name = trial.branch.name if trial.branch else 'Our Gym'

            body_text = (
                f"Hi {lead.first_name}, your free trial "
                f"is confirmed for {dt_str} at {tm_str} at {branch_name}! Reply CONFIRM to confirm your attendance."
            )

            idempotency_key = f"TRIAL_CONFIRMATION:{trial.id}:{channel.upper()}"

            return cls.send_communication(
                organization=lead.organization,
                channel=channel,
                recipient=recipient,
                lead=lead,
                subject=f"Your Trial Session Booking — {branch_name}",
                body=body_text,
                purpose='TRANSACTIONAL',
                idempotency_key=idempotency_key,
                trigger_type='TRIAL_CONFIRMATION',
                related_trial=trial,
            )
        except Exception as e:
            logger.warning("Decoupled trial confirmation dispatch failed gracefully: %s", e)
            return None

    @classmethod
    def process_due_trial_reminders(
        cls,
        as_of_time: Optional[datetime] = None,
        db_alias: Optional[str] = None,
    ) -> List[CommunicationMessage]:
        """
        Durable trial reminder worker:
        Scans active TrialBooking records ('BOOKED', 'CONFIRMED').
        Excludes cancelled, rescheduled, attended, or no-show trials.
        Calculates scheduled reminder intervals against tenant CRMTrialReminderPolicy.
        Idempotency key: TRIAL_REMINDER:{trial.id}:{offset_hours}:{channel}.
        """
        from apps.tenant_core.context import get_current_tenant_db_alias
        from apps.tenant_core.models_crm import CRMTrialReminderPolicy
        from django.db import connections

        alias = db_alias or get_current_tenant_db_alias()
        if not alias or alias == 'default':
            if 'tenant_test' in connections:
                alias = 'tenant_test'
            else:
                alias = 'default'

        now = as_of_time or timezone.now()
        dispatched_messages = []

        # Find active trials with scheduled_start starting in the future
        active_trials = TrialBooking.objects.using(alias).filter(
            status__in=['BOOKED', 'CONFIRMED'],
            scheduled_start__gt=now,
        ).select_related('lead', 'lead__organization', 'branch')

        for trial in active_trials:
            org = trial.lead.organization
            policy = CRMTrialReminderPolicy.objects.using(alias).filter(organization=org).first()
            if not policy:
                continue

            channels = []
            if policy.immediate_whatsapp:
                channels.append('WHATSAPP')
            if policy.immediate_email:
                channels.append('EMAIL')
            if policy.immediate_sms:
                channels.append('SMS')
            if not channels:
                channels = ['WHATSAPP']

            offsets = policy.reminder_offsets or [24]
            start_time = trial.scheduled_start

            for offset in offsets:
                reminder_due_time = start_time - timedelta(hours=offset)
                # If reminder point has passed, but we are within the delivery window before class start
                if reminder_due_time <= now < start_time:
                    for ch in channels:
                        idempotency_key = f"TRIAL_REMINDER:{trial.id}:{offset}:{ch.upper()}"
                        # Check if already sent
                        if CommunicationMessage.objects.using(alias).filter(
                            organization=org,
                            idempotency_key=idempotency_key,
                        ).exists():
                            continue

                        recipient = trial.lead.phone_normalized if ch.upper() in ('WHATSAPP', 'SMS') else trial.lead.email_normalized
                        if not recipient:
                            continue

                        branch_name = trial.branch.name if trial.branch else 'Sweat'
                        body = (
                            f"Reminder: Hi {trial.lead.first_name}, your trial at {branch_name} "
                            f"starts in {offset} hours ({start_time.strftime('%I:%M %p')}). See you soon!"
                        )

                        try:
                            msg = cls.send_communication(
                                organization=org,
                                channel=ch,
                                recipient=recipient,
                                lead=trial.lead,
                                subject=f"Trial Reminder: {offset} hours to class",
                                body=body,
                                purpose='TRANSACTIONAL',
                                idempotency_key=idempotency_key,
                                trigger_type='TRIAL_REMINDER',
                                related_trial=trial,
                            )
                            dispatched_messages.append(msg)
                        except Exception as exc:
                            logger.error("Failed to dispatch trial reminder: %s", exc)

        return dispatched_messages

    # Backward compatibility alias
    send_direct = send_communication
