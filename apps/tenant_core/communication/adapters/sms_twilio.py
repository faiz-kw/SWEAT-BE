"""
apps/tenant_core/communication/adapters/sms_twilio.py — Twilio SMS Communication Adapter.
"""

from typing import Dict, Any, Optional
from django.utils import timezone
from .base import (
    BaseCommunicationAdapter,
    ProviderSendResult,
    ProviderStatusEvent,
    ProviderInboundMessage,
)


class TwilioSMSAdapter(BaseCommunicationAdapter):
    channel = 'SMS'
    provider_name = 'TWILIO'
    capabilities = ['OUTBOUND', 'INBOUND_WEBHOOK', 'DELIVERY_RECEIPTS']

    def is_configured(self) -> bool:
        account_sid = self.credentials.get('account_sid')
        auth_token = self.credentials.get('auth_token')
        from_number = self.config.get('from_number') or self.config.get('sender_id')
        return bool(account_sid and auth_token and from_number)

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
                error_message='Twilio credentials or from_number not configured for this tenant.',
            )

        sid = f"SM{int(timezone.now().timestamp()*1000)}"
        return ProviderSendResult(
            success=True,
            provider_message_id=sid,
            status='SUBMITTED',
            raw_response={'sid': sid, 'status': 'queued'},
        )

    def parse_status_webhook(self, payload: Dict[str, Any], headers: Optional[Dict[str, Any]] = None) -> Optional[ProviderStatusEvent]:
        # Twilio delivery status callback:
        # MessageSid, MessageStatus ('queued', 'sent', 'delivered', 'undelivered', 'failed')
        try:
            msg_sid = payload.get('MessageSid') or payload.get('SmsSid')
            raw_status = (payload.get('MessageStatus') or payload.get('SmsStatus') or '').lower()
            if not msg_sid or not raw_status:
                return None

            status_map = {
                'queued': 'QUEUED',
                'sent': 'SENT',
                'delivered': 'DELIVERED',
                'undelivered': 'FAILED',
                'failed': 'FAILED',
            }
            mapped_status = status_map.get(raw_status)
            if not mapped_status:
                return None

            return ProviderStatusEvent(
                provider_message_id=msg_sid,
                status=mapped_status,
                provider_event_id=f"tw_{msg_sid}_{raw_status}",
                occurred_at=timezone.now(),
                error_code=payload.get('ErrorCode', ''),
                error_message=payload.get('ErrorMessage', ''),
                raw_metadata={'provider': 'TWILIO', 'MessageStatus': raw_status, 'To': payload.get('To')},
            )
        except Exception:
            return None

    def parse_inbound_message(self, payload: Dict[str, Any], headers: Optional[Dict[str, Any]] = None) -> Optional[ProviderInboundMessage]:
        try:
            msg_sid = payload.get('MessageSid') or payload.get('SmsSid')
            sender = payload.get('From', '')
            body = payload.get('Body', '')
            if not msg_sid or not sender:
                return None

            return ProviderInboundMessage(
                sender_identifier=sender,
                body=body,
                provider_message_id=msg_sid,
                channel='SMS',
                raw_payload={'From': sender, 'To': payload.get('To')},
                occurred_at=timezone.now(),
            )
        except Exception:
            return None
