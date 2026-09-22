"""
Tenant RBAC DRF Permission Classes — Phase 1 Layer 1.
Integrates DRF ViewSets with the centralized RBACAuthorizationEngine.

Enforces server-side authorization across the 10-check schema chain:
  - Fails closed with HTTP 403 Forbidden.
  - Resolves required module, submodule, and action permission from ViewSet metadata.
  - Dynamically detects target branch context for branch-scoped operations.
"""

import logging
from rest_framework import permissions
from rest_framework.exceptions import PermissionDenied, AuthenticationFailed
from .rbac_engine import RBACAuthorizationEngine

logger = logging.getLogger(__name__)


class TenantRBACPermission(permissions.BasePermission):
    """
    Primary DRF permission class for all tenant-scoped API endpoints.
    Invokes the centralized RBACAuthorizationEngine.

    View attributes supported:
      - required_module: str (e.g. 'crm', 'members', 'iam')
      - required_submodule: Optional[str] (e.g. 'leads', 'users')
      - required_permission: Optional[str] (exact permission code override)
      - permission_prefix: Optional[str] (e.g. 'crm.leads' -> will append .view, .create, .edit, .delete)
      - action_permission_map: Optional[dict] mapping action -> full permission code
    """

    # Standard DRF action -> permission action suffix
    ACTION_MAP = {
        'list': 'view',
        'retrieve': 'view',
        'create': 'create',
        'update': 'edit',
        'partial_update': 'edit',
        'destroy': 'delete',
    }

    def _resolve_permission_code(self, view, request) -> str:
        """Derive permission code from view metadata and action."""
        # 1. Check action_permission_map dictionary
        action_map = getattr(view, 'action_permission_map', {}) or getattr(view, 'permission_action_map', {})
        act = getattr(view, 'action', None)
        if act and act in action_map:
            return action_map[act]

        # 2. Check direct override on view
        if hasattr(view, 'required_permission') and view.required_permission:
            return view.required_permission

        # 3. Check permission_prefix + action suffix
        prefix = getattr(view, 'permission_prefix', None)
        if prefix:
            action_suffix = self.ACTION_MAP.get(act, 'view')
            return f"{prefix}.{action_suffix}"

        return None

    def _resolve_branch_id(self, request, view, obj=None) -> str:
        """Extract target branch_id from request params, body, kwargs, or object."""
        if obj is not None and hasattr(obj, 'branch_id') and obj.branch_id:
            return str(obj.branch_id)

        # URL kwarg (e.g. /branches/<branch_id>/...)
        if hasattr(view, 'kwargs') and view.kwargs:
            if 'branch_id' in view.kwargs:
                return str(view.kwargs['branch_id'])
            if 'branch_pk' in view.kwargs:
                return str(view.kwargs['branch_pk'])

        # Query param
        if request.query_params:
            if 'branch_id' in request.query_params:
                return request.query_params['branch_id']
            if 'branch' in request.query_params:
                return request.query_params['branch']

        # Request data (for POST/PUT/PATCH)
        if request.data and isinstance(request.data, dict):
            if 'branch_id' in request.data:
                return str(request.data['branch_id'])
            if 'branch' in request.data:
                return str(request.data['branch'])

        return None

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            raise AuthenticationFailed('A valid tenant JWT authentication is required.')

        # Allow platform superuser / admin override for testing and system administration
        is_platform_admin = (
            getattr(user, '_auth_type', None) == 'platform'
            and (getattr(user, 'is_superuser', False) or 'SUPER_ADMIN' in getattr(user, '_role_codes', set()))
        ) or getattr(user, 'is_superuser', False)

        if getattr(user, '_auth_type', None) != 'tenant' and not is_platform_admin:
            raise AuthenticationFailed('A valid tenant JWT authentication is required.')

        if is_platform_admin:
            return True

        required_module = getattr(view, 'required_module', None)
        required_submodule = getattr(view, 'required_submodule', None)
        required_permission = self._resolve_permission_code(view, request)
        branch_id = self._resolve_branch_id(request, view)

        # FAIL CLOSED: Enforce required RBAC metadata
        if not required_module:
            logger.warning('TenantRBACPermission FAIL-CLOSED: view=%s missing required_module', view.__class__.__name__)
            raise PermissionDenied('Protected tenant endpoint is missing required module metadata.')

        if not required_permission:
            action_name = getattr(view, 'action', None) or request.method
            logger.warning(
                'TenantRBACPermission FAIL-CLOSED: view=%s action=%s missing required_permission',
                view.__class__.__name__, action_name,
            )
            raise PermissionDenied(f"Protected tenant endpoint is missing required permission mapping for action '{action_name}'.")

        # FAIL CLOSED: If module has submodules in catalog, required_submodule must not be omitted
        if not required_submodule:
            from config.routers import get_tenant_db_alias
            from apps.tenant_core.models_rbac import SubmoduleCatalog
            db = getattr(user, '_db_alias', None) or get_tenant_db_alias()
            if db:
                try:
                    has_submodules = SubmoduleCatalog.objects.using(db).filter(
                        module__module_code__iexact=required_module
                    ).exists()
                    if has_submodules:
                        raise PermissionDenied(
                            f"Protected tenant endpoint is missing required submodule metadata for module '{required_module}'."
                        )
                except Exception as exc:
                    if isinstance(exc, PermissionDenied):
                        raise
                    pass

        allowed, reason, check_code = RBACAuthorizationEngine.evaluate(
            user=user,
            required_module=required_module,
            required_submodule=required_submodule,
            required_permission=required_permission,
            branch_id=branch_id,
            request=request,
        )

        if not allowed and hasattr(view, 'alternative_permissions'):
            for alt_perm in view.alternative_permissions:
                alt_allowed, _, _ = RBACAuthorizationEngine.evaluate(
                    user=user,
                    required_module=required_module,
                    required_submodule=required_submodule,
                    required_permission=alt_perm,
                    branch_id=branch_id,
                    request=request,
                )
                if alt_allowed:
                    allowed = True
                    break

        if not allowed:
            logger.warning(
                'TenantRBACPermission DENIED: user=%s view=%s check=%s reason=%s',
                user.email, view.__class__.__name__, check_code, reason,
            )
            raise PermissionDenied(reason)

        return True

    def has_object_permission(self, request, view, obj):
        # Validate object-level branch boundary if object has a branch
        branch_id = self._resolve_branch_id(request, view, obj=obj)
        required_module = getattr(view, 'required_module', None)
        required_submodule = getattr(view, 'required_submodule', None)
        required_permission = self._resolve_permission_code(view, request)

        if not required_module:
            raise PermissionDenied('Protected tenant endpoint is missing required module metadata.')
        if not required_permission:
            raise PermissionDenied('Protected tenant endpoint is missing required permission metadata.')

        allowed, reason, check_code = RBACAuthorizationEngine.evaluate(
            user=request.user,
            required_module=required_module,
            required_submodule=required_submodule,
            required_permission=required_permission,
            branch_id=branch_id,
            request=request,
        )
        if not allowed:
            raise PermissionDenied(reason)

        return True


class RequireActiveTenantAndOrg(permissions.BasePermission):
    """
    Lightweight check for read-only or foundational metadata endpoints
    that do not require module-level action permissions, but strictly require
    Tenant ACTIVE, Org ACTIVE, and User ACTIVE (Checks 1, 2, 6).
    """

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            raise AuthenticationFailed('A valid tenant JWT authentication is required.')

        is_platform_admin = (
            getattr(user, '_auth_type', None) == 'platform'
            and (getattr(user, 'is_superuser', False) or 'SUPER_ADMIN' in getattr(user, '_role_codes', set()))
        ) or getattr(user, 'is_superuser', False)

        if getattr(user, '_auth_type', None) != 'tenant' and not is_platform_admin:
            raise AuthenticationFailed('A valid tenant JWT authentication is required.')

        if is_platform_admin:
            return True

        allowed, reason, check_code = RBACAuthorizationEngine.evaluate(
            user=user,
            required_module=None,
            required_permission=None,
            request=request,
        )

        if not allowed:
            raise PermissionDenied(reason)

        return True
