"""
apps/tenant_core/services_telephony_fallback.py

Inbound overflow chain (/telephony/dial-fallback/) and call close (/telephony/hangup/).

Views stay thin: they parse the request, call a function here, publish the returned
Redis events AFTER the database lock is released, and return the XML.

Idempotency:
- Vobiz retries a callback up to 3 times on non-200, so handlers must tolerate repeats.
- Each overflow Dial `action` URL carries ``attempt=N`` where N == len(tried_reps) when the
  URL was issued. A repeat of the last request replays the same XML with no side effects.
  Older repeats return an empty <Response/>. A missing ``attempt`` is a call that was
  already in flight before this deploy; the stale check is skipped for it.
- The CallSession row is locked with select_for_update inside transaction.atomic.
- ``ended_at`` is the closed marker. Once set, every later callback is a no-op.
"""

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from apps.tenant_core.communication.adapters.voice_vobiz import VobizVoiceAdapter
from apps.tenant_core.models_crm import AgentPresence, CallSession, LeadActivity
from apps.tenant_core.services_telephony_config import TelephonyConfigService

logger = logging.getLogger(__name__)

DIAL_RESULT_EVENT = 'dial'
MAX_CLAIM_RETRIES = 3
FIRST_OVERFLOW_WHISPER = "Please stay on the line while we connect you to our senior consultant."

_XML_HEAD = '<?xml version="1.0" encoding="UTF-8"?>'
HANGUP_XML = f'{_XML_HEAD}<Response><Hangup/></Response>'
EMPTY_XML = f'{_XML_HEAD}<Response/>'
EXHAUSTED_XML = f"""{_XML_HEAD}
<Response>
  <Speak voice="WOMAN" language="en-IN">All consultants are currently assisting members. We have scheduled an immediate callback for you. Thank you for calling SWEAT.</Speak>
  <Hangup/>
</Response>"""

Event = tuple[str, str, dict[str, Any]]  # (user_id, event_type, payload)


@dataclass
class FallbackOutcome:
    xml: str
    events: list[Event] = field(default_factory=list)


@dataclass
class HangupOutcome:
    events: list[Event] = field(default_factory=list)
    archive_session_id: str = ''
    archive_recording_url: str = ''


def parse_attempt(raw: str | None) -> int | None:
    """Parses the ``attempt`` query value. None means a legacy in-flight call."""
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _find_locked(alias: str, *, session_id: str | None, call_uuid: str, uuid_first: bool) -> CallSession | None:
    """Returns the CallSession row locked FOR UPDATE. Must run inside transaction.atomic(using=alias)."""
    qs = CallSession.objects.using(alias).select_for_update()

    def by_uuid() -> CallSession | None:
        return qs.filter(provider_attempt_id=call_uuid).first() if call_uuid else None

    def by_id() -> CallSession | None:
        try:
            parsed = uuid.UUID(str(session_id))
        except ValueError:
            return None
        return qs.filter(id=parsed).first()

    first, second = (by_uuid, by_id) if uuid_first else (by_id, by_uuid)
    return first() or second()


def _release_if_assigned(alias: str, user_id: str | None, session_id: Any) -> None:
    """Frees a rep only while they are still assigned to THIS session (never clobbers a newer call)."""
    if not user_id:
        return
    AgentPresence.objects.using(alias).filter(
        user_id=user_id, active_session_id=session_id,
    ).update(status='AVAILABLE', active_session=None)


def _close_missed(alias: str, session: CallSession, tried: list[str], reason: str, call_uuid: str,
                  events: list[Event]) -> None:
    """Closes an inbound call nobody answered. Writes the LeadActivity once and queues MISSED_CALL."""
    session.status = 'NO_ANSWER'
    session.hangup_cause = reason
    session.ended_at = timezone.now()
    session.tried_reps = tried
    session.disposition_notes = (
        f"MISSED: Inbound call from {session.user_phone_number}. No reps were available to answer."
    )
    session.save(update_fields=['status', 'hangup_cause', 'ended_at', 'tried_reps', 'disposition_notes'])
    if session.lead_id:
        LeadActivity.objects.using(alias).create(
            lead=session.lead,
            activity_type='CALL',
            outcome='MISSED',
            notes=f"Missed inbound call from {session.user_phone_number}. No reps were available to answer.",
            external_reference=str(session.id),
        )
    org_user_ids = AgentPresence.objects.using(alias).filter(
        organization_id=session.organization_id,
    ).values_list('user_id', flat=True)
    for user_id in org_user_ids:
        events.append((str(user_id), 'MISSED_CALL', {
            'session_id': str(session.id),
            'call_uuid': call_uuid,
            'caller_phone': session.user_phone_number,
        }))


def _overflow_xml(session: CallSession, presence: AgentPresence, host: str, next_attempt: int,
                  whisper: str | None) -> str:
    vobiz_cfg = TelephonyConfigService.get_vobiz_config()
    registrar_host = vobiz_cfg.get('registrar_host') or 'registrar.vobiz.ai'
    session_caller_id = (
        session.agent_phone_number
        if session.agent_phone_number and str(session.agent_phone_number).startswith('+') else ''
    )
    return VobizVoiceAdapter.build_fallback_overflow_xml(
        secondary_sip_uri=f"sip:{presence.sip_username}@{registrar_host}",
        caller_id=session_caller_id or vobiz_cfg.get('from_number') or '',
        complete_action_url=f"{host}/api/v1/telephony/dial-fallback/?session_id={session.id}&attempt={next_attempt}",
        dial_events_url=f"{host}/api/v1/telephony/dial-events/?session_id={session.id}",
        whisper_prompt=whisper,
        timeout=int(vobiz_cfg.get('ring_timeout_sec') or 20),
        registrar_host=registrar_host,
    )


def _claim_next_rep(alias: str, session: CallSession, excluded: list[str]) -> AgentPresence | None:
    """
    Picks the next live, AVAILABLE rep not already tried, then claims them with a conditional
    UPDATE (AVAILABLE -> ON_CALL). If another call won the race, exclude that rep and re-select.
    """
    skip = set(excluded)
    for _ in range(MAX_CLAIM_RETRIES):
        candidate = TelephonyConfigService.get_available_agent(
            db_alias=alias, org=session.organization, exclude_user_ids=skip,
        )
        if candidate is None:
            return None
        claimed = AgentPresence.objects.using(alias).filter(
            pk=candidate.pk, status='AVAILABLE',
        ).update(status='ON_CALL', active_session=session, last_call_assigned_at=timezone.now())
        if claimed:
            return candidate
        skip.add(str(candidate.user_id))
    return None


def _popup_event(session: CallSession, call_uuid: str, user_id: str) -> Event:
    lead = session.lead
    caller_phone = session.user_phone_number or ''
    masked = f"+91-XXXXX-{caller_phone[-4:]}" if len(caller_phone) >= 4 else "+91-XXXXX-XXXX"
    return (user_id, 'INCOMING_CALL_POPUP', {
        'session_id': str(session.id),
        'call_uuid': call_uuid,
        'lead_id': str(lead.id) if lead else None,
        'lead_name': f"{lead.first_name} {lead.last_name or ''}".strip() if lead else "Member Call (Overflow)",
        'masked_phone': masked,
        'branch': lead.branch.name if (lead and lead.branch) else "General",
        'status': lead.current_status if lead else "OVERFLOW",
    })


def handle_dial_fallback(*, alias: str, session_id: str | None, call_uuid: str, dial_status: str,
                         attempt: int | None, host: str) -> FallbackOutcome:
    """
    Handles the Dial `action` result of an inbound ring (primary or overflow).

    Returns the XML to send and the Redis events to publish after the lock is released.
    """
    status = (dial_status or '').lower()
    with transaction.atomic(using=alias):
        session = _find_locked(alias, session_id=session_id, call_uuid=call_uuid, uuid_first=False)
        if session is None:
            logger.warning("dial-fallback: no session for session_id=%s call_uuid=%s", session_id, call_uuid)
            return FallbackOutcome(xml=EXHAUSTED_XML)
        if session.ended_at is not None:
            logger.info("dial-fallback: session %s already closed (%s); no-op", session.id, session.hangup_cause)
            return FallbackOutcome(xml=HANGUP_XML)
        if status in ('completed', 'answered'):
            # Closing is left to /hangup/, the end-of-call signal that carries Duration and recording.
            logger.info("dial-fallback: session %s Dial %s; waiting for hangup", session.id, status)
            return FallbackOutcome(xml=HANGUP_XML)

        tried = [str(u) for u in (session.tried_reps or [])]
        tried_count = len(tried)
        if attempt is not None and attempt != tried_count:
            if attempt == tried_count - 1 and session.sales_rep_id:
                presence = AgentPresence.objects.using(alias).filter(user_id=session.sales_rep_id).first()
                if presence is not None:
                    logger.info("dial-fallback: replaying attempt %s for session %s", attempt, session.id)
                    return FallbackOutcome(xml=_overflow_xml(
                        session, presence, host, tried_count,
                        FIRST_OVERFLOW_WHISPER if attempt == 0 else None,
                    ))
            logger.info("dial-fallback: stale attempt %s (expected %s) for session %s", attempt, tried_count, session.id)
            return FallbackOutcome(xml=EMPTY_XML)

        events: list[Event] = []
        failed_user = str(session.sales_rep_id) if session.sales_rep_id else None
        if failed_user and failed_user not in tried:
            tried.append(failed_user)
        _release_if_assigned(alias, failed_user, session.id)

        if status == 'cancel':
            logger.info("dial-fallback: caller cancelled session %s; chain stopped", session.id)
            _close_missed(alias, session, tried, 'CALLER_CANCELLED', call_uuid, events)
            return FallbackOutcome(xml=HANGUP_XML, events=events)

        max_attempts = int(getattr(settings, 'TELEPHONY_MAX_OVERFLOW_ATTEMPTS_AFTER_PRIMARY', 3))
        if tried_count >= max_attempts:
            logger.warning(
                "dial-fallback: session %s hit max overflow attempts (%s) after primary; tried=%s",
                session.id, max_attempts, tried,
            )
            _close_missed(alias, session, tried, 'OVERFLOW_MAX_ATTEMPTS', call_uuid, events)
            return FallbackOutcome(xml=EXHAUSTED_XML, events=events)

        presence = _claim_next_rep(alias, session, tried)
        if presence is None:
            logger.warning("dial-fallback: session %s has no available rep left; tried=%s", session.id, tried)
            _close_missed(alias, session, tried, 'OVERFLOW_NO_AGENT', call_uuid, events)
            return FallbackOutcome(xml=EXHAUSTED_XML, events=events)

        session.sales_rep = presence.user
        session.tried_reps = tried
        session.save(update_fields=['sales_rep', 'tried_reps'])
        events.append(_popup_event(session, call_uuid, str(presence.user_id)))
        xml = _overflow_xml(
            session, presence, host, len(tried),
            FIRST_OVERFLOW_WHISPER if tried_count == 0 else None,
        )
        return FallbackOutcome(xml=xml, events=events)


def _presence_still_on(alias: str, session: CallSession):
    """Presence rows of the session's rep that are free or still bound to this session."""
    return AgentPresence.objects.using(alias).filter(user_id=session.sales_rep_id).filter(
        models.Q(active_session__isnull=True) | models.Q(active_session_id=session.id),
    )


def _backfill_closed(session: CallSession, duration_raw: float, recording_url: str,
                     outcome: HangupOutcome) -> None:
    """A repeat or late hangup for a closed call only fills in missing data. No events, no presence change."""
    fields: list[str] = []
    if duration_raw > (session.duration_seconds or 0):
        session.duration_seconds = duration_raw
        session.bill_duration_seconds = VobizVoiceAdapter.calculate_billable_duration(duration_raw)
        fields += ['duration_seconds', 'bill_duration_seconds']
        if session.status == 'COMPLETED' and duration_raw >= 30.0 and not session.is_meaningful:
            session.is_meaningful = True
            fields.append('is_meaningful')
    if recording_url and not session.recording_url:
        session.recording_url = recording_url
        fields.append('recording_url')
        outcome.archive_session_id = str(session.id)
        outcome.archive_recording_url = recording_url
    if fields:
        session.save(update_fields=fields)


def apply_hangup(*, alias: str, session_id: str | None, call_uuid: str, duration_raw: float,
                 recording_url: str, hangup_cause: str, dial_result: bool, dial_status: str) -> HangupOutcome:
    """
    Closes a call from a /hangup/ hit. ``dial_result`` is True when the hit came from a Dial
    `action` URL (marked ``event=dial``); False for the call-level hangup.

    A Dial result closes the call too: if Vobiz sends no call-level hangup for a call, the
    Dial result is the only close signal. A late call-level hangup then only back-fills.
    """
    outcome = HangupOutcome()
    with transaction.atomic(using=alias):
        session = _find_locked(alias, session_id=session_id, call_uuid=call_uuid, uuid_first=True)
        if session is None:
            return outcome
        if session.ended_at is not None:
            _backfill_closed(session, duration_raw, recording_url, outcome)
            return outcome

        bill_duration = VobizVoiceAdapter.calculate_billable_duration(duration_raw)
        if dial_result:
            # Answered only if the customer leg completed AND either reported time or
            # /dial-events/ already saw it connect. busy / no-answer / failed / cancel close as unanswered.
            was_answered = dial_status == 'completed' and (duration_raw > 0 or session.status == 'CONNECTED')
        else:
            # Caller-leg Duration includes ring time. An INBOUND call only counts as answered if
            # /dial-events/ saw the agent leg answer, or an earlier close already marked it COMPLETED.
            was_answered = duration_raw > 0 and (
                session.direction != 'INBOUND' or session.status in ('CONNECTED', 'COMPLETED')
            )
        is_meaningful = was_answered and duration_raw >= 30.0

        session.duration_seconds = duration_raw
        session.bill_duration_seconds = bill_duration
        session.is_meaningful = is_meaningful
        session.recording_url = recording_url
        session.hangup_cause = hangup_cause
        session.status = 'COMPLETED' if was_answered else 'NO_ANSWER'
        session.ended_at = timezone.now()
        session.save(update_fields=[
            'duration_seconds', 'bill_duration_seconds', 'is_meaningful',
            'recording_url', 'hangup_cause', 'status', 'ended_at',
        ])
        if recording_url:
            outcome.archive_session_id = str(session.id)
            outcome.archive_recording_url = recording_url

        if session.sales_rep_id:
            rep_id = str(session.sales_rep_id)
            presence = _presence_still_on(alias, session)
            if was_answered and not session.disposition:
                presence.update(status='WRAP_UP', active_session=session)
                lead = session.lead
                outcome.events.append((rep_id, 'CALL_ENDED', {
                    'session_id': str(session.id),
                    'call_uuid': call_uuid,
                    'duration_seconds': int(duration_raw),
                    'bill_duration_seconds': bill_duration,
                    'is_meaningful': is_meaningful,
                    'direction': session.direction,
                    'caller_phone': session.user_phone_number or '',
                    'lead_id': str(lead.id) if lead else None,
                    'lead_name': f"{lead.first_name} {lead.last_name or ''}".strip() if lead else (session.user_phone_number or "Customer"),
                    'recording_url': recording_url,
                }))
            else:
                # Disposition already submitted from the browser, or the call was never answered.
                presence.update(status='AVAILABLE', active_session=None)
                if not was_answered and session.direction == 'INBOUND':
                    outcome.events.append((rep_id, 'MISSED_CALL', {
                        'session_id': str(session.id),
                        'call_uuid': call_uuid,
                        'caller_phone': session.user_phone_number,
                    }))
    return outcome
