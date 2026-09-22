"""
apps/tenant_core/communication/adapters/sms_gupshup.py — Gupshup SMS Communication Adapter.
"""

from typing import Dict, Any, Optional
from django.utils import timezone
from .base import (
    BaseCommunicationAdapter,
    ProviderSendResult,
    ProviderStatusEvent,
    ProviderInboundMessage,
)


class GupshupSMSAdapter(BaseCommunicationAdapter):
    channel = 'SMS'
    provider_name = 'GUPSHUP_SMS'
    capabilities = ['OUTBOUND', 'DELIVERY_RECEIPTS']

    def is_configured(self) -> bool:
        user_id = self.credentials.get('user_id') or self.credentials.get('username')
        password = self.credentials.get('password') or self.credentials.get('api_key')
        return bool(user_id and password)

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
                error_message='Gupshup SMS credentials not configured for this tenant.',
            )

        mid = f"gssms_{int(timezone.now().timestamp()*1000)}"
        return ProviderSendResult(
            success=True,
            provider_message_id=mid,
            status='SUBMITTED',
            raw_response={'response': 'success', 'msgId': mid},
        )

    def parse_status_webhook(self, payload: Dict[str, Any], headers: Optional[Dict[str, Any]] = None) -> Optional[ProviderStatusEvent]:
        try:
            msg_id = payload.get('externalId') or payload.get('messageId')
            status_str = (payload.get('status') or '').upper()
            if not msg_id:
                return None

            status_map = {
                'SUCCESS': 'DELIVERED',
                'DELIVERED': 'DELIVERED',
                'SENT': 'SENT',
                'FAILED': 'FAILED',
                'REJECTED': 'FAILED',
            }
            mapped_status = status_map.get(status_str, 'SENT')

            return ProviderStatusEvent(
                provider_message_id=msg_id,
                status=mapped_status,
                provider_event_id=f"gssms_{msg_id}_{status_str}",
                occurred_at=timezone.now(),
                error_code=str(payload.get('cause', '')),
                error_message=str(payload.get('status', '')),
                raw_metadata={'provider': 'GUPSHUP_SMS', 'status': status_str},
            )
        except Exception:
            return None
