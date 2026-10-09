"""
URL configuration for PerformanceOS — Phase 1 Layer 1 Foundation.
"""

from django.contrib import admin
from django.urls import path, include
from django.views.generic.base import RedirectView
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularSwaggerView,
    SpectacularRedocView,
)
from apps.master.views import BillingWebhookView
from apps.tenant_core.views_security_policy import SecurityPolicyView
from apps.tenant_core.views_communication_webhook import CommunicationWebhookView
from apps.tenant_core.views_lead_webhook import PublicLeadCaptureView
from apps.tenant_core.views_razorpay_webhook import RazorpayWebhookView
from apps.tenant_core.views_meta_webhook import MetaLeadWebhookView
from apps.tenant_core.views_legal import privacy_policy_view, terms_of_service_view, data_deletion_view
from apps.tenant_core.views_health import HealthLivenessView, HealthReadinessView

# Django admin branding
admin.site.site_header = "PerformanceOS • Platform Control Panel"
admin.site.site_title = "PerformanceOS Admin"
admin.site.index_title = "Phase 1 Layer 1 — Master Control Panel"

urlpatterns = [
    path('', RedirectView.as_view(url='/admin/', permanent=False)),
    path('privacy-policy/', privacy_policy_view, name='privacy-policy'),
    path('terms-of-service/', terms_of_service_view, name='terms-of-service'),
    path('data-deletion/', data_deletion_view, name='data-deletion'),
    path('health/', HealthLivenessView.as_view(), name='platform-liveness'),
    path('ready/', HealthReadinessView.as_view(), name='platform-readiness'),
    path('admin/', admin.site.urls),

    # Auth — Platform login, Tenant login, Token refresh
    path('api/v1/auth/', include('apps.authentication.urls')),

    # Platform (Super Admin) — Tenant management, SaaS plans, provisioning, IAM
    path('api/v1/platform/', include('apps.master.urls')),
    path('api/v1/billing/webhook/', BillingWebhookView.as_view({'post': 'create'}), name='global-billing-webhook'),
    path('api/v1/webhooks/communications/<str:provider>/<str:public_integration_id>/', CommunicationWebhookView.as_view(), name='communication-provider-webhook'),
    path('api/v1/webhooks/leads/<str:tenant_public_id>/', PublicLeadCaptureView.as_view(), name='public-lead-capture-webhook'),
    path('api/v1/webhooks/meta/leads/', MetaLeadWebhookView.as_view(), name='meta-lead-webhook-global'),
    path('api/v1/webhooks/meta/leads/<str:tenant_public_id>/', MetaLeadWebhookView.as_view(), name='meta-lead-webhook-tenant'),
    path('api/v1/webhooks/razorpay/', RazorpayWebhookView.as_view(), name='razorpay-webhook-global'),
    path('api/v1/webhooks/razorpay/<str:tenant_slug>/', RazorpayWebhookView.as_view(), name='razorpay-webhook-tenant'),

    # Tenant Admin — Org/Branch/User/RBAC management (scoped to tenant DB)
    path('api/v1/admin/', include('apps.tenant_core.urls')),
    path('api/v1/tenant/', include(('apps.tenant_core.urls', 'tenant_core'), namespace='tenant_scoped')),
    path('api/v1/admin-config/security-policy/current/', SecurityPolicyView.as_view(), name='admin-config-security-policy'),

    # Mobile App API — Consumer / Member facing endpoints
    path('api/v1/mobile/', include('apps.tenant_core.urls_mobile')),

    # OpenAPI 3.1 Schema & Interactive Docs
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
]
