"""
Serializers for Invoices, Razorpay Payments, Coupons, and Billing Summaries.
"""

from rest_framework import serializers
from .models import Invoice, Payment, Coupon, DiscountType, InvoiceStatus, PaymentMethod, PaymentStatus


class CouponSerializer(serializers.ModelSerializer):
    tenant_id = serializers.CharField(source='tenant.id', read_only=True)

    class Meta:
        model = Coupon
        fields = [
            'id',
            'tenant_id',
            'code',
            'description',
            'discount_type',
            'discount_value',
            'min_order_amount',
            'max_discount_amount',
            'valid_from',
            'valid_until',
            'usage_limit',
            'times_used',
            'is_active',
        ]
        read_only_fields = ['tenant_id', 'times_used']


class PaymentSerializer(serializers.ModelSerializer):
    tenant_id = serializers.CharField(source='tenant.id', read_only=True)
    member_name = serializers.CharField(source='member.name', read_only=True)

    class Meta:
        model = Payment
        fields = [
            'id',
            'tenant_id',
            'invoice',
            'member',
            'member_name',
            'amount',
            'payment_method',
            'transaction_id',
            'status',
            'paid_at',
        ]
        read_only_fields = ['tenant_id', 'member_name']


class InvoiceSerializer(serializers.ModelSerializer):
    tenant_id = serializers.CharField(source='tenant.id', read_only=True)
    member_name = serializers.CharField(source='member.name', read_only=True)
    location_name = serializers.CharField(source='location.name', read_only=True)
    coupon_code = serializers.CharField(source='coupon.code', read_only=True, default='')
    payments = PaymentSerializer(many=True, read_only=True)
    discount_amount = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, default=0.00)
    tax_amount = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, default=0.00)
    total_amount = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, default=0.00)

    class Meta:
        model = Invoice
        fields = [
            'id',
            'tenant_id',
            'member',
            'member_name',
            'subscription',
            'location',
            'location_name',
            'coupon',
            'coupon_code',
            'description',
            'subtotal',
            'discount_amount',
            'tax_amount',
            'total_amount',
            'status',
            'due_date',
            'paid_at',
            'payments',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['tenant_id', 'member_name', 'location_name', 'coupon_code', 'payments', 'created_at', 'updated_at']
