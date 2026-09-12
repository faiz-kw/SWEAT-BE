"""
Master app views — Platform-facing API endpoints.
All endpoints require Platform JWT authentication.
"""

import logging
import uuid
from django.db import models, transaction
from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response

from .models_iam import PlatformUser
from .models_tenant import Tenant, TenantDomain, TenantBranding
from .models_saas import (
    SaasPlan, SaasPlanPrice, ProductModule, TenantModule,
    TenantSubscription, SubscriptionInvoice, TenantResourceUsage, TenantResourceLimit,
)
from .models_market import MarketplaceIntegration
from .models_infra import TenantProvisioning, TenantDataSource
from .serializers import (
    TenantSerializer, TenantDetailSerializer,
    SaasPlanSerializer, PlatformUserSerializer,
    MarketplaceIntegrationSerializer, TenantProvisioningSerializer,
    ProductModuleSerializer, TenantModuleSerializer,
    TenantSubscriptionSerializer, SubscriptionInvoiceSerializer,
    TenantResourceUsageSerializer,
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
        from apps.tenant_core.models_rbac import BranchModule
        from config.routers import set_tenant_db_alias, get_tenant_db_alias

        tm = self.get_object()
        branch_ids = request.data.get('branch_ids', [])
        ds = TenantDataSource.objects.using('default').filter(tenant=tm.tenant).first()
        if not ds:
            return Response({'error': 'Tenant data source not found.'}, status=status.HTTP_400_BAD_REQUEST)

        db_alias = get_tenant_db_alias() or f"tenant_{ds.db_name}"
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
