"""
URLs for Platform & Multi-Tenancy endpoints.
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    TenantViewSet, LocationViewSet, PlatformPlanViewSet,
    TenantBrandingViewSet, TenantUsageViewSet, TenantOnboardView,
    MarketplaceAppViewSet
)

router = DefaultRouter()
router.register(r'tenants', TenantViewSet, basename='tenant')
router.register(r'locations', LocationViewSet, basename='location')
router.register(r'plans', PlatformPlanViewSet, basename='platform-plan')
router.register(r'branding', TenantBrandingViewSet, basename='tenant-branding')
router.register(r'usage', TenantUsageViewSet, basename='tenant-usage')
router.register(r'marketplace', MarketplaceAppViewSet, basename='marketplace-app')

urlpatterns = [
    path('onboard/', TenantOnboardView.as_view(), name='tenant-onboard'),
    path('', include(router.urls)),
]

