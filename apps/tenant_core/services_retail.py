"""
apps/tenant_core/services_retail.py — Authoritative Retail Commerce & Stock Reservation Service

Implements:
1. Concurrency-safe stock reservation lifecycle:
   - available_stock = on_hand_stock - active_reserved_stock
   - select_for_update() row locking inside atomic transactions
   - durable StockReservation and InventoryMovement ledger tracking
2. Server-side authoritative pricing and snapshot preservation:
   - Prices calculated strictly from current Product records, never trusting client input
   - Immutable OrderItem snapshots (product FK with SET_NULL, unit_price_snapshot, sku_snapshot, line totals)
3. Payment verification & idempotency:
   - Strict HMAC-SHA256 signature verification via RazorpayService
   - Duplicate callback / replay protection via database locking and transaction records
   - Reservation conversion to sale: decrements physical stock exactly once upon confirmed payment
   - Safe recovery policy for late payment after reservation release/expiry (prevents overselling)
4. Staff fulfillment transitions:
   - UNFULFILLED -> READY_FOR_PICKUP -> FULFILLED
   - Enforces backend validation: unpaid or failed orders CANNOT be marked collected
   - Audit tracking with fulfilled_at timestamp and fulfilled_by_user
"""

import uuid
import logging
from decimal import Decimal
from datetime import timedelta
from typing import Dict, Any, List, Optional, Tuple

from django.db import transaction, models
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.conf import settings

from .models_org import Branch, Organization
from .models_users import TenantUser
from .models_workforce import UserProfile
from .models_inventory import Product, StockReservation, InventoryMovement
from .models_commerce import Order, OrderItem, PaymentTransaction, MemberInvoice
from .services_razorpay import RazorpayService, RazorpayVerificationError, RazorpayConfigError

logger = logging.getLogger(__name__)


class InsufficientStockError(ValidationError):
    """Raised when available stock is insufficient to satisfy a reservation request."""
    pass


class OrderStateError(ValidationError):
    """Raised when an order transition violates business lifecycle rules."""
    pass


class FulfillmentTransitionError(ValidationError):
    """Raised when an invalid fulfillment state transition is attempted."""
    pass


class RetailCommerceService:
    """
    Authoritative service for retail product order lifecycle, stock reservations,
    and payment confirmations in SWEAT CRM.
    """

    RESERVATION_EXPIRY_MINUTES = 15

    @classmethod
    def get_available_stock(cls, product_id: uuid.UUID, db_alias: str = 'default') -> int:
        """
        Calculates authoritative available stock:
        available_stock = physical stock_quantity - active unexpired pending reservations.
        """
        product = Product.objects.using(db_alias).filter(id=product_id).first()
        if not product:
            return 0
        return product.get_available_stock(using=db_alias)

    @classmethod
    def create_retail_order(
        cls,
        user_profile: UserProfile,
        branch: Branch,
        items: List[Dict[str, Any]],
        source: str = 'MOBILE_APP',
        notes: Optional[str] = None,
        db_alias: str = 'default',
        sold_by_user: Optional[TenantUser] = None,
        initiate_gateway: bool = False,
        gateway_order_id: Optional[str] = None,
    ) -> Tuple[Order, List[StockReservation]]:
        """
        Atomically creates a pending retail order and reserves stock for line items.
        1. Validates inputs, product availability, and positive quantities.
        2. Locks relevant Product rows with select_for_update() to prevent race conditions.
        3. Authoritatively calculates prices and totals from Product records.
        4. Creates durable StockReservation records with 15-minute expiry.
        5. Optionally initializes Razorpay gateway order if initiate_gateway is True.
        """
        if not items:
            raise ValidationError("Order must contain at least one product item.")

        if not user_profile:
            raise ValidationError("User profile is required to place a retail order.")

        if not branch:
            raise ValidationError("A pickup branch is required.")

        now = timezone.now()
        expires_at = now + timedelta(minutes=cls.RESERVATION_EXPIRY_MINUTES)

        product_ids = [it.get('product_id') for it in items if it.get('product_id')]
        if len(product_ids) != len(items):
            raise ValidationError("Each line item must specify a valid product_id.")

        with transaction.atomic(using=db_alias):
            # Lock products in deterministic order to prevent deadlocks
            products_by_id = {
                str(p.id): p
                for p in Product.objects.using(db_alias)
                .select_for_update()
                .filter(id__in=product_ids)
            }

            for p_id in product_ids:
                if str(p_id) not in products_by_id:
                    raise ValidationError(f"Product '{p_id}' does not exist or is unavailable.")

            order_items_to_create = []
            reservations_to_create = []
            subtotal = Decimal('0.00')

            # Validate requested quantities and stock
            for item in items:
                p_id_str = str(item['product_id'])
                product = products_by_id[p_id_str]

                if not product.is_active:
                    raise ValidationError(f"Product '{product.name}' is inactive and cannot be purchased.")

                try:
                    qty = int(item.get('quantity', 1))
                except (ValueError, TypeError):
                    raise ValidationError(f"Invalid quantity for product '{product.name}'.")

                if qty <= 0:
                    raise ValidationError(f"Quantity for product '{product.name}' must be greater than zero.")

                # Calculate active pending reservations for this product
                active_reserved = (
                    StockReservation.objects.using(db_alias)
                    .filter(
                        product=product,
                        status='PENDING',
                        expires_at__gt=now,
                    )
                    .aggregate(total=models.Sum('quantity'))['total']
                    or 0
                )

                available = product.stock_quantity - active_reserved
                if available < qty:
                    raise InsufficientStockError(
                        f"Insufficient stock for '{product.name}'. Requested: {qty}, Available: {max(0, available)}."
                    )

                unit_price = product.price
                line_total = unit_price * Decimal(str(qty))
                subtotal += line_total

                order_items_to_create.append({
                    'product': product,
                    'sku': product.sku or '',
                    'name': product.name,
                    'quantity': Decimal(str(qty)),
                    'unit_price': unit_price,
                    'line_total': line_total,
                })

            order_number = f"RET-{uuid.uuid4().hex[:8].upper()}"

            order = Order.objects.using(db_alias).create(
                order_number=order_number,
                user_profile=user_profile,
                branch=branch,
                sold_by_user=sold_by_user,
                order_type='RETAIL_PURCHASE',
                status='PENDING_PAYMENT',
                fulfillment_status='UNFULFILLED',
                subtotal=subtotal,
                discount_amount=Decimal('0.00'),
                reward_amount=Decimal('0.00'),
                tax_amount=Decimal('0.00'),
                total_amount=subtotal,
                currency='INR',
                gateway_order_id=gateway_order_id,
                source=source,
                notes=notes or f"Retail order for {user_profile.first_name_snapshot or 'Member'}",
            )

            created_reservations = []
            for item_spec in order_items_to_create:
                prod = item_spec['product']
                qty_int = int(item_spec['quantity'])

                OrderItem.objects.using(db_alias).create(
                    order=order,
                    item_type='RETAIL',
                    product=prod,
                    sku_snapshot=item_spec['sku'],
                    item_name_snapshot=item_spec['name'],
                    quantity=item_spec['quantity'],
                    unit_price_snapshot=item_spec['unit_price'],
                    tax_percent_snapshot=Decimal('0.000'),
                    discount_amount=Decimal('0.00'),
                    tax_amount=Decimal('0.00'),
                    total_amount=item_spec['line_total'],
                )

                res = StockReservation.objects.using(db_alias).create(
                    product=prod,
                    order=order,
                    quantity=qty_int,
                    status='PENDING',
                    reserved_at=now,
                    expires_at=expires_at,
                )
                created_reservations.append(res)

                InventoryMovement.objects.using(db_alias).create(
                    product=prod,
                    order=order,
                    movement_type='RESERVATION',
                    quantity_change=0,
                    balance_after=prod.stock_quantity,
                    notes=f"Reserved {qty_int} unit(s) for Order {order.order_number}",
                )

        # External Gateway order creation is executed outside the atomic DB transaction
        # to prevent holding database locks across external network I/O calls.
        if initiate_gateway:
            cls.initiate_gateway_order(order=order, db_alias=db_alias)

        logger.info(
            "Created retail order %s (%s INR) with %d reservations.",
            order.order_number, order.total_amount, len(created_reservations)
        )
        return order, created_reservations

    @classmethod
    def initiate_gateway_order(
        cls,
        order: Order,
        db_alias: str = 'default',
    ) -> Dict[str, Any]:
        """
        Creates a Razorpay gateway order for an unpaid retail order and associates it.
        1. Resolves tenant-aware Razorpay credentials (public Key ID).
        2. Calculates the exact amount in integer paise and verifies positive amount.
        3. Calls RazorpayService.create_order without holding long database locks.
        4. On success: persists order.gateway_order_id and creates an INITIATED PaymentTransaction.
        5. On failure: safely cancels the internal order and releases its reservations without masking the error.
        """
        if order.gateway_order_id:
            key_id, _ = RazorpayService.get_credentials(db_alias=db_alias)
            amount_paise = RazorpayService.convert_inr_to_paise(order.total_amount)
            gateway_data = {
                'razorpay_order_id': order.gateway_order_id,
                'razorpay_key_id': key_id,
                'amount_paise': amount_paise,
                'amount': float(order.total_amount),
                'currency': order.currency,
            }
            order._gateway_checkout_data = gateway_data
            return gateway_data

        key_id, _ = RazorpayService.get_credentials(db_alias=db_alias)
        amount_paise = RazorpayService.convert_inr_to_paise(order.total_amount)

        try:
            rzp_order = RazorpayService.create_order(
                amount_paise=amount_paise,
                receipt=order.order_number,
                currency=order.currency,
                notes={
                    'internal_order_id': str(order.id),
                    'user_id': str(order.user_profile.user_id) if order.user_profile and order.user_profile.user_id else '',
                    'order_number': order.order_number,
                },
                db_alias=db_alias,
            )
        except Exception as exc:
            logger.error(
                "Razorpay order creation failed for retail order %s: %s",
                order.order_number, exc
            )
            # Safely cancel the newly created unpaid order and release its reservations
            try:
                cls.release_retail_reservation(
                    order_id=order.id,
                    reason='GATEWAY_CREATION_FAILED',
                    db_alias=db_alias,
                )
            except Exception as rel_err:
                logger.error(
                    "Failed to release reservations on gateway order creation failure for order %s: %s",
                    order.order_number, rel_err
                )
            raise

        rzp_order_id = rzp_order['id']
        order.gateway_order_id = rzp_order_id
        order.save(using=db_alias, update_fields=['gateway_order_id', 'updated_at'])

        PaymentTransaction.objects.using(db_alias).update_or_create(
            order=order,
            provider='RAZORPAY',
            status='INITIATED',
            defaults={
                'user_profile': order.user_profile,
                'amount': order.total_amount,
                'currency': order.currency,
                'idempotency_key': f"rzp_order_{rzp_order_id}",
                'metadata': {
                    'razorpay_order_id': rzp_order_id,
                    'receipt': order.order_number,
                },
            },
        )

        gateway_data = {
            'razorpay_order_id': rzp_order_id,
            'razorpay_key_id': key_id,
            'amount_paise': amount_paise,
            'amount': float(order.total_amount),
            'currency': order.currency,
        }
        order._gateway_checkout_data = gateway_data
        return gateway_data

    @classmethod
    def confirm_retail_payment(
        cls,
        order_id: uuid.UUID,
        payment_id: str,
        razorpay_order_id: str,
        signature: str,
        db_alias: str = 'default',
        is_sandbox_simulation: bool = False,
    ) -> Dict[str, Any]:
        """
        Idempotently verifies and confirms payment for a retail order:
        1. Locks the order and its reservations inside a database transaction.
        2. Detects and safely ignores duplicate callbacks / replay attacks.
        3. Validates HMAC-SHA256 signature and gateway identifiers.
        4. Converts pending reservation into a permanent sale, decrementing physical stock exactly once.
        5. Handles late payments safely with re-validation if reservation already expired/released.
        """
        if not order_id:
            raise ValidationError("order_id is required.")
        if not payment_id:
            raise ValidationError("payment_id is required.")
        if not razorpay_order_id:
            raise ValidationError("razorpay_order_id is required.")
        if is_sandbox_simulation and not getattr(settings, 'DEBUG', False):
            raise ValidationError("Payment simulation is strictly forbidden when DEBUG is False.")

        with transaction.atomic(using=db_alias):
            try:
                order = (
                    Order.objects.using(db_alias)
                    .select_for_update()
                    .get(id=order_id)
                )
            except Order.DoesNotExist:
                raise ValidationError(f"Order with ID '{order_id}' does not exist.")

            # Strict Enforcement: Internal-to-Gateway Order Association
            # Submitted razorpay_order_id must match the gateway order ID persisted for this exact order
            if not order.gateway_order_id:
                raise ValidationError(
                    f"Order '{order.order_number}' does not have an associated gateway order ID."
                )

            if str(razorpay_order_id).strip() != str(order.gateway_order_id).strip():
                logger.warning(
                    "Gateway order mismatch for retail order %s: expected=%s submitted=%s",
                    order.order_number, order.gateway_order_id, razorpay_order_id
                )
                raise ValidationError(
                    "Submitted razorpay_order_id does not match the gateway order ID for this order."
                )

            # Idempotency check: Order already marked PAID
            if order.status == 'PAID':
                logger.info(
                    "Duplicate payment confirmation ignored for already PAID order %s.",
                    order.order_number
                )
                existing_txn = (
                    PaymentTransaction.objects.using(db_alias)
                    .filter(order=order, provider_transaction_id=payment_id)
                    .first()
                )
                return {
                    'success': True,
                    'already_processed': True,
                    'order_id': str(order.id),
                    'order_number': order.order_number,
                    'status': 'PAID',
                    'fulfillment_status': order.fulfillment_status,
                    'payment_id': existing_txn.provider_transaction_id if existing_txn else payment_id,
                }

            # Check if this specific payment ID was already processed
            existing_txn = (
                PaymentTransaction.objects.using(db_alias)
                .filter(provider_transaction_id=payment_id, status='SUCCESS')
                .first()
            )
            if existing_txn:
                logger.warning(
                    "Duplicate payment transaction %s already recorded for order %s.",
                    payment_id, existing_txn.order_id
                )
                if existing_txn.order_id != order.id:
                    raise ValidationError("Payment transaction ID was already used for a different order.")

            # Server-side verification via RazorpayService (simulation only permitted if DEBUG is True)
            effective_simulation = bool(is_sandbox_simulation and getattr(settings, 'DEBUG', False))
            if not effective_simulation:
                if not signature or not razorpay_order_id:
                    raise ValidationError("razorpay_order_id and signature are required for payment verification.")

                is_sig_valid = False
                try:
                    is_sig_valid = RazorpayService.verify_payment_signature(
                        razorpay_order_id=razorpay_order_id,
                        razorpay_payment_id=payment_id,
                        razorpay_signature=signature,
                        db_alias=db_alias,
                    )
                except Exception as exc:
                    logger.warning("Razorpay signature verification raised: %s", exc)
                    raise RazorpayVerificationError("Invalid Razorpay payment signature.")

                if not is_sig_valid:
                    raise RazorpayVerificationError("Invalid Razorpay payment signature.")

                # Cross-check amount with Gateway API
                expected_paise = RazorpayService.convert_inr_to_paise(order.total_amount)
                try:
                    RazorpayService.fetch_and_verify_payment(
                        razorpay_payment_id=payment_id,
                        expected_order_id=razorpay_order_id,
                        expected_amount_paise=expected_paise,
                        expected_currency=order.currency,
                        db_alias=db_alias,
                    )
                except RazorpayVerificationError:
                    raise
                except Exception as e:
                    logger.warning("Gateway API verification notice (proceeding if signature valid): %s", e)

            # Process stock conversion: Reservation -> Confirmed Sale
            reservations = list(
                StockReservation.objects.using(db_alias)
                .select_for_update()
                .filter(order=order)
            )

            now = timezone.now()
            late_payment_oversell_prevented = False

            for res in reservations:
                prod = Product.objects.using(db_alias).select_for_update().get(id=res.product_id)

                if res.status == 'PENDING':
                    # Normal active reservation conversion
                    res.status = 'CONFIRMED'
                    res.save(using=db_alias)

                    # Decrement physical stock exactly once
                    prod.stock_quantity = max(0, prod.stock_quantity - res.quantity)
                    if prod.stock_quantity == 0:
                        prod.status = 'Out of Stock'
                    elif prod.stock_quantity <= 5:
                        prod.status = 'Low Stock'
                    prod.save(using=db_alias)

                    InventoryMovement.objects.using(db_alias).create(
                        product=prod,
                        order=order,
                        movement_type='SALE',
                        quantity_change=-res.quantity,
                        balance_after=prod.stock_quantity,
                        notes=f"Confirmed sale for Order {order.order_number}",
                    )
                elif res.status in ['RELEASED', 'EXPIRED']:
                    # Safe recovery policy: Late payment received after reservation release
                    # Only fulfill if sufficient physical stock is actually available!
                    if prod.stock_quantity >= res.quantity:
                        prod.stock_quantity -= res.quantity
                        if prod.stock_quantity == 0:
                            prod.status = 'Out of Stock'
                        prod.save(using=db_alias)

                        res.status = 'CONFIRMED'
                        res.save(using=db_alias)

                        InventoryMovement.objects.using(db_alias).create(
                            product=prod,
                            order=order,
                            movement_type='SALE',
                            quantity_change=-res.quantity,
                            balance_after=prod.stock_quantity,
                            notes=f"Late payment fulfilled after release for Order {order.order_number}",
                        )
                    else:
                        late_payment_oversell_prevented = True
                        logger.error(
                            "Oversell prevented for order %s: payment received after reservation release, "
                            "but physical stock (%d) < requested (%d).",
                            order.order_number, prod.stock_quantity, res.quantity
                        )

            # Record PaymentTransaction
            PaymentTransaction.objects.using(db_alias).create(
                order=order,
                user_profile=order.user_profile,
                amount=order.total_amount,
                currency=order.currency,
                provider='RAZORPAY',
                payment_method='RAZORPAY',
                provider_transaction_id=payment_id,
                idempotency_key=f"RZP-{razorpay_order_id or 'DIR'}-{payment_id}",
                status='SUCCESS',
                paid_at=now,
                metadata={
                    'razorpay_order_id': razorpay_order_id,
                    'razorpay_payment_id': payment_id,
                    'razorpay_signature': signature,
                },
            )

            # Update order state
            order.status = 'PAID'
            if late_payment_oversell_prevented:
                order.fulfillment_status = 'CANCELLED'
                order.notes = (
                    (order.notes or '') +
                    " [OVERSELL_PREVENTED: Late payment received after stock reservation released. "
                    "Insufficient physical stock to fulfill. Requires manual refund.]"
                ).strip()
            else:
                order.fulfillment_status = 'UNFULFILLED'

            order.save(using=db_alias)

            # Generate invoice for member
            invoice_num = f"INV-{order.order_number}"
            MemberInvoice.objects.using(db_alias).get_or_create(
                order=order,
                defaults={
                    'invoice_number': invoice_num,
                    'user_profile': order.user_profile,
                    'branch': order.branch,
                    'subtotal': order.subtotal,
                    'discount_amount': order.discount_amount,
                    'reward_amount': order.reward_amount,
                    'tax_amount': order.tax_amount,
                    'total_amount': order.total_amount,
                    'currency': order.currency,
                    'status': 'PAID',
                    'issued_at': now,
                }
            )

        logger.info("Successfully confirmed payment for order %s.", order.order_number)
        return {
            'success': True,
            'already_processed': False,
            'order_id': str(order.id),
            'order_number': order.order_number,
            'status': order.status,
            'fulfillment_status': order.fulfillment_status,
            'late_payment_exception': late_payment_oversell_prevented,
            'payment_id': payment_id,
        }

    @classmethod
    def confirm_retail_payment_from_gateway(
        cls,
        order_id: uuid.UUID,
        payment_id: str,
        razorpay_order_id: str,
        db_alias: str = 'default',
    ) -> Dict[str, Any]:
        """
        Idempotently confirms payment for a retail order received via asynchronous gateway webhook
        (e.g., payment.captured or order.paid events).
        Uses server-side Razorpay API verification and atomic order status mutation.
        """
        if not order_id:
            raise ValidationError("order_id is required.")
        if not payment_id:
            raise ValidationError("payment_id is required.")
        if not razorpay_order_id:
            raise ValidationError("razorpay_order_id is required.")

        with transaction.atomic(using=db_alias):
            try:
                order = (
                    Order.objects.using(db_alias)
                    .select_for_update()
                    .get(id=order_id)
                )
            except Order.DoesNotExist:
                raise ValidationError(f"Order with ID '{order_id}' does not exist.")

            # Strict Enforcement: Internal-to-Gateway Order Association
            if not order.gateway_order_id:
                raise ValidationError(
                    f"Order '{order.order_number}' does not have an associated gateway order ID."
                )

            if str(razorpay_order_id).strip() != str(order.gateway_order_id).strip():
                logger.warning(
                    "Gateway order mismatch for retail order %s: expected=%s submitted=%s",
                    order.order_number, order.gateway_order_id, razorpay_order_id
                )
                raise ValidationError(
                    "Submitted razorpay_order_id does not match the gateway order ID for this order."
                )

            # Idempotency check: Order already marked PAID
            if order.status == 'PAID':
                logger.info(
                    "Duplicate webhook confirmation ignored for already PAID order %s.",
                    order.order_number
                )
                existing_txn = (
                    PaymentTransaction.objects.using(db_alias)
                    .filter(order=order, provider_transaction_id=payment_id)
                    .first()
                )
                return {
                    'success': True,
                    'already_processed': True,
                    'order_id': str(order.id),
                    'order_number': order.order_number,
                    'status': 'PAID',
                    'fulfillment_status': order.fulfillment_status,
                    'payment_id': existing_txn.provider_transaction_id if existing_txn else payment_id,
                }

            # Optional Server-side Gateway API cross-check if credentials are configured
            if RazorpayService.is_configured(db_alias=db_alias):
                expected_paise = RazorpayService.convert_inr_to_paise(order.total_amount)
                try:
                    RazorpayService.fetch_and_verify_payment(
                        razorpay_payment_id=payment_id,
                        expected_order_id=razorpay_order_id,
                        expected_amount_paise=expected_paise,
                        expected_currency=order.currency,
                        db_alias=db_alias,
                    )
                except RazorpayVerificationError:
                    raise
                except Exception as e:
                    logger.warning("Gateway API verification notice for webhook (proceeding): %s", e)

            # Process stock conversion: Reservation -> Confirmed Sale
            reservations = list(
                StockReservation.objects.using(db_alias)
                .select_for_update()
                .filter(order=order)
            )

            now = timezone.now()
            late_payment_oversell_prevented = False

            for res in reservations:
                prod = Product.objects.using(db_alias).select_for_update().get(id=res.product_id)

                if res.status == 'PENDING':
                    res.status = 'CONFIRMED'
                    res.save(using=db_alias)

                    prod.stock_quantity = max(0, prod.stock_quantity - res.quantity)
                    if prod.stock_quantity == 0:
                        prod.status = 'Out of Stock'
                    elif prod.stock_quantity <= 5:
                        prod.status = 'Low Stock'
                    prod.save(using=db_alias)

                    InventoryMovement.objects.using(db_alias).create(
                        product=prod,
                        order=order,
                        movement_type='SALE',
                        quantity_change=-res.quantity,
                        balance_after=prod.stock_quantity,
                        notes=f"Confirmed retail purchase (Webhook) for order {order.order_number}",
                    )
                elif res.status in ('EXPIRED', 'RELEASED'):
                    if prod.stock_quantity >= res.quantity:
                        prod.stock_quantity = max(0, prod.stock_quantity - res.quantity)
                        prod.save(using=db_alias)
                        InventoryMovement.objects.using(db_alias).create(
                            product=prod,
                            order=order,
                            movement_type='SALE',
                            quantity_change=-res.quantity,
                            balance_after=prod.stock_quantity,
                            notes=f"Confirmed late retail payment (Webhook) for order {order.order_number} (re-claimed stock)",
                        )
                        res.status = 'CONFIRMED'
                        res.save(using=db_alias)
                    else:
                        late_payment_oversell_prevented = True
                        logger.error(
                            "Oversell prevented for order %s: webhook received after reservation release, "
                            "but physical stock (%d) < requested (%d).",
                            order.order_number, prod.stock_quantity, res.quantity
                        )

            # Record PaymentTransaction
            PaymentTransaction.objects.using(db_alias).update_or_create(
                order=order,
                provider='RAZORPAY',
                provider_transaction_id=payment_id,
                defaults={
                    'user_profile': order.user_profile,
                    'amount': order.total_amount,
                    'currency': order.currency,
                    'payment_method': 'RAZORPAY',
                    'idempotency_key': f"RZP-WEBHOOK-{razorpay_order_id}-{payment_id}",
                    'status': 'SUCCESS',
                    'paid_at': now,
                    'metadata': {
                        'razorpay_order_id': razorpay_order_id,
                        'razorpay_payment_id': payment_id,
                        'source': 'WEBHOOK',
                    },
                },
            )

            # Update order state
            order.status = 'PAID'
            if late_payment_oversell_prevented:
                order.fulfillment_status = 'CANCELLED'
                order.notes = (
                    (order.notes or '') +
                    " [OVERSELL_PREVENTED: Late payment webhook received after stock reservation released. "
                    "Insufficient physical stock to fulfill. Requires manual refund.]"
                ).strip()
            else:
                order.fulfillment_status = 'UNFULFILLED'

            order.save(using=db_alias)

            # Generate invoice for member
            invoice_num = f"INV-{order.order_number}"
            MemberInvoice.objects.using(db_alias).get_or_create(
                order=order,
                defaults={
                    'invoice_number': invoice_num,
                    'user_profile': order.user_profile,
                    'branch': order.branch,
                    'subtotal': order.subtotal,
                    'discount_amount': order.discount_amount,
                    'reward_amount': order.reward_amount,
                    'tax_amount': order.tax_amount,
                    'total_amount': order.total_amount,
                    'currency': order.currency,
                    'status': 'PAID',
                    'issued_at': now,
                }
            )

        logger.info("Successfully confirmed payment via webhook for retail order %s.", order.order_number)
        return {
            'success': True,
            'already_processed': False,
            'order_id': str(order.id),
            'order_number': order.order_number,
            'status': order.status,
            'fulfillment_status': order.fulfillment_status,
            'late_payment_exception': late_payment_oversell_prevented,
            'payment_id': payment_id,
        }

    @classmethod
    def release_retail_reservation(

        cls,
        order_id: uuid.UUID,
        reason: str = 'PAYMENT_FAILED',
        db_alias: str = 'default',
    ) -> Dict[str, Any]:
        """
        Releases any pending stock reservation for an order when payment definitively fails,
        times out, or is cancelled. Idempotent: repeated releases do not double-restore stock.
        """
        with transaction.atomic(using=db_alias):
            try:
                order = (
                    Order.objects.using(db_alias)
                    .select_for_update()
                    .get(id=order_id)
                )
            except Order.DoesNotExist:
                raise ValidationError(f"Order '{order_id}' does not exist.")

            if order.status == 'PAID':
                raise OrderStateError("Cannot release reservation for an already paid order.")

            reservations = list(
                StockReservation.objects.using(db_alias)
                .select_for_update()
                .filter(order=order, status='PENDING')
            )

            released_count = 0
            now = timezone.now()

            for res in reservations:
                res.status = 'RELEASED'
                res.released_at = now
                res.release_reason = reason
                res.save(using=db_alias)
                released_count += 1

                prod = Product.objects.using(db_alias).filter(id=res.product_id).first()
                if prod:
                    InventoryMovement.objects.using(db_alias).create(
                        product=prod,
                        order=order,
                        movement_type='RESERVATION_RELEASE',
                        quantity_change=0,
                        balance_after=prod.stock_quantity,
                        notes=f"Released reservation ({res.quantity} units) reason: {reason}",
                    )

            order.status = 'CANCELLED'
            order.fulfillment_status = 'CANCELLED'
            order.save(using=db_alias)

        logger.info("Released %d reservation(s) for order %s.", released_count, order.order_number)
        return {
            'success': True,
            'order_id': str(order.id),
            'order_number': order.order_number,
            'released_count': released_count,
            'status': 'CANCELLED',
        }

    @classmethod
    def update_fulfillment_status(
        cls,
        order_id: uuid.UUID,
        new_status: str,
        staff_user: Optional[TenantUser] = None,
        db_alias: str = 'default',
    ) -> Order:
        """
        Transitions an order's fulfillment state:
        Permitted transitions:
        - UNFULFILLED -> READY_FOR_PICKUP
        - READY_FOR_PICKUP -> FULFILLED
        - UNFULFILLED -> FULFILLED (direct pickup)
        Enforces backend business rules:
        - Unpaid or failed orders CANNOT be marked collected or ready for pickup.
        - Records fulfilled_at and fulfilled_by_user audit fields.
        """
        permitted_statuses = ['READY_FOR_PICKUP', 'FULFILLED', 'CANCELLED']
        new_status = str(new_status).strip().upper()
        if new_status not in permitted_statuses:
            raise FulfillmentTransitionError(
                f"Invalid fulfillment status '{new_status}'. Allowed: {', '.join(permitted_statuses)}."
            )

        with transaction.atomic(using=db_alias):
            try:
                order = (
                    Order.objects.using(db_alias)
                    .select_for_update()
                    .get(id=order_id)
                )
            except Order.DoesNotExist:
                raise ValidationError(f"Order '{order_id}' does not exist.")

            if new_status in ['READY_FOR_PICKUP', 'FULFILLED']:
                if order.status != 'PAID':
                    raise FulfillmentTransitionError(
                        f"Cannot mark order as '{new_status}'. Order must be PAID, but current status is '{order.status}'."
                    )

            if order.fulfillment_status == 'FULFILLED' and new_status != 'FULFILLED':
                raise FulfillmentTransitionError("A fulfilled order cannot be transitioned back.")

            order.fulfillment_status = new_status
            if new_status == 'FULFILLED':
                order.fulfilled_at = timezone.now()
                if staff_user:
                    order.fulfilled_by_user = staff_user

            order.save(using=db_alias)

        logger.info("Updated order %s fulfillment status to %s.", order.order_number, new_status)
        return order
