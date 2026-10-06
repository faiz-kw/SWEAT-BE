"""
apps/tenant_core/serializers_commerce.py — Serializers for Layer 2 Module G: Commerce, Payments & Billing
"""

from rest_framework import serializers
from .models_commerce import (
    Order,
    OrderItem,
    PaymentTransaction,
    Refund,
    MemberInvoice,
    PaymentLink,
    PaymentLinkEvent,
)


class OrderItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderItem
        fields = [
            'id', 'order', 'item_type', 'package', 'package_version',
            'package_price', 'class_template', 'class_price', 'appointment_type',
            'item_name_snapshot', 'quantity', 'unit_price_snapshot',
            'tax_percent_snapshot', 'discount_amount', 'tax_amount',
            'total_amount', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class PaymentTransactionSerializer(serializers.ModelSerializer):
    order_number = serializers.ReadOnlyField(source='order.order_number')
    branch_name = serializers.ReadOnlyField(source='order.branch.name')
    branch_id = serializers.ReadOnlyField(source='order.branch.id')
    member_name = serializers.SerializerMethodField()
    recorded_by_name = serializers.SerializerMethodField()
    approved_by_name = serializers.SerializerMethodField()
    approval_status = serializers.SerializerMethodField()

    class Meta:
        model = PaymentTransaction
        fields = [
            'id', 'order', 'order_number', 'branch_name', 'branch_id',
            'user_profile', 'member_name', 'provider', 'payment_method',
            'provider_transaction_id', 'idempotency_key', 'amount',
            'currency', 'status', 'paid_at', 'recorded_by_name',
            'approved_by_name', 'approval_status', 'metadata',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_member_name(self, obj):
        if obj.user_profile:
            return f"{obj.user_profile.first_name_snapshot or ''} {obj.user_profile.last_name_snapshot or ''}".strip()
        if obj.order and obj.order.lead:
            return f"{obj.order.lead.first_name or ''} {obj.order.lead.last_name or ''}".strip()
        return ''

    def get_recorded_by_name(self, obj):
        if isinstance(obj.metadata, dict):
            return obj.metadata.get('recorded_by_name') or obj.metadata.get('cash_recorded_by_name') or obj.metadata.get('recorded_by') or ''
        return ''

    def get_approved_by_name(self, obj):
        if isinstance(obj.metadata, dict):
            return obj.metadata.get('approved_by_name') or obj.metadata.get('approved_by') or ''
        return ''

    def get_approval_status(self, obj):
        if isinstance(obj.metadata, dict):
            return obj.metadata.get('approval_status')
        return None


class RefundSerializer(serializers.ModelSerializer):
    order_number = serializers.ReadOnlyField(source='order.order_number')
    branch_name = serializers.ReadOnlyField(source='order.branch.name')
    member_name = serializers.SerializerMethodField()
    requested_by_name = serializers.SerializerMethodField()
    approved_by_name = serializers.SerializerMethodField()

    class Meta:
        model = Refund
        fields = [
            'id', 'payment_transaction', 'order', 'order_number',
            'branch_name', 'member_name', 'amount', 'reason_code',
            'reason_text', 'provider_reference', 'status',
            'requested_by_user', 'requested_by_name', 'approved_by_user',
            'approved_by_name', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_member_name(self, obj):
        if obj.order and obj.order.user_profile:
            return f"{obj.order.user_profile.first_name_snapshot or ''} {obj.order.user_profile.last_name_snapshot or ''}".strip()
        if obj.order and obj.order.lead:
            return f"{obj.order.lead.first_name or ''} {obj.order.lead.last_name or ''}".strip()
        return ''

    def get_requested_by_name(self, obj):
        if obj.requested_by_user:
            return getattr(obj.requested_by_user, 'display_name', '') or obj.requested_by_user.email
        return ''

    def get_approved_by_name(self, obj):
        if obj.approved_by_user:
            return getattr(obj.approved_by_user, 'display_name', '') or obj.approved_by_user.email
        return ''


class MemberInvoiceSerializer(serializers.ModelSerializer):
    member_name = serializers.SerializerMethodField()
    branch_name = serializers.ReadOnlyField(source='branch.name')
    order_number = serializers.ReadOnlyField(source='order.order_number')

    class Meta:
        model = MemberInvoice
        fields = [
            'id', 'invoice_number', 'order', 'order_number', 'user_profile', 'member_name',
            'branch', 'branch_name', 'subtotal', 'discount_amount',
            'reward_amount', 'tax_amount', 'total_amount', 'currency',
            'status', 'file', 'issued_at', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'invoice_number', 'issued_at', 'created_at', 'updated_at']

    def get_member_name(self, obj):
        if obj.user_profile:
            return f"{obj.user_profile.first_name_snapshot or ''} {obj.user_profile.last_name_snapshot or ''}".strip()
        return ''


class OrderSerializer(serializers.ModelSerializer):
    from decimal import Decimal
    items = OrderItemSerializer(many=True, read_only=True)
    payments = PaymentTransactionSerializer(many=True, read_only=True)
    invoices = MemberInvoiceSerializer(many=True, read_only=True)
    branch_name = serializers.ReadOnlyField(source='branch.name')
    member_name = serializers.SerializerMethodField()
    lead_name = serializers.SerializerMethodField()
    paid_amount = serializers.SerializerMethodField()
    outstanding_balance = serializers.SerializerMethodField()
    item_summary = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            'id', 'order_number', 'lead', 'lead_name', 'user_profile',
            'member_name', 'branch', 'branch_name', 'sold_by_user',
            'order_type', 'status', 'subtotal', 'discount_amount',
            'reward_amount', 'tax_amount', 'total_amount', 'currency',
            'paid_amount', 'outstanding_balance', 'item_summary',
            'source', 'notes', 'items', 'payments', 'invoices',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'order_number', 'created_at', 'updated_at']

    def get_member_name(self, obj):
        if obj.user_profile:
            return f"{obj.user_profile.first_name_snapshot or ''} {obj.user_profile.last_name_snapshot or ''}".strip()
        if obj.lead:
            return f"{obj.lead.first_name or ''} {obj.lead.last_name or ''}".strip()
        return ''

    def get_lead_name(self, obj):
        if obj.lead:
            return f"{obj.lead.first_name or ''} {obj.lead.last_name or ''}".strip()
        return ''

    def get_paid_amount(self, obj):
        from decimal import Decimal
        successful_txns = [p for p in obj.payments.all() if p.status == 'SUCCESS']
        total = sum((p.amount for p in successful_txns), Decimal('0.00'))
        return str(total)

    def get_outstanding_balance(self, obj):
        from decimal import Decimal
        total_amount = obj.total_amount or Decimal('0.00')
        successful_txns = [p for p in obj.payments.all() if p.status == 'SUCCESS']
        total_paid = sum((p.amount for p in successful_txns), Decimal('0.00'))
        return str(max(Decimal('0.00'), total_amount - total_paid))

    def get_item_summary(self, obj):
        first_item = obj.items.first()
        if first_item:
            name = first_item.item_name_snapshot
            if not name and first_item.package:
                name = first_item.package.name
            count = obj.items.count()
            if count > 1:
                return f"{name} (+{count - 1} more)"
            return name or 'Membership Package'
        return 'Membership Order'


class PaymentLinkEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentLinkEvent
        fields = ['id', 'payment_link', 'event_type', 'event_at', 'provider_reference', 'metadata', 'created_at']
        read_only_fields = ['id', 'created_at']


class PaymentLinkSerializer(serializers.ModelSerializer):
    events = PaymentLinkEventSerializer(many=True, read_only=True)

    class Meta:
        model = PaymentLink
        fields = [
            'id', 'lead', 'user_profile', 'order', 'provider',
            'external_reference', 'payment_url', 'amount', 'currency',
            'expires_at', 'status', 'created_by_user', 'events',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
