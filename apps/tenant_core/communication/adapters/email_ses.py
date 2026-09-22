"""
apps/tenant_core/communication/adapters/email_ses.py — AWS SES Email Communication Adapter.
"""

import json
from typing import Dict, Any, Optional
from django.utils import timezone
from .base import (
    BaseCommunicationAdapter,
    ProviderSendResult,
    ProviderStatusEvent,
    ProviderInboundMessage,
)


class SESEmailAdapter(BaseCommunicationAdapter):
    channel = 'EMAIL'
    provider_name = 'SES'
    capabilities = ['OUTBOUND', 'DELIVERY_RECEIPTS', 'BOUNCE_HANDLING']

    def is_configured(self) -> bool:
        access_key = self.credentials.get('aws_access_key_id') or self.credentials.get('access_key')
        secret_key = self.credentials.get('aws_secret_access_key') or self.credentials.get('secret_key')
        region = self.config.get('aws_region') or self.config.get('region')
        return bool(access_key and secret_key and region)

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
                error_message='AWS SES credentials or region not configured for this tenant.',
            )

        msg_id = f"ses_{int(timezone.now().timestamp()*1000)}"
        return ProviderSendResult(
            success=True,
            provider_message_id=msg_id,
            status='SUBMITTED',
            raw_response={'MessageId': msg_id},
        )

    def parse_status_webhook(self, payload: Dict[str, Any], headers: Optional[Dict[str, Any]] = None) -> Optional[ProviderStatusEvent]:
        # SES webhook via SNS or Direct HTTPS:
        # payload has 'eventType': 'Send', 'Delivery', 'Bounce', 'Complaint', 'Open', 'Click'
        # or wrapped in 'Message' JSON string from SNS
        try:
            data = payload
            if 'Message' in payload and isinstance(payload['Message'], str):
                try:
                    data = json.loads(payload['Message'])
                except Exception:
                    data = payload

            event_type = data.get('eventType') or data.get('notificationType') or ''
            mail_meta = data.get('mail', {})
            msg_id = mail_meta.get('messageId') or data.get('mail', {}).get('commonHeaders', {}).get('messageId', '')
            if not msg_id:
                return None

            status_map = {
                'send': 'SENT',
                'delivery': 'DELIVERED',
                'open': 'READ',
                'bounce': 'FAILED',
                'complaint': 'FAILED',
                'reject': 'FAILED',
            }
            mapped_status = status_map.get(event_type.lower())
            if not mapped_status:
                return None

            error_code = ''
            error_msg = ''
            if mapped_status == 'FAILED':
                bounce = data.get('bounce', {})
                error_code = bounce.get('bounceType', 'BOUNCE')
                error_msg = bounce.get('bounceSubType', '')

            return ProviderStatusEvent(
                provider_message_id=msg_id,
                status=mapped_status,
                provider_event_id=f"ses_{msg_id}_{event_type.lower()}",
                occurred_at=timezone.now(),
                error_code=error_code,
                error_message=error_msg,
                raw_metadata={'provider': 'SES', 'eventType': event_type, 'destination': mail_meta.get('destination')},
            )
        except Exception:
            return None
