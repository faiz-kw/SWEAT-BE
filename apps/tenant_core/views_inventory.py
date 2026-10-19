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
        serializer.save(using=alias, organization=org)

    def perform_update(self, serializer):
        alias = _get_db(self.request)
        serializer.save(using=alias)

    def perform_destroy(self, instance):
        alias = _get_db(self.request)
        instance.delete(using=alias)
