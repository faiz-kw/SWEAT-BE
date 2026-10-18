"""
apps/tenant_core/services_voice_calling.py — Authoritative Voice Calling & Sarvam AI Service.

Handles:
- Lead eligibility & consent validation
- Duplicate active call prevention
- Deterministic client idempotency
- Submission to Sarvam Voice Agents Instant Outbound API
- Idempotent, out-of-order-safe webhook ingestion
- Lead stage advancement upon connected call
"""

import re
import logging
import zoneinfo
from typing import Dict, Any, Optional
from datetime import timedelta
from django.utils import timezone
from django.db import transaction
from django.db.models import Q

from apps.tenant_core.models_org import Organization
from apps.tenant_core.models_crm import Lead, CallSession
from apps.tenant_core.communication.registry import CommunicationProviderRegistry
from apps.tenant_core.communication.adapters.voice_sarvam import SarvamVoiceAdapter

logger = logging.getLogger(__name__)

STATUS_HIERARCHY = {
    'QUEUED': 10,
    'INITIATED': 20,
    'RINGING': 30,
    'CONNECTED': 40,
    'COMPLETED': 50,
    'BUSY': 50,
    'NO_ANSWER': 50,
    'FAILED': 50,
    'CANCELLED': 50,
}

PHONE_REGEX = re.compile(r'^\+?[1-9]\d{7,14}$')


class VoiceCallingService:
    """
    Authoritative service for managing voice call sessions and Sarvam Voice Agent dispatches.
    """

    @classmethod
    def normalize_phone(cls, phone: Optional[str]) -> str:
        """Sanitizes phone number into E.164 compliant string."""
        if not phone:
            return ''
        clean = re.sub(r'[\s\-\(\)]', '', phone.strip())
        if clean.startswith('00'):
            clean = '+' + clean[2:]
        elif not clean.startswith('+'):
            # Default to India +91 if 10-digit mobile number provided
            if len(clean) == 10 and clean[0] in '6789':
                clean = '+91' + clean
            else:
                clean = '+' + clean
        return clean

    @classmethod
    def get_lead_voice_consent_status(
        cls,
        lead: Lead,
        db_alias: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Determines the authoritative voice consent state for a lead for UI visibility and pre-flight checks.
        Never infers voice consent from SMS, WhatsApp, or Email.
        Returns:
            {
                'status': 'GRANTED' | 'WITHDRAWN' | 'MISSING' | 'UNKNOWN',
                'is_eligible': bool,
                'reason': str,
                'source': Optional[str],
                'recorded_at': Optional[str],
                'method': Optional[str],
            }
        """
        if hasattr(lead, '_cached_voice_consent'):
            return lead._cached_voice_consent

        alias = db_alias or getattr(getattr(lead, '_state', None), 'db', None) or 'default'

        # 1. Do Not Contact suppression check
        if getattr(lead, 'do_not_contact', False):
            res = {
                'status': 'WITHDRAWN' if getattr(lead, 'consent_voice', None) is False else 'MISSING',
                'is_eligible': False,
                'reason': f"Lead {lead.id} is marked Do Not Contact.",
                'source': None,
                'recorded_at': None,
                'method': None,
            }
            lead._cached_voice_consent = res
            return res

        # 2. General CRM communication opt-out check
        if getattr(lead, 'is_opted_out', False):
            res = {
                'status': 'WITHDRAWN',
                'is_eligible': False,
                'reason': f"Lead {lead.id} has opted out of communications.",
                'source': None,
                'recorded_at': None,
                'method': None,
            }
            lead._cached_voice_consent = res
            return res

        # 3. Check for explicit voice consent withdrawal in compliance ConsentRecord table
        try:
            from .models_privacy import ConsentRecord
            latest_record = ConsentRecord.objects.using(alias).filter(
                subject_type='lead',
                subject_id=lead.id,
                purpose__code__in=['VOICE_CALL', 'COMMUNICATION_VOICE', 'MARKETING_VOICE', 'AI_VOICE'],
            ).order_by('-recorded_at', '-created_at').first()

            if latest_record:
                if latest_record.status == 'WITHDRAWN':
                    rec_time = latest_record.recorded_at or latest_record.withdrawn_at or latest_record.created_at
                    res = {
                        'status': 'WITHDRAWN',
                        'is_eligible': False,
                        'reason': f"Lead {lead.id} has explicitly withdrawn voice calling consent.",
                        'source': latest_record.capture_source or latest_record.consent_method or 'CONSENT_RECORD',
                        'recorded_at': rec_time.isoformat() if rec_time else None,
                        'method': 'DATABASE_CONSENT_RECORD',
                    }
                    lead._cached_voice_consent = res
                    return res
                elif latest_record.status == 'GRANTED':
                    rec_time = latest_record.recorded_at or latest_record.granted_at or latest_record.created_at
                    res = {
                        'status': 'GRANTED',
                        'is_eligible': True,
                        'reason': 'Explicit voice calling consent grant verified.',
                        'source': latest_record.capture_source or latest_record.consent_method or 'CONSENT_RECORD',
                        'recorded_at': rec_time.isoformat() if rec_time else None,
                        'method': 'DATABASE_CONSENT_RECORD',
                    }
                    lead._cached_voice_consent = res
                    return res
        except Exception as priv_err:
            logger.debug("ConsentRecord check error in get_lead_voice_consent_status: %s", priv_err)
            res = {
                'status': 'UNKNOWN',
                'is_eligible': False,
                'reason': 'Voice consent record could not be verified due to an internal query error.',
                'source': None,
                'recorded_at': None,
                'method': None,
            }
            lead._cached_voice_consent = res
            return res

        # 4. Explicit voice denial check on lead
        if getattr(lead, 'consent_voice', None) is False:
            res = {
                'status': 'WITHDRAWN',
                'is_eligible': False,
                'reason': f"Lead {lead.id} has opted out of voice communications.",
                'source': getattr(lead, 'consent_voice_source', None),
                'recorded_at': (
                    getattr(lead, 'consent_voice_recorded_at', None).isoformat()
                    if getattr(lead, 'consent_voice_recorded_at', None)
                    else None
                ),
                'method': 'LEAD_MODEL_FIELD',
            }
            lead._cached_voice_consent = res
            return res

        # 5. Persisted lead model boolean
        if getattr(lead, 'consent_voice', None) is True:
            rec_at = getattr(lead, 'consent_voice_recorded_at', None)
            res = {
                'status': 'GRANTED',
                'is_eligible': True,
                'reason': 'Explicit voice calling consent verified on lead record.',
                'source': getattr(lead, 'consent_voice_source', None) or 'LEAD_MODEL',
                'recorded_at': rec_at.isoformat() if rec_at else timezone.now().isoformat(),
                'method': 'LEAD_MODEL_FIELD',
            }
            lead._cached_voice_consent = res
            return res

        # 6. Fallback: Missing consent (SMS, WhatsApp, Email consent never authorize voice calls)
        res = {
            'status': 'MISSING',
            'is_eligible': False,
            'reason': f"Lead {lead.id} does not have an explicit, verified voice-calling consent grant.",
            'source': None,
            'recorded_at': None,
            'method': None,
        }
        lead._cached_voice_consent = res
        return res

    @classmethod
    def _check_calling_hours(cls) -> None:
        """
        Enforces outbound voice calling hours in IST (Asia/Kolkata).

        Blocks calls outside the configured window. Default: 09:00–21:00 IST, every day.
        Override via env:
            VOICE_CALLING_HOURS_START  (int, 0-23, default 9)
            VOICE_CALLING_HOURS_END    (int, 0-23, default 21)

        Bypass in tests/dev with: VOICE_CALLING_ENFORCE_HOURS=false
        """
        from django.conf import settings
        if not getattr(settings, 'VOICE_CALLING_ENFORCE_HOURS', True):
            return

        IST = zoneinfo.ZoneInfo('Asia/Kolkata')
        now_ist = timezone.now().astimezone(IST)
        current_hour = now_ist.hour

        start_hour = getattr(settings, 'VOICE_CALLING_HOURS_START', 9)
        end_hour = getattr(settings, 'VOICE_CALLING_HOURS_END', 21)

        if not (start_hour <= current_hour < end_hour):
            raise ValueError(
                f"Outbound call rejected: AI voice calls are only permitted between "
                f"{start_hour:02d}:00 and {end_hour:02d}:00 IST. "
                f"Current time is {now_ist.strftime('%H:%M IST')}."
            )

    @classmethod
    def verify_lead_calling_eligibility(
        cls,
        lead: Lead,
        db_alias: Optional[str] = None,
        custom_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Validates calling hours (IST), lead suppression, Do Not Contact, and trusted voice consent.
        Fails closed. Uses authoritative get_lead_voice_consent_status logic.
        """
        alias = db_alias or 'default'

        # 0. Calling hours gate (IST 9am-9pm) — fail closed
        cls._check_calling_hours()

        # 1. Check explicit invocation metadata denial
        if custom_metadata and custom_metadata.get('has_voice_consent') is False:
            raise ValueError(f"Outbound call rejected: Explicit voice calling consent was denied for lead {lead.id}.")

        consent_info = cls.get_lead_voice_consent_status(lead, db_alias=alias)

        if not consent_info['is_eligible'] or consent_info['status'] != 'GRANTED':
            raise ValueError(f"Outbound call rejected: {consent_info['reason']}")

        return {
            'voice_consent_verified': True,
            'voice_consent_source': consent_info['source'] or '',
            'voice_consent_recorded_at': consent_info['recorded_at'],
            'voice_consent_method': consent_info['method'] or '',
        }

    @classmethod
    def initiate_outbound_call(
        cls,
        lead: Lead,
        actor_user=None,
        idempotency_key: Optional[str] = None,
        custom_metadata: Optional[Dict[str, Any]] = None,
        db_alias: Optional[str] = None,
        timeout: int = 15,
    ) -> CallSession:
        """
        Validates lead, enforces trusted consent, prevents duplicate calls using row-level locking,
        and submits the verified call to Sarvam Instant Outbound API.
        
        Concurrency Safety:
        Serializes checks and session creation within a database transaction holding
        select_for_update() on the Lead row. Does NOT hold the row lock during external HTTP I/O.
        """
        alias = db_alias or 'default'
        org = lead.organization

        # 0. Global Outbound Safety Gates (Fail-closed)
        from django.conf import settings
        if not getattr(settings, 'COMMUNICATIONS_OUTBOUND_ENABLED', False):
            raise ValueError('Outbound communications are disabled in this deployment.')
        if not getattr(settings, 'VOICE_CALLING_ENABLED', False):
            raise ValueError('Outbound voice calling is disabled in this deployment.')

        # 1. Acquire row lock on Lead within atomic block to serialize concurrent call requests
        with transaction.atomic(using=alias):
            locked_lead = Lead.objects.using(alias).select_for_update().get(id=lead.id)

            # Phone validation
            phone_val = locked_lead.phone_normalized or getattr(locked_lead, 'phone', '')
            recipient_phone = cls.normalize_phone(phone_val)
            if not recipient_phone or not PHONE_REGEX.match(recipient_phone):
                raise ValueError(f"Lead {locked_lead.id} has invalid recipient phone number: '{recipient_phone}'")

            # Consent & Suppression Check (Fail-closed; extracts verifiable provenance from trusted storage)
            consent_provenance = cls.verify_lead_calling_eligibility(
                locked_lead,
                db_alias=alias,
                custom_metadata=custom_metadata,
            )

            # Sanitize caller-supplied custom_metadata to prevent spoofing or tampering with consent provenance
            safe_metadata = {}
            if custom_metadata:
                disallowed_prefixes = ('voice_consent_', 'consent_')
                disallowed_exact = {
                    'has_voice_consent',
                    'voice_consent_verified',
                    'voice_consent_source',
                    'voice_consent_recorded_at',
                    'voice_consent_method',
                    'consent_voice',
                }
                for k, v in custom_metadata.items():
                    k_clean = str(k).strip()
                    k_lower = k_clean.lower()
                    if (
                        k_lower not in disallowed_exact
                        and not any(k_lower.startswith(pfx) for pfx in disallowed_prefixes)
                    ):
                        safe_metadata[k] = v
            safe_metadata.update(consent_provenance)
            custom_metadata = safe_metadata

            # Idempotency Check inside lock
            if idempotency_key:
                existing = CallSession.objects.using(alias).filter(
                    organization=org,
                    idempotency_key=idempotency_key,
                ).first()
                if existing:
                    return existing

            # Duplicate Active Call Check (within last 15 minutes) inside lock
            cutoff = timezone.now() - timedelta(minutes=15)
            active_call = CallSession.objects.using(alias).filter(
                organization=org,
                lead=locked_lead,
                status__in=['QUEUED', 'INITIATED', 'RINGING', 'CONNECTED'],
                created_at__gte=cutoff,
            ).first()
            if active_call:
                logger.warning("Active call session %s already in progress for lead %s", active_call.id, locked_lead.id)
                return active_call

            # Resolve Provider Adapter for Voice
            adapter, integration = CommunicationProviderRegistry.resolve_for_tenant('VOICE', provider_preference='SARVAM')

            if not adapter or not adapter.is_configured():
                session = CallSession.objects.using(alias).create(
                    organization=org,
                    lead=locked_lead,
                    provider='SARVAM',
                    status='FAILED',
                    direction='OUTBOUND',
                    user_phone_number=recipient_phone,
                    idempotency_key=idempotency_key,
                    error_code='UNCONFIGURED',
                    error_message='Sarvam Voice provider is not configured or missing required parameters.',
                    metadata=custom_metadata or {},
                )
                return session

            agent_phone = adapter.config.get('agent_phone_number', '')

            # Create CallSession in INITIATED state while holding the lock
            session = CallSession.objects.using(alias).create(
                organization=org,
                lead=locked_lead,
                provider='SARVAM',
                status='INITIATED',
                direction='OUTBOUND',
                agent_phone_number=agent_phone,
                user_phone_number=recipient_phone,
                started_at=timezone.now(),
                idempotency_key=idempotency_key,
                metadata=custom_metadata or {},
            )

        # 2. Database transaction is committed and row lock released BEFORE external HTTP request.
        # Construct Callback Webhook URL and Metadata
        public_id = adapter.config.get('public_integration_id') or getattr(org, 'code', None) or (str(integration.id) if integration else 'sarvam')
        webhook_base = adapter.config.get('webhook_base_url') or 'https://api.fitness.vibecopilot.ai'
        webhook_token = adapter.config.get('webhook_token')
        webhook_url = f"{webhook_base.rstrip('/')}/api/v1/webhooks/communications/sarvam/{public_id}/"
        if webhook_token:
            webhook_url += f"?token={webhook_token}"

        lead_full_name = f"{lead.first_name} {lead.last_name}".strip()
        call_metadata = {
            'lead_id': str(lead.id),
            'call_session_id': str(session.id),
            'lead_name': lead_full_name,
            'caller_name': lead.first_name,
            'name': lead.first_name,
            'phone': recipient_phone,
            'user_phone_number': recipient_phone,
            'program': lead.interested_program or '',
            'interested_program': lead.interested_program or '',
            'branch': lead.branch.name if lead.branch else '',
            'branch_name': lead.branch.name if lead.branch else '',
            'fitness_goal': lead.fitness_goal or '',
            **(custom_metadata or {}),
        }

        # 3. Submit to Sarvam Instant Outbound API (Outside DB lock)
        result = adapter.create_instant_outbound_call(
            recipient_phone=recipient_phone,
            webhook_url=webhook_url,
            metadata=call_metadata,
            timeout=timeout,
        )

        # 4. Record Dispatch Result or Ambiguous Timeout
        with transaction.atomic(using=alias):
            if result.success and result.provider_message_id:
                session.provider_attempt_id = result.provider_message_id
                session.status = 'INITIATED'
                session.error_code = ''
                session.error_message = ''
                session.save(using=alias, update_fields=['provider_attempt_id', 'status', 'error_code', 'error_message', 'updated_at'])
            elif result.error_code == 'TIMEOUT':
                # CRITICAL SAFETY: Ambiguous provider timeout.
                # Do NOT mark as FAILED because Sarvam may have scheduled or placed the call.
                # Retain status as INITIATED (blocking duplicate calls) and flag TIMEOUT_UNCONFIRMED.
                session.status = 'INITIATED'
                session.error_code = 'TIMEOUT_UNCONFIRMED'
                session.error_message = result.error_message or f"Request to Sarvam timed out after {timeout}s; awaiting webhook reconciliation."
                session.save(using=alias, update_fields=['status', 'error_code', 'error_message', 'updated_at'])
            else:
                session.status = 'FAILED'
                session.error_code = result.error_code or 'SUBMISSION_FAILED'
                session.error_message = result.error_message or 'Failed to place call via Sarvam.'
                session.save(using=alias, update_fields=['status', 'error_code', 'error_message', 'updated_at'])

        return session

    @classmethod
    def record_call_webhook(
        cls,
        organization: Organization,
        payload: Dict[str, Any],
        db_alias: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Ingests post-call webhook callback from Sarvam with idempotency
        and strict terminal status overwrite protection.
        """
        alias = db_alias or 'default'
        attempt_id = payload.get('attempt_id')
        raw_status = str(payload.get('status') or '').strip().lower()
        # Per official Sarvam webhook schema, metadata is echoed inside webhook_config.metadata,
        # NOT at the top-level payload.metadata key (which does not exist in the Sarvam spec).
        # See: https://docs.sarvam.ai/voice-agents/api/instant-outbound/webhook-payload.md
        _wc = payload.get('webhook_config')
        meta = (_wc.get('metadata') or {}) if isinstance(_wc, dict) else {}
        if not isinstance(meta, dict):
            meta = {}
        call_session_id = meta.get('call_session_id')

        # 1. Match session by session ID or attempt ID
        session = None
        if call_session_id:
            try:
                import uuid
                valid_session_id = uuid.UUID(str(call_session_id).strip())
                session = CallSession.objects.using(alias).filter(
                    organization=organization,
                    id=valid_session_id,
                ).first()
            except (ValueError, TypeError, Exception):
                session = None

        if not session and attempt_id:
            session = CallSession.objects.using(alias).filter(
                organization=organization,
                provider_attempt_id=attempt_id,
            ).first()

        if not session:
            logger.warning("Unmatched Sarvam call webhook received. attempt_id=%s call_session_id=%s", attempt_id, call_session_id)
            return {'status': 'unmatched', 'attempt_id': attempt_id}

        # 2. Map Status & Out-of-Order Check
        mapped_status = SarvamVoiceAdapter.SARVAM_STATUS_MAP.get(
            raw_status,
            'COMPLETED' if raw_status == 'connected' else raw_status.upper()
        )

        current_rank = STATUS_HIERARCHY.get(session.status, 0)
        new_rank = STATUS_HIERARCHY.get(mapped_status, 0)

        # 3. Terminal Status Protection (current_rank >= 50)
        if current_rank >= 50:
            # Late non-terminal event (e.g. RINGING / CONNECTED arriving after COMPLETED/FAILED) -> ignore
            if new_rank < current_rank:
                logger.info("Ignoring out-of-order non-terminal status %s for terminal session %s (%s)", mapped_status, session.id, session.status)
                return {'status': 'ignored_out_of_order', 'call_session_id': str(session.id), 'call_status': session.status}

            # Conflicting terminal status (e.g. FAILED arriving when session is already COMPLETED) -> ignore conflict
            if mapped_status != session.status:
                logger.warning("Ignoring conflicting terminal status %s for already %s session %s", mapped_status, session.status, session.id)
                return {'status': 'ignored_terminal_conflict', 'call_session_id': str(session.id), 'call_status': session.status}

            # Duplicate identical terminal event (e.g. COMPLETED arriving again) -> idempotent update of missing fields only
            with transaction.atomic(using=alias):
                updated_fields = ['updated_at']
                if attempt_id and not session.provider_attempt_id:
                    session.provider_attempt_id = attempt_id
                    updated_fields.append('provider_attempt_id')

                if 'duration' in payload:
                    try:
                        new_dur = float(payload['duration'])
                        if new_dur > 0 and session.duration_seconds == 0:
                            session.duration_seconds = new_dur
                            updated_fields.append('duration_seconds')
                    except (ValueError, TypeError):
                        pass

                if payload.get('interaction_transcript') and not session.transcript:
                    session.transcript = payload['interaction_transcript']
                    updated_fields.append('transcript')

                if payload.get('final_agent_variables'):
                    session.final_agent_variables = {
                        **(session.final_agent_variables or {}),
                        **payload['final_agent_variables'],
                    }
                    updated_fields.append('final_agent_variables')

                session.save(using=alias, update_fields=updated_fields)
                return {'status': 'duplicate_terminal', 'call_session_id': str(session.id), 'call_status': session.status}

        # 4. Update Session Record (Transition to new status or reconcile uncertain timeout)
        with transaction.atomic(using=alias):
            session.status = mapped_status
            if attempt_id and not session.provider_attempt_id:
                session.provider_attempt_id = attempt_id

            # Reconcile uncertain timeout session if webhook confirmed call outcome
            if session.error_code == 'TIMEOUT_UNCONFIRMED':
                logger.info("Reconciling uncertain timeout session %s via webhook status %s", session.id, mapped_status)
                if mapped_status != 'FAILED':
                    session.error_code = ''
                    session.error_message = ''

            if 'duration' in payload:
                try:
                    session.duration_seconds = float(payload['duration'])
                except (ValueError, TypeError):
                    pass

            if 'interaction_transcript' in payload and payload['interaction_transcript']:
                session.transcript = payload['interaction_transcript']

            if 'final_agent_variables' in payload and payload['final_agent_variables']:
                session.final_agent_variables = {
                    **(session.final_agent_variables or {}),
                    **payload['final_agent_variables'],
                }

            session.ended_at = timezone.now()
            session.metadata.update(meta)
            session.save(using=alias, update_fields=[
                'status', 'provider_attempt_id', 'error_code', 'error_message',
                'duration_seconds', 'transcript', 'final_agent_variables',
                'ended_at', 'metadata', 'updated_at'
            ])

            # 5. Lead Lifecycle Progression (Connected -> Advance status if NEW_LEAD)
            if session.lead and mapped_status == 'COMPLETED':
                lead = session.lead
                if getattr(lead, 'current_status', None) == 'NEW_LEAD':
                    from apps.tenant_core.services_crm import CRMLeadService
                    try:
                        CRMLeadService.transition_lead_status(
                            lead=lead,
                            new_status='FOLLOW_UP_PENDING',
                            reason_code='AI_CALL_CONNECTED',
                            reason_text=f"AI voice call connected successfully ({int(session.duration_seconds)}s)",
                            actor_user=None,
                            db_alias=alias,
                        )
                    except Exception as e:
                        logger.warning("Failed to auto-advance lead %s status: %s", lead.id, e)

        return {
            'status': 'recorded',
            'call_session_id': str(session.id),
            'call_status': session.status,
            'duration': session.duration_seconds,
        }
