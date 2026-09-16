"""
apps/tenant_core/views_commerce.py — ViewSets for Layer 2 Module G: Commerce, Payments & Billing
"""

from decimal import Decimal
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.core.exceptions import ValidationError

from apps.tenant_core.permissions import RequireActiveTenantAndOrg, TenantRBACPermission
from apps.tenant_core.context import get_tenant_db_alias

from .models_commerce import (
    Order,
    OrderItem,
    PaymentTransaction,
    Refund,
    MemberInvoice,
    PaymentLink,
    PaymentLinkEvent,
)
from .serializers_commerce import (
    OrderSerializer,
    OrderItemSerializer,
    PaymentTransactionSerializer,
    RefundSerializer,
    MemberInvoiceSerializer,
    PaymentLinkSerializer,
)
from .services_commerce import CommerceService


def _get_db(request):
    return get_tenant_db_alias() or 'default'


class OrderViewSet(viewsets.ModelViewSet):
    serializer_class = OrderSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'
    permission_action_map = {
        'create': 'core.settings.edit',
        'update': 'core.settings.edit',
        'destroy': 'core.settings.edit',
        'record_payment': 'core.settings.edit',
        'create_payment_link': 'core.settings.edit',
    }

    def get_queryset(self):
        alias = _get_db(self.request)
        qs = Order.objects.using(alias).all()
        branch_id = self.request.query_params.get('branch_id')
        user_profile_id = self.request.query_params.get('user_profile_id')
        lead_id = self.request.query_params.get('lead_id')
        status_param = self.request.query_params.get('status')

        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if user_profile_id:
            qs = qs.filter(user_profile_id=user_profile_id)
        if lead_id:
            qs = qs.filter(lead_id=lead_id)
        if status_param:
            qs = qs.filter(status=status_param)

        return qs.select_related('branch', 'user_profile', 'lead').prefetch_related('items', 'payments', 'invoices').order_by('-created_at')

    @action(detail=True, methods=['post'], url_path='record-payment')
    def record_payment(self, request, pk=None):
        alias = _get_db(request)
        amount = request.data.get('amount')
        provider = request.data.get('provider', 'CASH')
        payment_method = request.data.get('payment_method', 'CASH')
        provider_transaction_id = request.data.get('provider_transaction_id')
        idempotency_key = request.data.get('idempotency_key')
        metadata = request.data.get('metadata', {})

        if not amount:
            return Response({'error': 'amount is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            txn, invoice = CommerceService.record_payment(
                order_id=pk,
                amount=Decimal(str(amount)),
                provider=provider,
                payment_method=payment_method,
                provider_transaction_id=provider_transaction_id,
                idempotency_key=idempotency_key,
                metadata=metadata,
                actor=request.user,
                db_alias=alias,
            )
            return Response({
                'payment': PaymentTransactionSerializer(txn).data,
                'invoice': MemberInvoiceSerializer(invoice).data if invoice else None,
            }, status=status.HTTP_201_CREATED)
        except (ValidationError, Exception) as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='create-payment-link')
    def create_payment_link(self, request, pk=None):
        alias = _get_db(request)
        expiry_hours = int(request.data.get('expiry_hours', 48))
        provider = request.data.get('provider', 'RAZORPAY')

        try:
            link = CommerceService.create_payment_link(
                order_id=pk,
                expiry_hours=expiry_hours,
                provider=provider,
                actor=request.user,
                db_alias=alias,
            )
            return Response(PaymentLinkSerializer(link).data, status=status.HTTP_201_CREATED)
        except (ValidationError, Exception) as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


class OrderItemViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = OrderItemSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'

    def get_queryset(self):
        alias = _get_db(self.request)
        qs = OrderItem.objects.using(alias).all()
        order_id = self.request.query_params.get('order_id')
        if order_id:
            qs = qs.filter(order_id=order_id)
        return qs.order_by('created_at')


class PaymentTransactionViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = PaymentTransactionSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'
    permission_action_map = {'refund': 'core.settings.edit'}

    def get_queryset(self):
        alias = _get_db(self.request)
        qs = PaymentTransaction.objects.using(alias).all()
        order_id = self.request.query_params.get('order_id')
        if order_id:
            qs = qs.filter(order_id=order_id)
        return qs.order_by('-created_at')

    @action(detail=True, methods=['post'], url_path='refund')
    def refund(self, request, pk=None):
        alias = _get_db(request)
        amount = request.data.get('amount')
        reason_text = request.data.get('reason_text')
        reason_code = request.data.get('reason_code')
        provider_reference = request.data.get('provider_reference')

        if not amount:
            return Response({'error': 'amount is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            refund_obj = CommerceService.process_refund(
                payment_transaction_id=pk,
                amount=Decimal(str(amount)),
                reason_code=reason_code,
                reason_text=reason_text,
                provider_reference=provider_reference,
                actor=request.user,
                db_alias=alias,
            )
            return Response(RefundSerializer(refund_obj).data, status=status.HTTP_201_CREATED)
        except (ValidationError, Exception) as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


class RefundViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = RefundSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'

    def get_queryset(self):
        alias = _get_db(self.request)
        return Refund.objects.using(alias).all().order_by('-created_at')


class MemberInvoiceViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = MemberInvoiceSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'

    def get_queryset(self):
        alias = _get_db(self.request)
        qs = MemberInvoice.objects.using(alias).all()
        user_profile_id = self.request.query_params.get('user_profile_id')
        branch_id = self.request.query_params.get('branch_id')
        if user_profile_id:
            qs = qs.filter(user_profile_id=user_profile_id)
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        return qs.select_related('branch', 'user_profile').order_by('-issued_at')


class PaymentLinkViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = PaymentLinkSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'

    def get_queryset(self):
        alias = _get_db(self.request)
        return PaymentLink.objects.using(alias).prefetch_related('events').all().order_by('-created_at')
