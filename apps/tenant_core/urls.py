from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    OrganizationViewSet, LocationViewSet, BranchViewSet, CompanyEntityViewSet,
    TenantUserViewSet, DepartmentViewSet, RoleViewSet, RoleAssignmentViewSet, UserBranchViewSet,
    ModuleCatalogViewSet, SubmoduleCatalogViewSet, PermissionViewSet,
    BranchModuleViewSet, RolePermissionSetViewSet,
    OrganizationSettingsViewSet, BranchSettingsViewSet,
    NotificationTemplateViewSet, TenantAuditEventViewSet,
    BranchWorkingHoursViewSet, BranchOperatingExceptionViewSet,
    VerifyAccessView,
)
from .views_storage import (
    FileViewSet,
    StorageConfirmUploadView,
    StoragePresignDownloadView,
    StoragePresignUploadView,
)

router = DefaultRouter()
router.register(r'organizations', OrganizationViewSet, basename='organization')
router.register(r'company-entities', CompanyEntityViewSet, basename='company-entity')
router.register(r'locations', LocationViewSet, basename='location')
router.register(r'branches', BranchViewSet, basename='branch')
router.register(r'users', TenantUserViewSet, basename='user')
router.register(r'user-branches', UserBranchViewSet, basename='user-branch')
router.register(r'departments', DepartmentViewSet, basename='department')
router.register(r'roles', RoleViewSet, basename='role')
router.register(r'role-assignments', RoleAssignmentViewSet, basename='role-assignment')
router.register(r'modules', ModuleCatalogViewSet, basename='module')
router.register(r'module-catalog', ModuleCatalogViewSet, basename='module-catalog')
router.register(r'submodules', SubmoduleCatalogViewSet, basename='submodule')
router.register(r'permissions', PermissionViewSet, basename='permission')
router.register(r'branch-modules', BranchModuleViewSet, basename='branch-module')
router.register(r'permission-sets', RolePermissionSetViewSet, basename='permission-set')
router.register(r'organization-settings', OrganizationSettingsViewSet, basename='organization-settings')
router.register(r'branch-settings', BranchSettingsViewSet, basename='branch-settings')
router.register(r'branch-working-hours', BranchWorkingHoursViewSet, basename='branch-working-hours')
router.register(r'branch-operating-exceptions', BranchOperatingExceptionViewSet, basename='branch-operating-exceptions')
router.register(r'notification-templates', NotificationTemplateViewSet, basename='notification-template')
router.register(r'audit-events', TenantAuditEventViewSet, basename='audit-event')
router.register(r'files', FileViewSet, basename='file')

app_name = 'tenant_core'

urlpatterns = [
    path('verify-access/', VerifyAccessView.as_view(), name='verify-access'),
    path('storage/presign-upload/', StoragePresignUploadView.as_view(), name='storage-presign-upload'),
    path('storage/confirm-upload/', StorageConfirmUploadView.as_view(), name='storage-confirm-upload'),
    path('storage/presign-download/<uuid:id>/', StoragePresignDownloadView.as_view(), name='storage-presign-download'),
    path('', include(router.urls)),
]

