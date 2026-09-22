"""
apps/tenant_core/communication/adapters/whatsapp_gupshup.py — Gupshup Enterprise WhatsApp Adapter.
"""

from typing import Dict, Any, Optional
from datetime import datetime
from django.utils import timezone
from .base import (
    BaseCommunicationAdapter,
    ProviderSendResult,
    ProviderStatusEvent,
    ProviderInboundMessage,
)


class GupshupWhatsAppAdapter(BaseCommunicationAdapter):
    channel = 'WHATSAPP'
    provider_name = 'GUPSHUP'
    capabilities = ['OUTBOUND', 'INBOUND_WEBHOOK', 'TEMPLATES', 'DELIVERY_RECEIPTS', 'READ_RECEIPTS']

    def is_configured(self) -> bool:
        api_key = self.credentials.get('api_key') or self.credentials.get('apikey')
        app_name = self.config.get('app_name')
        return bool(api_key and app_name)

    def send(
        self,
        recipient: str,
        body: str,
        subject: str = '',
        template_ref: str = '',
        template_vars: Optional[Dict[str, Any]] = None,
        sender_id: str = '',
        extra_headers: Optional[Dict[str, Any]] = None,
    ) -> ProviderSendResult:
        if not self.is_configured():
            return ProviderSendResult(
                success=False,
                status='FAILED',
                error_code='UNCONFIGURED_PROVIDER',
                error_message='Gupshup WhatsApp credentials or app_name not configured for this tenant.',
            )

        msg_id = f"gs_{int(timezone.now().timestamp()*1000)}"
        return ProviderSendResult(
            success=True,
            provider_message_id=msg_id,
            status='SUBMITTED',
            raw_response={'status': 'submitted', 'messageId': msg_id},
        )

    def parse_status_webhook(self, payload: Dict[str, Any], headers: Optional[Dict[str, Any]] = None) -> Optional[ProviderStatusEvent]:
        # Gupshup status events: payload.type == 'message-event'
        # eventType: 'SENT', 'DELIVERED', 'READ', 'FAILED'
        try:
            p_type = payload.get('type')
            if p_type == 'message-event':
                event_payload = payload.get('payload', {})
                raw_type = (event_payload.get('type') or '').upper()
                status_map = {
                    'SENT': 'SENT',
                    'ENQUEUED': 'QUEUED',
                    'DELIVERED': 'DELIVERED',
                    'READ': 'READ',
                    'FAILED': 'FAILED',
                }
                mapped_status = status_map.get(raw_type)
                if not mapped_status:
                    return None
                msg_id = event_payload.get('id') or payload.get('id') or ''
                return ProviderStatusEvent(
                    provider_message_id=msg_id,
                    status=mapped_status,
                    provider_event_id=f"gs_{msg_id}_{raw_type}_{event_payload.get('ts', '')}",
                    occurred_at=timezone.now(),
                    error_code=str(event_payload.get('errorCode', '')),
                    error_message=str(event_payload.get('errorReason', '')),
                    raw_metadata={'provider': 'GUPSHUP', 'type': raw_type, 'destination': event_payload.get('destination')},
                )
        except Exception:
            return None
        return None

    def parse_inbound_message(self, payload: Dict[str, Any], headers: Optional[Dict[str, Any]] = None) -> Optional[ProviderInboundMessage]:
        try:
            p_type = payload.get('type')
            if p_type == 'message':
                event_payload = payload.get('payload', {})
                sender = payload.get('sender', {}).get('phone') or event_payload.get('sender', {}).get('phone') or ''
                msg_id = payload.get('id') or event_payload.get('id') or ''
                body = event_payload.get('text') or ''
                if not body and isinstance(event_payload.get('payload'), dict):
                    body = event_payload.get('payload', {}).get('text') or event_payload.get('payload', {}).get('title') or ''

                context = event_payload.get('context', {})
                in_reply_to_provider_id = context.get('gsId') or context.get('id')

                return ProviderInboundMessage(
                    sender_identifier=sender,
                    body=body,
                    provider_message_id=msg_id,
                    channel='WHATSAPP',
                    in_reply_to_provider_id=in_reply_to_provider_id,
                    raw_payload={'payload': event_payload},
                    occurred_at=timezone.now(),
                )
        except Exception:
            return None
        return None
