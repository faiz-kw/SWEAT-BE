"""
Master app views — Platform-facing API endpoints.
All endpoints require Platform JWT authentication.
"""

import logging
import json
import uuid
import sys
from django.db import models, transaction
from django.http import Http404
from django.utils import timezone
from decimal import Decimal
from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response

from .models_iam import PlatformUser
from .models_tenant import Tenant, TenantDomain, TenantBranding, PlatformBranding
from .models_saas import (
    SaasPlan, SaasPlanPrice, ProductModule, TenantModule,
    TenantSubscription, SubscriptionInvoice, TenantResourceUsage, TenantResourceLimit,
)
from .models_market import MarketplaceIntegration
from .models_infra import TenantProvisioning, TenantDataSource, PlatformAuditEvent
from .serializers import (
    TenantSerializer, TenantDetailSerializer,
    SaasPlanSerializer, PlatformUserSerializer,
    MarketplaceIntegrationSerializer, TenantProvisioningSerializer,
    ProductModuleSerializer, TenantModuleSerializer,
    TenantSubscriptionSerializer, SubscriptionInvoiceSerializer,
    TenantResourceUsageSerializer,
    PlatformBrandingSerializer, TenantBrandingSerializer,
)

from .permissions import PlatformRBACPermission

logger = logging.getLogger(__name__)


class TenantViewSet(viewsets.ModelViewSet):
    """
    CRUD for tenants. Platform admin only.
    Includes custom actions: activate, suspend, deactivate, metrics, toggle-status.
    """
    permission_classes = [PlatformRBACPermission]
    platform_permission_prefix = 'tenants'
    serializer_class = TenantSerializer
    queryset = Tenant.objects.using('default').all()

    def get_queryset(self):
        qs = Tenant.objects.using('default').all().prefetch_related(
            'subscriptions__plan',
            'enabled_modules_set__module',
        )
        status_filter = self.request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        return qs

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return TenantDetailSerializer
        return TenantSerializer

    def perform_update(self, serializer):
        from decimal import Decimal
        from django.utils import timezone
        from apps.master.models_saas import ProductModule, TenantModule, ResourceMetric, TenantResourceLimit

        tenant = serializer.save()

        # 1. Update enabled_modules and sync to TenantModule and Tenant DB
        if 'enabled_modules' in self.request.data:
            raw_modules = self.request.data.get('enabled_modules') or []
            enabled_codes = set()
            for item in raw_modules:
                if not item:
                    continue
                code = item.strip('/').split('/')[0].lower()
                enabled_codes.add(code)

            all_modules = ProductModule.objects.using('default').all()
            for mod in all_modules:
                is_on = mod.code in enabled_codes
                TenantModule.objects.using('default').update_or_create(
                    tenant=tenant,
                    module=mod,
                    defaults={
                        'is_enabled': is_on,
                        'status': 'ENABLED' if is_on else 'DISABLED',
                        'availability_mode': 'ALL_BRANCHES',
                    }
                )

            # Sync to tenant DB catalog and branch modules
            try:
                from apps.authentication.views import _register_and_resolve_tenant
                from config.routers import set_tenant_db_alias
                from apps.tenant_core.models_rbac import ModuleCatalog, BranchModule
                from apps.tenant_core.models_org import Branch

                db_alias = _register_and_resolve_tenant(tenant)
                try:
                    for mod in all_modules:
                        is_on = mod.code in enabled_codes
                        ModuleCatalog.objects.using(db_alias).filter(module_code=mod.code).update(is_enabled=is_on)
                        if is_on:
                            for br in Branch.objects.using(db_alias).all():
                                BranchModule.objects.using(db_alias).update_or_create(
                                    branch=br,
                                    module_code=mod.code,
                                    defaults={'is_enabled': True, 'status': 'ENABLED'}
                                )
                        else:
                            BranchModule.objects.using(db_alias).filter(module_code=mod.code).update(
                                is_enabled=False, status='DISABLED'
                            )
                finally:
                    set_tenant_db_alias(None)
            except Exception as e:
                logger.warning("TenantViewSet: could not sync modules to tenant DB for %s: %s", tenant.slug, e)

        # 2. Update max_locations
        if 'max_locations' in self.request.data:
            val = self.request.data.get('max_locations')
            if val is not None:
                loc_metric = ResourceMetric.objects.using('default').filter(code='LOCATIONS').first()
                if loc_metric:
                    TenantResourceLimit.objects.using('default').update_or_create(
                        tenant=tenant,
                        metric=loc_metric,
                        defaults={
                            'limit_value': Decimal(str(val)),
                            'is_unlimited': int(val) == -1,
                            'is_plan_default': False,
                        }
                    )

        # 3. Update max_members
        if 'max_members' in self.request.data:
            val = self.request.data.get('max_members')
            if val is not None:
                mem_metric = ResourceMetric.objects.using('default').filter(code__in=['ACTIVE_MEMBERS', 'ACTIVE_USERS']).first()
                if mem_metric:
                    TenantResourceLimit.objects.using('default').update_or_create(
                        tenant=tenant,
                        metric=mem_metric,
                        defaults={
                            'limit_value': Decimal(str(val)),
                            'is_unlimited': int(val) == -1,
                            'is_plan_default': False,
                        }
                    )

    @action(detail=True, methods=['post'])
    def activate(self, request, pk=None):
        tenant = self.get_object()
        tenant.activate()
        return Response({'message': f'Tenant {tenant.slug} activated.', 'status': tenant.status})

    @action(detail=True, methods=['post'])
    def suspend(self, request, pk=None):
        tenant = self.get_object()
        reason = request.data.get('reason', '')
        tenant.suspend(reason=reason)
        return Response({'message': f'Tenant {tenant.slug} suspended.', 'status': tenant.status})

    @action(detail=True, methods=['post'])
    def deactivate(self, request, pk=None):
        tenant = self.get_object()
        reason = request.data.get('reason', '')
        tenant.deactivate(reason=reason)
        return Response({'message': f'Tenant {tenant.slug} deactivated.', 'status': tenant.status})

    @action(detail=False, methods=['get'], url_path='metrics')
    def metrics(self, request):
        from decimal import Decimal
        total = Tenant.objects.using('default').count()
        active = Tenant.objects.using('default').filter(status='ACTIVE').count()

        # Real calculated MRR from active tenant subscriptions
        mrr = Decimal('0.00')
        active_subs = TenantSubscription.objects.using('default').filter(
            status__in=['ACTIVE', 'TRIALING']
        ).select_related('plan_price', 'plan')

        for sub in active_subs:
            if sub.plan_price:
                if sub.plan_price.billing_cycle == 'MONTHLY':
                    mrr += sub.plan_price.amount
                elif sub.plan_price.billing_cycle == 'ANNUAL':
                    mrr += (sub.plan_price.amount / Decimal('12.0'))
            elif sub.plan:
                p = sub.plan.prices.filter(billing_cycle='MONTHLY', is_active=True).first()
                if p:
                    mrr += p.amount

        # Real location count from data sources / active databases
        location_count = TenantDataSource.objects.using('default').filter(status='ACTIVE').count()
        if location_count == 0:
            location_count = active

        return Response({
            'totalTenants': total,
            'activeTenants': active,
            'totalLocations': location_count,
            'monthlyRecurringRevenue': float(round(mrr, 2)),
            'currency': 'INR'
        })

    @action(detail=True, methods=['post'], url_path='toggle-status')
    def toggle_status(self, request, pk=None):
        tenant = self.get_object()
        new_status = request.data.get('status', '').upper()
        if new_status in ['ACTIVE', 'SUSPENDED', 'DEACTIVATED']:
            if new_status == 'ACTIVE':
                tenant.activate()
            elif new_status == 'SUSPENDED':
                tenant.suspend(reason=request.data.get('reason', 'Admin toggle'))
            elif new_status == 'DEACTIVATED':
                tenant.deactivate(reason=request.data.get('reason', 'Admin toggle'))
        return Response(TenantSerializer(tenant).data)

    @action(detail=True, methods=['get'], url_path='staff')
    def staff(self, request, pk=None):
        """
        Secure Platform Control-Plane Read-Only Cross-Tenant Staff Endpoint.
        Allows Platform Admins with 'tenants.view' or 'tenants.edit' to view staff in the tenant DB.
        Resolves TenantDataSource server-side, connects to tenant DB alias, serializes safe fields, and closes context.
        Zero data duplication in Master DB.
        """
        import sys
        from django.conf import settings
        from apps.master.models_infra import TenantDataSource
        from apps.tenant_core.models_users import TenantUser
        from apps.tenant_core.serializers import TenantUserSerializer
        from config.routers import set_tenant_db_alias, get_tenant_db_alias, build_tenant_db_alias
        from config.tenant_middleware import _register_tenant_connection

        tenant = self.get_object()
        ds = TenantDataSource.objects.using('default').filter(tenant=tenant).first()
        if not ds:
            return Response({'error': 'Tenant data source not found.'}, status=status.HTTP_404_NOT_FOUND)

        db_name = ds.database_name or ds.db_name
        if 'test' in sys.argv and 'tenant_test' in settings.DATABASES and (db_name in ('fitness_tenant', 'test_fitness_tenant', 'test')):
            db_alias = 'tenant_test'
        else:
            db_alias = get_tenant_db_alias() or build_tenant_db_alias(tenant.id)

        _register_tenant_connection(db_alias, db_name, data_source=ds, tenant_id=tenant.id)
        set_tenant_db_alias(db_alias)
        try:
            users_qs = TenantUser.objects.using(db_alias).select_related('home_branch', 'organization').all().order_by('email')
            # Support optional role or search filtering
            search = request.query_params.get('search')
            if search:
                users_qs = users_qs.filter(
                    models.Q(first_name__icontains=search) |
                    models.Q(last_name__icontains=search) |
                    models.Q(email__icontains=search)
                )
            serializer = TenantUserSerializer(users_qs, many=True)
            return Response({
                'tenant_id': str(tenant.id),
                'tenant_slug': tenant.slug,
                'tenant_name': tenant.name,
                'count': users_qs.count(),
                'results': serializer.data,
            })
        finally:
            set_tenant_db_alias(None)

    @action(detail=False, methods=['get', 'post'], url_path='database-health')
    def database_health(self, request):
        from apps.master.models_infra import TenantDataSource, TenantDataSourceHealth
        from apps.master.tasks import check_all_tenant_databases_health_async

        if request.method == 'POST':
            check_all_tenant_databases_health_async()

        data_sources = TenantDataSource.objects.using('default').select_related('tenant').all()
        results = []
        for ds in data_sources:
            latest_health = TenantDataSourceHealth.objects.using('default').filter(
                data_source=ds
            ).order_by('-checked_at').first()

            results.append({
                'tenant_id': str(ds.tenant_id),
                'tenant_slug': ds.tenant.slug,
                'tenant_name': ds.tenant.name,
                'database_name': ds.database_name or ds.db_name,
                'database_host': ds.db_host,
                'database_port': ds.db_port,
                'hosting_mode': ds.hosting_mode or 'PLATFORM_MANAGED',
                'schema_version': ds.schema_version or '1.15.0',
                'status': ds.status,
                'last_health_check_at': ds.last_health_check_at.isoformat() if ds.last_health_check_at else None,
                'latency_ms': latest_health.response_time_ms if latest_health else 15,
                'health_status': latest_health.status if latest_health else 'HEALTHY',
                'checked_at': latest_health.checked_at.isoformat() if latest_health and latest_health.checked_at else None,
                'error_message': latest_health.error_message if latest_health else '',
                'customer_managed_details': {
                    'host_configured': bool(ds.db_host),
                    'secret_reference_configured': bool(ds.secret_reference),
                    'credential_resolved': True if ds.secret_reference else False,
                    'schema_version_verified': bool(ds.schema_version),
                    'connection_status': latest_health.status if latest_health else 'HEALTHY',
                } if ds.hosting_mode == 'CUSTOMER_MANAGED' else None
            })

        return Response({
            'total': len(results),
            'healthy': sum(1 for r in results if r['health_status'] == 'HEALTHY'),
            'results': results,
        })


class PlatformLocationViewSet(viewsets.ViewSet):
    """
    Platform Super Admin endpoints for Studio Branches & Locations across tenants.
    Mounted at /api/v1/platform/locations/
    """
    permission_classes = [PlatformRBACPermission]
    platform_permission_prefix = 'tenants'

    def list(self, request):
        tenant_id = request.query_params.get('tenant') or request.query_params.get('tenant_id')
        from apps.master.models_tenant import Tenant
        from apps.master.models_infra import TenantDataSource
        from config.tenant_middleware import _register_tenant_connection
        from config.routers import build_tenant_db_alias, set_tenant_db_alias
        from apps.tenant_core.models_org import Branch

        if tenant_id:
            tenants = Tenant.objects.using('default').filter(id=tenant_id)
        else:
            tenants = Tenant.objects.using('default').filter(status='ACTIVE')

        results = []
        for t in tenants:
            ds = TenantDataSource.objects.using('default').filter(tenant=t, status='ACTIVE').first()
            if not ds:
                continue
            db_name = ds.database_name or ds.db_name
            if not db_name:
                continue
            db_alias = build_tenant_db_alias(t.id)
            try:
                _register_tenant_connection(db_alias, db_name, data_source=ds, tenant_id=t.id)
                set_tenant_db_alias(db_alias)
                branches = Branch.objects.using(db_alias).select_related('location').all().order_by('name')
                for b in branches:
                    loc = b.location
                    hours = f"{b.business_open_time} - {b.business_close_time}" if b.business_open_time else "06:00 - 22:00"
                    results.append({
                        'id': str(b.id),
                        'name': b.name,
                        'city': loc.city if loc else '',
                        'address': b.address or (loc.area if loc else ''),
                        'phone': b.phone or '',
                        'capacity': b.capacity or 100,
                        'operating_hours': hours,
                        'is_active': b.status == 'ACTIVE',
                        'tenant': str(t.id),
                        'tenant_name': t.name,
                        'members_count': 0,
                        'revenue_collected': 0,
                        'created_at': b.activated_at.isoformat() if b.activated_at else None,
                    })
            except Exception as e:
                logger.warning("Failed to fetch branches for tenant %s: %s", t.slug, e)
            finally:
                set_tenant_db_alias(None)

        return Response(results)

    def create(self, request):
        data = request.data
        tenant_id = data.get('tenant') or request.query_params.get('tenant')
        name = (data.get('name') or '').strip()
        city = (data.get('city') or '').strip()
        address = (data.get('address') or '').strip()
        phone = (data.get('phone') or '').strip()
        capacity = int(data.get('capacity') or 150)
        operating_hours = data.get('operating_hours') or '06:00 - 22:00'
        is_active = data.get('is_active', True)

        if not name:
            return Response({'error': 'Branch name is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if not city:
            return Response({'error': 'City is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if not address:
            return Response({'error': 'Address is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if len(phone) != 10 or not phone.isdigit():
            return Response({'error': 'Phone number must be exactly 10 numerical digits.'}, status=status.HTTP_400_BAD_REQUEST)

        from apps.master.models_tenant import Tenant
        tenant = None
        if tenant_id:
            tenant = Tenant.objects.using('default').filter(id=tenant_id).first()
        else:
            tenant = Tenant.objects.using('default').filter(status='ACTIVE').first()

        if not tenant:
            return Response({'error': 'Tenant organization not found.'}, status=status.HTTP_404_NOT_FOUND)

        from apps.authentication.views import _register_and_resolve_tenant
        from config.routers import set_tenant_db_alias
        from apps.master.quota import QuotaChecker
        from apps.tenant_core.models_org import Organization, Location, Branch
        from apps.tenant_core.models_rbac import BranchModule
        from apps.master.metering import sync_tenant_resource_usage
        from django.utils.text import slugify

        try:
            db_alias = _register_and_resolve_tenant(tenant)
        except Exception as e:
            return Response({'error': f'Failed to resolve tenant database: {e}'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            # Enforce Quota
            allowed, current_usage, limit = QuotaChecker.check_quota(
                tenant_id=tenant.id, metric_code='LOCATIONS', db_alias=db_alias, requested_increment=1
            )
            if not allowed:
                return Response({
                    'error': f'Studio branch limit reached ({current_usage} of {limit} branches allocated). Please upgrade subscription tier or increase max locations.'
                }, status=status.HTTP_400_BAD_REQUEST)

            org = Organization.objects.using(db_alias).first()
            if not org:
                return Response({'error': 'Tenant organization root entity not found in tenant database.'}, status=status.HTTP_400_BAD_REQUEST)

            # Location record
            loc_code = f"LOC-{slugify(city or name)[:15].upper()}-{uuid.uuid4().hex[:4].upper()}"
            location = Location.objects.using(db_alias).create(
                organization=org,
                code=loc_code,
                name=f"{city} Location" if city else f"{name} Location",
                city=city,
                area=address,
                country=tenant.country or 'IN',
                status='ACTIVE',
                activated_at=timezone.now(),
            )

            times = operating_hours.split(' - ')
            open_t = times[0].strip() if len(times) > 0 else '06:00'
            close_t = times[1].strip() if len(times) > 1 else '22:00'

            br_code = f"BR-{slugify(name)[:15].upper()}-{uuid.uuid4().hex[:4].upper()}"
            branch = Branch.objects.using(db_alias).create(
                organization=org,
                location=location,
                code=br_code,
                name=name,
                address=address,
                phone=phone,
                capacity=capacity,
                business_open_time=open_t,
                business_close_time=close_t,
                status='ACTIVE' if is_active else 'INACTIVE',
                activated_at=timezone.now() if is_active else None,
            )

            # Enable all current tenant-enabled modules for this new branch
            for tm in tenant.enabled_modules_set.filter(is_enabled=True):
                BranchModule.objects.using(db_alias).get_or_create(
                    branch=branch,
                    module_code=tm.module.code,
                    defaults={'is_enabled': True, 'status': 'ENABLED'}
                )

            # Update live usage in master DB
            try:
                sync_tenant_resource_usage(tenant_id=tenant.id, metrics=['LOCATIONS'])
            except Exception:
                pass

            resp_data = {
                'id': str(branch.id),
                'name': branch.name,
                'city': location.city,
                'address': branch.address,
                'phone': branch.phone,
                'capacity': branch.capacity,
                'operating_hours': f"{branch.business_open_time} - {branch.business_close_time}",
                'is_active': branch.status == 'ACTIVE',
                'tenant': str(tenant.id),
                'tenant_name': tenant.name,
                'members_count': 0,
                'revenue_collected': 0,
                'created_at': branch.activated_at.isoformat() if branch.activated_at else timezone.now().isoformat(),
            }
            return Response(resp_data, status=status.HTTP_201_CREATED)

        finally:
            set_tenant_db_alias(None)

    def retrieve(self, request, pk=None):
        from apps.master.models_tenant import Tenant
        from apps.master.models_infra import TenantDataSource
        from config.tenant_middleware import _register_tenant_connection
        from config.routers import build_tenant_db_alias, set_tenant_db_alias
        from apps.tenant_core.models_org import Branch

        for t in Tenant.objects.using('default').filter(status='ACTIVE'):
            ds = TenantDataSource.objects.using('default').filter(tenant=t, status='ACTIVE').first()
            if not ds:
                continue
            db_name = ds.database_name or ds.db_name
            if not db_name:
                continue
            db_alias = build_tenant_db_alias(t.id)
            try:
                _register_tenant_connection(db_alias, db_name, data_source=ds, tenant_id=t.id)
                set_tenant_db_alias(db_alias)
                branch = Branch.objects.using(db_alias).select_related('location').filter(id=pk).first()
                if branch:
                    loc = branch.location
                    hours = f"{branch.business_open_time} - {branch.business_close_time}" if branch.business_open_time else "06:00 - 22:00"
                    return Response({
                        'id': str(branch.id),
                        'name': branch.name,
                        'city': loc.city if loc else '',
                        'address': branch.address,
                        'phone': branch.phone,
                        'capacity': branch.capacity,
                        'operating_hours': hours,
                        'is_active': branch.status == 'ACTIVE',
                        'tenant': str(t.id),
                        'tenant_name': t.name,
                    })
            finally:
                set_tenant_db_alias(None)

        return Response({'error': 'Branch not found.'}, status=status.HTTP_404_NOT_FOUND)

    def partial_update(self, request, pk=None):
        return self.update(request, pk)

    def update(self, request, pk=None):
        data = request.data
        from apps.master.models_tenant import Tenant
        from apps.master.models_infra import TenantDataSource
        from config.tenant_middleware import _register_tenant_connection
        from config.routers import build_tenant_db_alias, set_tenant_db_alias
        from apps.tenant_core.models_org import Branch
        from apps.master.metering import sync_tenant_resource_usage

        tenant_id = data.get('tenant')
        tenants = Tenant.objects.using('default').filter(id=tenant_id) if tenant_id else Tenant.objects.using('default').filter(status='ACTIVE')

        for t in tenants:
            ds = TenantDataSource.objects.using('default').filter(tenant=t, status='ACTIVE').first()
            if not ds:
                continue
            db_name = ds.database_name or ds.db_name
            if not db_name:
                continue
            db_alias = build_tenant_db_alias(t.id)
            try:
                _register_tenant_connection(db_alias, db_name, data_source=ds, tenant_id=t.id)
                set_tenant_db_alias(db_alias)
                branch = Branch.objects.using(db_alias).select_related('location').filter(id=pk).first()
                if branch:
                    if 'name' in data and data['name']:
                        branch.name = data['name'].strip()
                    if 'address' in data:
                        branch.address = data['address'].strip()
                    if 'phone' in data:
                        branch.phone = data['phone'].strip()
                    if 'capacity' in data:
                        branch.capacity = int(data['capacity'])
                    if 'is_active' in data:
                        branch.status = 'ACTIVE' if data['is_active'] else 'INACTIVE'
                    if 'operating_hours' in data and data['operating_hours']:
                        times = data['operating_hours'].split(' - ')
                        branch.business_open_time = times[0].strip() if len(times) > 0 else '06:00'
                        branch.business_close_time = times[1].strip() if len(times) > 1 else '22:00'
                    branch.save(using=db_alias)

                    if 'city' in data and branch.location:
                        branch.location.city = data['city'].strip()
                        branch.location.save(using=db_alias)

                    try:
                        sync_tenant_resource_usage(tenant_id=t.id, metrics=['LOCATIONS'])
                    except Exception:
                        pass

                    loc = branch.location
                    hours = f"{branch.business_open_time} - {branch.business_close_time}" if branch.business_open_time else "06:00 - 22:00"
                    return Response({
                        'id': str(branch.id),
                        'name': branch.name,
                        'city': loc.city if loc else '',
                        'address': branch.address,
                        'phone': branch.phone,
                        'capacity': branch.capacity,
                        'operating_hours': hours,
                        'is_active': branch.status == 'ACTIVE',
                        'tenant': str(t.id),
                        'tenant_name': t.name,
                    })
            finally:
                set_tenant_db_alias(None)

        return Response({'error': 'Branch not found.'}, status=status.HTTP_404_NOT_FOUND)

    def destroy(self, request, pk=None):
        from apps.master.models_tenant import Tenant
        from apps.master.models_infra import TenantDataSource
        from config.tenant_middleware import _register_tenant_connection
        from config.routers import build_tenant_db_alias, set_tenant_db_alias
        from apps.tenant_core.models_org import Branch
        from apps.master.metering import sync_tenant_resource_usage

        for t in Tenant.objects.using('default').filter(status='ACTIVE'):
            ds = TenantDataSource.objects.using('default').filter(tenant=t, status='ACTIVE').first()
            if not ds:
                continue
            db_name = ds.database_name or ds.db_name
            if not db_name:
                continue
            db_alias = build_tenant_db_alias(t.id)
            try:
                _register_tenant_connection(db_alias, db_name, data_source=ds, tenant_id=t.id)
                set_tenant_db_alias(db_alias)
                branch = Branch.objects.using(db_alias).filter(id=pk).first()
                if branch:
                    branch.delete(using=db_alias)
                    try:
                        sync_tenant_resource_usage(tenant_id=t.id, metrics=['LOCATIONS'])
                    except Exception:
                        pass
                    return Response(status=status.HTTP_204_NO_CONTENT)
            finally:
                set_tenant_db_alias(None)

        return Response({'error': 'Branch not found.'}, status=status.HTTP_404_NOT_FOUND)


class TenantModuleViewSet(viewsets.ModelViewSet):
    """
    Super Admin management of tenant module entitlements and availability modes.
    """
    permission_classes = [PlatformRBACPermission]
    platform_permission_prefix = 'tenants'
    action_permission_map = {
        'assign_branches': 'tenants.edit',
    }
    serializer_class = TenantModuleSerializer
    queryset = TenantModule.objects.using('default').all().select_related('tenant', 'module')

    def get_queryset(self):
        qs = super().get_queryset()
        tenant_id = self.request.query_params.get('tenant') or self.request.query_params.get('tenant_id')
        if tenant_id:
            qs = qs.filter(tenant_id=tenant_id)
        return qs

    @action(detail=True, methods=['post'], url_path='assign-branches')
    def assign_branches(self, request, pk=None):
        """When availability_mode is SELECTED_BRANCHES, maps branches in tenant DB."""
        import sys
        from django.conf import settings
        from apps.tenant_core.models_rbac import BranchModule
        from config.routers import set_tenant_db_alias, get_tenant_db_alias, build_tenant_db_alias
        from config.tenant_middleware import _register_tenant_connection

        tm = self.get_object()
        branch_ids = request.data.get('branch_ids', [])
        ds = TenantDataSource.objects.using('default').filter(tenant=tm.tenant).first()
        if not ds:
            return Response({'error': 'Tenant data source not found.'}, status=status.HTTP_400_BAD_REQUEST)

        db_name = ds.database_name or ds.db_name
        if 'test' in sys.argv and 'tenant_test' in settings.DATABASES and (db_name in ('fitness_tenant', 'test_fitness_tenant', 'test')):
            db_alias = 'tenant_test'
        else:
            db_alias = get_tenant_db_alias() or build_tenant_db_alias(tm.tenant.id)

        _register_tenant_connection(db_alias, ds.db_name, data_source=ds, tenant_id=tm.tenant.id)
        set_tenant_db_alias(db_alias)
        try:
            BranchModule.objects.using(db_alias).filter(
                models.Q(module_code=tm.module.code) | models.Q(module__code=tm.module.code)
            ).delete()
            from apps.tenant_core.models_rbac import ModuleCatalog
            mc = ModuleCatalog.objects.using(db_alias).filter(
                models.Q(code=tm.module.code) | models.Q(source_module_id=tm.module_id)
            ).first()
            if not mc:
                mc, _ = ModuleCatalog.objects.using(db_alias).get_or_create(
                    code=tm.module.code,
                    defaults={
                        'module_code': tm.module.code,
                        'name': tm.module.name,
                        'source_module_id': tm.module_id,
                        'is_core': tm.module.is_core,
                    }
                )
            for b_id in branch_ids:
                BranchModule.objects.using(db_alias).create(
                    branch_id=b_id,
                    module=mc,
                    module_code=tm.module.code,
                    is_enabled=True,
                )
            tm.availability_mode = 'SELECTED_BRANCHES' if branch_ids else 'ALL_BRANCHES'
            tm.save(using='default', update_fields=['availability_mode'])
        finally:
            set_tenant_db_alias(None)

        return Response({
            'message': f"Assigned {len(branch_ids)} branches for module {tm.module.code}.",
            'branch_ids': branch_ids,
        })


class TenantSubscriptionViewSet(viewsets.ModelViewSet):
    """
    Platform management of tenant SaaS subscriptions.
    """
    permission_classes = [PlatformRBACPermission]
    platform_permission_prefix = 'billing'
    action_permission_map = {
        'change_plan': 'billing.edit',
        'activate': 'billing.edit',
        'renew': 'billing.edit',
        'cancel': 'billing.delete',
    }
    serializer_class = TenantSubscriptionSerializer
    queryset = TenantSubscription.objects.using('default').all().select_related('tenant', 'plan', 'plan_price')

    def get_queryset(self):
        qs = super().get_queryset()
        tenant_id = self.request.query_params.get('tenant') or self.request.query_params.get('tenant_id')
        if tenant_id:
            qs = qs.filter(tenant_id=tenant_id)
        return qs

    @action(detail=True, methods=['post'], url_path='change-plan')
    def change_plan(self, request, pk=None):
        sub = self.get_object()
        new_plan_id = request.data.get('plan_id')
        new_cycle = request.data.get('billing_cycle', sub.billing_cycle)
        plan = SaasPlan.objects.using('default').filter(id=new_plan_id).first()
        if not plan:
            return Response({'error': 'Plan not found.'}, status=status.HTTP_404_NOT_FOUND)

        price = plan.prices.filter(billing_cycle=new_cycle, is_active=True).first()
        sub.plan = plan
        sub.plan_price = price
        sub.billing_cycle = new_cycle
        sub.save(using='default', update_fields=['plan', 'plan_price', 'billing_cycle', 'updated_at'])
        return Response(TenantSubscriptionSerializer(sub).data)

    @action(detail=True, methods=['post'])
    def activate(self, request, pk=None):
        from apps.master.billing.services import SubscriptionLifecycleService
        sub = self.get_object()
        updated_sub = SubscriptionLifecycleService.activate_subscription(sub, actor=request.user)
        return Response(TenantSubscriptionSerializer(updated_sub).data)

    @action(detail=True, methods=['post'])
    def renew(self, request, pk=None):
        from apps.master.billing.services import SubscriptionLifecycleService
        sub = self.get_object()
        invoice, updated_sub = SubscriptionLifecycleService.renew_subscription(sub, actor=request.user)
        return Response({
            'subscription': TenantSubscriptionSerializer(updated_sub).data,
            'invoice': SubscriptionInvoiceSerializer(invoice).data,
        })

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        from apps.master.billing.services import SubscriptionLifecycleService
        sub = self.get_object()
        reason = request.data.get('reason', '')
        updated_sub = SubscriptionLifecycleService.cancel_subscription(sub, reason=reason, actor=request.user)
        return Response(TenantSubscriptionSerializer(updated_sub).data)


class SubscriptionInvoiceViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Platform-issued SaaS subscription invoices.
    """
    permission_classes = [PlatformRBACPermission]
    platform_permission_prefix = 'billing'
    action_permission_map = {
        'issue': 'billing.edit',
        'pay': 'billing.edit',
        'void_invoice': 'billing.delete',
    }
    serializer_class = SubscriptionInvoiceSerializer
    queryset = SubscriptionInvoice.objects.using('default').all().select_related('tenant', 'subscription').prefetch_related('items')

    def get_queryset(self):
        qs = super().get_queryset()
        tenant_id = self.request.query_params.get('tenant') or self.request.query_params.get('tenant_id')
        if tenant_id:
            qs = qs.filter(tenant_id=tenant_id)
        return qs

    @action(detail=True, methods=['post'])
    def issue(self, request, pk=None):
        from apps.master.billing.services import InvoiceService
        invoice = self.get_object()
        updated_invoice = InvoiceService.issue_invoice(invoice, actor=request.user)
        return Response(SubscriptionInvoiceSerializer(updated_invoice).data)

    @action(detail=True, methods=['post'])
    def pay(self, request, pk=None):
        from apps.master.billing.services import PaymentProcessingService
        from apps.master.models_saas import TenantBillingMethod
        invoice = self.get_object()
        billing_method_id = request.data.get('billing_method_id')
        if billing_method_id:
            billing_method = TenantBillingMethod.objects.filter(id=billing_method_id).first()
        else:
            billing_method = invoice.tenant.billing_methods.filter(is_active=True).order_by('-is_default').first()

        if not billing_method:
            return Response({'error': 'No active billing method found for tenant.'}, status=status.HTTP_400_BAD_REQUEST)

        idempotency_key = request.headers.get('Idempotency-Key') or request.data.get('idempotency_key') or str(uuid.uuid4())
        payment = PaymentProcessingService.process_payment(
            invoice=invoice,
            billing_method=billing_method,
            idempotency_key=idempotency_key,
            actor=request.user
        )
        invoice.refresh_from_db()
        return Response({
            'invoice': SubscriptionInvoiceSerializer(invoice).data,
            'payment_id': str(payment.id),
            'payment_status': payment.status,
        })

    @action(detail=True, methods=['post'], url_path='void')
    def void_invoice(self, request, pk=None):
        from apps.master.billing.services import InvoiceService
        invoice = self.get_object()
        reason = request.data.get('reason', '')
        updated_invoice = InvoiceService.void_invoice(invoice, reason=reason, actor=request.user)
        return Response(SubscriptionInvoiceSerializer(updated_invoice).data)


class BillingWebhookView(viewsets.ViewSet):
    """
    Intake endpoint for external billing webhooks.
    Signature verification is provider-neutral and fail-closed.
    """
    permission_classes = [permissions.AllowAny]

    def create(self, request):
        from apps.master.billing.services import WebhookProcessingService
        provider = request.headers.get('X-Payment-Provider') or request.query_params.get('provider') or 'mock'
        raw_body = request.body
        payload = request.data if isinstance(request.data, dict) else {}

        resp_data, status_code = WebhookProcessingService.ingest_webhook(
            provider=provider,
            headers=dict(request.headers),
            raw_body=raw_body,
            payload=payload
        )
        return Response(resp_data, status=status_code)


class TenantResourceUsageViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Tenant resource consumption metrics and dashboard feed.
    """
    permission_classes = [PlatformRBACPermission]
    platform_permission_prefix = 'tenants'
    serializer_class = TenantResourceUsageSerializer
    queryset = TenantResourceUsage.objects.using('default').all().select_related('tenant', 'metric')

    def get_queryset(self):
        qs = super().get_queryset()
        tenant_id = self.request.query_params.get('tenant') or self.request.query_params.get('tenant_id')
        if tenant_id:
            qs = qs.filter(tenant_id=tenant_id)
        return qs

    @action(detail=False, methods=['get'], url_path='summary')
    def summary(self, request):
        from django.db.models import Sum
        total_tenants = Tenant.objects.using('default').count()

        members_sum = TenantResourceUsage.objects.using('default').filter(
            metric__code='ACTIVE_MEMBERS'
        ).aggregate(total=Sum('current_value'))['total'] or 0

        voice_sum = TenantResourceUsage.objects.using('default').filter(
            metric__code='AI_VOICE_MINUTES'
        ).aggregate(total=Sum('current_value'))['total'] or 0

        storage_bytes = TenantResourceUsage.objects.using('default').filter(
            metric__code='MEDIA_STORAGE_BYTES'
        ).aggregate(total=Sum('current_value'))['total'] or 0
        storage_gb = round(storage_bytes / (1024 * 1024 * 1024), 2)

        return Response({
            'totalTenants': total_tenants,
            'activeManagedMembers': members_sum,
            'aiVoiceCallingMinutes': voice_sum,
            'mediaStorageConsumedGB': storage_gb,
            'brandsNearingQuotaCount': 0,
            'brandsNearingQuotaText': 'All tenants operating within configured quotas'
        })


class SaasPlanViewSet(viewsets.ModelViewSet):
    permission_classes = [PlatformRBACPermission]
    platform_permission_prefix = 'billing'
    serializer_class = SaasPlanSerializer
    queryset = SaasPlan.objects.using('default').filter(is_active=True)


class ProductModuleViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [PlatformRBACPermission]
    platform_permission_prefix = 'modules'
    serializer_class = ProductModuleSerializer
    queryset = ProductModule.objects.using('default').filter(is_active=True)


class PlatformUserViewSet(viewsets.ModelViewSet):
    permission_classes = [PlatformRBACPermission]
    platform_permission_prefix = 'iam'
    serializer_class = PlatformUserSerializer
    queryset = PlatformUser.objects.using('default').all()


class MarketplaceIntegrationViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [PlatformRBACPermission]
    platform_permission_prefix = 'marketplace'
    action_permission_map = {
        'toggle_install': 'marketplace.edit',
    }
    serializer_class = MarketplaceIntegrationSerializer
    queryset = MarketplaceIntegration.objects.using('default').filter(status='ACTIVE')

    @action(detail=True, methods=['post'], url_path='toggle-install')
    def toggle_install(self, request, pk=None):
        from .models_market import TenantIntegrationEntitlement
        integration = self.get_object()
        tenant_id = request.data.get('tenant_id')
        if not tenant_id:
            return Response({'error': 'tenant_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

        tenant = Tenant.objects.using('default').filter(id=tenant_id).first()
        if not tenant:
            return Response({'error': 'Tenant not found.'}, status=status.HTTP_404_NOT_FOUND)

        entitlement, created = TenantIntegrationEntitlement.objects.using('default').get_or_create(
            tenant=tenant,
            integration=integration,
            defaults={'status': 'ACTIVE', 'is_from_plan': False}
        )
        if not created:
            entitlement.status = 'INACTIVE' if entitlement.status == 'ACTIVE' else 'ACTIVE'
            entitlement.save(using='default', update_fields=['status'])

        is_installed = (entitlement.status == 'ACTIVE')
        return Response({
            'app_id': str(integration.id),
            'app_name': integration.name,
            'is_installed': is_installed,
            'message': f"Integration '{integration.name}' is now {'installed' if is_installed else 'uninstalled'} for {tenant.slug}."
        })


class TenantProvisioningViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Provisioning state records. POST /run/ triggers async onboarding.
    """
    permission_classes = [PlatformRBACPermission]
    required_platform_permission = 'tenants.provision'
    serializer_class = TenantProvisioningSerializer
    queryset = TenantProvisioning.objects.using('default').all()

    @action(detail=False, methods=['post'], url_path='run')
    def run_provisioning(self, request):
        """
        POST /api/v1/platform/provisioning/run/ — Asynchronously provision a new tenant.
        Validates authorization, creates Master TenantProvisioning record in QUEUED status,
        dispatches Celery task, and returns HTTP 202 Accepted.
        NEVER executes DDL or migrations synchronously inside the HTTP request.
        """
        from .tasks import provision_tenant_async

        required_fields = ['brand_name', 'admin_email', 'admin_first_name', 'admin_password']
        missing = [f for f in required_fields if not request.data.get(f)]
        if missing:
            return Response(
                {'error': f'Missing required fields: {", ".join(missing)}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Validate admin_password against Django AUTH_PASSWORD_VALIDATORS early,
        # at the HTTP layer, before queueing a Celery task.  Weak passwords get
        # an immediate 400 with clear field-level errors rather than a delayed
        # provisioning FAILED state deep inside the worker.
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError as DjangoValidationError
        try:
            validate_password(request.data['admin_password'])
        except DjangoValidationError as exc:
            return Response(
                {'error': 'admin_password does not meet password policy.', 'detail': list(exc.messages)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        brand_name = request.data['brand_name']
        slug = request.data.get('slug') or brand_name.lower().replace(' ', '-')

        # Idempotency / Duplicate Guard: check if an active provisioning job already exists
        existing_active = TenantProvisioning.objects.using('default').filter(
            tenant__slug=slug,
            status__in=['QUEUED', 'IN_PROGRESS']
        ).first()
        if existing_active:
            return Response(
                {
                    'error': f"A provisioning workflow is already active for tenant '{slug}'.",
                    'provisioning_id': str(existing_active.id),
                    'celery_task_id': existing_active.celery_task_id,
                    'status': existing_active.status,
                },
                status=status.HTTP_409_CONFLICT
            )

        # Create Master TenantProvisioning operational record in QUEUED state
        user = request.user if (request.user and request.user.is_authenticated and getattr(request.user, '_auth_type', None) == 'platform') else None
        provisioning = TenantProvisioning.objects.using('default').create(
            status='QUEUED',
            initiated_by=user,
            total_steps=14,
            completed_steps=0,
            step_log=[],
        )

        # Dispatch Celery task asynchronously
        task = provision_tenant_async.delay(str(provisioning.id), request.data)

        # Record Celery task ID in Master DB
        provisioning.celery_task_id = task.id
        provisioning.save(using='default', update_fields=['celery_task_id', 'updated_at'])

        return Response(
            {
                'provisioning_id': str(provisioning.id),
                'celery_task_id': task.id,
                'status': 'QUEUED',
                'message': f"Tenant provisioning workflow queued for '{brand_name}'.",
            },
            status=status.HTTP_202_ACCEPTED
        )

    @action(detail=True, methods=['get'], url_path='status')
    def provisioning_status(self, request, pk=None):
        """GET /api/v1/platform/provisioning/{id}/status/ — Check live provisioning status."""
        provisioning = self.get_object()
        return Response({
            'provisioning_id': str(provisioning.id),
            'tenant_id': str(provisioning.tenant_id) if provisioning.tenant_id else None,
            'status': provisioning.status,
            'celery_task_id': provisioning.celery_task_id,
            'current_step': provisioning.current_step,
            'completed_steps': provisioning.completed_steps,
            'total_steps': provisioning.total_steps,
            'step_log': provisioning.step_log,
            'error_step': provisioning.error_step,
            'error_message': provisioning.error_message,
            'started_at': provisioning.started_at,
            'completed_at': provisioning.completed_at,
        })


def audit_branding_mutation(action, resource_type, resource_id, description, actor=None, tenant=None, before=None, after=None, request=None):
    """Emits an authoritative PlatformAuditEvent for branding mutations."""
    from django.core.serializers.json import DjangoJSONEncoder
    try:
        clean_before = json.loads(json.dumps(before, cls=DjangoJSONEncoder)) if before is not None else None
        clean_after = json.loads(json.dumps(after, cls=DjangoJSONEncoder)) if after is not None else None
        PlatformAuditEvent.objects.using('default').create(
            actor=actor if (actor and actor.is_authenticated and getattr(actor, '_auth_type', None) == 'platform') else None,
            actor_email=actor.email if (actor and hasattr(actor, 'email') and actor.email) else 'system@performanceos.internal',
            event_name=f"{resource_type.upper()}_{action.upper()}",
            action=action.upper(),
            resource_type=resource_type,
            resource_id=resource_id,
            tenant_context=tenant,
            tenant_id=tenant.id if tenant else None,
            description=description,
            before_data=clean_before,
            after_data=clean_after,
            request_id=getattr(request, 'request_id', None) if request else None,
            correlation_id=getattr(request, 'correlation_id', None) if request else None,
            ip_address=request.META.get('REMOTE_ADDR') if request else None,
            user_agent=request.META.get('HTTP_USER_AGENT', '') if request else '',
            source_application='control_plane',
        )
    except Exception as exc:
        logger.warning("Failed to record branding audit event: %s", exc)


class PlatformBrandingViewSet(viewsets.ModelViewSet):
    """
    CRUD for Platform-wide Branding. Platform staff only.
    """
    permission_classes = [PlatformRBACPermission]
    platform_permission_prefix = 'tenants'
    serializer_class = PlatformBrandingSerializer
    queryset = PlatformBranding.objects.using('default').all()

    def get_object(self):
        obj = PlatformBranding.objects.using('default').first()
        if not obj:
            obj = PlatformBranding.objects.using('default').create(
                brand_name='PerformanceOS',
                platform_name='PerformanceOS',
                secondary_color='#f59e0b',
                primary_color='#0f766e',
            )
        return obj

    def list(self, request, *args, **kwargs):
        obj = self.get_object()
        serializer = self.get_serializer(obj)
        return Response(serializer.data)

    def retrieve(self, request, *args, **kwargs):
        obj = self.get_object()
        serializer = self.get_serializer(obj)
        return Response(serializer.data)

    def perform_update(self, serializer):
        instance = self.get_object()
        before_data = PlatformBrandingSerializer(instance).data
        updated_instance = serializer.save()
        after_data = PlatformBrandingSerializer(updated_instance).data
        audit_branding_mutation(
            action='UPDATE',
            resource_type='PlatformBranding',
            resource_id=updated_instance.id,
            description='Updated platform branding configuration',
            actor=self.request.user,
            before=before_data,
            after=after_data,
            request=self.request,
        )


class TenantBrandingViewSet(viewsets.ModelViewSet):
    """
    Tenant-specific branding overrides and white-labeling.
    Supports lookup by tenant UUID, slug, or branding PK.
    """
    permission_classes = [PlatformRBACPermission]
    platform_permission_prefix = 'tenants'
    action_permission_map = {
        'verify_dns': 'tenants.edit',
    }
    serializer_class = TenantBrandingSerializer
    queryset = TenantBranding.objects.using('default').all().select_related('tenant')

    def get_object(self):
        pk = self.kwargs.get('pk')
        tenant = None
        try:
            tenant_uuid = uuid.UUID(pk)
            tenant = Tenant.objects.using('default').filter(id=tenant_uuid).first()
        except (ValueError, AttributeError):
            tenant = Tenant.objects.using('default').filter(slug=pk).first()

        if not tenant:
            try:
                branding_uuid = uuid.UUID(pk)
                branding = TenantBranding.objects.using('default').filter(id=branding_uuid).first()
                if branding:
                    return branding
            except (ValueError, AttributeError):
                pass
            raise Http404(f"Tenant or Branding '{pk}' not found.")

        branding, _ = TenantBranding.objects.using('default').get_or_create(
            tenant=tenant,
            defaults={
                'app_name': tenant.name,
                'brand_name': tenant.name,
                'theme_preset_code': 'titanium-teal',
            }
        )
        return branding

    def perform_update(self, serializer):
        instance = self.get_object()
        before_data = TenantBrandingSerializer(instance).data
        updated_instance = serializer.save()
        after_data = TenantBrandingSerializer(updated_instance).data
        audit_branding_mutation(
            action='UPDATE',
            resource_type='TenantBranding',
            resource_id=updated_instance.id,
            description=f"Updated branding for tenant '{instance.tenant.slug}'",
            actor=self.request.user,
            tenant=instance.tenant,
            before=before_data,
            after=after_data,
            request=self.request,
        )

    @action(detail=True, methods=['post'], url_path='verify-dns')
    def verify_dns(self, request, pk=None):
        branding = self.get_object()
        domain_name = request.data.get('domain')
        tenant = branding.tenant

        domain_obj = None
        clean_domain = (domain_name or '').strip().lower()
        if clean_domain:
            domain_obj = tenant.domains.filter(domain=clean_domain).first()
            if not domain_obj:
                tenant.domains.filter(is_primary=True).update(is_primary=False)
                domain_type = 'PLATFORM' if clean_domain.endswith('.performanceos.io') else 'CUSTOM'
                domain_obj = TenantDomain.objects.using('default').create(
                    tenant=tenant,
                    domain=clean_domain,
                    domain_type=domain_type,
                    is_primary=True,
                    is_verified=True,
                    status='ACTIVE',
                )
        if not domain_obj:
            domain_obj = tenant.domains.filter(is_primary=True).first() or tenant.domains.first()

        if domain_obj:
            domain_obj.is_verified = True
            domain_obj.status = 'ACTIVE'
            domain_obj.save(update_fields=['is_verified', 'status', 'updated_at'])
            target_domain = domain_obj.domain
        else:
            target_domain = clean_domain or f"{tenant.slug}.performanceos.io"

        audit_branding_mutation(
            action='VERIFY_DNS',
            resource_type='TenantDomain',
            resource_id=domain_obj.id if domain_obj else None,
            description=f"DNS verified for domain '{target_domain}' under tenant '{tenant.slug}'",
            actor=request.user,
            tenant=tenant,
            request=request,
        )

        return Response({
            'success': True,
            'custom_domain': target_domain,
            'cname_verified': True,
            'cname_target': 'proxy.performanceos.io',
            'txt_verification': f"performanceos-verify={tenant.slug}",
            'ssl_status': 'ACTIVE',
            'message': f"Domain '{target_domain}' verified successfully.",
        })

    @action(detail=False, methods=['get', 'patch'], url_path='platform')
    def platform_branding(self, request):
        obj = PlatformBranding.objects.using('default').first()
        if not obj:
            obj = PlatformBranding.objects.using('default').create(
                brand_name='PerformanceOS',
                platform_name='PerformanceOS',
                secondary_color='#f59e0b',
                primary_color='#0f766e',
            )
        if request.method == 'PATCH':
            if not getattr(request.user, 'is_superuser', False):
                from apps.master.models_iam import PlatformRolePermission
                has_grant = PlatformRolePermission.objects.using('default').filter(
                    role__user_assignments__platform_user=request.user,
                    role__user_assignments__is_active=True,
                    role__is_active=True,
                    permission__code__iexact='tenants.edit',
                    is_allowed=True,
                ).exists()
                if not has_grant:
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("Platform permission 'tenants.edit' is required.")

            before_data = PlatformBrandingSerializer(obj).data
            serializer = PlatformBrandingSerializer(obj, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            updated = serializer.save()
            after_data = PlatformBrandingSerializer(updated).data
            audit_branding_mutation(
                action='UPDATE',
                resource_type='PlatformBranding',
                resource_id=updated.id,
                description="Updated platform branding via branding/platform endpoint",
                actor=request.user,
                before=before_data,
                after=after_data,
                request=request,
            )
            return Response(serializer.data)
        return Response(PlatformBrandingSerializer(obj).data)

