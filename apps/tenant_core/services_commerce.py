"""
apps/tenant_core/services_commerce.py — Business Services for Module G: Commerce, Payments & Billing

Key Rules Enforced:
1. Historical Financial Snapshots: Order line items and invoices capture exact prices at transaction time;
   never recomputed dynamically from changing catalog prices.
2. Idempotent Payment Processing: Guarantees payment webhooks and retries never double-charge or create duplicate transactions.
3. Atomic Invoice Issuance: Completed payments automatically generate and issue an immutable MemberInvoice.
4. Comprehensive Auditing & Outbox: Emits structured business audits and reliable domain outbox events.
"""

import uuid
from decimal import Decimal
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError

from .models_commerce import (
    Order,
    OrderItem,
    PaymentTransaction,
    Refund,
    MemberInvoice,
    PaymentLink,
    PaymentLinkEvent,
)
from .models_org import Branch
from .models_users import TenantUser
from .models_workforce import UserProfile
from .models_crm import Lead
from .models_catalog import Package, PackageVersion, PackagePrice
from .models_classes import ClassTemplate, ClassPrice
from .models_appointments import AppointmentType
from .services_reliability import record_business_audit, enqueue_outbox_event


class CommerceService:
    """
    Atomic commerce service managing orders, payments, invoices, refunds, and payment links.
    """

    @classmethod
    @transaction.atomic
    def create_order(
        cls,
        branch: Branch,
        items_data: List[Dict[str, Any]],
        user_profile: Optional[UserProfile] = None,
        lead: Optional[Lead] = None,
        sold_by: Optional[TenantUser] = None,
        order_type: str = 'NEW_MEMBERSHIP',
        source: str = 'FRONT_DESK',
        currency: str = 'INR',
        notes: Optional[str] = None,
        created_by: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> Order:
        """
        Creates an order with its line items atomically.
        Requires either user_profile (existing member) or lead (new prospect).
        """
        alias = db_alias or 'default'

        if not user_profile and not lead:
            raise ValidationError("Order must be associated with either a member (user_profile) or a lead.")

        if not items_data:
            raise ValidationError("Order must contain at least one line item.")

        order_number = f"ORD-{uuid.uuid4().hex[:8].upper()}"

        subtotal = Decimal('0.00')
        total_discount = Decimal('0.00')
        total_tax = Decimal('0.00')
        total_amount = Decimal('0.00')

        prepared_items = []
        for idx, item in enumerate(items_data):
            unit_price = Decimal(str(item.get('unit_price', '0.00')))
            qty = Decimal(str(item.get('quantity', '1.00')))
            tax_pct = Decimal(str(item.get('tax_percent', '0.000')))
            discount = Decimal(str(item.get('discount_amount', '0.00')))

            line_base = unit_price * qty
            line_after_discount = max(Decimal('0.00'), line_base - discount)
            line_tax = (line_after_discount * tax_pct / Decimal('100.00')).quantize(Decimal('0.01'))
            line_total = line_after_discount + line_tax

            subtotal += line_base
            total_discount += discount
            total_tax += line_tax
            total_amount += line_total

            prepared_items.append({
                'item_type': item.get('item_type', 'PACKAGE'),
                'package_id': item.get('package_id'),
                'package_version_id': item.get('package_version_id'),
                'package_price_id': item.get('package_price_id'),
                'class_template_id': item.get('class_template_id'),
                'class_price_id': item.get('class_price_id'),
                'appointment_type_id': item.get('appointment_type_id'),
                'item_name_snapshot': item.get('item_name_snapshot', f"Item #{idx+1}"),
                'quantity': qty,
                'unit_price_snapshot': unit_price,
                'tax_percent_snapshot': tax_pct,
                'discount_amount': discount,
                'tax_amount': line_tax,
                'total_amount': line_total,
            })

        order = Order(
            order_number=order_number,
            branch=branch,
            user_profile=user_profile,
            lead=lead,
            sold_by_user=sold_by,
            order_type=order_type,
            status='PENDING_PAYMENT',
            subtotal=subtotal,
            discount_amount=total_discount,
            tax_amount=total_tax,
            total_amount=total_amount,
            currency=currency,
            source=source,
            notes=notes,
        )
        order.save(using=alias)

        for pi in prepared_items:
            order_item = OrderItem(
                order=order,
                item_type=pi['item_type'],
                package_id=pi['package_id'],
                package_version_id=pi['package_version_id'],
                package_price_id=pi['package_price_id'],
                class_template_id=pi['class_template_id'],
                class_price_id=pi['class_price_id'],
                appointment_type_id=pi['appointment_type_id'],
                item_name_snapshot=pi['item_name_snapshot'],
                quantity=pi['quantity'],
                unit_price_snapshot=pi['unit_price_snapshot'],
                tax_percent_snapshot=pi['tax_percent_snapshot'],
                discount_amount=pi['discount_amount'],
                tax_amount=pi['tax_amount'],
                total_amount=pi['total_amount'],
            )
            order_item.save(using=alias)

        record_business_audit(
            organization=branch.organization,
            branch=branch,
            module='commerce',
            action_code='ORDER_CREATED',
            entity_type='Order',
            entity_id=order.id,
            actor_user=created_by,
            event_description=f"Created Order {order.order_number} for {order.total_amount} {currency}",
            after_data={
                'order_number': order.order_number,
                'total_amount': str(order.total_amount),
                'items_count': len(prepared_items),
                'status': order.status,
            },
            db_alias=alias,
        )

        enqueue_outbox_event(
            organization=branch.organization,
            event_type='ORDER_CREATED',
            aggregate_type='Order',
            aggregate_id=str(order.id),
            payload={
                'order_id': str(order.id),
                'order_number': order.order_number,
                'total_amount': str(order.total_amount),
                'currency': currency,
            },
            db_alias=alias,
        )

        return order

    @classmethod
    def record_payment(
        cls,
        order_id: str,
        amount: Decimal,
        provider: str = 'CASH',
        payment_method: Optional[str] = 'CASH',
        provider_transaction_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        actor: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> Tuple[PaymentTransaction, Optional[MemberInvoice]]:
        """
        Records a payment transaction against an order with strict idempotency protection.
        If payment completes the balance, marks order as PAID and automatically issues MemberInvoice.
        """
        alias = db_alias or 'default'
        with transaction.atomic(using=alias):
            order = Order.objects.using(alias).select_for_update().get(id=order_id)
            if order.status in ['CANCELLED', 'REFUNDED']:
                raise ValidationError(f"Cannot accept payment for order with status {order.status}.")

            # 1. Idempotency check inside lock (protects against concurrent duplicate webhooks)
            if idempotency_key:
                existing_txn = PaymentTransaction.objects.using(alias).filter(
                    idempotency_key=idempotency_key,
                    status='SUCCESS',
                ).first()
                if existing_txn:
                    invoice = MemberInvoice.objects.using(alias).filter(order=order).first()
                    return existing_txn, invoice

            if provider_transaction_id:
                existing_txn = PaymentTransaction.objects.using(alias).filter(
                    provider_transaction_id=provider_transaction_id,
                    status='SUCCESS',
                ).first()
                if existing_txn:
                    invoice = MemberInvoice.objects.using(alias).filter(order=order).first()
                    return existing_txn, invoice

            amt = Decimal(str(amount))
            if amt <= Decimal('0.00'):
                raise ValidationError("Payment amount must be greater than zero.")

            # Create payment record
            txn = PaymentTransaction(
                order=order,
                user_profile=order.user_profile,
                provider=provider,
                payment_method=payment_method,
                provider_transaction_id=provider_transaction_id,
                idempotency_key=idempotency_key,
                amount=amt,
                currency=order.currency,
                status='SUCCESS',
                paid_at=timezone.now(),
                metadata=metadata or {},
            )
            txn.save(using=alias)

            # Compute total paid
            successful_payments = PaymentTransaction.objects.using(alias).filter(
                order=order,
                status='SUCCESS',
            )
            total_paid = sum(p.amount for p in successful_payments)

            invoice = None
            if total_paid >= order.total_amount:
                order.status = 'PAID'
                order.save(using=alias)

                # Issue MemberInvoice
                invoice_number = f"INV-{uuid.uuid4().hex[:8].upper()}"
                invoice = MemberInvoice(
                    invoice_number=invoice_number,
                    order=order,
                    user_profile=order.user_profile,
                    branch=order.branch,
                    subtotal=order.subtotal,
                    discount_amount=order.discount_amount,
                    reward_amount=order.reward_amount,
                    tax_amount=order.tax_amount,
                    total_amount=order.total_amount,
                    status='PAID',
                    issued_at=timezone.now(),
                )
                invoice.save(using=alias)
            else:
                order.status = 'PARTIALLY_PAID'
                order.save(using=alias)

            record_business_audit(
                organization=order.branch.organization,
                branch=order.branch,
                module='commerce',
                action_code='PAYMENT_RECORDED',
                entity_type='PaymentTransaction',
                entity_id=txn.id,
                actor_user=actor,
                metadata={
                    'order_id': str(order.id),
                    'amount': str(amt),
                    'provider': provider,
                    'status': txn.status,
                },
                db_alias=alias,
            )

            enqueue_outbox_event(
                organization=order.branch.organization,
                event_type='commerce.payment_recorded',
                aggregate_type='PaymentTransaction',
                aggregate_id=txn.id,
                payload={
                    'payment_id': str(txn.id),
                    'order_id': str(order.id),
                    'amount': str(amt),
                    'status': txn.status,
                },
                db_alias=alias,
            )

            return txn, invoice

    @classmethod
    def process_refund(
        cls,
        payment_transaction_id: str,
        amount: Decimal,
        reason_code: Optional[str] = None,
        reason_text: Optional[str] = None,
        provider_reference: Optional[str] = None,
        actor: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> Refund:
        alias = db_alias or 'default'
        with transaction.atomic(using=alias):
            txn = PaymentTransaction.objects.using(alias).select_for_update().get(id=payment_transaction_id)
            if txn.status not in ['SUCCESS', 'PARTIALLY_REFUNDED']:
                raise ValidationError(f"Cannot refund a payment transaction with status {txn.status}.")

            refund_amt = Decimal(str(amount))
            if refund_amt <= Decimal('0.00'):
                raise ValidationError("Refund amount must be greater than 0.")

            existing_refunds = Refund.objects.using(alias).filter(payment_transaction=txn, status='SUCCESS')
            already_refunded = sum(r.amount for r in existing_refunds)
            if already_refunded + refund_amt > txn.amount:
                raise ValidationError(
                    f"Total refund amount ({already_refunded + refund_amt}) cannot exceed captured transaction amount ({txn.amount})."
                )

            refund = Refund(
                payment_transaction=txn,
                order=txn.order,
                amount=refund_amt,
                reason_code=reason_code,
                reason_text=reason_text,
                provider_reference=provider_reference,
                status='SUCCESS',
                requested_by_user=actor,
                approved_by_user=actor,
            )
            refund.save(using=alias)

            total_refunded_now = already_refunded + refund_amt
            if total_refunded_now >= txn.amount:
                txn.status = 'REFUNDED'
                order_status = 'REFUNDED'
            else:
                txn.status = 'PARTIALLY_REFUNDED'
                order_status = 'PARTIALLY_REFUNDED'
            txn.save(using=alias)

            order = txn.order
            order.status = order_status
            order.save(using=alias)

            record_business_audit(
                organization=order.branch.organization,
                branch=order.branch,
                module='commerce',
                action_code='REFUND_PROCESSED',
                entity_type='Refund',
                entity_id=refund.id,
                actor_user=actor,
                event_description=f"Processed refund of {refund_amt} for Order {order.order_number}",
                after_data={
                    'order_id': str(order.id),
                    'amount': str(refund_amt),
                    'reason': reason_text,
                },
                db_alias=alias,
            )

            enqueue_outbox_event(
                organization=order.branch.organization,
                event_type='REFUND_PROCESSED',
                aggregate_type='Refund',
                aggregate_id=str(refund.id),
                payload={
                    'order_id': str(order.id),
                    'refund_id': str(refund.id),
                    'amount': str(refund_amt),
                },
                db_alias=alias,
            )

            return refund

    @classmethod
    @transaction.atomic
    def create_payment_link(
        cls,
        order_id: str,
        expiry_hours: int = 48,
        provider: str = 'RAZORPAY',
        actor: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> PaymentLink:
        alias = db_alias or 'default'
        order = Order.objects.using(alias).get(id=order_id)
        if order.status in ['PAID', 'CANCELLED', 'REFUNDED']:
            raise ValidationError(f"Cannot generate payment link for order with status {order.status}.")

        expires_at = timezone.now() + timedelta(hours=expiry_hours)
        payment_url = f"https://pay.sweat.fit/order/{order.order_number}?token={uuid.uuid4().hex}"

        link = PaymentLink(
            order=order,
            user_profile=order.user_profile,
            lead=order.lead,
            provider=provider,
            payment_url=payment_url,
            amount=order.total_amount,
            currency=order.currency,
            expires_at=expires_at,
            status='CREATED',
            created_by_user=actor,
        )
        link.save(using=alias)

        PaymentLinkEvent.objects.using(alias).create(
            payment_link=link,
            event_type='CREATED',
            event_at=timezone.now(),
        )

        record_business_audit(
            organization=order.branch.organization,
            branch=order.branch,
            module='commerce',
            action_code='PAYMENT_LINK_CREATED',
            entity_type='PaymentLink',
            entity_id=link.id,
            actor_user=actor,
            event_description=f"Created payment link for Order {order.order_number}",
            db_alias=alias,
        )

        enqueue_outbox_event(
            organization=order.branch.organization,
            event_type='PAYMENT_LINK_CREATED',
            aggregate_type='PaymentLink',
            aggregate_id=str(link.id),
            payload={
                'order_id': str(order.id),
                'payment_link_id': str(link.id),
                'payment_url': payment_url,
            },
            db_alias=alias,
        )

        return link
