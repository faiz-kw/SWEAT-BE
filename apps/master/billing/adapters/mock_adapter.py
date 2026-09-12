"""
Master DB — Mock Payment Gateway Adapter (TEST / CI ONLY).
Phase 1 Layer 1 — Commercial Billing & Subscription Lifecycle.

Strictly intended for local development, automated test execution, and CI.
Under no circumstances should this be deployed or treated as a production payment provider.
"""

import hmac
import hashlib
import json
import uuid
from decimal import Decimal
from typing import Dict, Any, Optional
from django.utils import timezone

from .base import (
    BasePaymentGatewayAdapter,
    CustomerRegistrationResult,
    MandateSetupResult,
    PaymentExecutionResult,
    RefundExecutionResult,
    VerifiedWebhookEvent,
)


class MockPaymentGatewayAdapter(BasePaymentGatewayAdapter):
    """
    Deterministic in-memory mock payment adapter.
    Simulates gateway responses without external HTTP calls.
    """

    def __init__(self, webhook_secret: str = 'mock_webhook_secret_key_12345', signing_secret: Optional[str] = None):
        self.webhook_secret = signing_secret or webhook_secret
        self.simulated_failure_next_charge = False
        self.simulated_error_code = ''

    def create_customer(
        self,
        tenant_id: str,
        name: str,
        email: str,
        phone: str = ''
    ) -> CustomerRegistrationResult:
        cust_ref = f"mock_cust_{uuid.uuid5(uuid.NAMESPACE_DNS, str(tenant_id)).hex[:16]}"
        return CustomerRegistrationResult(
            provider_customer_ref=cust_ref,
            metadata={'tenant_id': str(tenant_id), 'name': name, 'email': email}
        )

    def setup_recurring_mandate(
        self,
        customer_ref: str,
        payment_token: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> MandateSetupResult:
        mandate_id = f"mock_mandate_{uuid.uuid4().hex[:16]}"
        method_id = f"mock_tok_{uuid.uuid4().hex[:16]}"
        # Determine method type from token prefix if provided
        method_type = 'CARD'
        display_name = 'Visa ending in 4242'
        if 'upi' in str(payment_token).lower():
            method_type = 'UPI'
            display_name = 'UPI AutoPay (user@upi)'

        return MandateSetupResult(
            mandate_reference=mandate_id,
            provider_method_ref=method_id,
            method_type=method_type,
            display_name=display_name,
            status='ACTIVE',
            metadata=metadata or {}
        )

    def charge_recurring_payment(
        self,
        mandate_ref: str,
        amount: Decimal,
        currency: str,
        invoice_number: str,
        idempotency_key: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> PaymentExecutionResult:
        if self.simulated_failure_next_charge:
            self.simulated_failure_next_charge = False
            return PaymentExecutionResult(
                success=False,
                provider_payment_id=f"mock_pay_fail_{uuid.uuid4().hex[:12]}",
                amount=amount,
                currency=currency,
                status='FAILED',
                error_code=self.simulated_error_code or 'INSUFFICIENT_FUNDS',
                error_message='Simulated payment failure for testing',
                paid_at=None,
                metadata={'idempotency_key': idempotency_key, 'invoice_number': invoice_number}
            )

        pay_id = f"mock_pay_{uuid.uuid4().hex[:16]}"
        return PaymentExecutionResult(
            success=True,
            provider_payment_id=pay_id,
            amount=amount,
            currency=currency,
            status='SUCCEEDED',
            paid_at=timezone.now(),
            metadata={'idempotency_key': idempotency_key, 'invoice_number': invoice_number}
        )

    def refund_payment(
        self,
        provider_payment_id: str,
        amount: Decimal,
        reason: str = ''
    ) -> RefundExecutionResult:
        refund_id = f"mock_rfnd_{uuid.uuid4().hex[:16]}"
        return RefundExecutionResult(
            success=True,
            refund_reference=refund_id,
            amount=amount,
            currency='INR',
            status='SUCCEEDED',
            metadata={'original_payment_id': provider_payment_id, 'reason': reason}
        )

    def verify_webhook_signature(
        self,
        raw_body: bytes,
        headers: Dict[str, str],
        signature: Optional[str] = None
    ) -> VerifiedWebhookEvent:
        # Case-insensitive header lookup
        sig_header = signature
        if not sig_header:
            for k, v in headers.items():
                if k.lower() in ('x-webhook-signature', 'x-razorpay-signature', 'stripe-signature', 'x-mock-signature'):
                    sig_header = v
                    break

        if not sig_header:
            return VerifiedWebhookEvent(
                event_id='',
                event_type='',
                provider='MOCK',
                payload={},
                is_valid=False,
                error_message='Missing webhook signature header'
            )

        expected_sig = hmac.new(
            self.webhook_secret.encode('utf-8'),
            raw_body,
            hashlib.sha256
        ).hexdigest()

        # In mock mode, allow test signature 'valid_mock_signature' or exact HMAC match
        if sig_header != 'valid_mock_signature' and not hmac.compare_digest(expected_sig, sig_header):
            return VerifiedWebhookEvent(
                event_id='',
                event_type='',
                provider='MOCK',
                payload={},
                is_valid=False,
                error_message='Invalid HMAC signature'
            )

        try:
            parsed = json.loads(raw_body.decode('utf-8'))
        except Exception as e:
            return VerifiedWebhookEvent(
                event_id='',
                event_type='',
                provider='MOCK',
                payload={},
                is_valid=False,
                error_message=f"JSON decode failure: {e}"
            )

        event_id = parsed.get('id') or parsed.get('event_id') or str(uuid.uuid4())
        event_type = parsed.get('event') or parsed.get('event_type') or 'payment.succeeded'

        return VerifiedWebhookEvent(
            event_id=event_id,
            event_type=event_type,
            provider='MOCK',
            payload=parsed,
            is_valid=True
        )
