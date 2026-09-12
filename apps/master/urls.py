from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    TenantViewSet, SaasPlanViewSet, PlatformUserViewSet,
    MarketplaceIntegrationViewSet, TenantProvisioningViewSet,
    ProductModuleViewSet, TenantModuleViewSet,
    TenantSubscriptionViewSet, SubscriptionInvoiceViewSet,
    TenantResourceUsageViewSet, BillingWebhookView,
)

router = DefaultRouter()
router.register(r'tenants', TenantViewSet, basename='tenant')
router.register(r'tenant-modules', TenantModuleViewSet, basename='tenant-module')
router.register(r'saas-plans', SaasPlanViewSet, basename='saas-plan')
router.register(r'plans', SaasPlanViewSet, basename='plan')
router.register(r'subscriptions', TenantSubscriptionViewSet, basename='subscription')
router.register(r'invoices', SubscriptionInvoiceViewSet, basename='invoice')
router.register(r'usage', TenantResourceUsageViewSet, basename='usage')
router.register(r'platform-users', PlatformUserViewSet, basename='platform-user')
router.register(r'marketplace', MarketplaceIntegrationViewSet, basename='marketplace')
router.register(r'provisioning', TenantProvisioningViewSet, basename='provisioning')
router.register(r'modules', ProductModuleViewSet, basename='module')
router.register(r'billing/webhook', BillingWebhookView, basename='billing-webhook')

app_name = 'master'

urlpatterns = [
    path('onboard/', TenantProvisioningViewSet.as_view({'post': 'run_provisioning'}), name='onboard'),
    path('billing/webhook/', BillingWebhookView.as_view({'post': 'create'}), name='billing-webhook-post'),
    path('', include(router.urls)),
]
