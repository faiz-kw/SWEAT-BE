"""
Sprint 10 — Commercial Billing Services
Provides subscription lifecycle, invoice generation, payment processing,
dunning retries, and webhook processing with transaction safety and full audit logging.
"""

from datetime import datetime, timedelta, time
from decimal import Decimal
import logging
import uuid

from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.master.models import (
    Tenant, TenantSubscription, SaasPlan, SaasPlanPrice,
    SubscriptionInvoice, SubscriptionInvoiceItem, SubscriptionPayment,
    TenantBillingMethod, SubscriptionDunningEvent, BillingWebhookEvent,
    PlatformAuditEvent
)
from apps.master.billing.sequences import allocate_next_invoice_number
from apps.master.billing.tax import DefaultIndiaTaxResolver, TaxCalculationRequest
from apps.master.billing.adapters.base import BasePaymentGatewayAdapter
from apps.master.billing.adapters.mock_adapter import MockPaymentGatewayAdapter

logger = logging.getLogger(__name__)


def audit_billing_action(action, resource_type, resource_id, tenant, description, actor=None, before=None, after=None):
    """
    Emits a Sprint 6-compliant PlatformAuditEvent for billing mutations.
    """
    try:
        PlatformAuditEvent.objects.using('default').create(
            actor=actor,
            actor_email=actor.email if actor and hasattr(actor, 'email') else 'system@performanceos.internal',
            action=action,
            event_name=action,
            resource_type=resource_type,
            resource_id=uuid.UUID(str(resource_id)) if resource_id else None,
            tenant_context=tenant,
            tenant_id=tenant.id if tenant else None,
            description=description,
            before_data=before,
            after_data=after,
            source_application='billing_engine',
        )
    except Exception as exc:
        logger.warning("Failed to record billing audit event: %s", exc)


class SubscriptionLifecycleService:
    """
    Manages tenant subscription state transitions:
    TRIALING -> ACTIVE, ACTIVE -> PAST_DUE, PAST_DUE -> SUSPENDED,
    ACTIVE/TRIALING -> CANCELED, TRIALING -> EXPIRED.
    """

    @classmethod
    @transaction.atomic
    def activate_subscription(cls, subscription: TenantSubscription, actor=None) -> TenantSubscription:
        before = {'status': subscription.status}
        subscription.status = 'ACTIVE'
        now = timezone.now()
        if not subscription.current_period_start or subscription.current_period_start < now - timedelta(days=365):
            subscription.current_period_start = now
            if subscription.billing_cycle == 'ANNUAL':
                subscription.current_period_end = now + timedelta(days=365)
            elif subscription.billing_cycle == 'QUARTERLY':
                subscription.current_period_end = now + timedelta(days=90)
            else:
                subscription.current_period_end = now + timedelta(days=30)
            subscription.next_renewal_at = subscription.current_period_end

        subscription.save()
        audit_billing_action(
            action='ACTIVATE_SUBSCRIPTION',
            resource_type='TenantSubscription',
            resource_id=subscription.id,
            tenant=subscription.tenant,
            description=f"Subscription activated for tenant {subscription.tenant.slug}.",
            actor=actor,
            before=before,
            after={'status': subscription.status, 'next_renewal_at': str(subscription.next_renewal_at)}
        )
        return subscription

    @classmethod
    @transaction.atomic
    def renew_subscription(cls, subscription: TenantSubscription, tax_resolver=None, actor=None) -> tuple[SubscriptionInvoice, TenantSubscription]:
        if subscription.status not in ('ACTIVE', 'PAST_DUE'):
            raise ValidationError(f"Cannot renew subscription in status {subscription.status}.")

        # Advance periods
        start_date = subscription.current_period_end or timezone.now()
        if subscription.billing_cycle == 'ANNUAL':
            end_date = start_date + timedelta(days=365)
        elif subscription.billing_cycle == 'QUARTERLY':
            end_date = start_date + timedelta(days=90)
        else:
            end_date = start_date + timedelta(days=30)

        # Generate invoice
        invoice = InvoiceService.generate_subscription_invoice(
            subscription=subscription,
            billing_period_start=start_date.date(),
            billing_period_end=end_date.date(),
            tax_resolver=tax_resolver,
            actor=actor
        )

        before = {
            'current_period_start': str(subscription.current_period_start),
            'current_period_end': str(subscription.current_period_end),
            'next_renewal_at': str(subscription.next_renewal_at),
        }
        subscription.current_period_start = start_date
        subscription.current_period_end = end_date
        subscription.next_renewal_at = end_date
        subscription.save()

        audit_billing_action(
            action='RENEW_SUBSCRIPTION',
            resource_type='TenantSubscription',
            resource_id=subscription.id,
            tenant=subscription.tenant,
            description=f"Subscription renewed for tenant {subscription.tenant.slug} through {end_date.date()}.",
            actor=actor,
            before=before,
            after={'next_renewal_at': str(subscription.next_renewal_at), 'invoice_id': str(invoice.id)}
        )
        return invoice, subscription

    @classmethod
    @transaction.atomic
    def cancel_subscription(cls, subscription: TenantSubscription, reason: str = '', actor=None) -> TenantSubscription:
        before = {'status': subscription.status}
        subscription.status = 'CANCELED'
        subscription.cancelled_at = timezone.now()
        subscription.cancellation_reason = reason
        subscription.save()

        audit_billing_action(
            action='CANCEL_SUBSCRIPTION',
            resource_type='TenantSubscription',
            resource_id=subscription.id,
            tenant=subscription.tenant,
            description=f"Subscription canceled for tenant {subscription.tenant.slug}. Reason: {reason}",
            actor=actor,
            before=before,
            after={'status': subscription.status, 'cancelled_at': str(subscription.cancelled_at)}
        )
        return subscription

    @classmethod
    @transaction.atomic
    def suspend_subscription(cls, subscription: TenantSubscription, reason: str = '', actor=None) -> TenantSubscription:
        before = {'status': subscription.status}
        subscription.status = 'SUSPENDED'
        subscription.save()

        audit_billing_action(
            action='SUSPEND_SUBSCRIPTION',
            resource_type='TenantSubscription',
            resource_id=subscription.id,
            tenant=subscription.tenant,
            description=f"Subscription suspended for tenant {subscription.tenant.slug}. Reason: {reason}",
            actor=actor,
            before=before,
            after={'status': subscription.status}
        )
        return subscription

    @classmethod
    @transaction.atomic
    def expire_trial(cls, subscription: TenantSubscription, actor=None) -> TenantSubscription:
        before = {'status': subscription.status}
        subscription.status = 'EXPIRED'
        subscription.save()

        audit_billing_action(
            action='EXPIRE_TRIAL',
            resource_type='TenantSubscription',
            resource_id=subscription.id,
            tenant=subscription.tenant,
            description=f"Trial expired for tenant {subscription.tenant.slug}.",
            actor=actor,
            before=before,
            after={'status': subscription.status}
        )
        return subscription


class InvoiceService:
    """
    Manages invoice creation, line items, sequential numbering, issuance, and immutability.
    """

    @classmethod
    @transaction.atomic
    def generate_subscription_invoice(
        cls,
        subscription: TenantSubscription,
        billing_period_start=None,
        billing_period_end=None,
        tax_resolver=None,
        actor=None
    ) -> SubscriptionInvoice:
        tenant = subscription.tenant
        today = timezone.now().date()
        year = today.year

        inv_number = allocate_next_invoice_number(tenant=tenant, year=year)

        if not billing_period_start:
            billing_period_start = subscription.current_period_start.date() if subscription.current_period_start else today
        if not billing_period_end:
            billing_period_end = subscription.current_period_end.date() if subscription.current_period_end else (billing_period_start + timedelta(days=30))

        # Base item price derived strictly from subscription.billing_amount
        base_amount = subscription.billing_amount
        if base_amount is None or base_amount == Decimal('0.00'):
            if subscription.plan_price:
                base_amount = subscription.plan_price.amount
            else:
                base_amount = Decimal('0.00')

        # Tax resolution
        if tax_resolver is None:
            tax_resolver = DefaultIndiaTaxResolver()

        tax_req = TaxCalculationRequest(
            subtotal=base_amount,
            currency=subscription.currency,
            customer_jurisdiction='IN',
            customer_tax_id=None,
            item_type='BASE_PLAN'
        )
        tax_res = tax_resolver.calculate_tax(tax_req)

        due_date = billing_period_start + timedelta(days=15)
        due_at = timezone.make_aware(datetime.combine(due_date, time.min))

        invoice = SubscriptionInvoice.objects.create(
            subscription=subscription,
            tenant=tenant,
            invoice_number=inv_number,
            status='DRAFT',
            currency=subscription.currency,
            subtotal=base_amount,
            tax_amount=tax_res.tax_amount,
            discount_amount=Decimal('0.00'),
            total=tax_res.total_amount,
            total_amount=tax_res.total_amount,
            billing_period_start=billing_period_start,
            billing_period_end=billing_period_end,
            due_date=due_date,
            due_at=due_at,
        )

        # Line item creation
        plan_name = subscription.plan.name if subscription.plan else 'Subscription'
        cycle_name = subscription.billing_cycle
        SubscriptionInvoiceItem.objects.create(
            invoice=invoice,
            item_type='BASE_PLAN',
            description=f"{plan_name} — {cycle_name} Base Plan Subscription",
            quantity=1,
            unit_price=base_amount,
            subtotal=base_amount,
            tax_rate=tax_res.tax_rate_percent,
            tax_amount=tax_res.tax_amount,
            total_amount=tax_res.total_amount,
            metadata={'tax_breakdown': tax_res.breakdown}
        )

        audit_billing_action(
            action='CREATE_INVOICE',
            resource_type='SubscriptionInvoice',
            resource_id=invoice.id,
            tenant=tenant,
            description=f"Generated draft invoice {inv_number} for {tax_res.total_amount} {subscription.currency}.",
            actor=actor,
            after={'invoice_number': inv_number, 'total_amount': str(tax_res.total_amount)}
        )
        return invoice

    @classmethod
    @transaction.atomic
    def issue_invoice(cls, invoice: SubscriptionInvoice, actor=None) -> SubscriptionInvoice:
        if invoice.status != 'DRAFT':
            raise ValidationError(f"Only DRAFT invoices can be issued (current status: {invoice.status}).")

        # Validate line items totals match invoice totals
        items = list(invoice.items.all())
        calc_subtotal = sum(i.subtotal for i in items)
        calc_tax = sum(i.tax_amount for i in items)
        calc_total = sum(i.total_amount for i in items)

        if invoice.subtotal != calc_subtotal or invoice.tax_amount != calc_tax or invoice.total_amount != calc_total:
            invoice.subtotal = calc_subtotal
            invoice.tax_amount = calc_tax
            invoice.total_amount = calc_total
            invoice.total = calc_total

        invoice.status = 'ISSUED'
        invoice.issued_at = timezone.now()
        invoice.save()

        audit_billing_action(
            action='ISSUE_INVOICE',
            resource_type='SubscriptionInvoice',
            resource_id=invoice.id,
            tenant=invoice.tenant,
            description=f"Invoice {invoice.invoice_number} issued and totals locked.",
            actor=actor,
            after={'status': 'ISSUED', 'issued_at': str(invoice.issued_at)}
        )
        return invoice

    @classmethod
    @transaction.atomic
    def pay_invoice(cls, invoice: SubscriptionInvoice, payment: SubscriptionPayment, actor=None) -> SubscriptionInvoice:
        if invoice.status not in ('DRAFT', 'ISSUED', 'OPEN'):
            raise ValidationError(f"Invoice {invoice.invoice_number} cannot be marked PAID from {invoice.status}.")

        invoice.status = 'PAID'
        invoice.paid_at = timezone.now()
        invoice.save()

        audit_billing_action(
            action='PAY_INVOICE',
            resource_type='SubscriptionInvoice',
            resource_id=invoice.id,
            tenant=invoice.tenant,
            description=f"Invoice {invoice.invoice_number} paid via payment {payment.id}.",
            actor=actor,
            after={'status': 'PAID', 'paid_at': str(invoice.paid_at)}
        )
        return invoice

    @classmethod
    @transaction.atomic
    def void_invoice(cls, invoice: SubscriptionInvoice, reason: str = '', actor=None) -> SubscriptionInvoice:
        if invoice.status == 'PAID':
            raise ValidationError("Paid invoices cannot be voided. A credit note is required.")

        invoice.status = 'VOID'
        invoice.notes = f"{invoice.notes}\nVOIDED: {reason}".strip()
        invoice.save()

        audit_billing_action(
            action='VOID_INVOICE',
            resource_type='SubscriptionInvoice',
            resource_id=invoice.id,
            tenant=invoice.tenant,
            description=f"Invoice {invoice.invoice_number} voided. Reason: {reason}",
            actor=actor,
            after={'status': 'VOID'}
        )
        return invoice


class PaymentProcessingService:
    """
    Idempotent payment execution using provider-neutral gateway adapter.
    """

    @classmethod
    @transaction.atomic
    def process_payment(
        cls,
        invoice: SubscriptionInvoice,
        billing_method: TenantBillingMethod,
        idempotency_key: str,
        gateway_adapter: BasePaymentGatewayAdapter = None,
        actor=None
    ) -> SubscriptionPayment:
        if invoice.status == 'PAID':
            existing = SubscriptionPayment.objects.filter(
                invoice=invoice,
                status='SUCCEEDED'
            ).first()
            if existing:
                return existing

        # Idempotency check on client idempotency key
        # provider_order_id stores idempotency key
        existing_attempt = SubscriptionPayment.objects.filter(
            invoice=invoice,
            provider_order_id=idempotency_key
        ).first()

        if existing_attempt:
            if existing_attempt.status == 'SUCCEEDED':
                return existing_attempt
            if existing_attempt.status == 'PROCESSING':
                return existing_attempt

        if gateway_adapter is None:
            gateway_adapter = MockPaymentGatewayAdapter()

        internal_attempt_id = str(uuid.uuid4())

        # Ensure invoice is at least issued
        if invoice.status == 'DRAFT':
            InvoiceService.issue_invoice(invoice, actor=actor)

        # Execute payment via gateway adapter
        result = gateway_adapter.execute_payment(
            invoice_id=str(invoice.id),
            amount=invoice.total_amount,
            currency=invoice.currency,
            customer_ref=billing_method.provider_customer_ref or billing_method.provider_customer_id,
            method_ref=billing_method.provider_method_ref or billing_method.provider_method_id,
            idempotency_key=idempotency_key,
            metadata={'attempt_id': internal_attempt_id}
        )

        payment = SubscriptionPayment.objects.create(
            invoice=invoice,
            tenant=invoice.tenant,
            billing_method=billing_method,
            amount=invoice.total_amount,
            currency=invoice.currency,
            status=result.status,
            provider=result.provider,
            provider_payment_id=result.provider_payment_id,
            provider_order_id=idempotency_key,
            failure_code=result.failure_code,
            failure_message=result.failure_message,
            paid_at=timezone.now() if result.is_success else None,
        )

        if result.is_success:
            InvoiceService.pay_invoice(invoice=invoice, payment=payment, actor=actor)
            # If subscription was PAST_DUE, restore to ACTIVE
            if invoice.subscription and invoice.subscription.status in ('PAST_DUE', 'TRIALING'):
                SubscriptionLifecycleService.activate_subscription(invoice.subscription, actor=actor)

            audit_billing_action(
                action='PAYMENT_SUCCEEDED',
                resource_type='SubscriptionPayment',
                resource_id=payment.id,
                tenant=invoice.tenant,
                description=f"Payment succeeded for invoice {invoice.invoice_number}: {payment.amount} {payment.currency}",
                actor=actor,
                after={'status': 'SUCCEEDED', 'provider_payment_id': payment.provider_payment_id}
            )
        else:
            audit_billing_action(
                action='PAYMENT_FAILED',
                resource_type='SubscriptionPayment',
                resource_id=payment.id,
                tenant=invoice.tenant,
                description=f"Payment failed for invoice {invoice.invoice_number}: {result.failure_message}",
                actor=actor,
                after={'status': 'FAILED', 'failure_code': result.failure_code}
            )
            # Trigger dunning workflow
            DunningService.schedule_dunning_retry(
                invoice=invoice,
                payment=payment,
                failure_code=result.failure_code,
                failure_message=result.failure_message,
                actor=actor
            )

        return payment


class DunningService:
    """
    Manages payment retries, dunning events, and grace-period lifecycle.
    Approved policy: 4 attempts over 14 days (Day 1, Day 3, Day 7, Day 14), followed by suspension.
    """
    MAX_RETRIES = 4
    # Intervals in days relative to current attempt: [1, 2, 4, 7]
    # Cumulative days from failure: 1, 3, 7, 14 days
    RETRY_DELAYS_DAYS = {
        1: 1,  # Day 1
        2: 2,  # Day 3
        3: 4,  # Day 7
        4: 7,  # Day 14
    }

    @classmethod
    @transaction.atomic
    def schedule_dunning_retry(
        cls,
        invoice: SubscriptionInvoice,
        payment: SubscriptionPayment,
        failure_code: str = '',
        failure_message: str = '',
        actor=None
    ) -> SubscriptionDunningEvent:
        subscription = invoice.subscription
        previous_retries = SubscriptionDunningEvent.objects.filter(
            subscription=subscription,
            event_type='RETRY_ATTEMPTED'
        ).count()

        attempt_number = previous_retries + 1

        # Mark subscription PAST_DUE
        if subscription.status == 'ACTIVE':
            subscription.status = 'PAST_DUE'
            subscription.save()

        if attempt_number <= cls.MAX_RETRIES:
            # 4 attempts over 14 days: Day 1, Day 3, Day 7, Day 14
            delay_days = cls.RETRY_DELAYS_DAYS.get(attempt_number, 2 ** (attempt_number - 1))
            scheduled_at = timezone.now() + timedelta(days=delay_days)
            event = SubscriptionDunningEvent.objects.create(
                tenant=subscription.tenant,
                subscription=subscription,
                invoice=invoice,
                payment=payment,
                event_type='RETRY_SCHEDULED',
                attempt_number=attempt_number,
                scheduled_at=scheduled_at,
                notes=f"Scheduled retry #{attempt_number} for invoice {invoice.invoice_number} at {scheduled_at}."
            )
            # Enqueue async Celery task
            from apps.master.tasks import execute_dunning_retry_async
            execute_dunning_retry_async.apply_async(
                args=[str(event.id)],
                eta=scheduled_at
            )
        else:
            # Max retries exceeded (4 attempts over 14 days) -> suspend subscription
            SubscriptionLifecycleService.suspend_subscription(
                subscription=subscription,
                reason=f"Suspended after {cls.MAX_RETRIES} failed payment retries over 14 days.",
                actor=actor
            )
            event = SubscriptionDunningEvent.objects.create(
                tenant=subscription.tenant,
                subscription=subscription,
                invoice=invoice,
                payment=payment,
                event_type='SUBSCRIPTION_SUSPENDED',
                attempt_number=attempt_number,
                notes=f"Subscription suspended after {cls.MAX_RETRIES} failed payment attempts over 14 days."
            )

        audit_billing_action(
            action=f"DUNNING_{event.event_type}",
            resource_type='SubscriptionDunningEvent',
            resource_id=event.id,
            tenant=subscription.tenant,
            description=event.notes,
            actor=actor,
            after={'event_type': event.event_type, 'attempt_number': attempt_number}
        )
        return event

    @classmethod
    @transaction.atomic
    def execute_retry(cls, dunning_event_id: str) -> bool:
        event = SubscriptionDunningEvent.objects.select_for_update().filter(id=dunning_event_id).first()
        if not event or not event.invoice:
            return False

        invoice = event.invoice
        subscription = event.subscription
        billing_method = TenantBillingMethod.objects.filter(
            tenant=subscription.tenant,
            is_active=True
        ).order_by('-is_default').first()

        if not billing_method:
            event.result = 'NO_PAYMENT_METHOD'
            event.executed_at = timezone.now()
            event.save()
            return False

        idempotency_key = f"dunning-{event.id}-{event.attempt_number}"
        payment = PaymentProcessingService.process_payment(
            invoice=invoice,
            billing_method=billing_method,
            idempotency_key=idempotency_key
        )

        event.executed_at = timezone.now()
        event.event_type = 'RETRY_ATTEMPTED'
        if payment.status == 'SUCCEEDED':
            event.result = 'SUCCESS'
            event.save()
            # Mark recovered
            SubscriptionDunningEvent.objects.create(
                tenant=subscription.tenant,
                subscription=subscription,
                invoice=invoice,
                payment=payment,
                event_type='SUBSCRIPTION_RECOVERED',
                attempt_number=event.attempt_number,
                notes=f"Subscription recovered on retry #{event.attempt_number}."
            )
            return True
        else:
            event.result = 'FAILED'
            event.save()
            return False


class WebhookProcessingService:
    """
    Webhook ingestion, signature verification, idempotency logging, and asynchronous dispatch.
    """

    @classmethod
    def sanitize_payload(cls, payload: dict) -> dict:
        """
        Removes sensitive information (PAN, card numbers, CVV, client secrets).
        """
        sensitive_keys = {'card_number', 'pan', 'cvv', 'cvc', 'secret', 'password', 'token'}
        sanitized = {}
        for k, v in payload.items():
            if k.lower() in sensitive_keys:
                sanitized[k] = '[REDACTED]'
            elif isinstance(v, dict):
                sanitized[k] = cls.sanitize_payload(v)
            else:
                sanitized[k] = v
        return sanitized

    @classmethod
    def ingest_webhook(
        cls,
        provider: str,
        headers: dict,
        raw_body: bytes,
        payload: dict,
        gateway_adapter: BasePaymentGatewayAdapter = None
    ) -> tuple[dict, int]:
        if gateway_adapter is None:
            gateway_adapter = MockPaymentGatewayAdapter()

        verified_event = gateway_adapter.verify_webhook_signature(raw_body, headers)
        if not verified_event.is_valid:
            logger.warning("Rejected webhook from %s with invalid signature: %s", provider, verified_event.error_message)
            return {'error': 'Invalid signature'}, 401

        provider_event_id = payload.get('event_id') or payload.get('id') or str(uuid.uuid4())
        event_type = payload.get('event_type') or payload.get('event') or 'unknown'

        # Check existing webhook event for idempotency
        existing = BillingWebhookEvent.objects.filter(
            provider=provider,
            provider_event_id=provider_event_id
        ).first()

        if existing:
            if existing.status == 'PROCESSED':
                return {'status': 'ALREADY_PROCESSED', 'event_id': str(existing.id)}, 200
            return {'status': 'RECEIVED', 'event_id': str(existing.id)}, 200

        sanitized = cls.sanitize_payload(payload)
        event = BillingWebhookEvent.objects.create(
            provider=provider,
            provider_event_id=provider_event_id,
            event_type=event_type,
            status='RECEIVED',
            signature_verified=True,
            payload=sanitized
        )

        # Enqueue async processing
        from apps.master.tasks import process_billing_webhook_async
        process_billing_webhook_async.delay(str(event.id))

        return {'status': 'ACCEPTED', 'event_id': str(event.id)}, 202

    @classmethod
    @transaction.atomic
    def process_event(cls, event_id: str):
        event = BillingWebhookEvent.objects.select_for_update().filter(id=event_id).first()
        if not event or event.status == 'PROCESSED':
            return

        event.status = 'PROCESSING'
        event.save()

        try:
            payload = event.payload
            event_type = event.event_type

            # Handle mock / generic webhook events
            if event_type in ('payment.captured', 'charge.succeeded'):
                payment_id = payload.get('payment_id')
                invoice_id = payload.get('invoice_id')
                if invoice_id:
                    invoice = SubscriptionInvoice.objects.filter(id=invoice_id).first()
                    if invoice and invoice.status != 'PAID':
                        invoice.status = 'PAID'
                        invoice.paid_at = timezone.now()
                        invoice.save()

            event.status = 'PROCESSED'
            event.processed_at = timezone.now()
            event.save()

            audit_billing_action(
                action='WEBHOOK_PROCESSED',
                resource_type='BillingWebhookEvent',
                resource_id=event.id,
                tenant=None,
                description=f"Processed webhook {event.provider}:{event.event_type} ({event.provider_event_id})."
            )

        except Exception as exc:
            event.status = 'FAILED'
            event.error_message = str(exc)
            event.save()
            logger.exception("Failed to process billing webhook event %s: %s", event_id, exc)
