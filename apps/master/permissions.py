"""
Platform IAM RBAC Permission Classes — Phase 1 Layer 1.
Enforces role-based action permissions for SaaS platform administration endpoints.

Queries Master DB records in real time:
  PlatformUser -> PlatformUserRole -> PlatformRole -> PlatformRolePermission -> PlatformPermission
Fails closed with HTTP 403 Forbidden.
"""

import logging
from rest_framework import permissions
from rest_framework.exceptions import PermissionDenied, AuthenticationFailed
from .models_iam import PlatformUserRole, PlatformRolePermission

logger = logging.getLogger(__name__)


class PlatformRBACPermission(permissions.BasePermission):
    """
    Evaluates platform action permissions from live Master DB records.
    Bypassed only for Platform superusers (is_superuser=True).

    View attributes supported:
      - required_platform_permission: Optional[str] (e.g. 'tenants.create', 'billing.view')
      - platform_permission_prefix: Optional[str] (e.g. 'tenants' -> appends .view, .create, .edit, .delete)
    """

    ACTION_MAP = {
        'list': 'view',
        'retrieve': 'view',
        'create': 'create',
        'update': 'edit',
        'partial_update': 'edit',
        'destroy': 'delete',
    }

    def _resolve_code(self, view) -> str:
        # 1. Explicit action_permission_map
        action_map = getattr(view, 'action_permission_map', {})
        action_name = getattr(view, 'action', None)
        if action_name in action_map:
            return action_map[action_name]

        # 2. Explicit required_platform_permission override
        if hasattr(view, 'required_platform_permission') and view.required_platform_permission:
            return view.required_platform_permission

        # 3. Existing canonical prefix/action resolution
        prefix = getattr(view, 'platform_permission_prefix', None)
        if prefix:
            action_suffix = self.ACTION_MAP.get(action_name, 'view')
            return f"{prefix}.{action_suffix}"

        # 4. Fail closed if no valid permission can be resolved
        return None

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated or getattr(user, '_auth_type', None) != 'platform':
            raise AuthenticationFailed('A valid platform JWT authentication is required.')

        # Platform Superusers have root access across control plane
        if getattr(user, 'is_superuser', False):
            return True

        perm_code = self._resolve_code(view)
        if not perm_code:
            logger.error(
                'PlatformRBACPermission DENIED: view=%s is missing required platform permission metadata.',
                view.__class__.__name__,
            )
            raise PermissionDenied('Platform endpoint is missing required platform permission metadata.')

        # Check live permission grant in Master DB
        has_grant = PlatformRolePermission.objects.using('default').filter(
            role__user_assignments__platform_user=user,
            role__user_assignments__is_active=True,
            role__is_active=True,
            permission__code__iexact=perm_code,
            is_allowed=True,
        ).exists()

        if not has_grant:
            logger.warning(
                'PlatformRBACPermission DENIED: user=%s code=%s view=%s',
                user.email, perm_code, view.__class__.__name__,
            )
            raise PermissionDenied(f"Platform permission '{perm_code}' is required.")

        return True
