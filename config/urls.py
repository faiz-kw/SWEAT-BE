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

# Django admin branding
admin.site.site_header = "PerformanceOS • Platform Control Panel"
admin.site.site_title = "PerformanceOS Admin"
admin.site.index_title = "Phase 1 Layer 1 — Master Control Panel"

urlpatterns = [
    path('', RedirectView.as_view(url='/admin/', permanent=False)),
    path('admin/', admin.site.urls),

    # Auth — Platform login, Tenant login, Token refresh
    path('api/v1/auth/', include('apps.authentication.urls')),

    # Platform (Super Admin) — Tenant management, SaaS plans, provisioning, IAM
    path('api/v1/platform/', include('apps.master.urls')),
    path('api/v1/billing/webhook/', BillingWebhookView.as_view({'post': 'create'}), name='global-billing-webhook'),
    path('api/v1/webhooks/communications/<str:provider>/<str:public_integration_id>/', CommunicationWebhookView.as_view(), name='communication-provider-webhook'),
    path('api/v1/webhooks/leads/<str:tenant_public_id>/', PublicLeadCaptureView.as_view(), name='public-lead-capture-webhook'),

    # Tenant Admin — Org/Branch/User/RBAC management (scoped to tenant DB)
    path('api/v1/admin/', include('apps.tenant_core.urls')),
    path('api/v1/tenant/', include(('apps.tenant_core.urls', 'tenant_core'), namespace='tenant_scoped')),
    path('api/v1/admin-config/security-policy/current/', SecurityPolicyView.as_view(), name='admin-config-security-policy'),

    # OpenAPI 3.1 Schema & Interactive Docs
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
]
