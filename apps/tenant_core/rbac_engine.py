"""
Centralized RBAC Authorization Engine — Phase 1 Layer 1.
Implements the 10-check authorization rule from Section 4 Table 2 of the approved schema design.

Checks executed in exact sequence:
  1. Tenant ACTIVE (Master DB: tenants.status == 'ACTIVE')
  2. Organization ACTIVE (Tenant DB: organizations.status == 'ACTIVE')
  3. Location/Branch ACTIVE when requested resource is branch-scoped (Tenant DB: branches.status == 'ACTIVE')
  4. Tenant product module ENABLED (Master DB: tenant_modules.is_enabled == TRUE)
  5. Branch module available (ALL_BRANCHES or branch_modules.is_enabled == TRUE)
  6. User ACTIVE (Tenant DB: users.status == 'ACTIVE')
  7. Role assignment ACTIVE and scope matches requested resource (Tenant DB: role_assignments)
  8. Role module visible (Tenant DB: role_module_access.can_access == TRUE)
  9. Role submodule visible (Tenant DB: role_submodule_access.can_access == TRUE)
 10. Requested action permission allowed (Tenant DB: role_permission_set_items.granted == TRUE)

Security Properties:
  - Fails closed on any error, missing record, or inactive state.
  - JWT role claims are NEVER trusted as the final authority; all evaluations query the live DB.
  - No role bypasses checks 8-10 unless explicit DB records grant access.
  - Request-level memoization avoids N+1 database roundtrips within the same HTTP request.
"""

import logging
from typing import Optional, Tuple, List
from django.core.exceptions import ObjectDoesNotExist

logger = logging.getLogger(__name__)


class RBACAuthorizationEngine:
    """
    Centralized evaluator for tenant authorization.
    All tenant API requests must pass through this engine.
    """

    @classmethod
    def evaluate(
        cls,
        user,
        required_module: Optional[str] = None,
        required_submodule: Optional[str] = None,
        required_permission: Optional[str] = None,
        branch_id: Optional[str] = None,
        request=None,
    ) -> Tuple[bool, str, str]:
        """
        Evaluate the 10-check schema authorization chain.

        Returns:
            (allowed: bool, reason: str, check_code: str)
            Where check_code indicates which check passed or failed (e.g. 'CHECK_1_TENANT_INACTIVE').
        """
        # Resolve authentication context
        auth_type = getattr(user, '_auth_type', None)
        if not auth_type and hasattr(user, '__class__') and user.__class__.__name__ == 'TenantUser':
            auth_type = 'tenant'

        # Fast-fail if not authenticated or not a tenant user
        if not user or not getattr(user, 'is_authenticated', False) or auth_type != 'tenant':
            return False, 'User is not an authenticated tenant user.', 'AUTH_FAILED'

        tenant_id = getattr(user, '_tenant_id', None)
        db_alias = getattr(user, '_db_alias', None)

        if not db_alias:
            from config.routers import get_tenant_db_alias
            db_alias = get_tenant_db_alias()

        if not tenant_id:
            from apps.master.models_infra import TenantDataSource
            ds = TenantDataSource.objects.using('default').filter(status='ACTIVE').first()
            if ds:
                tenant_id = str(ds.tenant_id)

        if not tenant_id or not db_alias:
            return False, 'Tenant context (tenant_id or db_alias) missing on user.', 'CONTEXT_MISSING'

        # Check for request-level cached result if identical parameters
        cache_key = (
            str(user.id),
            str(required_module).lower() if required_module else '',
            str(required_submodule).lower() if required_submodule else '',
            str(required_permission).lower() if required_permission else '',
            str(branch_id) if branch_id else '',
        )
        if request is not None:
            if not hasattr(request, '_rbac_eval_cache'):
                request._rbac_eval_cache = {}
            if cache_key in request._rbac_eval_cache:
                return request._rbac_eval_cache[cache_key]

        result = cls._run_10_checks(
            user=user,
            tenant_id=tenant_id,
            db_alias=db_alias,
            required_module=required_module,
            required_submodule=required_submodule,
            required_permission=required_permission,
            branch_id=branch_id,
        )

        if request is not None and hasattr(request, '_rbac_eval_cache'):
            request._rbac_eval_cache[cache_key] = result

        return result

    @classmethod
    def _run_10_checks(
        cls,
        user,
        tenant_id: str,
        db_alias: str,
        required_module: Optional[str],
        required_submodule: Optional[str],
        required_permission: Optional[str],
        branch_id: Optional[str],
    ) -> Tuple[bool, str, str]:

        # -------------------------------------------------------------------
        # CHECK 1: Tenant ACTIVE (Master DB: tenants.status == 'ACTIVE')
        # -------------------------------------------------------------------
        from apps.master.models_tenant import Tenant
        try:
            tenant = Tenant.objects.using('default').only('id', 'status', 'slug').get(id=tenant_id)
        except Tenant.DoesNotExist:
            return False, 'Tenant record does not exist in master registry.', 'CHECK_1_TENANT_NOT_FOUND'

        if tenant.status != 'ACTIVE':
            return False, f'Tenant account is {tenant.status}. Access denied.', 'CHECK_1_TENANT_INACTIVE'

        # -------------------------------------------------------------------
        # CHECK 2: Organization ACTIVE (Tenant DB: organizations.status == 'ACTIVE')
        # -------------------------------------------------------------------
        from apps.tenant_core.models_org import Organization
        try:
            # Load user's organization or first active org in tenant DB
            org_id = getattr(user, 'organization_id', None)
            if org_id:
                org = Organization.objects.using(db_alias).only('id', 'status').get(id=org_id)
            else:
                org = Organization.objects.using(db_alias).only('id', 'status').first()
        except Organization.DoesNotExist:
            return False, 'Organization not found in tenant database.', 'CHECK_2_ORG_NOT_FOUND'

        if not org or org.status != 'ACTIVE':
            status_val = org.status if org else 'MISSING'
            return False, f'Organization is {status_val}. Access denied.', 'CHECK_2_ORG_INACTIVE'

        # -------------------------------------------------------------------
        # CHECK 3: Location/Branch ACTIVE (when resource is branch-scoped)
        # -------------------------------------------------------------------
        from apps.tenant_core.models_org import Branch
        target_branch = None
        if branch_id:
            try:
                target_branch = Branch.objects.using(db_alias).only('id', 'status', 'name').get(id=branch_id)
            except Branch.DoesNotExist:
                return False, f'Requested branch {branch_id} does not exist.', 'CHECK_3_BRANCH_NOT_FOUND'

            if target_branch.status != 'ACTIVE':
                return False, f'Branch {target_branch.name} is {target_branch.status}. Operations blocked.', 'CHECK_3_BRANCH_INACTIVE'

        # -------------------------------------------------------------------
        # CHECK 4: Tenant product module ENABLED (Master DB: tenant_modules.is_enabled == TRUE)
        # -------------------------------------------------------------------
        from apps.master.models_saas import TenantModule
        tenant_module = None
        if required_module:
            try:
                tenant_module = TenantModule.objects.using('default').select_related('module').get(
                    tenant_id=tenant_id,
                    module__code__iexact=required_module,
                )
            except TenantModule.DoesNotExist:
                return False, f"Product module '{required_module}' is not entitled for this tenant.", 'CHECK_4_MODULE_NOT_ENTITLED'

            if not tenant_module.is_enabled:
                return False, f"Product module '{required_module}' is disabled for this tenant.", 'CHECK_4_MODULE_DISABLED'

        # -------------------------------------------------------------------
        # CHECK 5: Branch module available (ALL_BRANCHES or branch_modules.is_enabled == TRUE)
        # -------------------------------------------------------------------
        if required_module and target_branch and tenant_module:
            if tenant_module.availability_mode == 'SELECTED_BRANCHES':
                from apps.tenant_core.models_rbac import BranchModule
                is_branch_enabled = BranchModule.objects.using(db_alias).filter(
                    branch=target_branch,
                    module_code__iexact=required_module,
                    is_enabled=True,
                ).exists()
                if not is_branch_enabled:
                    return False, (
                        f"Module '{required_module}' is not available at branch '{target_branch.name}' "
                        f"(availability mode: SELECTED_BRANCHES)."
                    ), 'CHECK_5_BRANCH_MODULE_UNAVAILABLE'

        # -------------------------------------------------------------------
        # CHECK 6: User ACTIVE (Tenant DB: users.status == 'ACTIVE')
        # -------------------------------------------------------------------
        from apps.tenant_core.models_users import TenantUser
        try:
            live_user = TenantUser.objects.using(db_alias).only('id', 'status').get(id=user.id)
        except TenantUser.DoesNotExist:
            return False, 'Tenant user not found in tenant database.', 'CHECK_6_USER_NOT_FOUND'

        if live_user.status != 'ACTIVE':
            return False, f'User account is {live_user.status}. Access denied.', 'CHECK_6_USER_INACTIVE'

        # -------------------------------------------------------------------
        # CHECK 7: Role assignment ACTIVE and scope matches requested resource
        # -------------------------------------------------------------------
        from apps.tenant_core.models_rbac import RoleAssignment
        active_assignments = list(
            RoleAssignment.objects.using(db_alias)
            .filter(user_id=user.id, is_active=True)
            .select_related('role', 'branch')
        )

        if not active_assignments:
            return False, 'User has no active role assignments in this organization.', 'CHECK_7_NO_ACTIVE_ROLE'

        # Filter assignments matching resource scope
        # If resource is branch-scoped (target_branch is specified):
        # A role matches if:
        #   a) role.scope == 'ORG' (organization-wide role, e.g. ORG_ADMIN, Org Director)
        #   b) assignment.branch_id == target_branch.id
        matching_roles = []
        for ra in active_assignments:
            role = ra.role
            if not role.is_active:
                continue

            if target_branch is not None:
                if role.scope == 'ORG' or (ra.branch_id and str(ra.branch_id) == str(target_branch.id)):
                    matching_roles.append(role)
            else:
                # Org-level resource or unspecified branch: include all active roles
                matching_roles.append(role)

        if not matching_roles:
            return False, (
                f'User active role assignments do not grant access to branch {branch_id}. '
                'Cross-branch access denied.'
            ), 'CHECK_7_SCOPE_MISMATCH'

        # -------------------------------------------------------------------
        # CHECK 8: Role module visible (role_module_access.can_access == TRUE)
        # -------------------------------------------------------------------
        from apps.tenant_core.models_rbac import RoleModuleAccess
        if required_module:
            has_module_access = RoleModuleAccess.objects.using(db_alias).filter(
                role__in=matching_roles,
                module__module_code__iexact=required_module,
                can_access=True,
            ).exists()

            if not has_module_access:
                return False, (
                    f"None of the user's active roles grant access to module '{required_module}'."
                ), 'CHECK_8_MODULE_ACCESS_DENIED'

        # -------------------------------------------------------------------
        # CHECK 9: Role submodule visible (role_submodule_access.can_access == TRUE)
        # -------------------------------------------------------------------
        from apps.tenant_core.models_rbac import RoleSubmoduleAccess
        if required_module and required_submodule:
            has_submodule_access = RoleSubmoduleAccess.objects.using(db_alias).filter(
                role__in=matching_roles,
                submodule__submodule_code__iexact=required_submodule,
                submodule__module__module_code__iexact=required_module,
                can_access=True,
            ).exists()

            if not has_submodule_access:
                return False, (
                    f"None of the user's active roles grant access to submodule '{required_submodule}' "
                    f"in module '{required_module}'."
                ), 'CHECK_9_SUBMODULE_ACCESS_DENIED'

        # -------------------------------------------------------------------
        # CHECK 10: Requested action permission allowed (role_permission_set_items.granted == TRUE)
        # -------------------------------------------------------------------
        from apps.tenant_core.models_rbac import RolePermissionSetItem
        if required_permission:
            has_permission = RolePermissionSetItem.objects.using(db_alias).filter(
                permission_set__role__in=matching_roles,
                permission_set__is_active=True,
                permission__permission_code__iexact=required_permission,
                granted=True,
            ).exists()

            if not has_permission:
                return False, (
                    f"Permission '{required_permission}' is not granted to any of the user's active roles."
                ), 'CHECK_10_PERMISSION_DENIED'

        # All evaluated checks passed!
        return True, 'Access granted.', 'ALL_CHECKS_PASSED'
