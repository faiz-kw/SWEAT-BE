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
    class Meta:
        model = PaymentTransaction
        fields = [
            'id', 'order', 'user_profile', 'provider', 'payment_method',
            'provider_transaction_id', 'idempotency_key', 'amount',
            'currency', 'status', 'paid_at', 'metadata',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class RefundSerializer(serializers.ModelSerializer):
    class Meta:
        model = Refund
        fields = [
            'id', 'payment_transaction', 'order', 'amount',
            'reason_code', 'reason_text', 'provider_reference',
            'status', 'requested_by_user', 'approved_by_user',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class MemberInvoiceSerializer(serializers.ModelSerializer):
    member_name = serializers.SerializerMethodField()
    branch_name = serializers.ReadOnlyField(source='branch.name')

    class Meta:
        model = MemberInvoice
        fields = [
            'id', 'invoice_number', 'order', 'user_profile', 'member_name',
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
    items = OrderItemSerializer(many=True, read_only=True)
    payments = PaymentTransactionSerializer(many=True, read_only=True)
    invoices = MemberInvoiceSerializer(many=True, read_only=True)
    branch_name = serializers.ReadOnlyField(source='branch.name')
    member_name = serializers.SerializerMethodField()
    lead_name = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            'id', 'order_number', 'lead', 'lead_name', 'user_profile',
            'member_name', 'branch', 'branch_name', 'sold_by_user',
            'order_type', 'status', 'subtotal', 'discount_amount',
            'reward_amount', 'tax_amount', 'total_amount', 'currency',
            'source', 'notes', 'items', 'payments', 'invoices',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'order_number', 'created_at', 'updated_at']

    def get_member_name(self, obj):
        if obj.user_profile:
            return f"{obj.user_profile.first_name_snapshot or ''} {obj.user_profile.last_name_snapshot or ''}".strip()
        return ''

    def get_lead_name(self, obj):
        if obj.lead:
            return f"{obj.lead.first_name or ''} {obj.lead.last_name or ''}".strip()
        return ''


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
