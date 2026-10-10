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
        Validates lead, enforces consent, prevents duplicate calls,
        and submits the verified call to Sarvam Instant Outbound API.
        """
        alias = db_alias or 'default'
        org = lead.organization

        # 1. Phone validation
        phone_val = lead.phone_normalized or getattr(lead, 'phone', '')
        recipient_phone = cls.normalize_phone(phone_val)
        if not recipient_phone or not PHONE_REGEX.match(recipient_phone):
            raise ValueError(f"Lead {lead.id} has invalid recipient phone number: '{recipient_phone}'")

        # 2. Consent & Suppression Check
        if getattr(lead, 'do_not_contact', False):
            raise ValueError(f"Outbound call rejected: Lead {lead.id} is marked Do Not Contact.")
        if getattr(lead, 'is_opted_out', False):
            raise ValueError(f"Outbound call rejected: Lead {lead.id} has opted out of communications.")

        # 3. Idempotency Check
        if idempotency_key:
            existing = CallSession.objects.using(alias).filter(
                organization=org,
                idempotency_key=idempotency_key,
            ).first()
            if existing:
                return existing

        # 4. Duplicate Active Call Check (within last 15 minutes)
        cutoff = timezone.now() - timedelta(minutes=15)
        active_call = CallSession.objects.using(alias).filter(
            organization=org,
            lead=lead,
            status__in=['QUEUED', 'INITIATED', 'RINGING', 'CONNECTED'],
            created_at__gte=cutoff,
        ).first()
        if active_call:
            logger.warning("Active call session %s already in progress for lead %s", active_call.id, lead.id)
            return active_call

        # 5. Resolve Provider Adapter for Voice
        adapter, integration = CommunicationProviderRegistry.resolve_for_tenant('VOICE', provider_preference='SARVAM')

        if not adapter or not adapter.is_configured():
            # Create FAILED CallSession record for auditability
            session = CallSession.objects.using(alias).create(
                organization=org,
                lead=lead,
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

        # 6. Create CallSession in INITIATED state atomically
        with transaction.atomic(using=alias):
            session = CallSession.objects.using(alias).create(
                organization=org,
                lead=lead,
                provider='SARVAM',
                status='INITIATED',
                direction='OUTBOUND',
                agent_phone_number=agent_phone,
                user_phone_number=recipient_phone,
                started_at=timezone.now(),
                idempotency_key=idempotency_key,
                metadata=custom_metadata or {},
            )

        # 7. Construct Callback Webhook URL and Metadata
        public_id = adapter.config.get('public_integration_id') or getattr(org, 'code', None) or (str(integration.id) if integration else 'sarvam')
        webhook_base = adapter.config.get('webhook_base_url') or 'https://api.fitness.vibecopilot.ai'
        webhook_token = adapter.config.get('webhook_token')
        webhook_url = f"{webhook_base.rstrip('/')}/api/v1/webhooks/communications/sarvam/{public_id}/"
        if webhook_token:
            webhook_url += f"?token={webhook_token}"

        call_metadata = {
            'lead_id': str(lead.id),
            'call_session_id': str(session.id),
            **(custom_metadata or {}),
        }

        # 8. Submit to Sarvam Instant Outbound API
        result = adapter.create_instant_outbound_call(
            recipient_phone=recipient_phone,
            webhook_url=webhook_url,
            metadata=call_metadata,
            timeout=timeout,
        )

        with transaction.atomic(using=alias):
            if result.success and result.provider_message_id:
                session.provider_attempt_id = result.provider_message_id
                session.status = 'INITIATED'
                session.save(using=alias, update_fields=['provider_attempt_id', 'status', 'updated_at'])
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
        and out-of-order progression protection.
        """
        alias = db_alias or 'default'
        attempt_id = payload.get('attempt_id')
        raw_status = (payload.get('status') or '').strip().lower()
        meta = payload.get('metadata') or {}
        call_session_id = meta.get('call_session_id')

        # 1. Match session by session ID or attempt ID
        session = None
        if call_session_id:
            session = CallSession.objects.using(alias).filter(
                organization=organization,
                id=call_session_id,
            ).first()

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

        # Do not regress terminal status
        if current_rank >= 50 and new_rank < current_rank:
            logger.info("Ignoring out-of-order status %s for already terminal session %s", mapped_status, session.id)
            return {'status': 'ignored_out_of_order', 'call_session_id': str(session.id)}

        # 3. Update Session Record
        with transaction.atomic(using=alias):
            session.status = mapped_status
            if attempt_id and not session.provider_attempt_id:
                session.provider_attempt_id = attempt_id

            if 'duration' in payload:
                try:
                    session.duration_seconds = float(payload['duration'])
                except (ValueError, TypeError):
                    pass

            if 'interaction_transcript' in payload:
                session.transcript = payload['interaction_transcript'] or []

            if 'final_agent_variables' in payload:
                session.final_agent_variables = payload['final_agent_variables'] or {}

            session.ended_at = timezone.now()
            session.metadata.update(meta)
            session.save(using=alias, update_fields=[
                'status', 'provider_attempt_id', 'duration_seconds',
                'transcript', 'final_agent_variables', 'ended_at',
                'metadata', 'updated_at'
            ])

            # 4. Lead Lifecycle Progression (Connected -> Advance status if NEW_LEAD)
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
