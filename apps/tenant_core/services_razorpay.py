"""
apps/tenant_core/services_razorpay.py — Authoritative Razorpay Integration Service

Phase 1 Scope:
- Secure environment-based credential resolution (RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET)
- Official Razorpay API Client wrapper
- Server-side Order creation (amount in integer paise, INR)
- Server-side Payment Signature verification (HMAC-SHA256 via official razorpay utility)
- Server-side Payment verification via Razorpay API (amount, currency, order_id, status)
- Strict secret confidentiality: KEY_SECRET is NEVER logged, printed, or returned to clients.
- Controlled error codes: RAZORPAY_NOT_CONFIGURED, RAZORPAY_SIGNATURE_INVALID, RAZORPAY_VERIFICATION_FAILED
"""

import os
import logging
from typing import Tuple, Dict, Any, Optional
from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError

try:
    import razorpay
    from razorpay.errors import SignatureVerificationError, BadRequestError, GatewayError
except ImportError:
    razorpay = None
    class SignatureVerificationError(Exception):
        pass
    class BadRequestError(Exception):
        pass
    class GatewayError(Exception):
        pass

logger = logging.getLogger(__name__)


class RazorpayConfigError(ValidationError):
    """Raised when Razorpay credentials are missing, incomplete, or invalid."""
    pass


class RazorpayVerificationError(ValidationError):
    """Raised when Razorpay signature or payment details fail verification."""
    pass


class RazorpayService:
    """
    Backend-authoritative service for Razorpay Test/Live payment operations.
    """

    @classmethod
    def _validate_credentials(cls, key_id: str, key_secret: str) -> None:
        placeholder_indicators = ['<', 'placeholder', 'dummy', 'your-test-key']
        if not key_id or not key_secret:
            raise RazorpayConfigError(
                "Razorpay is not configured on this server.",
                code="RAZORPAY_NOT_CONFIGURED"
            )

        if any(p in key_id.lower() for p in placeholder_indicators) or any(p in key_secret.lower() for p in placeholder_indicators):
            raise RazorpayConfigError(
                "Razorpay credentials contain placeholder values.",
                code="RAZORPAY_NOT_CONFIGURED"
            )

        # Enforce Test Mode key format
        if not key_id.startswith('rzp_test_'):
            raise RazorpayConfigError(
                "Invalid Razorpay Key ID format for test mode. Key ID must start with 'rzp_test_'.",
                code="RAZORPAY_INVALID_KEY_FORMAT"
            )

        if len(key_secret) < 10:
            raise RazorpayConfigError(
                "Razorpay Key Secret is invalid or truncated.",
                code="RAZORPAY_INVALID_SECRET"
            )

    @classmethod
    def get_credentials(cls, db_alias: Optional[str] = None) -> Tuple[str, str]:
        """
        Safely retrieves and validates Razorpay credentials strictly from backend configuration.
        Resolution Strategy:
        1. Tenant-specific Integration record (models_infra.Integration) in the tenant database:
           - Key ID is read from integration.configuration['key_id']
           - Key Secret is securely resolved via SecretResolver from integration.secret_reference
        2. In DEVELOPMENT / UAT / TEST mode ONLY:
           - Safe fallback to backend .env RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET
        3. In Production:
           - Never falls back to another tenant or platform merchant account.
           - If the current tenant has no active Razorpay configuration, fails closed with RAZORPAY_NOT_CONFIGURED.
        Ensures key_secret is never exposed or logged.
        """
        alias = db_alias
        if not alias:
            try:
                from config.routers import get_tenant_db_alias
                alias = get_tenant_db_alias()
            except Exception:
                alias = None

        key_id = None
        key_secret = None

        # 1. Tenant-specific Integration record
        if alias and alias != 'default':
            try:
                from .models_infra import Integration
                from config.secrets import SecretResolver, SecretResolutionError

                integration = Integration.objects.using(alias).filter(
                    integration_type='PAYMENT',
                    provider__iexact='Razorpay',
                    status='ACTIVE',
                ).first()

                if integration:
                    config = integration.configuration or {}
                    cand_key_id = config.get('key_id') or config.get('KEY_ID')
                    if cand_key_id:
                        key_id = str(cand_key_id).strip()

                    if integration.secret_reference:
                        try:
                            resolved = SecretResolver.resolve(integration.secret_reference)
                            if resolved:
                                key_secret = str(resolved).strip()
                        except SecretResolutionError as sre:
                            logger.error("Failed to resolve tenant Razorpay secret_reference: %s", sre)
            except Exception as e:
                logger.debug("Error checking tenant Integration row: %s", type(e).__name__)

        # If both found in tenant Integration, validate and return
        if key_id and key_secret:
            cls._validate_credentials(key_id, key_secret)
            return key_id, key_secret

        # 2. Check if Development/UAT fallback is permitted
        allow_fallback = False
        is_debug = getattr(settings, 'DEBUG', False)
        env_name = getattr(settings, 'ENVIRONMENT', os.getenv('ENVIRONMENT', 'development')).lower()
        if is_debug or env_name in ['development', 'local', 'test', 'uat']:
            allow_fallback = True

        if allow_fallback:
            if getattr(settings, 'configured', False):
                fallback_key_id = getattr(settings, 'RAZORPAY_KEY_ID', None)
                if fallback_key_id is None:
                    fallback_key_id = os.getenv('RAZORPAY_KEY_ID', '')
                fallback_key_secret = getattr(settings, 'RAZORPAY_KEY_SECRET', None)
                if fallback_key_secret is None:
                    fallback_key_secret = os.getenv('RAZORPAY_KEY_SECRET', '')
            else:
                fallback_key_id = os.getenv('RAZORPAY_KEY_ID', '')
                fallback_key_secret = os.getenv('RAZORPAY_KEY_SECRET', '')

            fallback_key_id = str(fallback_key_id).strip()
            fallback_key_secret = str(fallback_key_secret).strip()

            if fallback_key_id and fallback_key_secret:
                cls._validate_credentials(fallback_key_id, fallback_key_secret)
                return fallback_key_id, fallback_key_secret

        # 3. Fail closed
        raise RazorpayConfigError(
            "Razorpay is not configured for this tenant.",
            code="RAZORPAY_NOT_CONFIGURED"
        )

    @classmethod
    def is_configured(cls, db_alias: Optional[str] = None) -> bool:
        """
        Returns True if valid Razorpay test credentials are configured for tenant, False otherwise.
        """
        try:
            cls.get_credentials(db_alias=db_alias)
            return True
        except (RazorpayConfigError, Exception):
            return False

    @classmethod
    def get_client(cls, db_alias: Optional[str] = None):
        """
        Returns an authenticated official Razorpay Client for tenant.
        """
        if razorpay is None:
            raise RazorpayConfigError(
                "The 'razorpay' Python library is not installed in the environment.",
                code="RAZORPAY_PACKAGE_MISSING"
            )
        key_id, key_secret = cls.get_credentials(db_alias=db_alias)
        return razorpay.Client(auth=(key_id, key_secret))

    @classmethod
    def convert_inr_to_paise(cls, amount: Decimal) -> int:
        """
        Converts a Decimal INR amount into an integer paise amount without floating-point errors.
        E.g. Decimal('100.00') -> 10000 paise.
        """
        if amount is None or amount <= Decimal('0.00'):
            raise ValidationError("Payment amount must be greater than zero.")
        # Exact integer quantification
        paise_decimal = (Decimal(str(amount)) * Decimal('100')).quantize(Decimal('1'))
        return int(paise_decimal)

    @classmethod
    def create_order(
        cls,
        amount_paise: int,
        receipt: str,
        currency: str = 'INR',
        notes: Optional[Dict[str, Any]] = None,
        db_alias: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Creates an external order at Razorpay via the official API using tenant credentials.
        Amount must be an integer in paise.
        """
        if not isinstance(amount_paise, int) or amount_paise <= 0:
            raise ValidationError("Razorpay amount must be a positive integer in paise.")

        client = cls.get_client(db_alias=db_alias)
        order_data = {
            'amount': amount_paise,
            'currency': currency,
            'receipt': str(receipt)[:40],
            'notes': notes or {},
        }

        try:
            order = client.order.create(data=order_data)
            provider_order_id = order.get('id')
            if not provider_order_id or not str(provider_order_id).startswith('order_'):
                raise RazorpayVerificationError(
                    f"Unexpected order ID returned by Razorpay: {provider_order_id}",
                    code="RAZORPAY_INVALID_RESPONSE"
                )
            return order
        except BadRequestError as e:
            logger.error("Razorpay order creation failed (BadRequest): %s", e)
            raise RazorpayVerificationError(
                f"Razorpay order creation failed: {e}",
                code="RAZORPAY_API_ERROR"
            )
        except Exception as e:
            logger.error("Razorpay order creation failed: %s", type(e).__name__)
            raise RazorpayVerificationError(
                "Payment gateway order creation failed. Please retry or contact support.",
                code="RAZORPAY_API_ERROR"
            )

    @classmethod
    def verify_payment_signature(
        cls,
        razorpay_order_id: str,
        razorpay_payment_id: str,
        razorpay_signature: str,
        db_alias: Optional[str] = None,
    ) -> bool:
        """
        Verifies the HMAC-SHA256 signature returned by Razorpay Checkout using tenant credentials.
        Uses the official Razorpay utility client.utility.verify_payment_signature.
        """
        if not razorpay_order_id or not razorpay_payment_id or not razorpay_signature:
            return False

        client = cls.get_client(db_alias=db_alias)
        params = {
            'razorpay_order_id': str(razorpay_order_id).strip(),
            'razorpay_payment_id': str(razorpay_payment_id).strip(),
            'razorpay_signature': str(razorpay_signature).strip(),
        }

        try:
            client.utility.verify_payment_signature(params)
            return True
        except (SignatureVerificationError, Exception) as exc:
            logger.warning(
                "Razorpay signature verification rejected: order=%s payment=%s reason=%s",
                razorpay_order_id, razorpay_payment_id, type(exc).__name__
            )
            return False

    @classmethod
    def fetch_and_verify_payment(
        cls,
        razorpay_payment_id: str,
        expected_order_id: str,
        expected_amount_paise: int,
        expected_currency: str = 'INR',
        db_alias: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Server-side cross-check: fetches the payment record directly from Razorpay API
        using tenant credentials and validates provider order ID, captured/authorized status, amount, and currency match.
        Fails closed on any mismatch.
        """
        if not razorpay_payment_id:
            raise RazorpayVerificationError(
                "Payment ID is required for verification.",
                code="RAZORPAY_PAYMENT_ID_MISSING"
            )

        client = cls.get_client(db_alias=db_alias)
        try:
            payment = client.payment.fetch(str(razorpay_payment_id).strip())
        except Exception as e:
            logger.error(
                "Failed to fetch Razorpay payment %s: %s",
                razorpay_payment_id, type(e).__name__
            )
            raise RazorpayVerificationError(
                f"Unable to verify payment with Razorpay: {type(e).__name__}",
                code="RAZORPAY_FETCH_FAILED"
            )

        # 1. Cross-check Order ID
        actual_order_id = payment.get('order_id')
        if actual_order_id != expected_order_id:
            logger.warning(
                "Razorpay order mismatch: expected=%s actual=%s for payment=%s",
                expected_order_id, actual_order_id, razorpay_payment_id
            )
            raise RazorpayVerificationError(
                f"Payment order mismatch: payment belongs to order {actual_order_id}, not {expected_order_id}.",
                code="RAZORPAY_ORDER_MISMATCH"
            )

        # 2. Cross-check Amount (in paise)
        actual_amount = payment.get('amount')
        if actual_amount != expected_amount_paise:
            logger.warning(
                "Razorpay amount mismatch: expected=%s actual=%s for payment=%s",
                expected_amount_paise, actual_amount, razorpay_payment_id
            )
            raise RazorpayVerificationError(
                f"Payment amount mismatch: expected {expected_amount_paise} paise, received {actual_amount} paise.",
                code="RAZORPAY_AMOUNT_MISMATCH"
            )

        # 3. Cross-check Currency
        actual_currency = payment.get('currency')
        if str(actual_currency).upper() != str(expected_currency).upper():
            raise RazorpayVerificationError(
                f"Payment currency mismatch: expected {expected_currency}, received {actual_currency}.",
                code="RAZORPAY_CURRENCY_MISMATCH"
            )

        # 4. Cross-check Payment Status
        status = payment.get('status')
        if status not in ('captured', 'authorized'):
            raise RazorpayVerificationError(
                f"Payment status is '{status}', not captured or authorized.",
                code="RAZORPAY_PAYMENT_NOT_SUCCESSFUL"
            )

        return payment
