"""
apps/tenant_core/communication/adapters/base.py — Base Communication Adapter & Contracts.

Defines the normalized interfaces and response data structures for all messaging providers
across WhatsApp, Email, SMS (and future channels).
"""

from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
from datetime import datetime
from django.utils import timezone


@dataclass
class ProviderSendResult:
    success: bool
    provider_message_id: Optional[str] = None
    status: str = 'SUBMITTED'  # 'SUBMITTED', 'SENT', 'FAILED'
    error_code: str = ''
    error_message: str = ''
    raw_response: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderStatusEvent:
    provider_message_id: str
    status: str  # 'SENT', 'DELIVERED', 'READ', 'FAILED'
    provider_event_id: Optional[str] = None
    occurred_at: datetime = field(default_factory=timezone.now)
    error_code: str = ''
    error_message: str = ''
    raw_metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderInboundMessage:
    sender_identifier: str
    body: str
    provider_message_id: str
    channel: str
    in_reply_to_provider_id: Optional[str] = None
    raw_payload: Dict[str, Any] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=timezone.now)


class BaseCommunicationAdapter:
    """
    Abstract communication adapter for a specific channel and provider.
    Each provider subclass encapsulates its unique payload schemas, authentication,
    signature verification, and webhook parsing.
    """
    channel: str = ''          # 'WHATSAPP', 'EMAIL', 'SMS'
    provider_name: str = ''    # 'META', 'GUPSHUP', 'SMTP', 'SES', 'TWILIO', 'MSG91'
    capabilities: List[str] = []

    def __init__(self, configuration: Optional[Dict[str, Any]] = None, secret_credentials: Optional[Dict[str, Any]] = None):
        self.config = configuration or {}
        self.credentials = secret_credentials or {}

    def is_configured(self) -> bool:
        """
        Check if required credentials and parameters are present for live execution.
        Must return False if credentials are placeholder or missing.
        """
        raise NotImplementedError

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
        """
        Send an outbound message through this provider.
        """
        raise NotImplementedError

    def verify_webhook_signature(self, payload_bytes: bytes, headers: Dict[str, Any]) -> bool:
        """
        Verify incoming webhook signature/auth token against tenant's configured secret.
        """
        return True

    def parse_status_webhook(self, payload: Dict[str, Any], headers: Optional[Dict[str, Any]] = None) -> Optional[ProviderStatusEvent]:
        """
        Extract delivery status progression from provider webhook callback.
        """
        return None

    def parse_inbound_message(self, payload: Dict[str, Any], headers: Optional[Dict[str, Any]] = None) -> Optional[ProviderInboundMessage]:
        """
        Extract inbound customer message from provider webhook payload.
        """
        return None
