"""
apps/tenant_core/models_commerce.py — Layer 2 Module G: Commerce, Payments & Billing

7 Domain Models:
  1. Order (orders)
  2. OrderItem (order_items)
  3. PaymentTransaction (payment_transactions)
  4. Refund (refunds)
  5. MemberInvoice (member_invoices)
  6. PaymentLink (payment_links)
  7. PaymentLinkEvent (payment_link_events)

Key Rules:
- Immutability: Paid orders and issued invoices contain immutable financial snapshots.
- Pricing decoupling: Historical orders are never recalculated from current catalog prices.
- Idempotency protection for retryable payment webhooks and transactions.
- Zero storage of raw card numbers or CVV.
- Atomic issuance of invoice and emission of outbox events upon successful payment.
"""

import uuid
from decimal import Decimal
from django.db import models
from django.utils import timezone
from .models_org import Organization, Branch
from .models_users import TenantUser
from .models_workforce import UserProfile
from .models_crm import Lead
from .models_catalog import Package, PackageVersion, PackagePrice
from .models_classes import ClassTemplate, ClassPrice
from .models_appointments import AppointmentType
from .models_infra import File


class Order(models.Model):
    """
    Commercial order for a lead or member purchase.
    """
    ORDER_TYPE_CHOICES = [
        ('NEW_MEMBERSHIP', 'New Membership'),
        ('RENEWAL', 'Membership Renewal'),
        ('EXTENSION', 'Membership Extension'),
        ('UPGRADE', 'Membership Upgrade'),
        ('DOWNGRADE', 'Membership Downgrade'),
        ('REJOIN', 'Rejoin Membership'),
        ('CLASS_PURCHASE', 'Class Session Purchase'),
        ('APPOINTMENT_PURCHASE', 'Appointment Service Purchase'),
        ('OTHER', 'Other Commercial Purchase'),
    ]
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('PENDING_PAYMENT', 'Pending Payment'),
        ('PARTIALLY_PAID', 'Partially Paid'),
        ('PAID', 'Paid / Completed'),
        ('CANCELLED', 'Cancelled'),
        ('REFUNDED', 'Refunded'),
    ]
    SOURCE_CHOICES = [
        ('WEB', 'Web Checkout'),
        ('MOBILE_APP', 'Mobile App'),
        ('FRONT_DESK', 'Front Desk POS'),
        ('SALES', 'Sales Representative'),
        ('POS', 'Point of Sale Machine'),
        ('API', 'External API'),
        ('MIGRATION', 'Data Migration'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order_number = models.CharField(max_length=100, unique=True)
    lead = models.ForeignKey(
        Lead, on_delete=models.PROTECT, null=True, blank=True, related_name='orders'
    )
    user_profile = models.ForeignKey(
        UserProfile, on_delete=models.PROTECT, null=True, blank=True, related_name='orders'
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT, related_name='orders'
    )
    sold_by_user = models.ForeignKey(
        TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='sold_orders'
    )
    order_type = models.CharField(max_length=40, choices=ORDER_TYPE_CHOICES, default='NEW_MEMBERSHIP')
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='DRAFT')
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    discount_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    reward_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    tax_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    currency = models.CharField(max_length=3, default='INR')
    source = models.CharField(max_length=30, choices=SOURCE_CHOICES, default='FRONT_DESK')
    notes = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'orders'
        indexes = [
            models.Index(fields=['order_number'], name='idx_orders_order_num'),
            models.Index(fields=['user_profile', 'status', 'created_at'], name='idx_orders_uprof_st_dt'),
            models.Index(fields=['lead', 'status'], name='idx_orders_lead_st'),
            models.Index(fields=['branch', 'created_at'], name='idx_orders_br_dt'),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(total_amount__gte=Decimal('0.00')),
                name='chk_order_total_non_neg'
            ),
        ]

    def __str__(self):
        return f"Order {self.order_number} ({self.total_amount} {self.currency}) [{self.status}]"


class OrderItem(models.Model):
    """
    Immutable order line item with exact commercial snapshots.
    """
    ITEM_TYPE_CHOICES = [
        ('PACKAGE', 'Package Version'),
        ('CLASS', 'Group Class'),
        ('APPOINTMENT', 'Appointment Service'),
        ('OTHER', 'Other Product / Service'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(
        Order, on_delete=models.PROTECT, related_name='items'
    )
    item_type = models.CharField(max_length=20, choices=ITEM_TYPE_CHOICES, default='PACKAGE')
    package = models.ForeignKey(
        Package, on_delete=models.PROTECT, null=True, blank=True, related_name='order_items'
    )
    package_version = models.ForeignKey(
        PackageVersion, on_delete=models.PROTECT, null=True, blank=True, related_name='order_items'
    )
    package_price = models.ForeignKey(
        PackagePrice, on_delete=models.PROTECT, null=True, blank=True, related_name='order_items'
    )
    class_template = models.ForeignKey(
        ClassTemplate, on_delete=models.PROTECT, null=True, blank=True, related_name='order_items'
    )
    class_price = models.ForeignKey(
        ClassPrice, on_delete=models.PROTECT, null=True, blank=True, related_name='order_items'
    )
    appointment_type = models.ForeignKey(
        AppointmentType, on_delete=models.PROTECT, null=True, blank=True, related_name='order_items'
    )
    item_name_snapshot = models.CharField(max_length=250)
    quantity = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('1.00'))
    unit_price_snapshot = models.DecimalField(max_digits=14, decimal_places=2)
    tax_percent_snapshot = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal('0.000'))
    discount_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    tax_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    total_amount = models.DecimalField(max_digits=14, decimal_places=2)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'order_items'
        indexes = [
            models.Index(fields=['order'], name='idx_orditem_order'),
            models.Index(fields=['package_version'], name='idx_orditem_pkgver'),
            models.Index(fields=['class_template'], name='idx_orditem_clstpl'),
        ]

    def __str__(self):
        return f"{self.item_name_snapshot} x{self.quantity} = {self.total_amount}"


class PaymentTransaction(models.Model):
    """
    Payment attempt or confirmed transaction linked to an order.
    Never stores raw card numbers or CVV.
    """
    PROVIDER_CHOICES = [
        ('RAZORPAY', 'Razorpay'),
        ('ICICI_POS', 'ICICI POS'),
        ('CASH', 'Cash'),
        ('BANK_TRANSFER', 'Bank Transfer / NEFT / RTGS'),
        ('STRIPE', 'Stripe'),
        ('OTHER', 'Other Provider'),
    ]
    STATUS_CHOICES = [
        ('INITIATED', 'Initiated'),
        ('PENDING', 'Pending'),
        ('SUCCESS', 'Success'),
        ('FAILED', 'Failed'),
        ('CANCELLED', 'Cancelled'),
        ('REFUNDED', 'Refunded'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(
        Order, on_delete=models.PROTECT, related_name='payments'
    )
    user_profile = models.ForeignKey(
        UserProfile, on_delete=models.PROTECT, null=True, blank=True, related_name='payment_transactions'
    )
    provider = models.CharField(max_length=30, choices=PROVIDER_CHOICES, default='CASH')
    payment_method = models.CharField(max_length=50, null=True, blank=True)
    provider_transaction_id = models.CharField(max_length=255, null=True, blank=True)
    idempotency_key = models.CharField(max_length=255, null=True, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default='INR')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='INITIATED')
    paid_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'payment_transactions'
        indexes = [
            models.Index(fields=['order', 'status'], name='idx_paytxn_ord_status'),
            models.Index(fields=['provider', 'provider_transaction_id'], name='idx_paytxn_prov_id'),
            models.Index(fields=['idempotency_key'], name='idx_paytxn_idem_key'),
        ]

    def __str__(self):
        return f"Payment {self.id} [{self.provider}] - {self.amount} {self.currency} ({self.status})"


class Refund(models.Model):
    """
    Refund lifecycle against a successful payment transaction or order.
    """
    STATUS_CHOICES = [
        ('REQUESTED', 'Requested'),
        ('APPROVED', 'Approved'),
        ('PROCESSING', 'Processing'),
        ('SUCCESS', 'Success'),
        ('FAILED', 'Failed'),
        ('REJECTED', 'Rejected'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payment_transaction = models.ForeignKey(
        PaymentTransaction, on_delete=models.PROTECT, related_name='refunds'
    )
    order = models.ForeignKey(
        Order, on_delete=models.PROTECT, related_name='refunds'
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    reason_code = models.CharField(max_length=100, null=True, blank=True)
    reason_text = models.TextField(null=True, blank=True)
    provider_reference = models.CharField(max_length=255, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='REQUESTED')
    requested_by_user = models.ForeignKey(
        TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='requested_refunds'
    )
    approved_by_user = models.ForeignKey(
        TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='approved_refunds'
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'refunds'
        indexes = [
            models.Index(fields=['payment_transaction', 'status'], name='idx_ref_pay_status'),
            models.Index(fields=['order', 'status'], name='idx_ref_ord_status'),
            models.Index(fields=['provider_reference'], name='idx_ref_prov_ref'),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=Decimal('0.00')),
                name='chk_refund_amount_positive'
            ),
        ]

    def __str__(self):
        return f"Refund {self.id} ({self.amount}) for Order {self.order.order_number} [{self.status}]"


class MemberInvoice(models.Model):
    """
    Issued financial invoice snapshot for tax, accounting, and member transparency.
    """
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('ISSUED', 'Issued'),
        ('PAID', 'Paid'),
        ('VOID', 'Void'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice_number = models.CharField(max_length=100, unique=True)
    order = models.ForeignKey(
        Order, on_delete=models.PROTECT, related_name='invoices'
    )
    user_profile = models.ForeignKey(
        UserProfile, on_delete=models.PROTECT, null=True, blank=True, related_name='invoices'
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT, related_name='invoices'
    )
    subtotal = models.DecimalField(max_digits=14, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    reward_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    tax_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    total_amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default='INR')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ISSUED')
    file = models.ForeignKey(
        File, on_delete=models.PROTECT, null=True, blank=True, related_name='invoices'
    )
    issued_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'member_invoices'
        indexes = [
            models.Index(fields=['invoice_number'], name='idx_inv_num'),
            models.Index(fields=['order'], name='idx_inv_order'),
            models.Index(fields=['user_profile', 'issued_at'], name='idx_inv_uprof_dt'),
        ]

    def __str__(self):
        return f"Invoice {self.invoice_number} ({self.total_amount} {self.currency}) [{self.status}]"


class PaymentLink(models.Model):
    """
    Generated checkout or payment link tied to an order.
    """
    STATUS_CHOICES = [
        ('CREATED', 'Created'),
        ('SENT', 'Sent'),
        ('OPENED', 'Opened'),
        ('PAID', 'Paid'),
        ('EXPIRED', 'Expired'),
        ('CANCELLED', 'Cancelled'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.ForeignKey(
        Lead, on_delete=models.PROTECT, null=True, blank=True, related_name='payment_links'
    )
    user_profile = models.ForeignKey(
        UserProfile, on_delete=models.PROTECT, null=True, blank=True, related_name='payment_links'
    )
    order = models.ForeignKey(
        Order, on_delete=models.PROTECT, related_name='payment_links'
    )
    provider = models.CharField(max_length=50, default='RAZORPAY')
    external_reference = models.CharField(max_length=255, null=True, blank=True)
    payment_url = models.TextField(null=True, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default='INR')
    expires_at = models.DateTimeField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='CREATED')
    created_by_user = models.ForeignKey(
        TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_payment_links'
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'payment_links'
        indexes = [
            models.Index(fields=['order', 'status'], name='idx_paylink_ord_status'),
            models.Index(fields=['external_reference'], name='idx_paylink_ext_ref'),
            models.Index(fields=['expires_at', 'status'], name='idx_paylink_exp_status'),
        ]

    def __str__(self):
        return f"PaymentLink {self.id} for Order {self.order.order_number} ({self.amount} {self.currency})"


class PaymentLinkEvent(models.Model):
    """
    Append-only lifecycle event trail for payment links.
    """
    EVENT_TYPE_CHOICES = [
        ('CREATED', 'Created'),
        ('SENT', 'Sent'),
        ('OPENED', 'Opened'),
        ('PAYMENT_ATTEMPTED', 'Payment Attempted'),
        ('PAID', 'Paid'),
        ('FAILED', 'Failed'),
        ('EXPIRED', 'Expired'),
        ('CANCELLED', 'Cancelled'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payment_link = models.ForeignKey(
        PaymentLink, on_delete=models.PROTECT, related_name='events'
    )
    event_type = models.CharField(max_length=30, choices=EVENT_TYPE_CHOICES)
    event_at = models.DateTimeField(default=timezone.now)
    provider_reference = models.CharField(max_length=255, null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'payment_link_events'
        indexes = [
            models.Index(fields=['payment_link', 'event_at'], name='idx_plevt_link_dt'),
            models.Index(fields=['provider_reference'], name='idx_plevt_prov_ref'),
        ]

    def __str__(self):
        return f"PaymentLinkEvent({self.payment_link_id} - {self.event_type} @ {self.event_at})"
