"""
apps/tenant_core/communication/whatsapp_automation.py — Phase 2 Gupshup WhatsApp Automation Engine.
"""

import logging
from datetime import timedelta
from datetime import date, datetime, timedelta
from typing import Dict, Any, List, Optional
from django.utils import timezone
from django.db import transaction

from apps.tenant_core.models_org import Organization
from apps.tenant_core.models_govern import WhatsAppAutomationPolicy, InAppNotification
from apps.tenant_core.models_bookings import Booking
from apps.tenant_core.models_crm import Lead, TrialBooking, SalesFollowupTask
from apps.tenant_core.models_communication import CommunicationMessage
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.services_reliability import record_business_audit
from .whatsapp_variables import WhatsAppVariableResolver
from .service import CommunicationService

logger = logging.getLogger(__name__)


class WhatsAppAutomationService:
    @classmethod
    def get_policy(cls, organization: Organization, db_alias: Optional[str] = None) -> WhatsAppAutomationPolicy:
        manager = WhatsAppAutomationPolicy.objects.using(db_alias) if db_alias else WhatsAppAutomationPolicy.objects
        defaults = {
            'booking_confirmation_variable_map': ['member_name', 'class_name', 'start_datetime', 'trainer_name'],
            'booking_reminder_variable_map': ['start_datetime'],
            'booking_reminder_offsets_minutes': [120, 30],
            'lead_welcome_variable_map': ['lead_name'],
            'trial_confirmation_variable_map': ['lead_name', 'class_name', 'start_datetime', 'trainer_name'],
        }
        policy, _ = manager.get_or_create(
            organization=organization,
            defaults=defaults,
        )
        return policy

    @classmethod
    def send_class_booking_confirmation(
        cls,
        booking: Booking,
        db_alias: Optional[str] = None,
        force: bool = False,
    ) -> Optional[CommunicationMessage]:
        occ = booking.occurrence
        org = (
            occ.class_template.organization if (occ and occ.class_template)
            else (booking.branch.organization if booking.branch else None)
        )
        if not org:
            return None

        policy = cls.get_policy(org, db_alias=db_alias)
        if not force and (not policy.whatsapp_enabled or not policy.booking_confirmation_enabled):
            return None

        user = booking.user_profile.user if booking.user_profile else None
        recipient = (user.phone if user else '') or ''
        if not recipient:
            return None

        var_map = policy.booking_confirmation_variable_map or [
            'member_name', 'class_name', 'start_datetime', 'trainer_name'
        ]
        resolver = WhatsAppVariableResolver(booking=booking, occurrence=booking.occurrence)
        resolved_params = resolver.resolve(var_map)

        preview_body = (
            f"Hi {resolved_params[0] if len(resolved_params) > 0 else 'there'}, "
            f"your session is booked for {resolved_params[1] if len(resolved_params) > 1 else 'Class'} "
            f"at {resolved_params[2] if len(resolved_params) > 2 else ''} "
            f"with Trainer {resolved_params[3] if len(resolved_params) > 3 else ''}. "
            "Reply CONFIRM to confirm or RESCHEDULE to change."
        )

        template_name = policy.booking_confirmation_template_name or 'sweat_class_booking_confirm'
        idempotency_key = f"BOOKING_CONFIRM:{booking.id}:WHATSAPP"

        try:
            msg = CommunicationService.send_communication(
                organization=org,
                channel='WHATSAPP',
                recipient=recipient,
                lead=None,
                template_reference=template_name,
                subject=f"Class Booking Confirmation: {booking.booking_number}",
                body=preview_body,
                context_data=resolved_params,
                purpose='TRANSACTIONAL',
                idempotency_key=idempotency_key,
                trigger_type='CLASS_CONFIRMATION',
            )

            record_business_audit(
                organization=org,
                module='COMMUNICATION',
                action_code='WHATSAPP_BOOKING_CONFIRMATION_SENT',
                entity_type='Booking',
                entity_id=booking.id,
                branch=booking.branch,
                event_description=f"WhatsApp booking confirmation dispatched for booking {booking.booking_number}",
                metadata={
                    'booking_id': str(booking.id),
                    'recipient': recipient[:3] + '***' if len(recipient) >= 6 else '***',
                    'template': template_name,
                    'message_id': str(msg.id),
                    'status': msg.status,
                },
                db_alias=db_alias,
            )
            return msg
        except Exception as exc:
            logger.error("Failed to send WhatsApp booking confirmation for %s: %s", booking.id, exc)
            return None

    @classmethod
    def send_trial_booking_confirmation(
        cls,
        trial: TrialBooking,
        db_alias: Optional[str] = None,
        force: bool = False,
    ) -> Optional[CommunicationMessage]:
        org = trial.lead.organization if trial.lead else (trial.branch.organization if trial.branch else None)
        if not org:
            return None

        policy = cls.get_policy(org, db_alias=db_alias)
        if not force and (not policy.whatsapp_enabled or not policy.trial_confirmation_enabled):
            return None

        lead = trial.lead
        recipient = (lead.phone_normalized or lead.phone_raw or '') if lead else ''
        if not recipient:
            return None

        var_map = policy.trial_confirmation_variable_map or [
            'lead_name', 'class_name', 'start_datetime', 'trainer_name'
        ]
        resolver = WhatsAppVariableResolver(
            trial=trial,
            lead=lead,
            occurrence=getattr(trial, 'class_occurrence', None)
        )
        resolved_params = resolver.resolve(var_map)

        preview_body = (
            f"Hi {resolved_params[0] if len(resolved_params) > 0 else 'there'}, "
            f"your trial session is booked for {resolved_params[1] if len(resolved_params) > 1 else 'Trial'} "
            f"at {resolved_params[2] if len(resolved_params) > 2 else ''} "
            f"with Trainer {resolved_params[3] if len(resolved_params) > 3 else ''}. "
            "Reply CONFIRM to confirm or RESCHEDULE to change."
        )

        template_name = policy.trial_confirmation_template_name or 'sweat_trial_booking_confirm'
        idempotency_key = f"TRIAL_CONFIRM:{trial.id}:WHATSAPP"

        try:
            msg = CommunicationService.send_communication(
                organization=org,
                channel='WHATSAPP',
                recipient=recipient,
                lead=lead,
                template_reference=template_name,
                subject="Trial Booking Confirmation",
                body=preview_body,
                context_data=resolved_params,
                purpose='TRANSACTIONAL',
                idempotency_key=idempotency_key,
                trigger_type='TRIAL_CONFIRMATION',
                related_trial=trial,
            )

            record_business_audit(
                organization=org,
                module='COMMUNICATION',
                action_code='WHATSAPP_TRIAL_CONFIRMATION_SENT',
                entity_type='TrialBooking',
                entity_id=trial.id,
                branch=trial.branch,
                event_description=f"WhatsApp trial confirmation dispatched for lead {lead.id}",
                metadata={
                    'trial_id': str(trial.id),
                    'lead_id': str(lead.id),
                    'recipient': recipient[:3] + '***' if len(recipient) >= 6 else '***',
                    'template': template_name,
                    'message_id': str(msg.id),
                    'status': msg.status,
                },
                db_alias=db_alias,
            )
            return msg
        except Exception as exc:
            logger.error("Failed to send WhatsApp trial confirmation for %s: %s", trial.id, exc)
            return None

    @classmethod
    def send_lead_welcome(
        cls,
        lead: Lead,
        db_alias: Optional[str] = None,
        force: bool = False,
    ) -> Optional[CommunicationMessage]:
        org = lead.organization
        if not org:
            return None

        policy = cls.get_policy(org, db_alias=db_alias)
        if not force and (not policy.whatsapp_enabled or not policy.lead_welcome_enabled):
            return None

        recipient = lead.phone_normalized or lead.phone_raw or ''
        if not recipient:
            return None

        var_map = policy.lead_welcome_variable_map or ['lead_name']
        resolver = WhatsAppVariableResolver(lead=lead)
        resolved_params = resolver.resolve(var_map)

        name = resolved_params[0] if resolved_params else 'there'
        preview_body = f"Hi {name}, thanks for reaching out to SWEAT! We're excited to help you start your fitness journey."
        template_name = policy.lead_welcome_template_name or 'sweat_lead_welcome'
        idempotency_key = f"LEAD_WELCOME:{lead.id}:WHATSAPP"

        try:
            msg = CommunicationService.send_communication(
                organization=org,
                channel='WHATSAPP',
                recipient=recipient,
                lead=lead,
                template_reference=template_name,
                subject="Welcome to SWEAT",
                body=preview_body,
                context_data=resolved_params,
                purpose='TRANSACTIONAL',
                idempotency_key=idempotency_key,
                trigger_type='LEAD_WELCOME',
            )
            return msg
        except Exception as exc:
            logger.error("Failed to send WhatsApp lead welcome for %s: %s", lead.id, exc)
            return None

    @classmethod
    def process_scheduled_reminders(cls, db_alias: Optional[str] = None) -> List[CommunicationMessage]:
        now = timezone.now()
        alias = db_alias or 'default'
        dispatched_messages: List[CommunicationMessage] = []

        active_bookings = Booking.objects.using(alias).filter(
            status__in=['CONFIRMED', 'RESERVED'],
            occurrence__start_at__gt=now,
        ).select_related(
            'occurrence', 'occurrence__class_template',
            'occurrence__branch', 'user_profile', 'user_profile__user', 'branch'
        )

        for booking in active_bookings:
            occ = booking.occurrence
            if not occ or not occ.start_at:
                continue

            org = (
                occ.class_template.organization if (occ.class_template and occ.class_template.organization)
                else (booking.branch.organization if booking.branch else None)
            )
            if not org:
                continue

            policy = cls.get_policy(org, db_alias=alias)
            if not policy.whatsapp_enabled or not policy.booking_reminder_enabled:
                continue

            user = booking.user_profile.user if booking.user_profile else None
            recipient = (user.phone if user else '') or ''
            if not recipient:
                continue

            offsets = policy.booking_reminder_offsets_minutes or [120, 30]
            start_at = occ.start_at

            for offset in offsets:
                reminder_trigger_time = start_at - timedelta(minutes=int(offset))
                if reminder_trigger_time <= now < start_at:
                    idempotency_key = f"BOOKING_REMINDER:{booking.id}:{offset}M:WHATSAPP"
                    if CommunicationMessage.objects.using(alias).filter(
                        organization=org,
                        idempotency_key=idempotency_key,
                    ).exists():
                        continue

                    var_map = policy.booking_reminder_variable_map or ['start_datetime']
                    resolver = WhatsAppVariableResolver(booking=booking, occurrence=occ)
                    resolved_params = resolver.resolve(var_map)

                    time_display = resolved_params[0] if resolved_params else start_at.strftime('%I:%M %p')
                    preview_body = f"Reminder: Your session at SWEAT is today at {time_display}."
                    template_name = policy.booking_reminder_template_name or 'sweat_class_reminder'

                    try:
                        msg = CommunicationService.send_communication(
                            organization=org,
                            channel='WHATSAPP',
                            recipient=recipient,
                            lead=None,
                            template_reference=template_name,
                            subject=f"Class Reminder: {offset} mins to session",
                            body=preview_body,
                            context_data=resolved_params,
                            purpose='TRANSACTIONAL',
                            idempotency_key=idempotency_key,
                            trigger_type='CLASS_REMINDER',
                        )
                        dispatched_messages.append(msg)
                    except Exception as exc:
                        logger.error("Failed to send booking reminder for %s: %s", booking.id, exc)

        active_trials = TrialBooking.objects.using(alias).filter(
            status__in=['BOOKED', 'CONFIRMED'],
            scheduled_start__gt=now,
        ).exclude(
            confirmation_status__in=['CANCELLED', 'DECLINED']
        ).select_related('lead', 'lead__organization', 'branch')

        for trial in active_trials:
            org = trial.lead.organization if trial.lead else (trial.branch.organization if trial.branch else None)
            if not org:
                continue

            policy = cls.get_policy(org, db_alias=alias)
            if not policy.whatsapp_enabled or not policy.booking_reminder_enabled:
                continue

            lead = trial.lead
            recipient = (lead.phone_normalized or lead.phone_raw or '') if lead else ''
            if not recipient:
                continue

            offsets = policy.booking_reminder_offsets_minutes or [120, 30]
            start_at = trial.scheduled_start

            for offset in offsets:
                reminder_trigger_time = start_at - timedelta(minutes=int(offset))
                if reminder_trigger_time <= now < start_at:
                    idempotency_key = f"TRIAL_REMINDER_WA:{trial.id}:{offset}M:WHATSAPP"
                    if CommunicationMessage.objects.using(alias).filter(
                        organization=org,
                        idempotency_key=idempotency_key,
                    ).exists():
                        continue

                    var_map = policy.booking_reminder_variable_map or ['start_datetime']
                    resolver = WhatsAppVariableResolver(
                        trial=trial, lead=lead, occurrence=getattr(trial, 'class_occurrence', None)
                    )
                    resolved_params = resolver.resolve(var_map)

                    time_display = resolved_params[0] if resolved_params else start_at.strftime('%I:%M %p')
                    preview_body = f"Reminder: Your trial session at SWEAT is today at {time_display}."
                    template_name = policy.booking_reminder_template_name or 'sweat_class_reminder'

                    try:
                        msg = CommunicationService.send_communication(
                            organization=org,
                            channel='WHATSAPP',
                            recipient=recipient,
                            lead=lead,
                            template_reference=template_name,
                            subject=f"Trial Reminder: {offset} mins to session",
                            body=preview_body,
                            context_data=resolved_params,
                            purpose='TRANSACTIONAL',
                            idempotency_key=idempotency_key,
                            trigger_type='TRIAL_REMINDER',
                            related_trial=trial,
                        )
                        dispatched_messages.append(msg)
                    except Exception as exc:
                        logger.error("Failed to send trial reminder for %s: %s", trial.id, exc)

        return dispatched_messages

    @classmethod
    def handle_inbound_keyword_action(
        cls,
        inbound_message: CommunicationMessage,
        body_text: Optional[str] = None,
        db_alias: Optional[str] = None,
    ) -> Dict[str, Any]:
        org = inbound_message.organization
        if not org:
            return {'action': 'NONE', 'reason': 'NO_ORG'}

        policy = cls.get_policy(org, db_alias=db_alias)
        alias = db_alias or 'default'
        if body_text is None:
            body_text = getattr(inbound_message, 'body_snapshot', '') or ''
        clean_text = (body_text or '').strip().upper()

        confirm_kw = (policy.inbound_confirm_keyword or 'CONFIRM').strip().upper()
        reschedule_kw = (policy.inbound_reschedule_keyword or 'RESCHEDULE').strip().upper()

        is_confirm = confirm_kw in clean_text
        is_reschedule = reschedule_kw in clean_text

        if not is_confirm and not is_reschedule:
            return {'action': 'NONE', 'matched': False}

        if is_confirm:
            trial = inbound_message.related_trial
            if not trial and inbound_message.lead:
                trial = TrialBooking.objects.using(alias).filter(
                    lead=inbound_message.lead,
                    status__in=['BOOKED', 'CONFIRMED', 'SCHEDULED'],
                ).exclude(confirmation_status__in=['CANCELLED', 'DECLINED']).first()

            if trial:
                from apps.tenant_core.services_crm import CRMLeadService
                CRMLeadService.confirm_trial(
                    trial=trial,
                    channel='WHATSAPP',
                    notes=f"Auto-confirmed via WhatsApp keyword '{confirm_kw}'",
                    db_alias=alias,
                )

                if policy.confirm_action_notify_staff:
                    cls._notify_staff_confirmation(org, trial=trial, db_alias=alias)

                record_business_audit(
                    organization=org,
                    module='COMMUNICATION',
                    action_code='WHATSAPP_INBOUND_CONFIRMED',
                    entity_type='TrialBooking',
                    entity_id=trial.id,
                    branch=trial.branch,
                    event_description=f"Trial booking confirmed via inbound WhatsApp '{confirm_kw}' from {inbound_message.sender_identifier}",
                    db_alias=alias,
                )
                return {'action': 'CONFIRMED', 'entity': 'TrialBooking', 'id': str(trial.id)}

            booking = cls._find_recent_booking_by_phone(org, inbound_message.sender_identifier, db_alias=alias)
            if booking:
                booking.confirmation_status = 'CONFIRMED'
                booking.confirmed_at = timezone.now()
                booking.save(using=alias, update_fields=['confirmation_status', 'confirmed_at', 'updated_at'])

                if policy.confirm_action_notify_staff:
                    cls._notify_staff_confirmation(org, booking=booking, db_alias=alias)

                record_business_audit(
                    organization=org,
                    module='COMMUNICATION',
                    action_code='WHATSAPP_INBOUND_CONFIRMED',
                    entity_type='Booking',
                    entity_id=booking.id,
                    branch=booking.branch,
                    event_description=f"Class booking {booking.booking_number} confirmed via inbound WhatsApp",
                    db_alias=alias,
                )
                return {'action': 'CONFIRMED', 'entity': 'Booking', 'id': str(booking.id)}

        if is_reschedule:
            trial = inbound_message.related_trial
            if not trial and inbound_message.lead:
                trial = TrialBooking.objects.using(alias).filter(
                    lead=inbound_message.lead,
                    status__in=['BOOKED', 'CONFIRMED', 'SCHEDULED'],
                ).exclude(confirmation_status__in=['CANCELLED', 'DECLINED']).first()

            if trial:
                trial.confirmation_status = 'RESCHEDULE_REQUESTED'
                reason_note = f"Reschedule requested via WhatsApp: {body_text[:150]}"
                trial.notes = f"{trial.notes}\n{reason_note}".strip() if trial.notes else reason_note
                trial.save(using=alias, update_fields=['confirmation_status', 'notes', 'updated_at'])

                if policy.reschedule_action_create_task:
                    cls._create_reschedule_task(org, trial=trial, message_body=body_text, db_alias=alias)

                record_business_audit(
                    organization=org,
                    module='COMMUNICATION',
                    action_code='WHATSAPP_INBOUND_RESCHEDULE_REQUESTED',
                    entity_type='TrialBooking',
                    entity_id=trial.id,
                    branch=trial.branch,
                    event_description=f"Reschedule requested via WhatsApp '{reschedule_kw}' by {inbound_message.sender_identifier}",
                    db_alias=alias,
                )
                return {'action': 'RESCHEDULE_REQUESTED', 'entity': 'TrialBooking', 'id': str(trial.id)}

            booking = cls._find_recent_booking_by_phone(org, inbound_message.sender_identifier, db_alias=alias)
            if booking:
                booking.status = 'RESCHEDULED'
                booking.save(using=alias, update_fields=['status', 'updated_at'])

                if policy.reschedule_action_create_task:
                    cls._create_reschedule_task(org, booking=booking, message_body=body_text, db_alias=alias)

                record_business_audit(
                    organization=org,
                    module='COMMUNICATION',
                    action_code='WHATSAPP_INBOUND_RESCHEDULE_REQUESTED',
                    entity_type='Booking',
                    entity_id=booking.id,
                    branch=booking.branch,
                    event_description=f"Reschedule requested for booking {booking.booking_number} via WhatsApp",
                    db_alias=alias,
                )
                return {'action': 'RESCHEDULE_REQUESTED', 'entity': 'Booking', 'id': str(booking.id)}

        return {'action': 'KEYWORD_MATCHED_NO_ENTITY'}


    # ── Sarvam Voice Agent Domain Event Handlers & HSM Templates ───────────────

    @classmethod
    def send_sarvam_trial_booking_confirmation(
        cls,
        trial: TrialBooking,
        db_alias: Optional[str] = None,
        force: bool = False,
        scheduled_start_override: Optional[datetime] = None,
    ) -> Optional[CommunicationMessage]:
        """
        Dispatched when Sarvam AI books a trial (or CRM_TRIAL_BOOKED domain event fires).
        Template: sweat_trial_booking_confirm (Phase 1 Outbound-Only)
        Parameters:
          {{1}}: Lead Name (lead.first_name)
          {{2}}: Program Name (e.g. "Sweat Pilates")
          {{3}}: Branch Name (e.g. "Sweat Bandra West")
          {{4}}: Class Date & Time in IST (e.g. "Monday, 19 Oct at 10:00 AM")
        """
        from .whatsapp_variables import fmt_ist_datetime
        from apps.tenant_core.models_crm import LeadActivity

        org = trial.lead.organization if trial.lead else (trial.branch.organization if trial.branch else None)
        if not org:
            return None

        policy = cls.get_policy(org, db_alias=db_alias)
        if not force and not policy.whatsapp_enabled:
            return None

        lead = trial.lead
        recipient = (lead.phone_normalized or lead.phone_raw or '') if lead else ''
        if not recipient:
            return None

        lead_name = (lead.first_name or '').strip() if lead else 'there'
        if not lead_name:
            lead_name = 'there'

        occ = getattr(trial, 'class_occurrence', None)
        if not occ and getattr(trial, 'class_occurrence_id', None):
            try:
                from apps.tenant_core.models_classes import ClassOccurrence
                occ = ClassOccurrence.objects.using(db_alias or 'default').select_related('class_template__program').filter(id=trial.class_occurrence_id).first()
            except Exception:
                pass

        program_name = (
            getattr(getattr(trial, 'program', None), 'name', None)
            or (occ.class_template.program.name if occ and occ.class_template and occ.class_template.program else None)
            or (occ.class_template.name if occ and occ.class_template else None)
            or "Sweat Pilates"
        )
        branch_name = trial.branch.name if trial.branch else "Sweat Studio"
        effective_start = scheduled_start_override or trial.scheduled_start
        class_dt_str = fmt_ist_datetime(effective_start)

        params = [lead_name, program_name, branch_name, class_dt_str]
        template_name = getattr(policy, 'trial_confirmation_template_name', None) or 'sweat_trial_booking_confirm'
        
        # Lifecycle-aware idempotency key:
        # Incorporates the slot timestamp and cancellation cycle so that a trial that is cancelled
        # and subsequently rebooked correctly dispatches a new booking confirmation while safely deduping retries.
        prior_cancels = CommunicationMessage.objects.using(db_alias or 'default').filter(
            organization=org,
            related_trial=trial,
            trigger_type='SARVAM_TRIAL_CANCELLATION',
        ).count()
        cycle_suffix = f":CYCLE_{prior_cancels}" if prior_cancels > 0 else ""
        idempotency_key = f"SARVAM_TRIAL_BOOKED:{trial.id}:{int(effective_start.timestamp())}{cycle_suffix}:WHATSAPP"

        body_preview = (
            f"Hello {lead_name}, your trial session for {program_name} at {branch_name} "
            f"is confirmed for {class_dt_str}."
        )

        try:
            msg = CommunicationService.send_communication(
                organization=org,
                channel='WHATSAPP',
                recipient=recipient,
                lead=lead,
                template_reference=template_name,
                subject=f"Trial Booking Confirmation — {program_name}",
                body=body_preview,
                context_data=params,
                purpose='TRANSACTIONAL',
                idempotency_key=idempotency_key,
                trigger_type='SARVAM_TRIAL_CONFIRMATION',
                related_trial=trial,
            )

            # Record in Lead 360 Timeline (LeadActivity)
            try:
                LeadActivity.objects.using(db_alias or 'default').create(
                    lead=lead,
                    activity_type='WHATSAPP',
                    outcome='SENT' if msg.status in ('SENT', 'SUBMITTED', 'QUEUED') else 'FAILED',
                    notes=f"WhatsApp Trial Confirmation ({template_name}): {body_preview[:200]}",
                    external_reference=f"MSG:{msg.id}",
                    activity_at=timezone.now(),
                )
            except Exception as act_err:
                logger.warning("Failed to record LeadActivity for WhatsApp trial confirmation: %s", act_err)

            record_business_audit(
                organization=org,
                module='COMMUNICATION',
                action_code='WHATSAPP_SARVAM_TRIAL_CONFIRMATION_SENT',
                entity_type='TrialBooking',
                entity_id=trial.id,
                branch=trial.branch,
                event_description=f"WhatsApp Sarvam trial confirmation dispatched to {lead_name}",
                metadata={
                    'trial_id': str(trial.id),
                    'lead_id': str(lead.id),
                    'template': template_name,
                    'message_id': str(msg.id),
                    'params': params,
                    'status': msg.status,
                },
                db_alias=db_alias,
            )
            return msg
        except Exception as exc:
            logger.error("Failed to send Sarvam WhatsApp trial confirmation for trial %s: %s", trial.id, exc)
            return None

    @classmethod
    def send_sarvam_trial_reschedule_confirmation(
        cls,
        trial: TrialBooking,
        db_alias: Optional[str] = None,
        force: bool = False,
        scheduled_start_override: Optional[datetime] = None,
    ) -> Optional[CommunicationMessage]:
        """
        Dispatched when Sarvam AI reschedules a trial (CRM_TRIAL_RESCHEDULED).
        Template: sweat_trial_reschedule_confirm (Phase 1 Outbound-Only)
        Parameters:
          {{1}} Lead Name
          {{2}} Program Name
          {{3}} Branch Name
          {{4}} New Class Date & Time in IST
        """
        from .whatsapp_variables import fmt_ist_datetime
        from apps.tenant_core.models_crm import LeadActivity

        org = trial.lead.organization if trial.lead else (trial.branch.organization if trial.branch else None)
        if not org:
            return None

        policy = cls.get_policy(org, db_alias=db_alias)
        if not force and not policy.whatsapp_enabled:
            return None

        lead = trial.lead
        recipient = (lead.phone_normalized or lead.phone_raw or '') if lead else ''
        if not recipient:
            return None

        lead_name = (lead.first_name or '').strip() if lead else 'there'
        if not lead_name:
            lead_name = 'there'

        occ = getattr(trial, 'class_occurrence', None)
        if not occ and getattr(trial, 'class_occurrence_id', None):
            try:
                from apps.tenant_core.models_classes import ClassOccurrence
                occ = ClassOccurrence.objects.using(db_alias or 'default').select_related('class_template__program').filter(id=trial.class_occurrence_id).first()
            except Exception:
                pass

        program_name = (
            getattr(getattr(trial, 'program', None), 'name', None)
            or (occ.class_template.program.name if occ and occ.class_template and occ.class_template.program else None)
            or (occ.class_template.name if occ and occ.class_template else None)
            or "Sweat Pilates"
        )
        branch_name = trial.branch.name if trial.branch else "Sweat Studio"
        effective_start = scheduled_start_override or trial.scheduled_start
        class_dt_str = fmt_ist_datetime(effective_start)

        params = [lead_name, program_name, branch_name, class_dt_str]
        template_name = 'sweat_trial_reschedule_confirm'
        idempotency_key = f"SARVAM_TRIAL_RESCHED:{trial.id}:{int(effective_start.timestamp())}:WHATSAPP"

        body_preview = (
            f"Hello {lead_name}, your trial session for {program_name} at {branch_name} has been rescheduled "
            f"to {class_dt_str}."
        )

        try:
            msg = CommunicationService.send_communication(
                organization=org,
                channel='WHATSAPP',
                recipient=recipient,
                lead=lead,
                template_reference=template_name,
                subject=f"Trial Rescheduled — {program_name}",
                body=body_preview,
                context_data=params,
                purpose='TRANSACTIONAL',
                idempotency_key=idempotency_key,
                trigger_type='SARVAM_TRIAL_RESCHEDULE',
                related_trial=trial,
            )

            try:
                LeadActivity.objects.using(db_alias or 'default').create(
                    lead=lead,
                    activity_type='WHATSAPP',
                    outcome='SENT' if msg.status in ('SENT', 'SUBMITTED', 'QUEUED') else 'FAILED',
                    notes=f"WhatsApp Trial Reschedule ({template_name}): {body_preview[:200]}",
                    external_reference=f"MSG:{msg.id}",
                    activity_at=timezone.now(),
                )
            except Exception as act_err:
                logger.warning("Failed to record LeadActivity for reschedule: %s", act_err)

            record_business_audit(
                organization=org,
                module='COMMUNICATION',
                action_code='WHATSAPP_SARVAM_TRIAL_RESCHEDULE_SENT',
                entity_type='TrialBooking',
                entity_id=trial.id,
                branch=trial.branch,
                event_description=f"WhatsApp Sarvam trial reschedule confirmation dispatched to {lead_name}",
                metadata={
                    'trial_id': str(trial.id),
                    'lead_id': str(lead.id),
                    'template': template_name,
                    'message_id': str(msg.id),
                    'params': params,
                    'status': msg.status,
                },
                db_alias=db_alias,
            )
            return msg
        except Exception as exc:
            logger.error("Failed to send Sarvam WhatsApp reschedule confirmation for trial %s: %s", trial.id, exc)
            return None

    @classmethod
    def send_sarvam_trial_cancellation(
        cls,
        trial: TrialBooking,
        db_alias: Optional[str] = None,
        force: bool = False,
        scheduled_start_override: Optional[datetime] = None,
    ) -> Optional[CommunicationMessage]:
        """
        Dispatched when a lead cancels during a voice call (CRM_TRIAL_CANCELLED).
        Template: sweat_trial_cancel_confirm (Phase 1 Outbound-Only)
        Parameters:
          {{1}} Lead Name
          {{2}} Program Name
          {{3}} Branch Name
          {{4}} Cancelled Class Date & Time in IST
        """
        from .whatsapp_variables import fmt_ist_datetime
        from apps.tenant_core.models_crm import LeadActivity

        org = trial.lead.organization if trial.lead else (trial.branch.organization if trial.branch else None)
        if not org:
            return None

        policy = cls.get_policy(org, db_alias=db_alias)
        if not force and not policy.whatsapp_enabled:
            return None

        lead = trial.lead
        recipient = (lead.phone_normalized or lead.phone_raw or '') if lead else ''
        if not recipient:
            return None

        lead_name = (lead.first_name or '').strip() if lead else 'there'
        if not lead_name:
            lead_name = 'there'

        occ = getattr(trial, 'class_occurrence', None)
        if not occ and getattr(trial, 'class_occurrence_id', None):
            try:
                from apps.tenant_core.models_classes import ClassOccurrence
                occ = ClassOccurrence.objects.using(db_alias or 'default').select_related('class_template__program').filter(id=trial.class_occurrence_id).first()
            except Exception:
                pass

        program_name = (
            getattr(getattr(trial, 'program', None), 'name', None)
            or (occ.class_template.program.name if occ and occ.class_template and occ.class_template.program else None)
            or (occ.class_template.name if occ and occ.class_template else None)
            or "Sweat Pilates"
        )
        branch_name = trial.branch.name if trial.branch else "Sweat Studio"
        effective_start = scheduled_start_override or trial.scheduled_start
        class_dt_str = fmt_ist_datetime(effective_start)

        params = [lead_name, program_name, branch_name, class_dt_str]
        template_name = 'sweat_trial_cancel_confirm'
        
        prior_bookings = CommunicationMessage.objects.using(db_alias or 'default').filter(
            organization=org,
            related_trial=trial,
            trigger_type='SARVAM_TRIAL_CONFIRMATION',
        ).count()
        cycle_suffix = f":CYCLE_{prior_bookings}" if prior_bookings > 1 else ""
        idempotency_key = f"SARVAM_TRIAL_CANCEL:{trial.id}:{int(effective_start.timestamp())}{cycle_suffix}:WHATSAPP"

        body_preview = (
            f"Hello {lead_name}, your trial session for {program_name} at {branch_name} "
            f"scheduled for {class_dt_str} has been cancelled."
        )

        try:
            msg = CommunicationService.send_communication(
                organization=org,
                channel='WHATSAPP',
                recipient=recipient,
                lead=lead,
                template_reference=template_name,
                subject="Trial Session Cancelled",
                body=body_preview,
                context_data=params,
                purpose='TRANSACTIONAL',
                idempotency_key=idempotency_key,
                trigger_type='SARVAM_TRIAL_CANCELLATION',
                related_trial=trial,
            )

            try:
                LeadActivity.objects.using(db_alias or 'default').create(
                    lead=lead,
                    activity_type='WHATSAPP',
                    outcome='SENT' if msg.status in ('SENT', 'SUBMITTED', 'QUEUED') else 'FAILED',
                    notes=f"WhatsApp Trial Cancellation ({template_name}): {body_preview[:200]}",
                    external_reference=f"MSG:{msg.id}",
                    activity_at=timezone.now(),
                )
            except Exception as act_err:
                logger.warning("Failed to record LeadActivity for cancellation: %s", act_err)

            record_business_audit(
                organization=org,
                module='COMMUNICATION',
                action_code='WHATSAPP_SARVAM_TRIAL_CANCEL_SENT',
                entity_type='TrialBooking',
                entity_id=trial.id,
                branch=trial.branch,
                event_description=f"WhatsApp Sarvam trial cancellation notice dispatched to {lead_name}",
                metadata={
                    'trial_id': str(trial.id),
                    'lead_id': str(lead.id),
                    'template': template_name,
                    'message_id': str(msg.id),
                    'params': params,
                    'status': msg.status,
                },
                db_alias=db_alias,
            )
            return msg
        except Exception as exc:
            logger.error("Failed to send Sarvam WhatsApp cancellation notice for trial %s: %s", trial.id, exc)
            return None

    @classmethod
    def send_sarvam_trial_reminder(
        cls,
        trial: TrialBooking,
        reminder_window: str = '24h',
        db_alias: Optional[str] = None,
        force: bool = False,
    ) -> Optional[CommunicationMessage]:
        """
        Dispatched at T-24h and T-2h before class start.
        Templates: sweat_trial_reminder_24h & sweat_trial_reminder_2h (Phase 1 Outbound-Only)
        Parameters:
          {{1}} Lead Name
          {{2}} Program Name
          {{3}} Branch Name
          {{4}} Class Date & Time in IST
          {{5}} Branch Physical Address
        """
        from .whatsapp_variables import fmt_ist_datetime
        from apps.tenant_core.models_crm import LeadActivity

        # 1. Guard against cancelled or rescheduled bookings
        if trial.status in ('CANCELLED', 'RESCHEDULED') or getattr(trial, 'confirmation_status', '') in ('CANCELLED', 'RESCHEDULE_REQUESTED'):
            logger.info("Suppressing WhatsApp reminder for trial %s: trial status is %s.", trial.id, trial.status)
            return None

        # 2. Guard against stale reminders (scheduled time has already passed)
        if trial.scheduled_start and trial.scheduled_start <= timezone.now():
            logger.info("Suppressing WhatsApp reminder for trial %s: scheduled start has passed.", trial.id)
            return None

        # 3. Guard against missing branch address (suppress dispatch rather than fabricate generic address)
        branch = trial.branch
        branch_address = ""
        if branch:
            branch_address = (
                getattr(branch, 'address', '')
                or f"{getattr(branch, 'address_line_1', '') or ''} {getattr(branch, 'address_line_2', '') or ''}".strip()
            ).strip()

        if not branch_address:
            logger.warning(
                "Suppressing WhatsApp reminder for trial %s: required location data is unavailable.",
                trial.id
            )
            return None

        org = trial.lead.organization if trial.lead else (trial.branch.organization if trial.branch else None)
        if not org:
            return None

        policy = cls.get_policy(org, db_alias=db_alias)
        if not force and not policy.whatsapp_enabled:
            return None

        lead = trial.lead
        recipient = (lead.phone_normalized or lead.phone_raw or '') if lead else ''
        if not recipient:
            return None

        lead_name = (lead.first_name or '').strip() if lead else 'there'
        if not lead_name:
            lead_name = 'there'

        occ = getattr(trial, 'class_occurrence', None)
        if not occ and getattr(trial, 'class_occurrence_id', None):
            try:
                from apps.tenant_core.models_classes import ClassOccurrence
                occ = ClassOccurrence.objects.using(db_alias or 'default').select_related('class_template__program').filter(id=trial.class_occurrence_id).first()
            except Exception:
                pass

        program_name = (
            getattr(getattr(trial, 'program', None), 'name', None)
            or (occ.class_template.program.name if occ and occ.class_template and occ.class_template.program else None)
            or (occ.class_template.name if occ and occ.class_template else None)
            or "Sweat Pilates"
        )
        branch_name = trial.branch.name if trial.branch else "Sweat Studio"
        class_dt_str = fmt_ist_datetime(trial.scheduled_start)

        params = [lead_name, program_name, branch_name, class_dt_str, branch_address]
        template_name = 'sweat_trial_reminder_24h' if reminder_window == '24h' else 'sweat_trial_reminder_2h'
        idempotency_key = f"SARVAM_TRIAL_REMINDER:{trial.id}:{reminder_window}:WHATSAPP"

        body_preview = (
            f"Hello {lead_name}, reminder: your trial session for {program_name} at {branch_name} "
            f"is scheduled for {class_dt_str}. Address: {branch_address}."
        )

        try:
            msg = CommunicationService.send_communication(
                organization=org,
                channel='WHATSAPP',
                recipient=recipient,
                lead=lead,
                template_reference=template_name,
                subject=f"Reminder: Trial Class — {program_name}",
                body=body_preview,
                context_data=params,
                purpose='TRANSACTIONAL',
                idempotency_key=idempotency_key,
                trigger_type=f'TRIAL_REMINDER_{reminder_window.upper()}',
                related_trial=trial,
            )

            try:
                LeadActivity.objects.using(db_alias or 'default').create(
                    lead=lead,
                    activity_type='WHATSAPP',
                    outcome='SENT' if msg.status in ('SENT', 'SUBMITTED', 'QUEUED') else 'FAILED',
                    notes=f"WhatsApp Trial Reminder ({template_name}): {body_preview[:200]}",
                    external_reference=f"MSG:{msg.id}",
                    activity_at=timezone.now(),
                )
            except Exception as act_err:
                logger.warning("Failed to record LeadActivity for reminder: %s", act_err)

            record_business_audit(
                organization=org,
                module='COMMUNICATION',
                action_code='WHATSAPP_SARVAM_TRIAL_REMINDER_SENT',
                entity_type='TrialBooking',
                entity_id=trial.id,
                branch=trial.branch,
                event_description=f"WhatsApp Sarvam trial reminder ({reminder_window}) dispatched to {lead_name}",
                metadata={
                    'trial_id': str(trial.id),
                    'lead_id': str(lead.id),
                    'template': template_name,
                    'reminder_window': reminder_window,
                    'message_id': str(msg.id),
                    'params': params,
                    'status': msg.status,
                },
                db_alias=db_alias,
            )
            return msg
        except Exception as exc:
            logger.error("Failed to send Sarvam WhatsApp reminder (%s) for trial %s: %s", reminder_window, trial.id, exc)
            return None

    @classmethod
    def _find_recent_booking_by_phone(cls, org: Organization, phone: str, db_alias: str = 'default') -> Optional[Booking]:
        if not phone:
            return None
        clean_digits = ''.join(c for c in phone if c.isdigit())[-10:]
        now = timezone.now()
        return Booking.objects.using(db_alias).filter(
            occurrence__branch__organization=org,
            user_profile__user__phone__icontains=clean_digits,
            status__in=['CONFIRMED', 'RESERVED'],
            occurrence__start_at__gte=now - timedelta(hours=2),
        ).order_by('occurrence__start_at').first()

    @classmethod
    def _notify_staff_confirmation(
        cls,
        org: Organization,
        trial: Optional[TrialBooking] = None,
        booking: Optional[Booking] = None,
        db_alias: str = 'default',
    ):
        target_user = None
        subject = "Booking Confirmed via WhatsApp"
        if trial:
            target_user = trial.lead.assigned_sales_user or trial.trainer
            lead_name = f"{trial.lead.first_name} {trial.lead.last_name}".strip()
            body = f"Trial session for {lead_name} was confirmed via WhatsApp."
        elif booking:
            member_name = f"{booking.user_profile.first_name_snapshot or ''}".strip() or "Member"
            body = f"Class booking {booking.booking_number} for {member_name} confirmed via WhatsApp."

        if not target_user:
            target_user = TenantUser.objects.using(db_alias).filter(organization=org, status='ACTIVE').first()

        if target_user:
            InAppNotification.objects.using(db_alias).create(
                organization=org,
                user=target_user,
                title=subject,
                message=body,
                notification_type='SYSTEM',
            )

    @classmethod
    def _create_reschedule_task(
        cls,
        org: Organization,
        trial: Optional[TrialBooking] = None,
        booking: Optional[Booking] = None,
        message_body: str = '',
        db_alias: str = 'default',
    ):
        lead = trial.lead if trial else None
        agent = (
            (lead.assigned_sales_user if lead else None)
            or TenantUser.objects.using(db_alias).filter(organization=org, status='ACTIVE').first()
        )
        if not agent:
            return

        ref_id = f"WA_RESCHED_{trial.id if trial else (booking.id if booking else 'UNKNOWN')}"
        if not SalesFollowupTask.objects.using(db_alias).filter(external_reference=ref_id).exists():
            name = (
                f"{lead.first_name} {lead.last_name}".strip() if lead
                else (booking.user_profile.first_name_snapshot if booking else "Client")
            )
            SalesFollowupTask.objects.using(db_alias).create(
                lead=lead,
                task_type='FOLLOWUP_CALL',
                priority='URGENT',
                due_at=timezone.now() + timedelta(minutes=30),
                status='PENDING',
                assigned_to_user=agent,
                created_by_user=agent,
                external_reference=ref_id,
                outcome=f"WhatsApp Reschedule Request from {name}: {message_body[:150]}",
            )
