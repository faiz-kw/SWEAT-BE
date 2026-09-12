"""
Master DB — Provider-Neutral Payment Gateway Adapter Interface.
Phase 1 Layer 1 — Commercial Billing & Subscription Lifecycle.

Architectural Principles:
- Provider-neutral: The billing engine interacts exclusively with this interface.
- Zero credential storage: Raw PAN, CVV, or card credentials never touch application servers.
- Typed results: All operations return structured, immutable data transfer objects.
- Mock support: Deterministic mock adapter for CI/test without external network dependencies.
"""

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Dict, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class CustomerRegistrationResult:
    provider_customer_ref: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MandateSetupResult:
    mandate_reference: str
    provider_method_ref: str
    method_type: str  # 'CARD', 'UPI', 'NETBANKING'
    display_name: str
    status: str  # 'ACTIVE', 'PENDING'
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PaymentExecutionResult:
    success: bool
    provider_payment_id: str
    amount: Decimal
    currency: str
    status: str  # 'SUCCEEDED', 'FAILED', 'PENDING'
    provider: str = 'MOCK'
    error_code: str = ''
    error_message: str = ''
    paid_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_success(self) -> bool:
        return self.success

    @property
    def failure_code(self) -> str:
        return self.error_code

    @property
    def failure_message(self) -> str:
        return self.error_message


@dataclass(frozen=True)
class RefundExecutionResult:
    success: bool
    refund_reference: str
    amount: Decimal
    currency: str
    status: str  # 'SUCCEEDED', 'FAILED'
    error_message: str = ''
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VerifiedWebhookEvent:
    event_id: str
    event_type: str
    provider: str
    payload: Dict[str, Any]
    is_valid: bool
    error_message: str = ''


class BasePaymentGatewayAdapter(ABC):
    """
    Abstract contract for payment gateway integrations.
    Any future selected production provider (Razorpay, Stripe, etc.) must implement this interface.
    """

    @abstractmethod
    def create_customer(
        self,
        tenant_id: str,
        name: str,
        email: str,
        phone: str = ''
    ) -> CustomerRegistrationResult:
        """Registers a customer profile at the provider."""
        pass

    @abstractmethod
    def setup_recurring_mandate(
        self,
        customer_ref: str,
        payment_token: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> MandateSetupResult:
        """Tokenizes and authorizes recurring billing mandate."""
        pass

    @abstractmethod
    def charge_recurring_payment(
        self,
        mandate_ref: str,
        amount: Decimal,
        currency: str,
        invoice_number: str,
        idempotency_key: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> PaymentExecutionResult:
        """Executes off-session recurring charge against a stored mandate."""
    def execute_payment(
        self,
        invoice_id: str,
        amount: Decimal,
        currency: str,
        customer_ref: str,
        method_ref: str,
        idempotency_key: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> PaymentExecutionResult:
        """Executes payment using stored mandate or customer reference."""
        return self.charge_recurring_payment(
            mandate_ref=method_ref,
            amount=amount,
            currency=currency,
            invoice_number=invoice_id,
            idempotency_key=idempotency_key,
            metadata=metadata
        )

    @abstractmethod
    def refund_payment(
        self,
        provider_payment_id: str,
        amount: Decimal,
        reason: str = ''
    ) -> RefundExecutionResult:
        """Initiates a full or partial refund."""
        pass

    @abstractmethod
    def verify_webhook_signature(
        self,
        raw_body: bytes,
        headers: Dict[str, str]
    ) -> VerifiedWebhookEvent:
        """Verifies cryptographic signature of incoming webhook and parses event."""
        pass
