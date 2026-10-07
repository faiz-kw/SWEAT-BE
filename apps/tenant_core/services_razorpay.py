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

    @classmethod
    def get_webhook_secret(cls, db_alias: Optional[str] = None) -> str:
        """
        Resolves Razorpay webhook secret from tenant integration configuration
        or falls back to settings/environment.
        """
        alias = db_alias or 'default'
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
                    cand_secret = config.get('webhook_secret') or config.get('WEBHOOK_SECRET')
                    if cand_secret:
                        return str(cand_secret).strip()

                    if integration.secret_reference:
                        try:
                            resolved = SecretResolver.resolve(f"{integration.secret_reference}/webhook_secret")
                            if resolved:
                                return str(resolved).strip()
                        except SecretResolutionError:
                            pass
            except Exception as e:
                logger.debug("Error checking tenant Integration row for webhook secret: %s", type(e).__name__)

        # Fallback to settings / env
        fallback_secret = getattr(settings, 'RAZORPAY_WEBHOOK_SECRET', None) or os.getenv('RAZORPAY_WEBHOOK_SECRET', '')
        if fallback_secret:
            return str(fallback_secret).strip()

        # In dev/test/uat fallback to KEY_SECRET if WEBHOOK_SECRET not explicitly set
        try:
            _, key_secret = cls.get_credentials(db_alias=alias)
            return key_secret
        except Exception:
            return ''

    @classmethod
    def verify_webhook_signature(
        cls,
        raw_body: bytes,
        signature: str,
        secret: Optional[str] = None,
        db_alias: Optional[str] = None,
    ) -> bool:
        """
        Verifies the HMAC-SHA256 signature sent by Razorpay in X-Razorpay-Signature
        against the raw request bytes and the tenant webhook secret.
        """
        if not signature or not raw_body:
            return False

        webhook_secret = secret or cls.get_webhook_secret(db_alias=db_alias)
        if not webhook_secret:
            logger.warning("No Razorpay webhook secret configured for signature verification.")
            return False

        import hmac
        import hashlib

        if isinstance(raw_body, str):
            raw_body = raw_body.encode('utf-8')

        expected_signature = hmac.new(
            webhook_secret.encode('utf-8'),
            raw_body,
            hashlib.sha256
        ).hexdigest()

        return hmac.compare_digest(str(expected_signature).lower(), str(signature).lower().strip())


class RazorpayPaymentFinalizerService:
    """
    Authoritative, idempotent convergence service for Razorpay payments.
    Both CRM checkout callbacks, mobile checkout callbacks, and Razorpay webhooks
    converge strictly on this service to guarantee:
    1. Exactly-once payment recording and invoice generation.
    2. Exactly-once membership activation and entitlement quota allocation.
    3. Seamless identity linkage (reusing existing user_profile without duplicate accounts).
    4. Outbox event publication and audit history logging.
    5. Clean recovery from captured payments with interrupted local activations.
    """

    @classmethod
    def finalize_payment(
        cls,
        razorpay_order_id: str,
        razorpay_payment_id: str,
        razorpay_signature: Optional[str] = None,
        order_id: Optional[str] = None,
        source: str = 'CALLBACK',  # 'CALLBACK', 'WEBHOOK', 'MOBILE_CALLBACK', 'RECOVERY'
        payment_method: Optional[str] = None,
        start_date = None,
        actor_user = None,
        db_alias: Optional[str] = None,
        raw_event: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        from datetime import date
        from django.db import transaction
        from django.utils import timezone
        from .models_commerce import Order, PaymentTransaction, MemberInvoice
        from .models_memberships import Membership
        from .models_crm import Lead
        from .services_commerce import CommerceService
        from .services_memberships import MembershipLifecycleService

        alias = db_alias or 'default'

        if not razorpay_order_id or not razorpay_payment_id:
            raise RazorpayVerificationError(
                "Both razorpay_order_id and razorpay_payment_id are required for finalization.",
                code="RAZORPAY_PARAMS_MISSING"
            )

        # 1. Verification for callbacks
        if source in ('CALLBACK', 'MOBILE_CALLBACK'):
            if not razorpay_signature:
                raise RazorpayVerificationError(
                    "Razorpay signature is required for checkout callback.",
                    code="RAZORPAY_SIGNATURE_MISSING"
                )
            if not RazorpayService.verify_payment_signature(razorpay_order_id, razorpay_payment_id, razorpay_signature, db_alias=alias):
                raise RazorpayVerificationError(
                    "Invalid Razorpay payment signature.",
                    code="RAZORPAY_SIGNATURE_INVALID"
                )

        # 2. Locate internal order
        order = None
        if order_id:
            order = Order.objects.using(alias).filter(id=order_id).first()

        if not order and razorpay_order_id:
            # Check by PaymentTransaction metadata
            tx = PaymentTransaction.objects.using(alias).filter(
                metadata__razorpay_order_id=razorpay_order_id
            ).select_related('order').first()
            if tx:
                order = tx.order
            else:
                # Check idempotency_key
                tx_idem = PaymentTransaction.objects.using(alias).filter(
                    idempotency_key=f"rzp_order_{razorpay_order_id}"
                ).select_related('order').first()
                if tx_idem:
                    order = tx_idem.order

        if not order and raw_event:
            # Check notes from raw webhook event
            payment_entity = raw_event.get('payload', {}).get('payment', {}).get('entity', {})
            order_entity = raw_event.get('payload', {}).get('order', {}).get('entity', {})
            notes = payment_entity.get('notes') or order_entity.get('notes') or {}
            internal_id = notes.get('internal_order_id')
            if internal_id:
                order = Order.objects.using(alias).filter(id=internal_id).first()

        if not order:
            logger.error("Cannot find internal order for Razorpay order %s (payment %s)", razorpay_order_id, razorpay_payment_id)
            raise RazorpayVerificationError(
                f"Internal order not found for Razorpay order {razorpay_order_id}",
                code="ORDER_NOT_FOUND"
            )

        # 3. Server-side API cross-check when credentials configured
        if RazorpayService.is_configured(db_alias=alias):
            expected_paise = RazorpayService.convert_inr_to_paise(order.total_amount)
            try:
                RazorpayService.fetch_and_verify_payment(
                    razorpay_payment_id=razorpay_payment_id,
                    expected_order_id=razorpay_order_id,
                    expected_amount_paise=expected_paise,
                    expected_currency=order.currency,
                    db_alias=alias,
                )
            except Exception as e:
                # If error is mismatch, reject
                if isinstance(e, RazorpayVerificationError):
                    logger.warning("Razorpay API cross-check verification failed: %s", e)
                    raise

        # 4. Atomic finalization
        with transaction.atomic(using=alias):
            order = Order.objects.using(alias).select_for_update().get(id=order.id)
            lead_locked = None
            if order.lead:
                lead_locked = Lead.objects.using(alias).select_for_update().filter(id=order.lead.id).first()

            # Ensure user profile exists on order
            if not order.user_profile and lead_locked:
                from .services_crm import LeadConversionService
                profile, _ = LeadConversionService.resolve_or_create_member_identity(
                    lead=lead_locked,
                    branch=order.branch,
                    actor_user=actor_user,
                    db_alias=alias,
                )
                order.user_profile = profile
                order.save(using=alias, update_fields=['user_profile'])

            existing_membership = Membership.objects.using(alias).filter(source_order=order).first()
            if not existing_membership and order.user_profile:
                existing_membership = Membership.objects.using(alias).filter(
                    user_profile=order.user_profile,
                    status__in=['ACTIVE', 'SUSPENDED', 'EXPIRED']
                ).order_by('-created_at').first()

            # Check if order is already paid AND membership is active (Idempotent replay)
            if order.status == 'PAID' and existing_membership:
                logger.info(
                    "Razorpay payment already finalized for order %s, membership %s. Returning existing state.",
                    order.id, existing_membership.id
                )
                return {
                    'status': 'ALREADY_FINALIZED',
                    'order_id': str(order.id),
                    'order_number': order.order_number,
                    'membership_id': str(existing_membership.id),
                    'membership_status': existing_membership.status,
                    'membership_activated': True,
                    'message': 'Payment already finalized and membership active.',
                }

            # If order is not paid, record payment
            if order.status != 'PAID':
                provider_txn_id = razorpay_payment_id
                payment_idem_key = f"rzp_pay_{razorpay_payment_id}"
                pay_metadata = {
                    'razorpay_order_id': razorpay_order_id,
                    'razorpay_payment_id': razorpay_payment_id,
                    'razorpay_signature': razorpay_signature or '',
                    'source': source,
                    'finalized_at': timezone.now().isoformat(),
                }
                CommerceService.record_payment(
                    order_id=str(order.id),
                    amount=order.total_amount,
                    provider='RAZORPAY',
                    payment_method=payment_method or 'RAZORPAY',
                    provider_transaction_id=provider_txn_id,
                    idempotency_key=payment_idem_key,
                    metadata=pay_metadata,
                    actor=actor_user,
                    db_alias=alias,
                )
                order.refresh_from_db(using=alias)

            # Activate membership if not active
            membership = existing_membership
            if not membership:
                order_item = order.items.using(alias).filter(item_type='PACKAGE').first()
                if not order_item or not order_item.package_version:
                    raise ValidationError("Order is missing a valid package version item.")

                if isinstance(start_date, str) and start_date:
                    from datetime import date as _d
                    s_date = _d.fromisoformat(start_date)
                elif isinstance(start_date, date):
                    s_date = start_date
                else:
                    s_date = timezone.now().date()

                membership = MembershipLifecycleService.activate_membership_from_order(
                    order=order,
                    order_item=order_item,
                    start_date=s_date,
                    db_alias=alias,
                    created_by_user=actor_user,
                )

            # Finalize CRM Lead conversion if lead is associated and not yet converted
            if lead_locked and lead_locked.current_status != 'CONVERTED':
                from .services_crm import LeadConversionService
                LeadConversionService._finalize_conversion_for_order(
                    order=order,
                    start_date=start_date,
                    actor_user=actor_user,
                    db_alias=alias,
                )

            return {
                'status': 'SUCCESS',
                'order_id': str(order.id),
                'order_number': order.order_number,
                'membership_id': str(membership.id) if membership else None,
                'membership_status': membership.status if membership else None,
                'membership_activated': bool(membership),
                'source': source,
            }
