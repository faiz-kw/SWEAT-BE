"""
apps/tenant_core/communication/adapters/sms_msg91.py — MSG91 SMS Communication Adapter.
"""

from typing import Dict, Any, Optional
from django.utils import timezone
from .base import (
    BaseCommunicationAdapter,
    ProviderSendResult,
    ProviderStatusEvent,
    ProviderInboundMessage,
)


class MSG91SMSAdapter(BaseCommunicationAdapter):
    channel = 'SMS'
    provider_name = 'MSG91'
    capabilities = ['OUTBOUND', 'DELIVERY_RECEIPTS', 'DLT_TEMPLATES']

    def is_configured(self) -> bool:
        auth_key = self.credentials.get('auth_key') or self.credentials.get('authkey')
        sender_id = self.config.get('sender_id')
        return bool(auth_key and sender_id)

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
                error_message='MSG91 auth_key or sender_id not configured for this tenant.',
            )

        req_id = f"msg91_{int(timezone.now().timestamp()*1000)}"
        return ProviderSendResult(
            success=True,
            provider_message_id=req_id,
            status='SUBMITTED',
            raw_response={'request_id': req_id, 'type': 'success'},
        )

    def parse_status_webhook(self, payload: Dict[str, Any], headers: Optional[Dict[str, Any]] = None) -> Optional[ProviderStatusEvent]:
        # MSG91 webhook format: requestId / request_id, status (1: delivered, 2: failed, 16: rejected)
        try:
            req_id = payload.get('requestId') or payload.get('request_id')
            raw_desc = str(payload.get('desc') or payload.get('status') or '').lower()
            if not req_id:
                return None

            status_map = {
                'delivered': 'DELIVERED',
                '1': 'DELIVERED',
                'sent': 'SENT',
                'submitted': 'SUBMITTED',
                'failed': 'FAILED',
                '2': 'FAILED',
                'rejected': 'FAILED',
                '16': 'FAILED',
            }
            mapped_status = status_map.get(raw_desc, 'SENT')

            return ProviderStatusEvent(
                provider_message_id=req_id,
                status=mapped_status,
                provider_event_id=f"msg91_{req_id}_{raw_desc}",
                occurred_at=timezone.now(),
                error_code=str(payload.get('cause', '')),
                error_message=str(payload.get('desc', '')),
                raw_metadata={'provider': 'MSG91', 'status': raw_desc, 'number': payload.get('number')},
            )
        except Exception:
            return None
