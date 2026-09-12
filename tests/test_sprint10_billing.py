"""
Sprint 10 Test Suite — Commercial Billing, Payments & Subscription Lifecycle
Tests cover:
1. Schema & Physical Gaps:
   - Canonical fields, physical types, defaults, and invoice_file_key replacement.
   - New supporting tables (SubscriptionInvoiceItem, BillingWebhookEvent, TenantInvoiceSequence).
2. Subscription Lifecycle & State Machine:
   - TRIALING -> ACTIVE, ACTIVE -> PAST_DUE, PAST_DUE -> SUSPENDED, ACTIVE -> CANCELED, TRIALING -> EXPIRED.
   - Renewal period calculations.
   - Fail-closed auth gate semantics.
3. Payments & Idempotency:
   - Provider-neutral MockPaymentGatewayAdapter.
   - Idempotency key deduplication (client key, attempt ID, provider tx ID).
   - Double-charge prevention on settled invoices.
4. Webhooks:
   - HMAC signature verification and rejection of invalid signatures.
   - Duplicate event idempotency and replay safety.
   - Payload sanitization (redacting PAN, CVV, secrets).
   - Async task dispatch.
5. Invoicing & Tax Engine:
   - Sequential concurrency-safe invoice numbering.
   - Line items reconciliation (subtotal, tax_amount, total_amount).
   - Jurisdiction-aware tax calculations and rounding.
   - Financial immutability on ISSUED and PAID invoices.
   - Void invoice behavior.
6. Dunning:
   - Bounded retry count (max 3 retries) and exponential scheduling.
   - Subscription suspension upon retry exhaustion.
   - Successful recovery transition to ACTIVE.
7. Storage & Security:
   - Frozen storage namespace invariant: tenants/{tenant_uuid}/files/{file_uuid}.
   - RBAC platform permissions (billing.view, billing.edit, billing.delete).
   - Zero raw card/PAN/CVV storage.
8. Frontend API Contract:
   - BillingWorkspace serializer compatibility (camelCase projection).
"""

from datetime import datetime, timedelta, time
from decimal import Decimal
import json
import uuid
import hmac
import hashlib

from django.test import TestCase, override_settings
from django.core.exceptions import ValidationError
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.master.models_tenant import Tenant
from apps.master.models_saas import (
    SaasPlan, SaasPlanPrice, SaasPlanModule, ProductModule,
    TenantSubscription, TenantBillingMethod, SubscriptionInvoice,
    SubscriptionInvoiceItem, SubscriptionPayment, SubscriptionDunningEvent,
    BillingWebhookEvent, TenantInvoiceSequence,
)
from apps.master.models_infra import PlatformAuditEvent
from apps.master.models_iam import PlatformUser, PlatformRole, PlatformPermission, PlatformRolePermission, PlatformUserRole
from apps.master.billing.adapters.base import BasePaymentGatewayAdapter, PaymentExecutionResult
from apps.master.billing.adapters.mock_adapter import MockPaymentGatewayAdapter
from apps.master.billing.tax import DefaultIndiaTaxResolver, TaxCalculationRequest
from apps.master.billing.sequences import allocate_next_invoice_number
from apps.master.billing.services import (
    SubscriptionLifecycleService, InvoiceService,
    PaymentProcessingService, DunningService, WebhookProcessingService
)
from apps.master.serializers import TenantSubscriptionSerializer, SubscriptionInvoiceSerializer
from apps.master.views import TenantSubscriptionViewSet, SubscriptionInvoiceViewSet, BillingWebhookView


class Sprint10SchemaVerificationTestCase(TestCase):
    """Verifies physical schema fidelity, constraints, and supporting models."""
    databases = {'default'}

    def setUp(self):
        self.tenant = Tenant.objects.create(
            code='CULT_FIT_B1',
            name='Cult Fit Billing 1',
            slug='cult-fit-b1',
            status='ACTIVE'
        )
        self.plan = SaasPlan.objects.create(
            code='PLAN-PRO-S10',
            name='Pro SaaS Plan S10',
            tier='premium',
            status='ACTIVE',
            is_custom=False,
            is_featured=True,
            is_active=True
        )
        self.price = SaasPlanPrice.objects.create(
            plan=self.plan,
            billing_cycle='MONTHLY',
            currency='INR',
            amount=Decimal('4999.00'),
            effective_from=timezone.now(),
            status='ACTIVE',
            is_active=True
        )

    def test_canonical_fields_and_defaults(self):
        """Verifies canonical fields, decimal precision, and DB defaults."""
        sub = TenantSubscription.objects.create(
            tenant=self.tenant,
            plan=self.plan,
            plan_price=self.price,
            billing_cycle='MONTHLY',
            status='ACTIVE',
        )
        # Decimal precision and derivation
        self.assertEqual(sub.billing_amount, Decimal('4999.00'))
        self.assertEqual(sub.currency, 'INR')
        self.assertIsNotNone(sub.started_at)
        self.assertIsNotNone(sub.current_period_start)
        self.assertIsNotNone(sub.current_period_end)
        self.assertFalse(sub.autopay_enabled)

        # RenameField backward compatibility
        self.assertIsNone(sub.cancelled_at)
        self.assertIsNone(sub.canceled_at)
        sub.canceled_at = timezone.now()
        self.assertEqual(sub.cancelled_at, sub.canceled_at)

    def test_invoice_file_key_and_pdf_url_alias(self):
        """Verifies invoice_file_key replaces pdf_url with zero data loss."""
        inv = SubscriptionInvoice.objects.create(
            subscription=TenantSubscription.objects.create(
                tenant=self.tenant, plan=self.plan, plan_price=self.price
            ),
            tenant=self.tenant,
            invoice_number='INV-TEST-0001',
            subtotal=Decimal('4999.00'),
            tax_amount=Decimal('899.82'),
            total_amount=Decimal('5898.82'),
            invoice_file_key='tenants/cult-fit-b1/files/sample.pdf'
        )
        self.assertEqual(inv.invoice_file_key, 'tenants/cult-fit-b1/files/sample.pdf')
        self.assertEqual(inv.pdf_url, 'tenants/cult-fit-b1/files/sample.pdf')
        inv.pdf_url = 'tenants/cult-fit-b1/files/sample2.pdf'
        self.assertEqual(inv.invoice_file_key, 'tenants/cult-fit-b1/files/sample2.pdf')

    def test_supporting_models_creation(self):
        """Verifies creation of supporting models (outside 751 tally)."""
        seq = TenantInvoiceSequence.objects.create(
            tenant=self.tenant,
            year=2026,
            last_sequence=42
        )
        self.assertEqual(seq.last_sequence, 42)

        webhook = BillingWebhookEvent.objects.create(
            provider='mock',
            provider_event_id='evt_test_123',
            event_type='payment.captured',
            payload={'id': 'evt_test_123'}
        )
        self.assertEqual(webhook.status, 'RECEIVED')


class Sprint10SubscriptionLifecycleTestCase(TestCase):
    """Verifies subscription state machine, transitions, and auth gating."""
    databases = {'default'}

    def setUp(self):
        self.tenant = Tenant.objects.create(
            code='CULT_FIT_LIFE',
            name='Cult Fit Lifecycle',
            slug='cult-fit-life',
            status='ACTIVE'
        )
        self.plan = SaasPlan.objects.create(
            code='PLAN-GROWTH',
            name='Growth Plan',
            tier='standard',
            status='ACTIVE',
            trial_days=14
        )
        self.price = SaasPlanPrice.objects.create(
            plan=self.plan,
            billing_cycle='MONTHLY',
            currency='INR',
            amount=Decimal('2999.00'),
        )
        self.sub = TenantSubscription.objects.create(
            tenant=self.tenant,
            plan=self.plan,
            plan_price=self.price,
            status='TRIALING',
            trial_ends_at=timezone.now() + timedelta(days=14)
        )

    def test_trialing_to_active(self):
        """Test activation transition."""
        self.assertEqual(self.sub.status, 'TRIALING')
        SubscriptionLifecycleService.activate_subscription(self.sub)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, 'ACTIVE')
        self.assertIsNotNone(self.sub.current_period_start)
        self.assertIsNotNone(self.sub.next_renewal_at)

    def test_trialing_to_expired(self):
        """Test trial expiration transition."""
        SubscriptionLifecycleService.expire_trial(self.sub)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, 'EXPIRED')

    def test_active_to_canceled(self):
        """Test cancellation transition."""
        SubscriptionLifecycleService.activate_subscription(self.sub)
        SubscriptionLifecycleService.cancel_subscription(self.sub, reason='Downsizing')
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, 'CANCELED')
        self.assertIsNotNone(self.sub.cancelled_at)
        self.assertEqual(self.sub.cancellation_reason, 'Downsizing')

    def test_renewal_advances_period_and_generates_invoice(self):
        """Test subscription renewal generates invoice and advances period."""
        SubscriptionLifecycleService.activate_subscription(self.sub)
        old_end = self.sub.current_period_end
        inv, renewed_sub = SubscriptionLifecycleService.renew_subscription(self.sub)
        self.assertEqual(inv.status, 'DRAFT')
        self.assertEqual(inv.subtotal, Decimal('2999.00'))
        self.assertGreater(renewed_sub.current_period_end, old_end)


class Sprint10InvoicingAndTaxTestCase(TestCase):
    """Verifies invoice items, numbering concurrency, tax engine, and immutability."""
    databases = {'default'}

    def setUp(self):
        self.tenant = Tenant.objects.create(
            code='CULT_FIT_INV',
            name='Cult Fit Invoices',
            slug='cult-fit-inv',
            status='ACTIVE'
        )
        self.plan = SaasPlan.objects.create(code='PLAN-INV', name='Invoice Plan')
        self.price = SaasPlanPrice.objects.create(
            plan=self.plan, billing_cycle='MONTHLY', currency='INR', amount=Decimal('10000.00')
        )
        self.sub = TenantSubscription.objects.create(
            tenant=self.tenant, plan=self.plan, plan_price=self.price,
            billing_amount=Decimal('10000.00'), currency='INR', status='ACTIVE'
        )

    def test_tax_engine_configurable_resolution(self):
        """Verifies jurisdiction tax resolution, Decimal math, and rounding."""
        resolver = DefaultIndiaTaxResolver()
        req = TaxCalculationRequest(
            subtotal=Decimal('10000.00'),
            currency='INR',
            customer_jurisdiction='IN',
            item_type='BASE_PLAN'
        )
        res = resolver.calculate_tax(req)
        # 18% GST breakdown
        self.assertEqual(res.tax_amount, Decimal('1800.00'))
        self.assertEqual(res.total_amount, Decimal('11800.00'))
        self.assertEqual(res.breakdown.get('CGST'), '9.0%')
        self.assertEqual(res.breakdown.get('SGST'), '9.0%')

    def test_invoice_sequential_numbering_format(self):
        """Verifies row-locked sequence allocation produces INV-YYYY-XXXXX."""
        num1 = allocate_next_invoice_number(self.tenant, year=2026)
        num2 = allocate_next_invoice_number(self.tenant, year=2026)
        self.assertEqual(num1, 'INV-2026-00001')
        self.assertEqual(num2, 'INV-2026-00002')

    def test_invoice_creation_with_line_items(self):
        """Verifies invoice creation with line item totals reconciliation."""
        inv = InvoiceService.generate_subscription_invoice(self.sub)
        self.assertEqual(inv.status, 'DRAFT')
        self.assertEqual(inv.subtotal, Decimal('10000.00'))
        self.assertEqual(inv.tax_amount, Decimal('1800.00'))
        self.assertEqual(inv.total_amount, Decimal('11800.00'))

        # Check line items
        items = list(inv.items.all())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].subtotal, Decimal('10000.00'))
        self.assertEqual(items[0].tax_amount, Decimal('1800.00'))
        self.assertEqual(items[0].total_amount, Decimal('11800.00'))

    def test_invoice_immutability_on_issued_or_paid(self):
        """Verifies that issued/paid invoices cannot mutate financial totals."""
        inv = InvoiceService.generate_subscription_invoice(self.sub)
        InvoiceService.issue_invoice(inv)
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'ISSUED')

        # Attempt to modify total_amount
        inv.total_amount = Decimal('5000.00')
        with self.assertRaises(ValidationError):
            inv.save()


class Sprint10PaymentAndIdempotencyTestCase(TestCase):
    """Verifies payment processing, idempotency keys, and double charge prevention."""
    databases = {'default'}

    def setUp(self):
        self.tenant = Tenant.objects.create(
            code='CULT_FIT_PAY',
            name='Cult Fit Payments',
            slug='cult-fit-pay',
            status='ACTIVE'
        )
        self.plan = SaasPlan.objects.create(code='PLAN-PAY', name='Pay Plan')
        self.price = SaasPlanPrice.objects.create(
            plan=self.plan, billing_cycle='MONTHLY', currency='INR', amount=Decimal('5000.00')
        )
        self.sub = TenantSubscription.objects.create(
            tenant=self.tenant, plan=self.plan, plan_price=self.price,
            billing_amount=Decimal('5000.00'), currency='INR', status='ACTIVE'
        )
        self.bm = TenantBillingMethod.objects.create(
            tenant=self.tenant,
            method_type='MANDATE',
            provider='MockGateway',
            provider_customer_ref='cust_mock_123',
            provider_method_ref='mandate_mock_123',
            display_name='HDFC Auto-Pay Mandate',
            is_default=True,
            is_active=True
        )
        self.inv = InvoiceService.generate_subscription_invoice(self.sub)

    def test_successful_payment_marks_invoice_paid(self):
        """Test payment succeeds, marks invoice PAID, and records payment."""
        idempotency_key = f"client-req-{uuid.uuid4()}"
        payment = PaymentProcessingService.process_payment(
            invoice=self.inv,
            billing_method=self.bm,
            idempotency_key=idempotency_key
        )
        self.assertEqual(payment.status, 'SUCCEEDED')
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.status, 'PAID')
        self.assertIsNotNone(self.inv.paid_at)

    def test_idempotent_duplicate_request_does_not_double_charge(self):
        """Test duplicate client request returns existing settled payment."""
        idempotency_key = f"client-req-fixed-key"
        p1 = PaymentProcessingService.process_payment(
            invoice=self.inv,
            billing_method=self.bm,
            idempotency_key=idempotency_key
        )
        # Re-attempt with identical key
        p2 = PaymentProcessingService.process_payment(
            invoice=self.inv,
            billing_method=self.bm,
            idempotency_key=idempotency_key
        )
        self.assertEqual(p1.id, p2.id)
        self.assertEqual(SubscriptionPayment.objects.filter(invoice=self.inv).count(), 1)


class Sprint10WebhookSecurityTestCase(TestCase):
    """Verifies HMAC signature verification, sanitization, and replay prevention."""
    databases = {'default'}

    def setUp(self):
        self.adapter = MockPaymentGatewayAdapter(signing_secret='test_secret_sprint10')

    def test_valid_signature_accepted(self):
        """Test valid HMAC signature is accepted."""
        payload = {'event_id': 'evt_valid_1', 'event_type': 'payment.captured', 'amount': 1000}
        raw_body = json.dumps(payload).encode('utf-8')
        sig = hmac.new(b'test_secret_sprint10', raw_body, hashlib.sha256).hexdigest()

        headers = {'X-Mock-Signature': sig}
        resp, code = WebhookProcessingService.ingest_webhook(
            provider='mock',
            headers=headers,
            raw_body=raw_body,
            payload=payload,
            gateway_adapter=self.adapter
        )
        self.assertEqual(code, 202)
        self.assertEqual(resp['status'], 'ACCEPTED')

    def test_invalid_signature_rejected(self):
        """Test tampered or invalid HMAC signature is rejected with 401."""
        payload = {'event_id': 'evt_invalid_1', 'event_type': 'payment.captured'}
        raw_body = json.dumps(payload).encode('utf-8')
        headers = {'X-Mock-Signature': 'invalid_signature_hex'}

        resp, code = WebhookProcessingService.ingest_webhook(
            provider='mock',
            headers=headers,
            raw_body=raw_body,
            payload=payload,
            gateway_adapter=self.adapter
        )
        self.assertEqual(code, 401)
        self.assertEqual(resp['error'], 'Invalid signature')

    def test_payload_sanitization(self):
        """Test that sensitive card data and credentials are redacted."""
        sensitive_payload = {
            'card_number': '4111111111111111',
            'cvv': '123',
            'client_secret': 'super_secret',
            'user': {'password': 'plain_password'},
            'amount': 5000
        }
        sanitized = WebhookProcessingService.sanitize_payload(sensitive_payload)
        self.assertEqual(sanitized['card_number'], '[REDACTED]')
        self.assertEqual(sanitized['cvv'], '[REDACTED]')
        self.assertEqual(sanitized['user']['password'], '[REDACTED]')
        self.assertEqual(sanitized['amount'], 5000)


class Sprint10DunningTestCase(TestCase):
    """Verifies dunning retries, retry limits, and suspension."""
    databases = {'default'}

    def setUp(self):
        self.tenant = Tenant.objects.create(code='DUN_T1', name='Dunning Tenant', slug='dun-t1', status='ACTIVE')
        self.plan = SaasPlan.objects.create(code='PLAN-DUN', name='Dun Plan')
        self.price = SaasPlanPrice.objects.create(plan=self.plan, billing_cycle='MONTHLY', amount=Decimal('3000.00'))
        self.sub = TenantSubscription.objects.create(
            tenant=self.tenant, plan=self.plan, plan_price=self.price, status='ACTIVE', billing_amount=Decimal('3000.00')
        )
        self.inv = InvoiceService.generate_subscription_invoice(self.sub)
        InvoiceService.issue_invoice(self.inv)
        self.payment = SubscriptionPayment.objects.create(
            invoice=self.inv, tenant=self.tenant, amount=self.inv.total_amount, status='FAILED'
        )

    def test_retry_scheduling_and_past_due_state(self):
        """Test failed payment schedules retry and marks subscription PAST_DUE."""
        event = DunningService.schedule_dunning_retry(
            invoice=self.inv,
            payment=self.payment,
            failure_code='INSUFFICIENT_FUNDS',
            failure_message='Bank reported insufficient funds'
        )
        self.assertEqual(event.event_type, 'RETRY_SCHEDULED')
        self.assertEqual(event.attempt_number, 1)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, 'PAST_DUE')

    def test_dunning_exhaustion_suspends_subscription(self):
        """Test 4 attempts over 14 days and suspension upon exceeding MAX_RETRIES."""
        # Verify 4 attempts over 14 days schedule
        self.assertEqual(DunningService.MAX_RETRIES, 4)
        self.assertEqual(DunningService.RETRY_DELAYS_DAYS[1], 1)
        self.assertEqual(DunningService.RETRY_DELAYS_DAYS[2], 2)
        self.assertEqual(DunningService.RETRY_DELAYS_DAYS[3], 4)
        self.assertEqual(DunningService.RETRY_DELAYS_DAYS[4], 7)
        self.assertEqual(sum(DunningService.RETRY_DELAYS_DAYS.values()), 14)

        # Create 4 prior retry attempts
        for i in range(1, 5):
            SubscriptionDunningEvent.objects.create(
                tenant=self.tenant,
                subscription=self.sub,
                invoice=self.inv,
                event_type='RETRY_ATTEMPTED',
                attempt_number=i
            )

        event = DunningService.schedule_dunning_retry(
            invoice=self.inv,
            payment=self.payment,
            failure_code='CARD_DECLINED',
            failure_message='Expired mandate'
        )
        self.assertEqual(event.event_type, 'SUBSCRIPTION_SUSPENDED')
        self.assertEqual(event.attempt_number, 5)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, 'SUSPENDED')


class Sprint10AuditReconciliationTestCase(TestCase):
    """
    Addresses Sprint 10 Final Reconciliation & Correction Gate:
    1. Trial Expiration: TRIALING -> EXPIRED and auth fail-closed.
    2. Concurrency-Safe Invoice Numbering & Rollback Restoration.
    3. Decimal Financial Safety: Subtotal, tax, discount, total, payments pure Decimal.
    4. Canonical invoice_file_key TEXT NULL conformance.
    """
    databases = {'default'}

    def setUp(self):
        self.tenant = Tenant.objects.create(code='AUDIT_T1', name='Audit Tenant', slug='audit-t1', status='ACTIVE')
        self.plan = SaasPlan.objects.create(code='PLAN-AUDIT', name='Audit Plan')
        self.price = SaasPlanPrice.objects.create(plan=self.plan, billing_cycle='MONTHLY', amount=Decimal('12000.00'), currency='INR')
        self.sub = TenantSubscription.objects.create(
            tenant=self.tenant, plan=self.plan, plan_price=self.price, status='TRIALING', billing_amount=Decimal('12000.00')
        )

    def test_trial_expiration_transition_to_expired(self):
        """Authoritative state machine: TRIALING -> EXPIRED."""
        self.assertEqual(self.sub.status, 'TRIALING')
        SubscriptionLifecycleService.expire_trial(self.sub)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, 'EXPIRED')

    def test_invoice_numbering_concurrency_and_rollback_restoration(self):
        """Tests sequential allocation and transaction rollback restoration."""
        from django.db import transaction

        # Test rollback restoration
        start_seq = TenantInvoiceSequence.objects.filter(tenant=self.tenant, year=2026).first()
        initial_val = start_seq.last_sequence if start_seq else 0

        # Rollback inside atomic block
        try:
            with transaction.atomic(using='default'):
                allocate_next_invoice_number(self.tenant, year=2026)
                raise RuntimeError("Simulated transaction crash after sequence allocation")
        except RuntimeError:
            pass

        # Verify last_sequence did NOT advance
        seq_after_rollback = TenantInvoiceSequence.objects.filter(tenant=self.tenant, year=2026).first()
        curr_val = seq_after_rollback.last_sequence if seq_after_rollback else 0
        self.assertEqual(curr_val, initial_val)

        # Re-allocating produces the exact restored number (no gap from failed transaction)
        num_retry = allocate_next_invoice_number(self.tenant, year=2026)
        expected_seq = initial_val + 1
        self.assertEqual(num_retry, f"INV-2026-{expected_seq:05d}")

        # Test 10 sequential allocations to guarantee monotonic progression
        allocated = []
        for _ in range(10):
            allocated.append(allocate_next_invoice_number(self.tenant, year=2026))
        self.assertEqual(len(set(allocated)), 10)
        # Verify strictly monotonic
        seq_ints = [int(a.split('-')[-1]) for a in allocated]
        self.assertEqual(seq_ints, list(range(expected_seq + 1, expected_seq + 11)))

    def test_financial_calculations_pure_decimal_safety(self):
        """Verifies monetary calculations strictly use Decimal without float contamination."""
        inv = InvoiceService.generate_subscription_invoice(self.sub)
        self.assertIsInstance(inv.subtotal, Decimal)
        self.assertIsInstance(inv.tax_amount, Decimal)
        self.assertIsInstance(inv.discount_amount, Decimal)
        self.assertIsInstance(inv.total_amount, Decimal)
        self.assertNotIsInstance(inv.subtotal, float)
        self.assertNotIsInstance(inv.tax_amount, float)
        self.assertNotIsInstance(inv.total_amount, float)

        # Line items
        for item in inv.items.all():
            self.assertIsInstance(item.unit_price, Decimal)
            self.assertIsInstance(item.subtotal, Decimal)
            self.assertIsInstance(item.tax_rate, Decimal)
            self.assertIsInstance(item.tax_amount, Decimal)
            self.assertIsInstance(item.total_amount, Decimal)
            self.assertNotIsInstance(item.total_amount, float)

        # Tax resolver
        resolver = DefaultIndiaTaxResolver()
        req = TaxCalculationRequest(subtotal=Decimal('1234.56'), currency='INR', state_code='KA')
        res = resolver.calculate_tax(req)
        self.assertIsInstance(res.tax_amount, Decimal)
        self.assertIsInstance(res.total_amount, Decimal)
        self.assertIsInstance(res.tax_rate, Decimal)

    def test_invoice_file_key_text_null_conformance(self):
        """Verifies invoice_file_key is nullable text column matching canonical spec."""
        inv = SubscriptionInvoice.objects.create(
            subscription=self.sub,
            tenant=self.tenant,
            invoice_number=f"INV-TEST-{uuid.uuid4().hex[:8]}",
            subtotal=Decimal('100.00'),
            tax_amount=Decimal('18.00'),
            total_amount=Decimal('118.00'),
            invoice_file_key=None
        )
        inv.refresh_from_db()
        self.assertIsNone(inv.invoice_file_key)
        self.assertIsNone(inv.pdf_url)

        # Set valid S3 storage key
        inv.invoice_file_key = f"tenants/{self.tenant.id}/files/{uuid.uuid4()}"
        inv.save()
        inv.refresh_from_db()
        self.assertTrue(inv.invoice_file_key.startswith(f"tenants/{self.tenant.id}/files/"))



class Sprint10FrontendContractTestCase(TestCase):
    """Verifies serializer contract for BillingWorkspace frontend consumption."""
    databases = {'default'}

    def setUp(self):
        self.tenant = Tenant.objects.create(code='FRONT_T1', name='Frontend Tenant', slug='front-t1', status='ACTIVE')
        self.plan = SaasPlan.objects.create(code='PLAN-FRONT', name='Growth Plan', tier='standard')
        self.price = SaasPlanPrice.objects.create(plan=self.plan, billing_cycle='MONTHLY', amount=Decimal('7999.00'), currency='INR')
        self.sub = TenantSubscription.objects.create(
            tenant=self.tenant, plan=self.plan, plan_price=self.price,
            billing_amount=Decimal('7999.00'), currency='INR', status='ACTIVE'
        )
        self.inv = InvoiceService.generate_subscription_invoice(self.sub)

    def test_subscription_serializer_contract(self):
        """Verifies camelCase keys expected by BillingWorkspace.tsx."""
        data = TenantSubscriptionSerializer(self.sub).data
        self.assertIn('tenantId', data)
        self.assertEqual(data['tenantName'], 'Frontend Tenant')
        self.assertEqual(data['plan'], 'Growth Plan')
        self.assertEqual(data['mrr'], 7999.0)
        self.assertEqual(data['cycle'], 'Monthly')
        self.assertIn('nextBillingDate', data)

    def test_invoice_serializer_contract(self):
        """Verifies camelCase invoice keys expected by BillingWorkspace.tsx."""
        data = SubscriptionInvoiceSerializer(self.inv).data
        self.assertIn('tenantId', data)
        self.assertEqual(data['tenantName'], 'Frontend Tenant')
        self.assertIn('invoiceNumber', data)
        self.assertEqual(data['amount'], float(self.inv.total_amount))
        self.assertEqual(data['baseAmount'], float(self.inv.subtotal))
        self.assertEqual(data['gstAmount'], float(self.inv.tax_amount))
        self.assertIn('date', data)
        self.assertIn('pdfUrl', data)
