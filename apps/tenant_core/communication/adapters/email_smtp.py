"""
apps/tenant_core/communication/adapters/email_smtp.py — SMTP Email Communication Adapter.
"""

from typing import Dict, Any, Optional
from django.utils import timezone
from django.core.mail import get_connection, EmailMultiAlternatives
from django.conf import settings
from .base import (
    BaseCommunicationAdapter,
    ProviderSendResult,
    ProviderStatusEvent,
    ProviderInboundMessage,
)


class SMTPEmailAdapter(BaseCommunicationAdapter):
    channel = 'EMAIL'
    provider_name = 'SMTP'
    capabilities = ['OUTBOUND']

    def is_configured(self) -> bool:
        host = self.config.get('host') or getattr(settings, 'EMAIL_HOST', None)
        port = self.config.get('port') or getattr(settings, 'EMAIL_PORT', None)
        # If host is configured or django default email backend is available
        return bool(host and port)

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
                error_message='SMTP host/port not configured for this tenant.',
            )

        from_email = sender_id or self.config.get('from_email') or getattr(settings, 'DEFAULT_FROM_EMAIL', 'no-reply@performanceos.internal')
        msg_id = f"smtp_{int(timezone.now().timestamp()*1000)}@{from_email.split('@')[-1] if '@' in from_email else 'localhost'}"

        try:
            # We construct email message
            # If running in test mode or with in-memory/console backend, this delivers cleanly
            connection = None
            if self.config.get('host'):
                connection = get_connection(
                    host=self.config.get('host'),
                    port=int(self.config.get('port', 587)),
                    username=self.credentials.get('username') or self.credentials.get('user', ''),
                    password=self.credentials.get('password', ''),
                    use_tls=bool(self.config.get('use_tls', True)),
                    fail_silently=False,
                )

            email = EmailMultiAlternatives(
                subject=subject or 'Update from Fitness Command Center',
                body=body,
                from_email=from_email,
                to=[recipient],
                headers={'Message-ID': f"<{msg_id}>", **(extra_headers or {})},
                connection=connection,
            )
            # In live production or test suite, send()
            try:
                email.send(fail_silently=False)
            except Exception as e:
                # If network/smtp is down, return graceful failure without crashing caller
                return ProviderSendResult(
                    success=False,
                    status='FAILED',
                    error_code='SMTP_CONNECTION_FAILED',
                    error_message=str(e),
                )

            return ProviderSendResult(
                success=True,
                provider_message_id=msg_id,
                status='SENT',
                raw_response={'message_id': msg_id, 'from': from_email, 'to': recipient},
            )
        except Exception as exc:
            return ProviderSendResult(
                success=False,
                status='FAILED',
                error_code='SMTP_ERROR',
                error_message=str(exc),
            )
