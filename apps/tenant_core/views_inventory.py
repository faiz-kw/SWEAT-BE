"""
ViewSets for Module: Retail Products, Pro Shop & Inventory
Supports full CRUD, search, category filtering, multi-image storage, and multi-tenant scoping.
"""

from rest_framework import viewsets, permissions, status
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from django.db.models import Q

from .context import get_tenant_db_alias
from .models_inventory import Product
from .serializers_inventory import ProductSerializer


def _get_db(request):
    return (
        get_tenant_db_alias()
        or getattr(getattr(request, 'user', None), '_db_alias', None)
        or getattr(request, '_tenant_db_alias', None)
        or 'default'
    )


def _get_org(request):
    org = getattr(request, 'organization', None)
    if not org:
        user = getattr(request, 'user', None)
        if user and hasattr(user, 'organization') and user.organization:
            return user.organization
        alias = _get_db(request)
        from .models_org import Organization
        if user and getattr(user, 'organization_id', None):
            return Organization.objects.using(alias).filter(id=user.organization_id).first()
        return Organization.objects.using(alias).filter(status='ACTIVE').first()
    return org


class InventoryPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 500


class ProductViewSet(viewsets.ModelViewSet):
    """
    CRUD ViewSet for retail products, gym gear, supplements, and digital downloads.
    """
    serializer_class = ProductSerializer
    pagination_class = InventoryPagination
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        alias = _get_db(self.request)
        qs = Product.objects.using(alias).all()

        org = _get_org(self.request)
        if org:
            qs = qs.filter(organization=org)

        branch_id = self.request.query_params.get('branch')
        if branch_id and branch_id != 'all':
            qs = qs.filter(Q(branch_id=branch_id) | Q(branch__isnull=True))

        category = self.request.query_params.get('category')
        if category and category != 'all':
            qs = qs.filter(category__iexact=category)

        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(
                Q(name__icontains=search) |
                Q(sku__icontains=search) |
                Q(description__icontains=search)
            )

        in_stock = self.request.query_params.get('in_stock')
        if in_stock in ['true', '1']:
            qs = qs.filter(stock_quantity__gt=0)

        return qs.order_by('-created_at')

    def perform_create(self, serializer):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        if not org:
            from .models_org import Organization
            org = Organization.objects.using(alias).first()
        serializer.save(organization=org)

    def perform_update(self, serializer):
        serializer.save()

    def perform_destroy(self, instance):
        instance.delete()


from rest_framework.decorators import action
from django.core.exceptions import ValidationError
from .models_commerce import Order
from .serializers_commerce import OrderSerializer
from .services_retail import (
    RetailCommerceService,
    InsufficientStockError,
    OrderStateError,
    FulfillmentTransitionError,
)
from .services_razorpay import RazorpayVerificationError


class RetailOrderViewSet(viewsets.ModelViewSet):
    """
    Staff ViewSet for Retail Orders, Member Purchase History, and Branch Pickup Fulfillment.
    Scoped by tenant database context and organization/branch authorization.
    """
    serializer_class = OrderSerializer
    pagination_class = InventoryPagination
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        alias = _get_db(self.request)
        qs = (
            Order.objects.using(alias)
            .filter(order_type='RETAIL_PURCHASE')
            .select_related('branch', 'user_profile', 'lead', 'fulfilled_by_user')
            .prefetch_related('items__product', 'payments', 'invoices')
        )

        branch_id = self.request.query_params.get('branch') or self.request.query_params.get('branch_id')
        if branch_id and str(branch_id).lower() != 'all':
            qs = qs.filter(branch_id=branch_id)

        status_param = self.request.query_params.get('status')
        if status_param and str(status_param).lower() != 'all':
            qs = qs.filter(status__iexact=status_param)

        fulfillment_status = self.request.query_params.get('fulfillment_status')
        if fulfillment_status and str(fulfillment_status).lower() != 'all':
            qs = qs.filter(fulfillment_status__iexact=fulfillment_status)

        search = self.request.query_params.get('search')
        if search:
            search = search.strip()
            qs = qs.filter(
                Q(order_number__icontains=search) |
                Q(user_profile__first_name_snapshot__icontains=search) |
                Q(user_profile__last_name_snapshot__icontains=search) |
                Q(user_profile__user__email__icontains=search)
            )

        date_from = self.request.query_params.get('date_from') or self.request.query_params.get('start_date')
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)

        date_to = self.request.query_params.get('date_to') or self.request.query_params.get('end_date')
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)

        user_profile_id = self.request.query_params.get('user_profile_id') or self.request.query_params.get('member_id')
        if user_profile_id:
            qs = qs.filter(user_profile_id=user_profile_id)

        return qs.order_by('-created_at')

    def create(self, request, *args, **kwargs):
        alias = _get_db(request)
        data = request.data
        user_profile_id = data.get('user_profile_id') or data.get('member_id')
        branch_id = data.get('branch_id') or data.get('branch')
        items = data.get('items', [])
        notes = data.get('notes')

        from .models_workforce import UserProfile
        from .models_org import Branch

        user_profile = None
        if user_profile_id:
            user_profile = UserProfile.objects.using(alias).filter(id=user_profile_id).first()
        elif getattr(request.user, 'is_authenticated', False) and hasattr(request.user, 'id'):
            user_profile = UserProfile.objects.using(alias).filter(user=request.user).first()

        if not user_profile:
            return Response({'detail': 'Valid user_profile / member is required.'}, status=status.HTTP_400_BAD_REQUEST)

        branch = None
        if branch_id and str(branch_id).lower() != 'all':
            branch = Branch.objects.using(alias).filter(id=branch_id).first()
        if not branch:
            branch = getattr(user_profile, 'preferred_branch', None) or Branch.objects.using(alias).filter(status='ACTIVE').first()

        if not branch:
            return Response({'detail': 'Valid pickup branch is required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            order, reservations = RetailCommerceService.create_retail_order(
                user_profile=user_profile,
                branch=branch,
                items=items,
                source=data.get('source', 'MOBILE_APP'),
                notes=notes,
                db_alias=alias,
            )
            return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED)
        except (InsufficientStockError, ValidationError) as e:
            return Response({'detail': str(e), 'code': 'ORDER_CREATION_FAILED'}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='fulfill')
    def fulfill(self, request, pk=None):
        alias = _get_db(request)
        new_status = request.data.get('fulfillment_status') or request.data.get('status') or 'FULFILLED'
        staff_user = request.user if getattr(request.user, 'is_authenticated', False) and hasattr(request.user, 'id') else None
        try:
            order = RetailCommerceService.update_fulfillment_status(
                order_id=pk,
                new_status=new_status,
                staff_user=staff_user,
                db_alias=alias,
            )
            return Response(OrderSerializer(order).data, status=status.HTTP_200_OK)
        except (FulfillmentTransitionError, ValidationError) as e:
            return Response({'detail': str(e), 'code': 'INVALID_FULFILLMENT_TRANSITION'}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='confirm-payment')
    def confirm_payment(self, request, pk=None):
        alias = _get_db(request)
        data = request.data
        payment_id = data.get('payment_id') or data.get('razorpay_payment_id')
        rzp_order_id = data.get('razorpay_order_id')
        signature = data.get('razorpay_signature')
        is_sandbox = data.get('is_sandbox_simulation', False)

        try:
            result = RetailCommerceService.confirm_retail_payment(
                order_id=pk,
                payment_id=payment_id,
                razorpay_order_id=rzp_order_id,
                signature=signature,
                db_alias=alias,
                is_sandbox_simulation=is_sandbox,
            )
            return Response(result, status=status.HTTP_200_OK)
        except (RazorpayVerificationError, ValidationError) as e:
            return Response({'detail': str(e), 'code': 'PAYMENT_VERIFICATION_FAILED'}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='cancel')
    def cancel(self, request, pk=None):
        alias = _get_db(request)
        reason = request.data.get('reason', 'CANCELLED_BY_STAFF')
        try:
            result = RetailCommerceService.release_retail_reservation(
                order_id=pk,
                reason=reason,
                db_alias=alias,
            )
            return Response(result, status=status.HTTP_200_OK)
        except (OrderStateError, ValidationError) as e:
            return Response({'detail': str(e), 'code': 'ORDER_CANCELLATION_FAILED'}, status=status.HTTP_400_BAD_REQUEST)

