"""
URLs for Administration endpoints.
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    ServiceViewSet, TenantSettingsViewSet, CustomFormViewSet,
    ApiKeyViewSet, AuditLogViewSet, SecurityPolicyViewSet
)

router = DefaultRouter()
router.register(r'services', ServiceViewSet, basename='service')
router.register(r'settings', TenantSettingsViewSet, basename='tenant-settings')
router.register(r'forms', CustomFormViewSet, basename='custom-form')
router.register(r'api-keys', ApiKeyViewSet, basename='api-key')
router.register(r'audit-logs', AuditLogViewSet, basename='audit-log')
router.register(r'security', SecurityPolicyViewSet, basename='security-policy')

urlpatterns = [
    path('', include(router.urls)),
]
