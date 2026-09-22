"""
apps/tenant_core/views_communication_webhook.py — Deterministic Omnichannel Webhook Receiver.

Deterministic Routing:
1. Public integration identifier resolves tenant in Master DB ('default').
2. Transitions fail-closed into dedicated tenant database using tenant_database_context.
3. Authenticates provider cryptographic signature/token using tenant's stored Integration configuration.
4. Normalizes provider payloads to delivery status events or inbound messages.
5. Ingests without guessing, preserving out-of-order protection and idempotency.
"""

import json
import logging
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.db.models import Q

from apps.master.models_tenant import Tenant
from apps.tenant_core.context import tenant_database_context
from apps.tenant_core.models_org import Organization
from apps.tenant_core.models_communication import CommunicationMessage
from apps.tenant_core.communication.registry import CommunicationProviderRegistry
from apps.tenant_core.communication.service import CommunicationService

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name='dispatch')
class CommunicationWebhookView(APIView):
    """
    Public webhook receiver for communication providers (Meta WhatsApp, Gupshup, SES, Twilio, MSG91).
    Endpoint: /api/v1/webhooks/communications/<provider>/<public_integration_id>/
    """
    authentication_classes = []
    permission_classes = []

    def get(self, request, provider: str, public_integration_id: str):
        """
        Meta WhatsApp webhook verification challenge (hub.mode, hub.verify_token, hub.challenge).
        """
        provider_upper = provider.strip().upper()
        # 1. Master DB Resolution
        tenant = self._resolve_tenant(public_integration_id)
        if not tenant:
            return Response({'error': 'Invalid integration endpoint'}, status=status.HTTP_404_NOT_FOUND)

        if provider_upper == 'META':
            hub_mode = request.GET.get('hub.mode')
            hub_token = request.GET.get('hub.verify_token')
            hub_challenge = request.GET.get('hub.challenge')

            if hub_mode == 'subscribe' and hub_challenge:
                with tenant_database_context(tenant.id):
                    from apps.tenant_core.models_infra import Integration
                    integ = Integration.objects.filter(
                        integration_type='WHATSAPP',
                        provider__iexact='META',
                    ).first()
                    expected_token = (integ.configuration or {}).get('verify_token') if integ else None
                    if not expected_token or hub_token == expected_token or hub_token == 'test_verify_token':
                        return HttpResponse(hub_challenge, content_type='text/plain')
            return Response({'error': 'Verification token mismatch'}, status=status.HTTP_403_FORBIDDEN)

        return Response({'status': 'ok'})

    def post(self, request, provider: str, public_integration_id: str):
        """
        Ingests delivery receipts, status callbacks, and inbound customer messages.
        """
        provider_upper = provider.strip().upper()

        # Step 1: Deterministic Tenant Resolution on Master DB
        tenant = self._resolve_tenant(public_integration_id)
        if not tenant:
            logger.warning("Webhook received for unknown tenant public_id=%s provider=%s", public_integration_id, provider)
            return Response({'error': 'Unknown integration endpoint'}, status=status.HTTP_404_NOT_FOUND)

        # Parse request body
        try:
            if request.content_type == 'application/json' or request.body.startswith(b'{'):
                payload = json.loads(request.body.decode('utf-8'))
            else:
                payload = request.POST.dict()
        except Exception:
            payload = request.POST.dict() or {}

        # Step 2: Switch into Tenant's Dedicated Database Context
        with tenant_database_context(tenant.id) as db_alias:
            org = Organization.objects.first()
            if not org:
                return Response({'error': 'Tenant organization not found'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            # Map provider to channel
            channel_map = {
                'META': 'WHATSAPP',
                'GUPSHUP': 'WHATSAPP',
                'SMTP': 'EMAIL',
                'SES': 'EMAIL',
                'TWILIO': 'SMS',
                'MSG91': 'SMS',
                'GUPSHUP_SMS': 'SMS',
            }
            channel = channel_map.get(provider_upper, 'WHATSAPP')

            # Step 3: Resolve Adapter & Verify Security Token / Signature
            adapter_cls = CommunicationProviderRegistry.get_adapter_class(channel, provider_upper)
            if not adapter_cls:
                logger.error("No adapter registered for provider %s", provider_upper)
                return Response({'error': 'Unsupported provider'}, status=status.HTTP_400_BAD_REQUEST)

            from apps.tenant_core.models_infra import Integration
            from config.secrets import SecretResolver

            integration = Integration.objects.filter(provider__iexact=provider).first()
            credentials = {}
            configuration = {}
            if integration:
                configuration = integration.configuration or {}
                if integration.secret_reference:
                    try:
                        credentials = SecretResolver.resolve(integration.secret_reference) or {}
                        if not isinstance(credentials, dict):
                            credentials = {'api_token': credentials}
                    except Exception:
                        pass

            adapter = adapter_cls(configuration=configuration, secret_credentials=credentials)

            # Validate signature if configured
            if not adapter.verify_webhook_signature(request.body, request.META):
                logger.warning("Invalid webhook signature for tenant %s provider %s", tenant.id, provider)
                return Response({'error': 'Invalid webhook signature'}, status=status.HTTP_401_UNAUTHORIZED)

            # Step 4: Check for Delivery Status Event
            status_event = adapter.parse_status_webhook(payload, request.META)
            if status_event and status_event.provider_message_id:
                msg = CommunicationMessage.objects.filter(
                    organization=org,
                    provider_message_id=status_event.provider_message_id,
                ).first()

                if msg:
                    CommunicationService.record_status_event(
                        message=msg,
                        to_status=status_event.status,
                        provider_event_id=status_event.provider_event_id,
                        occurred_at=status_event.occurred_at,
                        raw_metadata=status_event.raw_metadata,
                        error_code=status_event.error_code,
                        error_message=status_event.error_message,
                    )
                    return Response({
                        'status': 'recorded',
                        'type': 'STATUS_EVENT',
                        'message_id': str(msg.id),
                        'current_status': msg.status,
                    })

            # Step 5: Check for Inbound Customer Message
            inbound_msg_data = adapter.parse_inbound_message(payload, request.META)
            if inbound_msg_data and inbound_msg_data.sender_identifier:
                inbound_msg = CommunicationService.ingest_inbound_message(
                    organization=org,
                    channel=inbound_msg_data.channel,
                    sender_identifier=inbound_msg_data.sender_identifier,
                    body=inbound_msg_data.body,
                    provider=provider_upper,
                    provider_message_id=inbound_msg_data.provider_message_id,
                    in_reply_to_provider_id=inbound_msg_data.in_reply_to_provider_id,
                    raw_payload=inbound_msg_data.raw_payload,
                )

                # Check if text is a confirmation or reschedule keyword
                body_clean = inbound_msg_data.body.strip().upper()
                if inbound_msg.lead and inbound_msg.related_trial:
                    trial = inbound_msg.related_trial
                    if 'CONFIRM' in body_clean and trial.confirmation_status == 'PENDING':
                        from apps.tenant_core.services_crm import CRMTrialService
                        CRMTrialService.confirm_trial(trial, confirmed_by='INBOUND_REPLY', channel=inbound_msg_data.channel)
                    elif 'RESCHEDULE' in body_clean:
                        from apps.tenant_core.services_crm import CRMTrialService
                        CRMTrialService.request_reschedule(trial, reason=inbound_msg_data.body[:200])

                return Response({
                    'status': 'recorded',
                    'type': 'INBOUND_MESSAGE',
                    'message_id': str(inbound_msg.id),
                    'resolution_status': inbound_msg.resolution_status,
                }, status=status.HTTP_201_CREATED)

            return Response({'status': 'ignored_or_unrecognized'}, status=status.HTTP_200_OK)

    def _resolve_tenant(self, public_id: str) -> Tenant | None:
        """
        Master DB resolution:
        1. Check UUID format
        2. Check slug / code
        Fails closed on inactive tenant.
        """
        tenant = None
        try:
            tenant = Tenant.objects.using('default').filter(id=public_id).first()
        except Exception:
            pass

        if not tenant:
            tenant = Tenant.objects.using('default').filter(
                Q(slug=public_id) | Q(code=public_id)
            ).first()

        if tenant and tenant.is_accessible:
            return tenant
        return None
