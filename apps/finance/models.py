"""
Finance and Billing Models for PerformanceOS.
Provides Invoice, Payment (Razorpay / UPI / Cash), and Coupon models with tenant isolation.
"""

from django.db import models
from django.utils import timezone
from apps.tenants.models import TenantAwareModel, Location
from apps.members.models import Member, MemberSubscription

class DiscountType(models.TextChoices):
    PERCENTAGE = 'Percentage', 'Percentage'
    FIXED = 'Fixed', 'Fixed Amount'


class Coupon(TenantAwareModel):
    """
    Coupons and Promotional discount codes applied at checkout.
    """
    id = models.CharField(
        max_length=64,
        primary_key=True,
        help_text="Coupon ID (e.g. CPN-001)"
    )
    code = models.CharField(max_length=64, unique=True, help_text="Promotional code (e.g. WELCOME20)")
    description = models.CharField(max_length=255, blank=True, default='')
    discount_type = models.CharField(
        max_length=32,
        choices=DiscountType.choices,
        default=DiscountType.PERCENTAGE
    )
    discount_value = models.DecimalField(max_digits=10, decimal_places=2, help_text="Percentage (e.g. 20.00) or Flat INR amount")
    min_order_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    max_discount_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    valid_from = models.DateField(default=timezone.now)
    valid_until = models.DateField()
    usage_limit = models.IntegerField(null=True, blank=True, help_text="Max total redemptions")
    times_used = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'finance_coupons'
        ordering = ['-valid_from']

    def __str__(self):
        return f"{self.code} ({self.discount_value} {self.discount_type})"

    def calculate_discount(self, order_amount: float) -> float:
        """Calculates discount for a given amount."""
        if not self.is_active:
            return 0.0
        if order_amount < float(self.min_order_amount):
            return 0.0

        if self.discount_type == DiscountType.PERCENTAGE:
            disc = order_amount * (float(self.discount_value) / 100.0)
            if self.max_discount_amount:
                disc = min(disc, float(self.max_discount_amount))
            return disc
        else:
            return min(float(self.discount_value), order_amount)


class InvoiceStatus(models.TextChoices):
    DRAFT = 'Draft', 'Draft'
    ISSUED = 'Issued', 'Issued'
    PAID = 'Paid', 'Paid'
    OVERDUE = 'Overdue', 'Overdue'
    VOID = 'Void', 'Void'


class PaymentMethod(models.TextChoices):
    RAZORPAY = 'Razorpay', 'Razorpay'
    UPI = 'UPI', 'UPI'
    CARD = 'Credit/Debit Card', 'Credit/Debit Card'
    CASH = 'Cash', 'Cash'
    NET_BANKING = 'Net Banking', 'Net Banking'


class PaymentStatus(models.TextChoices):
    SUCCESS = 'Success', 'Success'
    PENDING = 'Pending', 'Pending'
    FAILED = 'Failed', 'Failed'
    REFUNDED = 'Refunded', 'Refunded'


class Invoice(TenantAwareModel):
    """
    Billing invoice generated for memberships, personal training, or retail products.
    """
    id = models.CharField(
        max_length=64,
        primary_key=True,
        help_text="Invoice ID (e.g. INV-001)"
    )
    member = models.ForeignKey(
        Member,
        on_delete=models.CASCADE,
        related_name='invoices'
    )
    subscription = models.ForeignKey(
        MemberSubscription,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='invoices'
    )
    location = models.ForeignKey(
        Location,
        on_delete=models.CASCADE,
        related_name='invoices'
    )
    coupon = models.ForeignKey(
        Coupon,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='applied_invoices'
    )
    description = models.CharField(max_length=255, blank=True, default='')
    subtotal = models.DecimalField(max_digits=10, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, help_text="GST / Tax amount")
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)

    status = models.CharField(
        max_length=32,
        choices=InvoiceStatus.choices,
        default=InvoiceStatus.ISSUED
    )
    due_date = models.DateField()
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'finance_invoices'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.id} - {self.member.name} (INR {self.total_amount}) [{self.status}]"


class Payment(TenantAwareModel):
    """
    Payment transaction record capturing Razorpay / UPI / Card / Cash settlements.
    """
    id = models.CharField(
        max_length=64,
        primary_key=True,
        help_text="Payment ID (e.g. PAY-001)"
    )
    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.CASCADE,
        related_name='payments'
    )
    member = models.ForeignKey(
        Member,
        on_delete=models.CASCADE,
        related_name='payments'
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    payment_method = models.CharField(
        max_length=32,
        choices=PaymentMethod.choices,
        default=PaymentMethod.RAZORPAY
    )
    transaction_id = models.CharField(
        max_length=128,
        blank=True,
        default='',
        help_text="Gateway transaction identifier (e.g. Razorpay payment ID)"
    )
    status = models.CharField(
        max_length=32,
        choices=PaymentStatus.choices,
        default=PaymentStatus.SUCCESS
    )
    paid_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'finance_payments'
        ordering = ['-paid_at']

    def __str__(self):
        return f"{self.id} - INR {self.amount} via {self.payment_method} ({self.status})"
