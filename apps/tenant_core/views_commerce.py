"""
apps/tenant_core/views_commerce.py — ViewSets for Layer 2 Module G: Commerce, Payments & Billing
"""

from decimal import Decimal
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from django.core.exceptions import ValidationError
from django.utils import timezone


class ConfigurablePageNumberPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 1000

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


def _parse_multi_param(request, *param_names):
    vals = []
    for name in param_names:
        if not name:
            continue
        if hasattr(request.query_params, 'getlist'):
            raw_list = request.query_params.getlist(name)
        else:
            v = request.query_params.get(name)
            raw_list = [v] if v else []
        for item in raw_list:
            if item:
                for part in str(item).split(','):
                    p = part.strip()
                    if p and p.lower() != 'all':
                        vals.append(p)
    return vals


class OrderViewSet(viewsets.ModelViewSet):
    serializer_class = OrderSerializer
    pagination_class = ConfigurablePageNumberPagination
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
        branch_ids = _parse_multi_param(self.request, 'branch_ids', 'branch_id', 'branch', 'location')
        program_ids = _parse_multi_param(self.request, 'program_ids', 'program_id')
        package_ids = _parse_multi_param(self.request, 'package_ids', 'package_id')
        sales_user_ids = _parse_multi_param(self.request, 'sales_user_ids', 'sales_user_id')
        trainer_ids = _parse_multi_param(self.request, 'trainer_ids', 'trainer_id')
        date_from = (self.request.query_params.get('date_from') or self.request.query_params.get('start_date') or '').strip()
        date_to = (self.request.query_params.get('date_to') or self.request.query_params.get('end_date') or '').strip()
        user_profile_id = self.request.query_params.get('user_profile_id')
        lead_id = self.request.query_params.get('lead_id')
        status_param = self.request.query_params.get('status')
        search_param = self.request.query_params.get('search')
        exclude_zero = self.request.query_params.get('exclude_zero')

        if branch_ids:
            qs = qs.filter(branch_id__in=branch_ids)
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)

        has_join_filter = False
        if program_ids:
            qs = qs.filter(items__package__program_id__in=program_ids)
            has_join_filter = True
        if package_ids:
            qs = qs.filter(items__package_id__in=package_ids)
            has_join_filter = True
        if sales_user_ids:
            qs = qs.filter(
                Q(sold_by_user_id__in=sales_user_ids) | Q(lead__assigned_sales_user_id__in=sales_user_ids)
            )
        if trainer_ids:
            qs = qs.filter(
                Q(sold_by_user_id__in=trainer_ids)
                | Q(lead__assigned_trainer_user_id__in=trainer_ids)
                | Q(lead__referred_by_user_id__in=trainer_ids)
            )

        if user_profile_id:
            qs = qs.filter(user_profile_id=user_profile_id)
        if lead_id:
            qs = qs.filter(lead_id=lead_id)
        if exclude_zero in ('1', 'true', 'True'):
            qs = qs.filter(total_amount__gt=Decimal('0.00'))
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

        if has_join_filter:
            qs = qs.distinct()

        return qs.select_related('branch', 'user_profile', 'lead').prefetch_related('items', 'payments', 'invoices').order_by('-created_at')

    @action(detail=False, methods=['get'], url_path='summary')
    def summary(self, request):
        from django.db.models import Sum, Count, Q
        from django.db.models.functions import TruncMonth
        from .models_org import Branch
        from .models_catalog import Program, Package
        from .models_crm import Lead

        alias = _get_db(request)

        def _parse_multi(param_name, legacy_name=None):
            vals = []
            if hasattr(request.query_params, 'getlist'):
                raw_list = request.query_params.getlist(param_name)
                if legacy_name:
                    raw_list = list(raw_list) + list(request.query_params.getlist(legacy_name))
            else:
                v = request.query_params.get(param_name)
                raw_list = [v] if v else []
                if legacy_name and request.query_params.get(legacy_name):
                    raw_list.append(request.query_params.get(legacy_name))
            for item in raw_list:
                if item:
                    for part in str(item).split(','):
                        p = part.strip()
                        if p and p.lower() != 'all':
                            vals.append(p)
            return vals

        branch_ids = _parse_multi('branch_ids', 'branch_id')
        program_ids = _parse_multi('program_ids', 'program_id')
        package_ids = _parse_multi('package_ids', 'package_id')
        sales_user_ids = _parse_multi('sales_user_ids', 'sales_user_id')
        trainer_ids = _parse_multi('trainer_ids', 'trainer_id')
        date_from = (request.query_params.get('date_from') or '').strip()
        date_to = (request.query_params.get('date_to') or '').strip()

        orders_qs = Order.objects.using(alias).all()
        txns_qs = PaymentTransaction.objects.using(alias).all()
        refunds_qs = Refund.objects.using(alias).all()

        if branch_ids:
            orders_qs = orders_qs.filter(branch_id__in=branch_ids)
            txns_qs = txns_qs.filter(order__branch_id__in=branch_ids)
            refunds_qs = refunds_qs.filter(order__branch_id__in=branch_ids)

        if date_from:
            orders_qs = orders_qs.filter(created_at__date__gte=date_from)
            txns_qs = txns_qs.filter(created_at__date__gte=date_from)
            refunds_qs = refunds_qs.filter(created_at__date__gte=date_from)

        if date_to:
            orders_qs = orders_qs.filter(created_at__date__lte=date_to)
            txns_qs = txns_qs.filter(created_at__date__lte=date_to)
            refunds_qs = refunds_qs.filter(created_at__date__lte=date_to)

        has_join_filter = False
        if program_ids:
            orders_qs = orders_qs.filter(items__package__program_id__in=program_ids)
            txns_qs = txns_qs.filter(order__items__package__program_id__in=program_ids)
            refunds_qs = refunds_qs.filter(order__items__package__program_id__in=program_ids)
            has_join_filter = True

        if package_ids:
            orders_qs = orders_qs.filter(items__package_id__in=package_ids)
            txns_qs = txns_qs.filter(order__items__package_id__in=package_ids)
            refunds_qs = refunds_qs.filter(order__items__package_id__in=package_ids)
            has_join_filter = True

        if sales_user_ids:
            sales_q = Q(sold_by_user_id__in=sales_user_ids) | Q(lead__assigned_sales_user_id__in=sales_user_ids)
            orders_qs = orders_qs.filter(sales_q)
            txns_qs = txns_qs.filter(
                Q(order__sold_by_user_id__in=sales_user_ids) | Q(order__lead__assigned_sales_user_id__in=sales_user_ids)
            )
            refunds_qs = refunds_qs.filter(
                Q(order__sold_by_user_id__in=sales_user_ids) | Q(order__lead__assigned_sales_user_id__in=sales_user_ids)
            )

        if trainer_ids:
            trainer_q = (
                Q(sold_by_user_id__in=trainer_ids) |
                Q(lead__assigned_trainer_user_id__in=trainer_ids) |
                Q(lead__referred_by_user_id__in=trainer_ids)
            )
            orders_qs = orders_qs.filter(trainer_q)
            txns_qs = txns_qs.filter(
                Q(order__sold_by_user_id__in=trainer_ids) |
                Q(order__lead__assigned_trainer_user_id__in=trainer_ids) |
                Q(order__lead__referred_by_user_id__in=trainer_ids)
            )
            refunds_qs = refunds_qs.filter(
                Q(order__sold_by_user_id__in=trainer_ids) |
                Q(order__lead__assigned_trainer_user_id__in=trainer_ids) |
                Q(order__lead__referred_by_user_id__in=trainer_ids)
            )

        if has_join_filter:
            orders_qs = orders_qs.distinct()
            txns_qs = txns_qs.distinct()
            refunds_qs = refunds_qs.distinct()

        # Order counts & amounts by status via SQL aggregation
        total_orders = orders_qs.count()
        settled_orders = orders_qs.filter(status__in=['PAID', 'SETTLED']).count()
        pending_orders = orders_qs.filter(status__in=['PENDING_PAYMENT', 'PARTIALLY_PAID']).count()
        cancelled_orders = orders_qs.filter(status='CANCELLED').count()

        success_txns = txns_qs.filter(status='SUCCESS')
        gross_collected = success_txns.aggregate(s=Sum('amount'))['s'] or Decimal('0.00')

        # Physical cash vs digital/online collections
        cash_q = Q(payment_method__iregex=r'cash') | (Q(provider='CASH') & (Q(payment_method__isnull=True) | Q(payment_method__exact='') | Q(payment_method__exact=' ')))
        upi_q = Q(payment_method__iregex=r'upi|gpay')
        bank_q = Q(payment_method__iregex=r'imps|neft|rtgs|netbanking|cheque')
        card_q = Q(payment_method__iregex=r'card|emi|wallet')

        cash_agg = success_txns.filter(cash_q).aggregate(c=Count('id'), s=Sum('amount'))
        cash_collected = cash_agg['s'] or Decimal('0.00')
        cash_txn_count = cash_agg['c'] or 0

        online_collected = max(Decimal('0.00'), gross_collected - cash_collected)
        online_txn_count = max(0, success_txns.count() - cash_txn_count)

        upi_agg = success_txns.exclude(cash_q).filter(upi_q).aggregate(c=Count('id'), s=Sum('amount'))
        bank_agg = success_txns.exclude(cash_q).exclude(upi_q).filter(bank_q).aggregate(c=Count('id'), s=Sum('amount'))
        card_agg = success_txns.exclude(cash_q).exclude(upi_q).exclude(bank_q).filter(card_q).aggregate(c=Count('id'), s=Sum('amount'))
        other_online_agg = success_txns.exclude(cash_q).exclude(upi_q).exclude(bank_q).exclude(card_q).aggregate(c=Count('id'), s=Sum('amount'))

        # Refunds (processed/approved refunds + any REFUNDED status orders/transactions)
        processed_refunds_qs = refunds_qs.filter(status__in=['SUCCESS', 'PROCESSED', 'APPROVED'])
        refunds_table_total = processed_refunds_qs.aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
        refunded_txns_total = txns_qs.filter(status='REFUNDED').aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
        refunds_total = refunds_table_total + refunded_txns_total
        refund_count = processed_refunds_qs.count() + txns_qs.filter(status='REFUNDED').count()
        pending_refunds_total = refunds_qs.filter(status__in=['REQUESTED', 'PROCESSING']).aggregate(s=Sum('amount'))['s'] or Decimal('0.00')

        net_collected = max(Decimal('0.00'), gross_collected - refunds_total)

        # Fast SQL calculation of outstanding balance on PENDING_PAYMENT / PARTIALLY_PAID orders
        pending_orders_qs = orders_qs.filter(status__in=['PENDING_PAYMENT', 'PARTIALLY_PAID'])
        pending_order_total = pending_orders_qs.aggregate(s=Sum('total_amount'))['s'] or Decimal('0.00')
        partial_paid_on_pending = txns_qs.filter(
            order__status__in=['PENDING_PAYMENT', 'PARTIALLY_PAID'],
            status='SUCCESS'
        ).aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
        total_outstanding = max(Decimal('0.00'), pending_order_total - partial_paid_on_pending)

        cancelled_amount = orders_qs.filter(status='CANCELLED').aggregate(s=Sum('total_amount'))['s'] or Decimal('0.00')

        pending_cash_qs = txns_qs.filter(status='PENDING').filter(cash_q)
        pending_cash_amount = pending_cash_qs.aggregate(s=Sum('amount'))['s'] or Decimal('0.00')

        # Total tax & discounts across PAID orders
        paid_orders_agg = orders_qs.filter(status__in=['PAID', 'SETTLED']).aggregate(
            tax=Sum('tax_amount'),
            discount=Sum('discount_amount'),
            subtotal=Sum('subtotal'),
        )

        # Payment mode breakdown for Cash Flow Report
        payment_mode_breakdown = [
            {
                'mode': 'UPI / GPay / QR',
                'category': 'ONLINE',
                'count': upi_agg['c'] or 0,
                'amount': str(upi_agg['s'] or Decimal('0.00')),
            },
            {
                'mode': 'Bank Transfer (IMPS / NEFT / Cheque)',
                'category': 'ONLINE',
                'count': bank_agg['c'] or 0,
                'amount': str(bank_agg['s'] or Decimal('0.00')),
            },
            {
                'mode': 'In-Studio Cash (Physical Cash)',
                'category': 'CASH',
                'count': cash_txn_count,
                'amount': str(cash_collected),
            },
            {
                'mode': 'Credit / Debit Card & POS',
                'category': 'ONLINE',
                'count': card_agg['c'] or 0,
                'amount': str(card_agg['s'] or Decimal('0.00')),
            },
            {
                'mode': 'Razorpay Online Checkout',
                'category': 'ONLINE',
                'count': other_online_agg['c'] or 0,
                'amount': str(other_online_agg['s'] or Decimal('0.00')),
            },
        ]

        # Monthly Cash Flow breakdown (most recent 12 active months)
        monthly_qs = (
            success_txns.filter(amount__gt=0)
            .annotate(month=TruncMonth('created_at'))
            .values('month')
            .annotate(
                gross_inflow=Sum('amount'),
                cash_inflow=Sum('amount', filter=cash_q),
                txn_count=Count('id'),
            )
            .order_by('-month')[:12]
        )
        monthly_cash_flow = []
        for m in reversed(list(monthly_qs)):
            gross_m = m['gross_inflow'] or Decimal('0.00')
            cash_m = m['cash_inflow'] or Decimal('0.00')
            online_m = max(Decimal('0.00'), gross_m - cash_m)
            month_str = m['month'].strftime('%b %Y') if m['month'] else 'Unknown'
            monthly_cash_flow.append({
                'month': month_str,
                'gross_inflow': str(gross_m),
                'cash_inflow': str(cash_m),
                'online_inflow': str(online_m),
                'refunded': '0.00',
                'net_cash_flow': str(gross_m),
                'txn_count': m['txn_count'] or 0,
            })

        # 1. Branch Breakdown (Which branch is returning more revenue)
        branch_order_rows = (
            orders_qs.values('branch__id', 'branch__name')
            .annotate(
                total_orders=Count('id'),
                paid_orders=Count('id', filter=Q(status__in=['PAID', 'SETTLED'])),
                pending_orders=Count('id', filter=Q(status__in=['PENDING_PAYMENT', 'PARTIALLY_PAID'])),
                revenue=Sum('total_amount', filter=Q(status__in=['PAID', 'SETTLED'])),
                outstanding=Sum('total_amount', filter=Q(status__in=['PENDING_PAYMENT', 'PARTIALLY_PAID'])),
            )
            .order_by('-revenue')
        )
        branch_cash_map = {
            str(r['order__branch_id']): (r['cash_rev'] or Decimal('0.00'))
            for r in success_txns.values('order__branch_id').annotate(cash_rev=Sum('amount', filter=cash_q))
        }
        branch_breakdown = []
        for b in branch_order_rows:
            bid = str(b['branch__id'] or '')
            rev = b['revenue'] or Decimal('0.00')
            c_rev = branch_cash_map.get(bid, Decimal('0.00'))
            o_rev = max(Decimal('0.00'), rev - c_rev)
            branch_breakdown.append({
                'id': bid,
                'name': b['branch__name'] or 'Main Studio',
                'total_orders': b['total_orders'] or 0,
                'paid_orders': b['paid_orders'] or 0,
                'pending_orders': b['pending_orders'] or 0,
                'revenue': str(rev),
                'cash_revenue': str(c_rev),
                'online_revenue': str(o_rev),
                'outstanding': str(b['outstanding'] or Decimal('0.00')),
            })

        # 2. Program Breakdown (Which program people are most interested in & buying)
        order_ids_sub = orders_qs.values('id')
        program_rows = (
            OrderItem.objects.using(alias)
            .filter(order_id__in=order_ids_sub, package__program__isnull=False)
            .values('package__program__id', 'package__program__name', 'package__program__category__name')
            .annotate(
                total_orders=Count('order_id', distinct=True),
                paid_orders=Count('order_id', filter=Q(order__status__in=['PAID', 'SETTLED']), distinct=True),
                revenue=Sum('total_amount', filter=Q(order__status__in=['PAID', 'SETTLED'])),
            )
            .order_by('-revenue', '-total_orders')[:20]
        )
        program_breakdown = [
            {
                'id': str(p['package__program__id']),
                'name': p['package__program__name'] or 'General Program',
                'category': p['package__program__category__name'] or 'Fitness',
                'total_orders': p['total_orders'] or 0,
                'paid_orders': p['paid_orders'] or 0,
                'revenue': str(p['revenue'] or Decimal('0.00')),
            }
            for p in program_rows
        ]

        # 3. Top Packages Breakdown
        top_packages_qs = (
            OrderItem.objects.using(alias)
            .filter(order_id__in=order_ids_sub, order__status__in=['PAID', 'SETTLED'], total_amount__gt=0)
            .values('package__id', 'package__name', 'package__program__name', 'item_name_snapshot')
            .annotate(count=Count('id'), revenue=Sum('total_amount'))
            .order_by('-revenue')[:15]
        )
        top_packages = [
            {
                'id': str(p['package__id'] or ''),
                'name': (p['package__name'] or p['item_name_snapshot'] or 'Membership Plan').strip(),
                'program_name': p['package__program__name'] or 'General',
                'orders_count': p['count'],
                'revenue': str(p['revenue'] or Decimal('0.00')),
            }
            for p in top_packages_qs
        ]

        # 4. Sales Person Breakdown & 5. Trainer Referrals Breakdown
        staff_rows = (
            orders_qs.filter(sold_by_user__isnull=False)
            .values(
                'sold_by_user__id',
                'sold_by_user__email',
                'sold_by_user__profile__first_name_snapshot',
                'sold_by_user__profile__last_name_snapshot',
                'sold_by_user__profile__employee_profile__sales_profile__id',
                'sold_by_user__profile__employee_profile__trainer_profile__id',
            )
            .annotate(
                total_orders=Count('id'),
                paid_orders=Count('id', filter=Q(status__in=['PAID', 'SETTLED'])),
                pending_orders=Count('id', filter=Q(status__in=['PENDING_PAYMENT', 'PARTIALLY_PAID'])),
                cancelled_orders=Count('id', filter=Q(status='CANCELLED')),
                revenue=Sum('total_amount', filter=Q(status__in=['PAID', 'SETTLED'])),
            )
            .order_by('-revenue', '-paid_orders')[:30]
        )

        sales_user_breakdown = []
        trainer_breakdown = []
        for s in staff_rows:
            uid = str(s['sold_by_user__id'])
            fname = (s['sold_by_user__profile__first_name_snapshot'] or '').strip()
            lname = (s['sold_by_user__profile__last_name_snapshot'] or '').strip()
            email = (s['sold_by_user__email'] or '').strip()
            display_name = f"{fname} {lname}".strip() or email.split('@')[0] or 'Staff'
            tot = s['total_orders'] or 0
            paid_c = s['paid_orders'] or 0
            pend_c = s['pending_orders'] or 0
            canc_c = s['cancelled_orders'] or 0
            rev = s['revenue'] or Decimal('0.00')
            conv_rate = round((paid_c / tot) * 100, 1) if tot > 0 else 0.0

            if s['sold_by_user__profile__employee_profile__sales_profile__id'] or not s['sold_by_user__profile__employee_profile__trainer_profile__id']:
                sales_user_breakdown.append({
                    'id': uid,
                    'name': display_name,
                    'email': email,
                    'total_orders': tot,
                    'paid_orders': paid_c,
                    'pending_orders': pend_c,
                    'cancelled_orders': canc_c,
                    'conversion_rate': conv_rate,
                    'revenue': str(rev),
                })

            if s['sold_by_user__profile__employee_profile__trainer_profile__id']:
                trainer_breakdown.append({
                    'id': uid,
                    'name': display_name,
                    'email': email,
                    'referrals_joined': paid_c,
                    'total_referrals': tot,
                    'conversion_rate': conv_rate,
                    'revenue': str(rev),
                })

        # Filter options for multi-select dropdowns
        all_branches = [
            {'id': str(b['id']), 'name': b['name']}
            for b in Branch.objects.using(alias).values('id', 'name').order_by('name')
        ]
        all_programs = [
            {'id': str(p['id']), 'name': p['name'], 'category_name': p['category__name'] or ''}
            for p in Program.objects.using(alias).values('id', 'name', 'category__name').order_by('name')
        ]
        all_packages = [
            {
                'id': str(pk['id']),
                'name': pk['name'],
                'program_id': str(pk['program_id'] or ''),
                'program_name': pk['program__name'] or '',
            }
            for pk in Package.objects.using(alias).values('id', 'name', 'program_id', 'program__name').order_by('name')
        ]

        all_staff_qs = (
            Order.objects.using(alias)
            .filter(sold_by_user__isnull=False)
            .values(
                'sold_by_user__id',
                'sold_by_user__email',
                'sold_by_user__profile__first_name_snapshot',
                'sold_by_user__profile__last_name_snapshot',
                'sold_by_user__profile__employee_profile__sales_profile__id',
                'sold_by_user__profile__employee_profile__trainer_profile__id',
            )
            .annotate(c=Count('id'))
            .order_by('-c')[:50]
        )
        all_sales_users = []
        all_trainers = []
        for st in all_staff_qs:
            uid = str(st['sold_by_user__id'])
            fname = (st['sold_by_user__profile__first_name_snapshot'] or '').strip()
            lname = (st['sold_by_user__profile__last_name_snapshot'] or '').strip()
            email = (st['sold_by_user__email'] or '').strip()
            name = f"{fname} {lname}".strip() or email.split('@')[0] or 'Staff'
            entry = {'id': uid, 'name': name, 'email': email}
            if st['sold_by_user__profile__employee_profile__sales_profile__id'] or not st['sold_by_user__profile__employee_profile__trainer_profile__id']:
                all_sales_users.append(entry)
            if st['sold_by_user__profile__employee_profile__trainer_profile__id']:
                all_trainers.append(entry)

        return Response({
            'total_orders': total_orders,
            'settled_orders': settled_orders,
            'pending_orders': pending_orders,
            'cancelled_orders': cancelled_orders,
            'total_settled_amount': str(gross_collected),
            'total_outstanding_amount': str(total_outstanding),
            'cancelled_amount': str(cancelled_amount),
            'gross_revenue': str(gross_collected),
            'refunded_amount': str(refunds_total),
            'refund_count': refund_count,
            'pending_refunds_amount': str(pending_refunds_total),
            'net_revenue': str(net_collected),
            'cash_collected': str(cash_collected),
            'cash_txn_count': cash_txn_count,
            'online_collected': str(online_collected),
            'online_txn_count': online_txn_count,
            'pending_approval_cash_amount': str(pending_cash_amount),
            'tax_collected': str(paid_orders_agg['tax'] or Decimal('0.00')),
            'discount_given': str(paid_orders_agg['discount'] or Decimal('0.00')),
            'subtotal_revenue': str(paid_orders_agg['subtotal'] or Decimal('0.00')),
            'payment_mode_breakdown': payment_mode_breakdown,
            'monthly_cash_flow': monthly_cash_flow,
            'branch_breakdown': branch_breakdown,
            'program_breakdown': program_breakdown,
            'top_packages': top_packages,
            'sales_user_breakdown': sales_user_breakdown,
            'trainer_breakdown': trainer_breakdown,
            'filter_options': {
                'branches': all_branches,
                'programs': all_programs,
                'packages': all_packages,
                'sales_users': all_sales_users,
                'trainers': all_trainers,
            },
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
    pagination_class = ConfigurablePageNumberPagination
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
        branch_ids_param = self.request.query_params.get('branch_ids')
        provider = self.request.query_params.get('provider')
        status_param = self.request.query_params.get('status')
        search_param = self.request.query_params.get('search')
        date_from = self.request.query_params.get('date_from') or self.request.query_params.get('start_date')
        date_to = self.request.query_params.get('date_to') or self.request.query_params.get('end_date')

        if order_id:
            qs = qs.filter(order_id=order_id)
        if branch_ids_param:
            b_ids = [b.strip() for b in branch_ids_param.split(',') if b.strip()]
            if b_ids:
                qs = qs.filter(order__branch_id__in=b_ids)
        elif branch_id:
            qs = qs.filter(order__branch_id=branch_id)
        if provider:
            qs = qs.filter(provider__iexact=provider)
        if status_param:
            qs = qs.filter(status__iexact=status_param)
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)
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
        branch_ids_param = request.query_params.get('branch_ids')
        date_param = request.query_params.get('date')
        date_from = request.query_params.get('date_from') or request.query_params.get('start_date')
        date_to = request.query_params.get('date_to') or request.query_params.get('end_date')
        status_param = request.query_params.get('status')
        search_param = request.query_params.get('search')

        cash_q = Q(payment_method__iregex=r'cash') | (
            Q(provider='CASH') & (Q(payment_method__isnull=True) | Q(payment_method__exact='') | Q(payment_method__exact=' '))
        )
        base_qs = PaymentTransaction.objects.using(alias).filter(cash_q)

        if branch_ids_param:
            b_ids = [b.strip() for b in branch_ids_param.split(',') if b.strip()]
            if b_ids:
                base_qs = base_qs.filter(order__branch_id__in=b_ids)
        elif branch_id:
            base_qs = base_qs.filter(order__branch_id=branch_id)
        if status_param:
            base_qs = base_qs.filter(status__iexact=status_param)
        if search_param:
            term = search_param.strip()
            base_qs = base_qs.filter(
                Q(order__order_number__icontains=term) |
                Q(user_profile__first_name_snapshot__icontains=term) |
                Q(user_profile__last_name_snapshot__icontains=term) |
                Q(order__lead__first_name__icontains=term) |
                Q(order__lead__last_name__icontains=term)
            )

        effective_date_label = 'All Time'
        qs = base_qs
        if date_from or date_to:
            if date_from:
                qs = qs.filter(created_at__date__gte=date_from)
            if date_to:
                qs = qs.filter(created_at__date__lte=date_to)
            effective_date_label = f"{date_from or 'Start'} to {date_to or 'Now'}"
        elif date_param and date_param.lower() != 'all':
            qs = base_qs.filter(created_at__date=date_param)
            effective_date_label = date_param

        total_txns = qs.count()
        physical_cash = qs.aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
        approved_cash = qs.filter(status='SUCCESS').aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
        pending_cash = qs.filter(status='PENDING').aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
        rejected_cash = qs.filter(status__in=['CANCELLED', 'FAILED']).aggregate(s=Sum('amount'))['s'] or Decimal('0.00')

        # Group by payment_method / channel & sold_by_user via SQL aggregation
        channel_groups = (
            qs.values('payment_method', 'order__sold_by_user__id', 'order__sold_by_user__email')
            .annotate(
                count=Count('id'),
                physical_cash=Sum('amount'),
                approved_cash=Sum('amount', filter=Q(status='SUCCESS')),
                pending_cash=Sum('amount', filter=Q(status='PENDING')),
                rejected_cash=Sum('amount', filter=Q(status__in=['CANCELLED', 'FAILED'])),
            )
            .order_by('-physical_cash')[:20]
        )

        agent_collections = []
        for idx, g in enumerate(channel_groups):
            method_label = (g.get('payment_method') or 'Cash').strip()
            agent_email = g.get('order__sold_by_user__email')
            agent_name = f"{agent_email} ({method_label})" if agent_email else f"Front Desk — {method_label}"
            agent_collections.append({
                'agent_id': str(g.get('order__sold_by_user__id') or f"channel-{idx}"),
                'agent_name': agent_name,
                'physical_cash': str(g['physical_cash'] or Decimal('0.00')),
                'approved_cash': str(g['approved_cash'] or Decimal('0.00')),
                'pending_cash': str(g['pending_cash'] or Decimal('0.00')),
                'rejected_cash': str(g['rejected_cash'] or Decimal('0.00')),
                'count': g['count'] or 0,
            })

        from .models_org import Branch
        branch_name = "All Branches"
        if branch_id:
            b = Branch.objects.using(alias).filter(id=branch_id).first()
            if b:
                branch_name = b.name

        serialized_txns = PaymentTransactionSerializer(
            qs.select_related('order', 'order__branch', 'user_profile', 'order__lead').order_by('-created_at')[:100],
            many=True
        ).data

        return Response({
            'branch_id': branch_id,
            'branch_name': branch_name,
            'date': effective_date_label,
            'total_transactions': total_txns,
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
