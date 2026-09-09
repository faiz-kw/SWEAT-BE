"""
URL configuration for PerformanceOS Admin Backend.
"""

from django.contrib import admin
from django.urls import path, include
from django.views.generic.base import RedirectView
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularSwaggerView,
    SpectacularRedocView,
)

# Custom Admin Site Branding
admin.site.site_header = "PerformanceOS • Enterprise Multi-Tenant Command Center"
admin.site.site_title = "PerformanceOS Admin"
admin.site.index_title = "Global Multi-Tenant Governance & Operational Modules"

urlpatterns = [
    path('', RedirectView.as_view(url='/admin/', permanent=False)),
    path('admin/', admin.site.urls),

    # Phase 0: Auth API Endpoints (/api/v1/auth/)
    path('api/v1/auth/', include('apps.authentication.urls')),

    # Phase 1: CRM & Sales API Endpoints (/api/v1/crm/)
    path('api/v1/crm/', include('apps.crm.urls')),

    # Phase 2: Members & Client 360 API Endpoints (/api/v1/members/)
    path('api/v1/members/', include('apps.members.urls')),

    # Phase 3: Operations & Scheduling API Endpoints (/api/v1/ops/)
    path('api/v1/ops/', include('apps.operations.urls')),

    # Finance & Billing API Endpoints (/api/v1/finance/)
    path('api/v1/finance/', include('apps.finance.urls')),

    # Master Integration Layer API Endpoints (/api/v1/integrations/)
    path('api/v1/integrations/', include('apps.integrations.urls')),

    # Phase 2: Coaching Layer API Endpoints (/api/v1/coaching/)
    path('api/v1/coaching/', include('apps.coaching.urls')),

    # Platform (Super Admin) API Endpoints (/api/v1/platform/)
    path('api/v1/platform/', include('apps.tenants.urls')),

    # Users & RBAC API Endpoints (/api/v1/users/)
    path('api/v1/users/', include('apps.users.urls')),

    # Administration & Configuration API Endpoints (/api/v1/admin-config/)
    path('api/v1/admin-config/', include('apps.administration.urls')),

    # OpenAPI 3.1 Schema & Interactive Documentation
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
]
