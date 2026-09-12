"""
Base abstract classes for all third-party Integration Layer adapters.
Ensures uniform contract across all external providers (payments, messaging, calling, email, leads, hardware).
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional

class BasePaymentAdapter(ABC):
    """Abstract interface for payment gateways (e.g. Razorpay, Stripe, PayU)."""

    @abstractmethod
    def create_order(self, amount: float, currency: str, receipt_id: str, notes: Dict[str, Any]) -> Dict[str, Any]:
        """Creates a payment order at the gateway."""
        pass

    @abstractmethod
    def verify_payment_signature(self, order_id: str, payment_id: str, signature: str) -> bool:
        """Verifies cryptographic signature from client checkout."""
        pass

    @abstractmethod
    def process_refund(self, payment_id: str, amount: float, reason: str = "") -> Dict[str, Any]:
        """Initiates refund request."""
        pass


class BaseMessagingAdapter(ABC):
    """Abstract interface for messaging platforms (e.g. Gupshup WhatsApp, Twilio)."""

    @abstractmethod
    def send_whatsapp_template(self, phone: str, template_name: str, params: Dict[str, str]) -> Dict[str, Any]:
        """Sends a pre-approved WhatsApp template message."""
        pass

    @abstractmethod
    def send_direct_message(self, phone: str, text: str) -> Dict[str, Any]:
        """Sends a transactional text/message."""
        pass


class BaseTelephonyAdapter(ABC):
    """Abstract interface for cloud telephony / calling (e.g. TeleCMI, Exotel)."""

    @abstractmethod
    def initiate_outbound_call(self, agent_phone: str, customer_phone: str, custom_data: Dict[str, Any]) -> Dict[str, Any]:
        """Bridges a sales rep or AI voice agent with a prospect."""
        pass

    @abstractmethod
    def parse_call_webhook(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Extracts call duration, status, and recording URL from vendor webhook."""
        pass


class BaseEmailAdapter(ABC):
    """Abstract interface for transactional email (e.g. Amazon SES, SendGrid)."""

    @abstractmethod
    def send_email(self, to_email: str, subject: str, body_html: str, body_text: str = "") -> Dict[str, Any]:
        """Sends an HTML/plain email."""
        pass


class BaseLeadSourceAdapter(ABC):
    """Abstract interface for external lead ingestion (e.g. Facebook Lead Ads, Google Ads)."""

    @abstractmethod
    def parse_lead_webhook(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Normalizes vendor lead payload into standard Lead dict."""
        pass


class BaseAccessControlAdapter(ABC):
    """Abstract interface for gym door turnstiles / access hardware."""

    @abstractmethod
    def generate_qr_credential(self, member_id: str, booking_id: str, valid_from: str, valid_until: str) -> str:
        """Generates a secure time-boxed QR token for gym door scanner."""
        pass

    @abstractmethod
    def validate_door_scan(self, qr_token: str, device_id: str) -> Dict[str, Any]:
        """Validates QR token at the turnstile and decides access."""
        pass
