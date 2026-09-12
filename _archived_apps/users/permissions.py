"""
Granular RBAC and Tenant Authorization Permission Classes for PerformanceOS.
"""

from rest_framework import permissions


class IsPlatformSuperAdmin(permissions.BasePermission):
    """
    Grants access only to Platform Super Admins.
    """
    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        return bool(user.is_superuser or (user.role and 'Super' in user.role) or user.tenant_id is None)


class IsTenantAdminOrSuperAdmin(permissions.BasePermission):
    """
    Grants access to Platform Super Admins or Tenant Admins / Owners within their tenant.
    """
    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if user.is_superuser or (user.role and 'Super' in user.role) or user.tenant_id is None:
            return True
        return user.role in ['Admin', 'Super Admin'] or (user.role_definition and user.role_definition.code in ['admin', 'tenant_owner', 'tenant_admin'])


class HasPermission(permissions.BasePermission):
    """
    Checks whether the requesting user has the required permission code.
    Usage:
        class LeadViewSet(viewsets.ModelViewSet):
            required_permission = 'crm.leads.view'
    """
    def __init__(self, perm_code=None):
        self.perm_code = perm_code

    def __call__(self):
        return self

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if user.is_platform_admin:
            return True

        required_perm = getattr(view, 'required_permission', self.perm_code)
        if not required_perm:
            return True

        return user.has_permission_code(required_perm)
