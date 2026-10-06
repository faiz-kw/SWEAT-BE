"""
apps/tenant_core/views_commerce.py — ViewSets for Layer 2 Module G: Commerce, Payments & Billing
"""

from decimal import Decimal
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.core.exceptions import ValidationError
from django.utils import timezone

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
        from django.db.models import Q
        alias = _get_db(self.request)
        qs = Order.objects.using(alias).all()
        branch_id = self.request.query_params.get('branch_id')
        user_profile_id = self.request.query_params.get('user_profile_id')
        lead_id = self.request.query_params.get('lead_id')
        status_param = self.request.query_params.get('status')
        search_param = self.request.query_params.get('search')

        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if user_profile_id:
            qs = qs.filter(user_profile_id=user_profile_id)
        if lead_id:
            qs = qs.filter(lead_id=lead_id)
        if status_param:
            if status_param.upper() in ['OUTSTANDING', 'PENDING']:
                qs = qs.filter(status__in=['PENDING_PAYMENT', 'PARTIALLY_PAID'])
            else:
                qs = qs.filter(status=status_param)
        if search_param:
            term = search_param.strip()
            qs = qs.filter(
                Q(order_number__icontains=term) |
                Q(user_profile__first_name_snapshot__icontains=term) |
                Q(user_profile__last_name_snapshot__icontains=term) |
                Q(lead__first_name__icontains=term) |
                Q(lead__last_name__icontains=term)
            )

        return qs.select_related('branch', 'user_profile', 'lead').prefetch_related('items', 'payments', 'invoices').order_by('-created_at')

    @action(detail=False, methods=['get'], url_path='summary')
    def summary(self, request):
        alias = _get_db(request)
        branch_id = request.query_params.get('branch_id')
        orders_qs = Order.objects.using(alias).all()
        txns_qs = PaymentTransaction.objects.using(alias).filter(status='SUCCESS')
        refunds_qs = Refund.objects.using(alias).filter(status='SUCCESS')

        if branch_id:
            orders_qs = orders_qs.filter(branch_id=branch_id)
            txns_qs = txns_qs.filter(order__branch_id=branch_id)
            refunds_qs = refunds_qs.filter(order__branch_id=branch_id)

        total_orders = orders_qs.count()
        settled_orders = orders_qs.filter(status__in=['PAID', 'SETTLED']).count()
        pending_orders = orders_qs.filter(status__in=['PENDING_PAYMENT', 'PARTIALLY_PAID']).count()

        gross_collected = sum((t.amount for t in txns_qs), Decimal('0.00'))
        cash_collected = sum((t.amount for t in txns_qs.filter(provider='CASH')), Decimal('0.00'))
        online_collected = sum((t.amount for t in txns_qs.exclude(provider='CASH')), Decimal('0.00'))
        refunds_total = sum((r.amount for r in refunds_qs), Decimal('0.00'))
        net_collected = max(Decimal('0.00'), gross_collected - refunds_total)

        total_outstanding = Decimal('0.00')
        for o in orders_qs.filter(status__in=['PENDING_PAYMENT', 'PARTIALLY_PAID']):
            paid = sum((p.amount for p in o.payments.all() if p.status == 'SUCCESS'), Decimal('0.00'))
            total_outstanding += max(Decimal('0.00'), (o.total_amount or Decimal('0.00')) - paid)

        pending_cash_qs = PaymentTransaction.objects.using(alias).filter(
            status='PENDING', provider='CASH'
        )
        if branch_id:
            pending_cash_qs = pending_cash_qs.filter(order__branch_id=branch_id)
        pending_cash_amount = sum((t.amount for t in pending_cash_qs), Decimal('0.00'))

        return Response({
            'total_orders': total_orders,
            'settled_orders': settled_orders,
            'pending_orders': pending_orders,
            'total_settled_amount': str(gross_collected),
            'total_outstanding_amount': str(total_outstanding),
            'gross_revenue': str(gross_collected),
            'refunded_amount': str(refunds_total),
            'net_revenue': str(net_collected),
            'cash_collected': str(cash_collected),
            'online_collected': str(online_collected),
            'pending_approval_cash_amount': str(pending_cash_amount),
        })

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
        from django.db.models import Q
        alias = _get_db(self.request)
        qs = PaymentTransaction.objects.using(alias).all()
        order_id = self.request.query_params.get('order_id')
        branch_id = self.request.query_params.get('branch_id')
        provider = self.request.query_params.get('provider')
        status_param = self.request.query_params.get('status')
        search_param = self.request.query_params.get('search')

        if order_id:
            qs = qs.filter(order_id=order_id)
        if branch_id:
            qs = qs.filter(order__branch_id=branch_id)
        if provider:
            qs = qs.filter(provider__iexact=provider)
        if status_param:
            qs = qs.filter(status__iexact=status_param)
        if search_param:
            term = search_param.strip()
            qs = qs.filter(
                Q(order__order_number__icontains=term) |
                Q(provider_transaction_id__icontains=term) |
                Q(user_profile__first_name_snapshot__icontains=term) |
                Q(user_profile__last_name_snapshot__icontains=term) |
                Q(order__lead__first_name__icontains=term) |
                Q(order__lead__last_name__icontains=term)
            )
        return qs.select_related('order', 'order__branch', 'user_profile', 'order__lead').order_by('-created_at')

    @action(detail=False, methods=['get'], url_path='cash-report')
    def cash_report(self, request):
        from django.db.models import Sum, Count, Q
        alias = _get_db(request)
        branch_id = request.query_params.get('branch_id')
        date_param = request.query_params.get('date')
        agent_id = request.query_params.get('agent_id')
        status_param = request.query_params.get('status')
        search_param = request.query_params.get('search')

        qs = PaymentTransaction.objects.using(alias).filter(provider='CASH')

        if branch_id:
            qs = qs.filter(order__branch_id=branch_id)
        if date_param:
            qs = qs.filter(created_at__date=date_param)
        if status_param:
            qs = qs.filter(status__iexact=status_param)
        if search_param:
            term = search_param.strip()
            qs = qs.filter(
                Q(order__order_number__icontains=term) |
                Q(user_profile__first_name_snapshot__icontains=term) |
                Q(user_profile__last_name_snapshot__icontains=term) |
                Q(order__lead__first_name__icontains=term) |
                Q(order__lead__last_name__icontains=term)
            )

        total_txns = qs.count()
        physical_cash = sum((t.amount for t in qs), Decimal('0.00'))
        approved_cash = sum((t.amount for t in qs.filter(status='SUCCESS')), Decimal('0.00'))
        pending_cash = sum((t.amount for t in qs.filter(status='PENDING')), Decimal('0.00'))
        rejected_cash = sum((t.amount for t in qs.filter(status='CANCELLED')), Decimal('0.00'))

        agent_data = {}
        for t in qs:
            meta = t.metadata or {}
            rec_id = meta.get('cash_recorded_by') or meta.get('receiving_agent_id') or 'unknown'
            rec_name = meta.get('recorded_by_name') or meta.get('cash_recorded_by_name') or meta.get('receiving_agent_name') or 'Staff'
            if rec_id not in agent_data:
                agent_data[rec_id] = {
                    'agent_id': rec_id,
                    'agent_name': rec_name,
                    'physical_cash': Decimal('0.00'),
                    'approved_cash': Decimal('0.00'),
                    'pending_cash': Decimal('0.00'),
                    'rejected_cash': Decimal('0.00'),
                    'count': 0,
                }
            agent_data[rec_id]['physical_cash'] += t.amount
            if t.status == 'SUCCESS':
                agent_data[rec_id]['approved_cash'] += t.amount
            elif t.status == 'PENDING':
                agent_data[rec_id]['pending_cash'] += t.amount
            elif t.status == 'CANCELLED':
                agent_data[rec_id]['rejected_cash'] += t.amount
            agent_data[rec_id]['count'] += 1

        agent_collections = [
            {
                'agent_id': v['agent_id'],
                'agent_name': v['agent_name'],
                'physical_cash': str(v['physical_cash']),
                'approved_cash': str(v['approved_cash']),
                'pending_cash': str(v['pending_cash']),
                'rejected_cash': str(v['rejected_cash']),
                'count': v['count'],
            }
            for v in agent_data.values()
        ]

        from .models_org import Branch
        branch_name = "All Branches"
        if branch_id:
            b = Branch.objects.using(alias).filter(id=branch_id).first()
            if b:
                branch_name = b.name

        serialized_txns = PaymentTransactionSerializer(qs.select_related('order', 'order__branch', 'user_profile', 'order__lead')[:100], many=True).data

        return Response({
            'branch_name': branch_name,
            'date': date_param or timezone.now().date().isoformat(),
            'transaction_count': total_txns,
            'total_physical_cash_recorded': str(physical_cash),
            'approved_cash': str(approved_cash),
            'pending_approval_cash': str(pending_cash),
            'rejected_cash': str(rejected_cash),
            'agent_collections': agent_collections,
            'transactions': serialized_txns,
        })

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
        from django.db.models import Q
        alias = _get_db(self.request)
        qs = Refund.objects.using(alias).all()
        branch_id = self.request.query_params.get('branch_id')
        status_param = self.request.query_params.get('status')
        search_param = self.request.query_params.get('search')

        if branch_id:
            qs = qs.filter(order__branch_id=branch_id)
        if status_param:
            qs = qs.filter(status__iexact=status_param)
        if search_param:
            term = search_param.strip()
            qs = qs.filter(
                Q(order__order_number__icontains=term) |
                Q(reason_text__icontains=term) |
                Q(provider_reference__icontains=term)
            )
        return qs.select_related('order', 'order__branch', 'payment_transaction', 'requested_by_user', 'approved_by_user').order_by('-created_at')


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
