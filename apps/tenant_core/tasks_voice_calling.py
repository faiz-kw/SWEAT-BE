"""
apps/tenant_core/tasks_voice_calling.py — Celery Tasks for Sarvam AI Voice Calling Engine.

Provides:
- trigger_automated_sarvam_call_task: Automated outbound call initiation for new qualified leads.
- process_trial_voice_reminders_task: Outbound voice reminder calls for confirmed upcoming trials.
- process_no_show_recovery_calls_task: Automated follow-up calls for unattended trial bookings.
"""

import logging
import zoneinfo
from datetime import timedelta
from celery import shared_task
from django.utils import timezone
from django.db import transaction

from apps.tenant_core.context import tenant_database_context
from config.routers import TenantRoutingError

logger = logging.getLogger(__name__)
IST = zoneinfo.ZoneInfo('Asia/Kolkata')


@shared_task(
    bind=True,
    name='apps.tenant_core.tasks_voice_calling.trigger_automated_sarvam_call_task',
    max_retries=3,
    default_retry_delay=120,
    acks_late=True,
)
def trigger_automated_sarvam_call_task(self, tenant_id: str, lead_id: str, **kwargs):
    """
    Automated outbound voice call initiation for a qualified lead.
    Enforces calling hours (09:00-21:00 IST):
    - If called outside calling hours, calculates next morning 09:15 IST and reschedules.
    - If within calling hours, checks consent, duplicate calls, and calls Sarvam API.
    """
    if not tenant_id or not lead_id:
        logger.error("trigger_automated_sarvam_call_task missing tenant_id or lead_id.")
        return {'status': 'FAILED', 'error': 'tenant_id and lead_id are required'}

    try:
        with tenant_database_context(tenant_id) as db_alias:
            from apps.tenant_core.models_crm import Lead
            from apps.tenant_core.services_voice_calling import VoiceCallingService

            lead = Lead.objects.using(db_alias).select_related('organization', 'branch').filter(id=lead_id).first()
            if not lead:
                logger.warning("Lead %s not found in tenant DB %s", lead_id, db_alias)
                return {'status': 'FAILED', 'error': 'Lead not found'}

            # 1. Calling Hours Check (IST 09:00 - 21:00)
            now_ist = timezone.now().astimezone(IST)
            current_hour = now_ist.hour

            from django.conf import settings
            start_hour = getattr(settings, 'VOICE_CALLING_HOURS_START', 9)
            bypass_hours = (
                kwargs.get('bypass_calling_hours', False)
                or getattr(settings, 'VOICE_CALLING_BYPASS_HOURS', False)
                or not getattr(settings, 'VOICE_CALLING_ENFORCE_HOURS', True)
            )

            if not bypass_hours and not (start_hour <= current_hour < end_hour):
                # Outside calling hours: schedule for next morning at start_hour:15 IST
                if current_hour >= end_hour:
                    next_morning = (now_ist + timedelta(days=1)).replace(hour=start_hour, minute=15, second=0, microsecond=0)
                else:
                    next_morning = now_ist.replace(hour=start_hour, minute=15, second=0, microsecond=0)

                delay_seconds = max(60, int((next_morning - now_ist).total_seconds()))
                logger.info(
                    "Lead %s arrived outside calling hours (%s). Rescheduling Sarvam call in %d seconds (at %s).",
                    lead.id, now_ist.strftime('%H:%M IST'), delay_seconds, next_morning.strftime('%Y-%m-%d %H:%M IST')
                )
                trigger_automated_sarvam_call_task.apply_async(
                    args=[tenant_id, lead_id],
                    countdown=delay_seconds,
                )
                return {
                    'status': 'SCHEDULED_FOR_MORNING',
                    'lead_id': str(lead.id),
                    'scheduled_at_ist': next_morning.isoformat(),
                }

            # 2. Check Voice Consent
            consent_status = VoiceCallingService.get_lead_voice_consent_status(lead, db_alias=db_alias)
            if not consent_status.get('is_eligible') or consent_status.get('status') != 'GRANTED':
                logger.warning(
                    "Automated call skipped for lead %s: Voice consent not granted (%s).",
                    lead.id, consent_status.get('reason')
                )
                return {
                    'status': 'SKIPPED_NO_CONSENT',
                    'lead_id': str(lead.id),
                    'reason': consent_status.get('reason'),
                }

            # 3. Check if lead is in eligible stage (e.g. NEW_LEAD or FOLLOW_UP_PENDING)
            if lead.current_status in ('CONVERTED', 'LOST', 'TRIAL_CONFIRMED', 'TRIAL_ATTENDED'):
                logger.info("Lead %s already in terminal or booked status %s. Skipping automated call.", lead.id, lead.current_status)
                return {'status': 'SKIPPED_STATUS', 'lead_id': str(lead.id), 'current_status': lead.current_status}

            # 3b. Check if direct walk-in / package purchase
            src_name = getattr(lead.lead_source, 'name', '') or ''
            src_type = getattr(lead.lead_source, 'source_type', '') or ''
            if any(k in f"{src_type} {src_name}".lower() for k in ('walk', 'direct', 'reception', 'in-person', 'in person')):
                logger.info("Lead %s is walk-in / direct purchase (source=%s). Skipping automated Sarvam call.", lead.id, src_name or src_type)
                return {'status': 'SKIPPED_WALK_IN', 'lead_id': str(lead.id)}

            # 4. Initiate Outbound Call via VoiceCallingService
            attempt_number = kwargs.get('attempt_number', 1)
            if attempt_number > 2:
                logger.info("Lead %s exceeded max AI attempts (%d). Skipping automated dial.", lead.id, attempt_number)
                return {'status': 'SKIPPED_MAX_ATTEMPTS', 'lead_id': str(lead.id), 'attempts': attempt_number}

            idempotency_key = f"AUTO_VOICE_LEAD_{lead.id}_ATT_{attempt_number}"
            session = VoiceCallingService.initiate_outbound_call(
                lead=lead,
                actor_user=None,
                idempotency_key=idempotency_key,
                custom_metadata={
                    'source': 'AUTOMATED_LEAD_ACQUISITION',
                    'automated': True,
                    'bypass_calling_hours': bypass_hours,
                },
                db_alias=db_alias,
            )

            logger.info("Automated Sarvam call initiated for lead %s. CallSession=%s status=%s", lead.id, session.id, session.status)
            return {
                'status': 'INITIATED',
                'lead_id': str(lead.id),
                'call_session_id': str(session.id),
                'call_status': session.status,
            }

    except TenantRoutingError as tre:
        logger.error("Tenant routing error in trigger_automated_sarvam_call_task: %s", tre)
        return {'status': 'FAILED', 'error': str(tre)}
    except Exception as exc:
        logger.error("Unhandled error in trigger_automated_sarvam_call_task for lead %s: %s", lead_id, exc, exc_info=True)
        raise self.retry(exc=exc, countdown=120)


@shared_task(
    bind=True,
    name='apps.tenant_core.tasks_voice_calling.process_trial_voice_reminders_task',
    max_retries=1,
    acks_late=True,
)
def process_trial_voice_reminders_task(self, tenant_id: str = None, **kwargs):
    """
    Scans for confirmed trial bookings starting in 24 hours (23h to 25h window).
    Initiates AI voice reminder calls to confirm attendance and answer last-minute questions.
    """
    if not tenant_id:
        from apps.master.models_tenant import Tenant
        tenants = Tenant.objects.filter(status='ACTIVE')
        results = []
        for t in tenants:
            res = process_trial_voice_reminders_task.delay(str(t.id))
            results.append(str(t.id))
        return {'status': 'DISPATCHED_TO_ALL_TENANTS', 'tenant_count': len(results)}

    try:
        with tenant_database_context(tenant_id) as db_alias:
            from apps.tenant_core.models_crm import TrialBooking, CRMTrialReminderPolicy
            from apps.tenant_core.services_voice_calling import VoiceCallingService

            policy = CRMTrialReminderPolicy.objects.using(db_alias).first()
            if policy and getattr(policy, 'ai_calling_enabled', False) is False:
                logger.info("AI calling disabled for tenant %s in CRMTrialReminderPolicy. Skipping reminders.", tenant_id)
                return {'status': 'DISABLED', 'tenant_id': str(tenant_id), 'reason': 'AI voice reminder calling is disabled in trial policy'}

            offset_minutes = getattr(policy, 'ai_calling_offset_minutes', 1440) or 1440
            skip_confirmed = getattr(policy, 'ai_calling_skip_confirmed', False) or False

            now = timezone.now()
            target_time = now + timedelta(minutes=offset_minutes)
            start_window = target_time - timedelta(minutes=60)
            end_window = target_time + timedelta(minutes=60)

            status_filters = ['PENDING'] if skip_confirmed else ['CONFIRMED', 'PENDING']

            trials = TrialBooking.objects.using(db_alias).filter(
                status='CONFIRMED',
                confirmation_status__in=status_filters,
                scheduled_start__gte=start_window,
                scheduled_start__lte=end_window,
            ).select_related('lead', 'branch', 'lead__organization')

            dispatched = 0
            for tb in trials:
                lead = tb.lead
                if not lead or not lead.phone_normalized:
                    continue

                idempotency_key = f"VOICE_REMINDER_TRIAL_{tb.id}"
                try:
                    VoiceCallingService.initiate_outbound_call(
                        lead=lead,
                        actor_user=None,
                        idempotency_key=idempotency_key,
                        custom_metadata={
                            'source': 'TRIAL_REMINDER_24H',
                            'trial_id': str(tb.id),
                            'scheduled_start': tb.scheduled_start.isoformat(),
                        },
                        db_alias=db_alias,
                    )
                    dispatched += 1
                except Exception as call_err:
                    logger.warning("Voice reminder call could not be placed for trial %s: %s", tb.id, call_err)

            return {
                'status': 'COMPLETED',
                'tenant_id': str(tenant_id),
                'dispatched_count': dispatched,
            }
    except Exception as exc:
        logger.error("Error in process_trial_voice_reminders_task for tenant %s: %s", tenant_id, exc)
        return {'status': 'FAILED', 'error': str(exc)}
