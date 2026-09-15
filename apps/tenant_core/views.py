"""
Tenant core views — Tenant-scoped API endpoints.
All endpoints require Tenant JWT authentication and route to the tenant's dedicated DB.
Enforces real-time database-driven RBAC authorization and branch/resource scoping.
"""

import logging
from django.db import transaction
from rest_framework import viewsets, permissions, status
from rest_framework.views import APIView
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied, AuthenticationFailed

from config.routers import get_tenant_db_alias
from .models_org import Organization, CompanyEntity, Location, Branch
from .models_users import TenantUser, Department, UserBranch
from .models_rbac import (
    Role, RoleAssignment, ModuleCatalog, SubmoduleCatalog,
    Permission, RolePermissionSet, RolePermissionSetItem,
    RoleModuleAccess, RoleSubmoduleAccess, BranchModule,
)
from .models_govern import (
    OrganizationSettings, BranchSettings, NotificationTemplate,
    BranchWorkingHours, BranchOperatingException,
)
from .models_privacy import TenantAuditEvent
from .serializers import (
    OrganizationSerializer, CompanyEntitySerializer, LocationSerializer, BranchSerializer,
    TenantUserSerializer, TenantUserCreateSerializer, DepartmentSerializer, UserBranchSerializer,
    RoleSerializer, RoleAssignmentSerializer, ModuleCatalogSerializer, SubmoduleCatalogSerializer,
    PermissionSerializer, RolePermissionSetSerializer, RolePermissionSetItemSerializer,
    BranchModuleSerializer, OrganizationSettingsSerializer, BranchSettingsSerializer,
    NotificationTemplateSerializer, TenantAuditEventSerializer,
    BranchWorkingHoursSerializer, BranchOperatingExceptionSerializer,
)
from .permissions import TenantRBACPermission, RequireActiveTenantAndOrg
from .rbac_engine import RBACAuthorizationEngine
from .audit import emit_audit_event, snapshot_model_state

logger = logging.getLogger(__name__)


class TenantDBMixin:
    """
    Mixin that routes queryset to the active tenant's dedicated DB.

    Fail-closed: raises PermissionDenied (HTTP 403) if no tenant DB alias is
    active on the current request. This prevents any accidental fallback to
    the Master DB ('default').
    """

    def get_db(self) -> str:
        """Return the active tenant DB alias, or raise PermissionDenied."""
        alias = get_tenant_db_alias()
        if not alias:
            raise PermissionDenied(
                'Tenant context is not active for this request. '
                'A valid tenant JWT with a tenant_id (tid) claim is required '
                'to access tenant-scoped resources.'
            )
        return alias

    def get_queryset(self):
        if hasattr(super(), 'get_queryset'):
            qs = super().get_queryset()
        else:
            qs = self.queryset
        return qs.using(self.get_db())

    def perform_create(self, serializer):
        db = self.get_db()
        action_name = getattr(self, 'audit_action_map', {}).get('create', 'CREATE')
        with transaction.atomic(using=db):
            instance = serializer.save()
            after_state = snapshot_model_state(instance)
            emit_audit_event(
                action=action_name,
                resource_type=instance.__class__.__name__,
                resource_id=str(instance.pk),
                request=self.request,
                instance=instance,
                before_state=None,
                after_state=after_state,
                db_alias=db,
            )

    def perform_update(self, serializer):
        db = self.get_db()
        action_name = getattr(self, 'audit_action_map', {}).get('update', 'UPDATE')
        with transaction.atomic(using=db):
            before_state = snapshot_model_state(serializer.instance)
            instance = serializer.save()
            after_state = snapshot_model_state(instance)
            emit_audit_event(
                action=action_name,
                resource_type=instance.__class__.__name__,
                resource_id=str(instance.pk),
                request=self.request,
                instance=instance,
                before_state=before_state,
                after_state=after_state,
                db_alias=db,
            )

    def perform_destroy(self, instance):
        db = self.get_db()
        action_name = getattr(self, 'audit_action_map', {}).get('destroy', 'DELETE')
        with transaction.atomic(using=db):
            before_state = snapshot_model_state(instance)
            resource_type = instance.__class__.__name__
            resource_id = str(instance.pk)
            emit_audit_event(
                action=action_name,
                resource_type=resource_type,
                resource_id=resource_id,
                request=self.request,
                instance=instance,
                before_state=before_state,
                after_state=None,
                db_alias=db,
            )
            instance.delete(using=db)

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['db_alias'] = self.get_db()
        return ctx


class TenantScopeMixin:
    """
    Enforces branch and resource scope boundaries on querysets and mutations.
    Queries live RoleAssignment records from the tenant database.

    Scope rules:
      - ORG scope: unrestricted access across the tenant organization.
      - BRANCH scope: restricted strictly to the user's assigned branch(es).
      - Cross-branch access is strictly blocked (fails closed).
    """

    def get_user_branch_scope(self):
        """
        Returns (is_org_wide: bool, allowed_branch_ids: list).
        Evaluates active assignments in real time from the tenant DB.
        """
        db = self.get_db()
        user = self.request.user
        if not user or not user.is_authenticated:
            return False, []

        active_assignments = list(
            RoleAssignment.objects.using(db)
            .filter(user_id=user.id, is_active=True)
            .select_related('role')
        )

        if not active_assignments:
            return False, []

        # If any active role has ORG scope, user has full organization visibility
        for ra in active_assignments:
            if ra.role.is_active and ra.role.scope == 'ORG':
                return True, []

        # Otherwise, collect assigned branch IDs
        branch_ids = [
            ra.branch_id for ra in active_assignments
            if ra.branch_id and ra.role.is_active
        ]
        return False, branch_ids

    def filter_queryset_by_scope(self, qs):
        is_org_wide, branch_ids = self.get_user_branch_scope()
        if is_org_wide:
            return qs

        # If user has no active branch assignments, return empty queryset
        if not branch_ids:
            return qs.none()

        model = qs.model
        if hasattr(model, 'home_branch'):
            return qs.filter(home_branch_id__in=branch_ids)
        elif hasattr(model, 'branch'):
            return qs.filter(branch_id__in=branch_ids)
        elif model == Branch:
            return qs.filter(id__in=branch_ids)

        return qs

    def get_queryset(self):
        qs = super().get_queryset()
        return self.filter_queryset_by_scope(qs)

    def validate_branch_scope(self, branch_id):
        """Raise PermissionDenied if target branch_id violates user's branch scope."""
        is_org_wide, branch_ids = self.get_user_branch_scope()
        if is_org_wide:
            return
        if not branch_ids or str(branch_id) not in [str(b) for b in branch_ids]:
            logger.warning(
                'Cross-branch violation blocked: user=%s target_branch=%s allowed=%s',
                self.request.user.email, branch_id, branch_ids,
            )
            raise PermissionDenied(
                "Branch scope violation: you cannot access or modify records for another branch."
            )

    def perform_create(self, serializer):
        data = self.request.data
        branch_id = data.get('branch_id') or data.get('home_branch') or data.get('branch')
        if branch_id:
            self.validate_branch_scope(branch_id)
        super().perform_create(serializer)

    def perform_update(self, serializer):
        instance = serializer.instance
        branch_id = (
            getattr(instance, 'home_branch_id', None) or
            getattr(instance, 'branch_id', None) or
            (instance.id if isinstance(instance, Branch) else None)
        )
        if branch_id:
            self.validate_branch_scope(branch_id)
        super().perform_update(serializer)

    def perform_destroy(self, instance):
        branch_id = (
            getattr(instance, 'home_branch_id', None) or
            getattr(instance, 'branch_id', None) or
            (instance.id if isinstance(instance, Branch) else None)
        )
        if branch_id:
            self.validate_branch_scope(branch_id)
        super().perform_destroy(instance)


# ---------------------------------------------------------------------------
# ViewSets
# ---------------------------------------------------------------------------

class OrganizationViewSet(TenantDBMixin, viewsets.ReadOnlyModelViewSet):
    """Read-only — org structure is managed by platform team."""
    permission_classes = [RequireActiveTenantAndOrg]
    serializer_class = OrganizationSerializer
    queryset = Organization.objects.all()


class LocationViewSet(TenantDBMixin, viewsets.ReadOnlyModelViewSet):
    """Read-only — locations managed by platform team."""
    permission_classes = [RequireActiveTenantAndOrg]
    serializer_class = LocationSerializer
    queryset = Location.objects.all()


class BranchViewSet(TenantScopeMixin, TenantDBMixin, viewsets.ReadOnlyModelViewSet):
    """Read-only — branches managed by platform team. Scoped to user's branch if not org-wide."""
    permission_classes = [RequireActiveTenantAndOrg]
    serializer_class = BranchSerializer
    queryset = Branch.objects.all()


class DepartmentViewSet(TenantDBMixin, viewsets.ModelViewSet):
    """Tenant departments management."""
    permission_classes = [TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'departments'
    permission_prefix = 'core.departments'
    serializer_class = DepartmentSerializer
    queryset = Department.objects.all()


class TenantUserViewSet(TenantScopeMixin, TenantDBMixin, viewsets.ModelViewSet):
    """
    Tenant user management.
    Requires active tenant, active org, and live active role assignment.
    Branch-scoped staff can only view and manage users within their assigned branch.
    """
    permission_classes = [TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'users'
    permission_prefix = 'core.users'
    queryset = TenantUser.objects.all()

    def get_serializer_class(self):
        if self.action == 'create':
            return TenantUserCreateSerializer
        return TenantUserSerializer


class RoleViewSet(TenantDBMixin, viewsets.ModelViewSet):
    """
    Role management. System roles (e.g. ORG_ADMIN) are strictly protected against
    deletion or immutable field modification.
    Authorized via centralized RBAC engine against 'core.roles.*'.
    """
    permission_classes = [TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'roles'
    permission_prefix = 'core.roles'
    serializer_class = RoleSerializer
    queryset = Role.objects.all()

    def get_queryset(self):
        return super().get_queryset().filter(is_active=True)

    def perform_update(self, serializer):
        instance = serializer.instance
        if instance.is_system:
            data = self.request.data
            if 'code' in data and data['code'] != instance.code:
                raise PermissionDenied('System role code is immutable.')
            if 'scope' in data and data['scope'] != instance.scope:
                raise PermissionDenied('System role scope is immutable.')
            if 'is_system' in data and not data['is_system']:
                raise PermissionDenied('System role flag is immutable.')
        super().perform_update(serializer)

    def perform_destroy(self, instance):
        if instance.is_system:
            raise PermissionDenied('System roles cannot be deleted.')
        super().perform_destroy(instance)


class RoleAssignmentViewSet(TenantDBMixin, viewsets.ModelViewSet):
    """
    Assign/revoke roles to users.
    Authorized via centralized RBAC engine against 'core.roles.assign' and 'core.roles.view'.
    """
    permission_classes = [TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'roles'
    action_permission_map = {
        'list': 'core.roles.view',
        'retrieve': 'core.roles.view',
        'create': 'core.roles.assign',
        'update': 'core.roles.assign',
        'partial_update': 'core.roles.assign',
        'destroy': 'core.roles.assign',
    }
    audit_action_map = {
        'create': 'ASSIGN',
        'destroy': 'UNASSIGN',
        'update': 'UPDATE',
        'partial_update': 'UPDATE',
    }
    serializer_class = RoleAssignmentSerializer
    queryset = RoleAssignment.objects.all()

    def get_queryset(self):
        return super().get_queryset().filter(is_active=True)


class ModuleCatalogViewSet(TenantDBMixin, viewsets.ReadOnlyModelViewSet):
    """Tenant's enabled module catalog."""
    permission_classes = [RequireActiveTenantAndOrg]
    serializer_class = ModuleCatalogSerializer
    queryset = ModuleCatalog.objects.all()

    def get_queryset(self):
        return super().get_queryset().filter(is_enabled=True)


class BranchModuleViewSet(TenantDBMixin, viewsets.ReadOnlyModelViewSet):
    """Read-only for tenants — branch-module mappings are controlled exclusively by platform team."""
    permission_classes = [RequireActiveTenantAndOrg]
    serializer_class = BranchModuleSerializer
    queryset = BranchModule.objects.all()


class RolePermissionSetViewSet(TenantDBMixin, viewsets.ModelViewSet):
    """Role permission sets management."""
    permission_classes = [TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'permissions'
    action_permission_map = {
        'list': 'core.permissions.view',
        'retrieve': 'core.permissions.view',
        'matrix': 'core.permissions.manage',
        'create': 'core.permissions.manage',
        'update': 'core.permissions.manage',
        'partial_update': 'core.permissions.manage',
        'destroy': 'core.permissions.manage',
    }
    serializer_class = RolePermissionSetSerializer
    queryset = RolePermissionSet.objects.all()

    def get_queryset(self):
        return super().get_queryset().filter(is_active=True)

    @action(detail=True, methods=['post', 'put'], url_path='matrix')
    def matrix(self, request, pk=None):
        """
        Atomic update of role permission set matrix:
        - role_module_access
        - role_submodule_access
        - role_permission_set_items
        Executed inside transaction.atomic(using=db).
        Enforces core.permissions.manage.
        Fails closed on invalid codes and protects system roles.
        """
        permission_set = self.get_object()
        db = self.get_db()
        role = permission_set.role

        if role.is_system:
            return Response(
                {'error': 'System roles cannot be modified.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        module_access_data = request.data.get('module_access', [])
        submodule_access_data = request.data.get('submodule_access', [])
        permissions_data = request.data.get('permissions', [])

        try:
            with transaction.atomic(using=db):
                # Capture before state snapshot
                current_modules = list(RoleModuleAccess.objects.using(db).filter(role=role).values('module__module_code', 'can_access'))
                current_submodules = list(RoleSubmoduleAccess.objects.using(db).filter(role=role).values('submodule__submodule_code', 'can_access'))
                current_perms = list(RolePermissionSetItem.objects.using(db).filter(permission_set=permission_set).values('permission__permission_code', 'granted'))
                before_state = {
                    'role_code': role.code,
                    'permission_set_id': str(permission_set.id),
                    'module_access': current_modules,
                    'submodule_access': current_submodules,
                    'permissions': current_perms,
                }

                # 1. Update module access
                for item in module_access_data:
                    m_code = item.get('module_code')
                    can_access = bool(item.get('is_allowed', item.get('can_access', True)))
                    mod_obj = ModuleCatalog.objects.using(db).filter(module_code=m_code).first()
                    if not mod_obj:
                        raise ValueError(f"Module code '{m_code}' not found in catalog.")
                    RoleModuleAccess.objects.using(db).update_or_create(
                        role=role,
                        module=mod_obj,
                        defaults={'permission_set': permission_set, 'can_access': can_access, 'is_visible': can_access}
                    )

                # 2. Update submodule access
                for item in submodule_access_data:
                    sm_code = item.get('submodule_code')
                    m_code = item.get('module_code')
                    can_access = bool(item.get('is_allowed', item.get('can_access', True)))
                    sm_filter = {'submodule_code': sm_code}
                    if m_code:
                        sm_filter['module__module_code'] = m_code
                    sm_obj = SubmoduleCatalog.objects.using(db).filter(**sm_filter).first()
                    if not sm_obj:
                        raise ValueError(f"Submodule code '{sm_code}' not found in catalog.")
                    RoleSubmoduleAccess.objects.using(db).update_or_create(
                        role=role,
                        submodule=sm_obj,
                        defaults={'permission_set': permission_set, 'can_access': can_access, 'is_visible': can_access}
                    )

                # 3. Update permission items
                for item in permissions_data:
                    p_code = item.get('permission_code')
                    granted = bool(item.get('is_granted', item.get('granted', True)))
                    perm_obj = Permission.objects.using(db).filter(permission_code=p_code).first()
                    if not perm_obj:
                        raise ValueError(f"Permission code '{p_code}' not found in catalog.")
                    RolePermissionSetItem.objects.using(db).update_or_create(
                        permission_set=permission_set,
                        permission=perm_obj,
                        defaults={'granted': granted}
                    )

                # Capture after state snapshot
                after_modules = list(RoleModuleAccess.objects.using(db).filter(role=role).values('module__module_code', 'can_access'))
                after_submodules = list(RoleSubmoduleAccess.objects.using(db).filter(role=role).values('submodule__submodule_code', 'can_access'))
                after_perms = list(RolePermissionSetItem.objects.using(db).filter(permission_set=permission_set).values('permission__permission_code', 'granted'))
                after_state = {
                    'role_code': role.code,
                    'permission_set_id': str(permission_set.id),
                    'module_access': after_modules,
                    'submodule_access': after_submodules,
                    'permissions': after_perms,
                }

                # Emit single MATRIX_UPDATE audit event inside the same transaction
                emit_audit_event(
                    action='MATRIX_UPDATE',
                    resource_type='RolePermissionSet',
                    resource_id=str(permission_set.id),
                    request=request,
                    instance=permission_set,
                    before_state=before_state,
                    after_state=after_state,
                    description=f"Matrix permissions updated for role '{role.code}'",
                    db_alias=db,
                )
        except ValueError as err:
            return Response({'error': str(err)}, status=status.HTTP_400_BAD_REQUEST)

        mod_access = list(RoleModuleAccess.objects.using(db).filter(role=role).values('module__module_code', 'can_access'))
        sub_access = list(RoleSubmoduleAccess.objects.using(db).filter(role=role).values('submodule__submodule_code', 'submodule__module__module_code', 'can_access'))
        perm_items = list(RolePermissionSetItem.objects.using(db).filter(permission_set=permission_set).values('permission__permission_code', 'granted'))

        return Response({
            'success': True,
            'permission_set_id': str(permission_set.id),
            'role_id': str(role.id),
            'role_code': role.code,
            'module_access': [{'module_code': m['module__module_code'], 'is_allowed': m['can_access']} for m in mod_access],
            'submodule_access': [{'module_code': s['submodule__module__module_code'], 'submodule_code': s['submodule__submodule_code'], 'is_allowed': s['can_access']} for s in sub_access],
            'permissions': [{'permission_code': p['permission__permission_code'], 'is_granted': p['granted']} for p in perm_items],
        }, status=status.HTTP_200_OK)


class SubmoduleCatalogViewSet(TenantDBMixin, viewsets.ReadOnlyModelViewSet):
    """Tenant's enabled submodule catalog (read-only)."""
    permission_classes = [TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'permissions'
    permission_prefix = 'core.permissions'
    serializer_class = SubmoduleCatalogSerializer
    queryset = SubmoduleCatalog.objects.all().select_related('module')

    def get_queryset(self):
        qs = super().get_queryset().filter(is_enabled=True)
        mod_code = self.request.query_params.get('module') or self.request.query_params.get('module_code')
        if mod_code:
            qs = qs.filter(module__module_code=mod_code)
        return qs


class PermissionViewSet(TenantDBMixin, viewsets.ReadOnlyModelViewSet):
    """Tenant's canonical action permission catalog (read-only)."""
    permission_classes = [TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'permissions'
    permission_prefix = 'core.permissions'
    serializer_class = PermissionSerializer
    queryset = Permission.objects.all().select_related('module', 'submodule')

    def get_queryset(self):
        qs = super().get_queryset().filter(is_active=True)
        mod_code = self.request.query_params.get('module') or self.request.query_params.get('module_code')
        if mod_code:
            qs = qs.filter(module__module_code=mod_code)
        sub_code = self.request.query_params.get('submodule') or self.request.query_params.get('submodule_code')
        if sub_code:
            qs = qs.filter(submodule__submodule_code=sub_code)
        return qs


class CompanyEntityViewSet(TenantDBMixin, viewsets.ReadOnlyModelViewSet):
    """Read-only on tenant API — legal entities managed by platform team."""
    permission_classes = [RequireActiveTenantAndOrg]
    serializer_class = CompanyEntitySerializer
    queryset = CompanyEntity.objects.all()


class UserBranchViewSet(TenantScopeMixin, TenantDBMixin, viewsets.ModelViewSet):
    """User branch association and passport scoping."""
    permission_classes = [TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'users'
    action_permission_map = {
        'list': 'core.users.view',
        'retrieve': 'core.users.view',
        'create': 'core.users.edit',
        'update': 'core.users.edit',
        'partial_update': 'core.users.edit',
        'destroy': 'core.users.edit',
    }
    audit_action_map = {
        'create': 'ASSIGN',
        'destroy': 'UNASSIGN',
        'update': 'UPDATE',
        'partial_update': 'UPDATE',
    }
    serializer_class = UserBranchSerializer
    queryset = UserBranch.objects.all().select_related('user', 'branch')


class OrganizationSettingsViewSet(TenantDBMixin, viewsets.ModelViewSet):
    """
    Tenant organization business defaults (tax, currency, cancellation, grace periods).
    Authorized via centralized RBAC against core.settings.*.
    """
    permission_classes = [TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    action_permission_map = {
        'list': 'core.settings.view',
        'retrieve': 'core.settings.view',
        'current': 'core.settings.view',
        'create': 'core.settings.edit',
        'update': 'core.settings.edit',
        'partial_update': 'core.settings.edit',
        'destroy': 'core.settings.edit',
    }
    serializer_class = OrganizationSettingsSerializer
    queryset = OrganizationSettings.objects.all().select_related('organization')

    @action(detail=False, methods=['get', 'put', 'patch'], url_path='current')
    def current(self, request):
        """Get or update current organization settings without needing the settings UUID."""
        org = Organization.objects.using(self.get_db()).first()
        if not org:
            return Response({'error': 'Organization not found.'}, status=status.HTTP_404_NOT_FOUND)

        settings_obj, _ = OrganizationSettings.objects.using(self.get_db()).get_or_create(
            organization=org,
            defaults={'currency': org.currency or 'INR'}
        )

        if request.method in ['PUT', 'PATCH']:
            db = self.get_db()
            with transaction.atomic(using=db):
                before_state = snapshot_model_state(settings_obj)
                serializer = OrganizationSettingsSerializer(
                    settings_obj,
                    data=request.data,
                    partial=(request.method == 'PATCH'),
                    context=self.get_serializer_context(),
                )
                serializer.is_valid(raise_exception=True)
                updated_instance = serializer.save()
                after_state = snapshot_model_state(updated_instance)
                emit_audit_event(
                    action='UPDATE',
                    resource_type='OrganizationSettings',
                    resource_id=str(updated_instance.pk),
                    request=request,
                    instance=updated_instance,
                    before_state=before_state,
                    after_state=after_state,
                    db_alias=db,
                )
            return Response(serializer.data)

        return Response(OrganizationSettingsSerializer(settings_obj).data)


class BranchSettingsViewSet(TenantScopeMixin, TenantDBMixin, viewsets.ModelViewSet):
    """
    Branch-specific business overrides (operating hours, capacity, local contacts).
    Authorized via centralized RBAC against core.settings.*.
    """
    permission_classes = [TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    action_permission_map = {
        'list': 'core.settings.view',
        'retrieve': 'core.settings.view',
        'create': 'core.settings.edit',
        'update': 'core.settings.edit',
        'partial_update': 'core.settings.edit',
        'destroy': 'core.settings.edit',
    }
    serializer_class = BranchSettingsSerializer
    queryset = BranchSettings.objects.all().select_related('branch')


class NotificationTemplateViewSet(TenantDBMixin, viewsets.ModelViewSet):
    """
    Tenant communication templates (SMS, WhatsApp, Email).
    Authorized via centralized RBAC against core.notifications.*.
    """
    permission_classes = [TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'notifications'
    action_permission_map = {
        'list': 'core.notifications.view',
        'retrieve': 'core.notifications.view',
        'create': 'core.notifications.manage',
        'update': 'core.notifications.manage',
        'partial_update': 'core.notifications.manage',
        'destroy': 'core.notifications.manage',
    }
    serializer_class = NotificationTemplateSerializer
    queryset = NotificationTemplate.objects.all()


class TenantAuditEventViewSet(TenantScopeMixin, TenantDBMixin, viewsets.ReadOnlyModelViewSet):
    """
    Append-only tenant administrative audit ledger (strictly read-only from API).
    Authorized via centralized RBAC against core.audit.view.
    """
    permission_classes = [TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'audit'
    permission_prefix = 'core.audit'
    action_permission_map = {
        'list': 'core.audit.view',
        'retrieve': 'core.audit.view',
    }
    serializer_class = TenantAuditEventSerializer
    queryset = TenantAuditEvent.objects.all().select_related('actor')


class VerifyAccessView(APIView):
    """
    Authorization testing & verification endpoint.
    Invokes the centralized 10-check RBACAuthorizationEngine.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        user = request.user
        required_module = request.data.get('module')
        required_submodule = request.data.get('submodule')
        required_permission = request.data.get('permission')
        branch_id = request.data.get('branch_id')

        allowed, reason, check_code = RBACAuthorizationEngine.evaluate(
            user=user,
            required_module=required_module,
            required_submodule=required_submodule,
            required_permission=required_permission,
            branch_id=branch_id,
            request=request,
        )

        status_code = status.HTTP_200_OK if allowed else status.HTTP_403_FORBIDDEN
        return Response({
            'allowed': allowed,
            'reason': reason,
            'check_code': check_code,
            'user': user.email,
        }, status=status_code)


class TenantDatabaseHealthView(APIView):
    """
    Live connection test & latency measurement for the active tenant database.
    Zero secrets exposed.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return self._measure(request)

    def post(self, request):
        return self._measure(request)

    def _measure(self, request):
        import time
        from django.db import connections
        from django.utils import timezone
        from config.routers import get_tenant_db_alias

        db_alias = get_tenant_db_alias() or 'default'
        t0 = time.time()
        status_val = 'HEALTHY'
        err = ''
        try:
            conn = connections[db_alias]
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1;")
                cursor.fetchone()
            latency_ms = max(1, int((time.time() - t0) * 1000))
            if latency_ms > 500:
                status_val = 'DEGRADED'
        except Exception as exc:
            latency_ms = max(1, int((time.time() - t0) * 1000))
            status_val = 'UNREACHABLE'
            err = str(exc)[:200]

        return Response({
            'tenant_db_alias': db_alias,
            'status': status_val,
            'latency_ms': latency_ms,
            'checked_at': timezone.now().isoformat(),
            'error': err,
        })


class BranchWorkingHoursViewSet(TenantScopeMixin, TenantDBMixin, viewsets.ModelViewSet):
    """
    Branch recurring weekly working hours (Mon-Sun).
    Authorized via centralized RBAC against core.settings.*.
    """
    permission_classes = [TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    action_permission_map = {
        'list': 'core.settings.view',
        'retrieve': 'core.settings.view',
        'create': 'core.settings.edit',
        'update': 'core.settings.edit',
        'partial_update': 'core.settings.edit',
        'destroy': 'core.settings.edit',
    }
    serializer_class = BranchWorkingHoursSerializer
    queryset = BranchWorkingHours.objects.all().select_related('branch')

    def perform_create(self, serializer):
        user = self.request.user if hasattr(self.request, 'user') and self.request.user.is_authenticated else None
        db = self.get_db()
        before_state = None
        instance = serializer.save(created_by=user, updated_by=user)
        after_state = snapshot_model_state(instance)
        emit_audit_event(
            action='CREATE',
            resource_type='BranchWorkingHours',
            resource_id=str(instance.pk),
            request=self.request,
            instance=instance,
            before_state=before_state,
            after_state=after_state,
            db_alias=db,
        )

    def perform_update(self, serializer):
        user = self.request.user if hasattr(self.request, 'user') and self.request.user.is_authenticated else None
        db = self.get_db()
        before_state = snapshot_model_state(serializer.instance)
        instance = serializer.save(updated_by=user)
        after_state = snapshot_model_state(instance)
        emit_audit_event(
            action='UPDATE',
            resource_type='BranchWorkingHours',
            resource_id=str(instance.pk),
            request=self.request,
            instance=instance,
            before_state=before_state,
            after_state=after_state,
            db_alias=db,
        )

    def perform_destroy(self, instance):
        db = self.get_db()
        before_state = snapshot_model_state(instance)
        resource_id = str(instance.pk)
        instance.delete()
        emit_audit_event(
            action='DELETE',
            resource_type='BranchWorkingHours',
            resource_id=resource_id,
            request=self.request,
            instance=instance,
            before_state=before_state,
            after_state=None,
            db_alias=db,
        )


class BranchOperatingExceptionViewSet(TenantScopeMixin, TenantDBMixin, viewsets.ModelViewSet):
    """
    Branch operating exceptions / date-specific overrides.
    Authorized via centralized RBAC against core.settings.*.
    """
    permission_classes = [TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    action_permission_map = {
        'list': 'core.settings.view',
        'retrieve': 'core.settings.view',
        'create': 'core.settings.edit',
        'update': 'core.settings.edit',
        'partial_update': 'core.settings.edit',
        'destroy': 'core.settings.edit',
    }
    serializer_class = BranchOperatingExceptionSerializer
    queryset = BranchOperatingException.objects.all().select_related('branch')

    def perform_create(self, serializer):
        user = self.request.user if hasattr(self.request, 'user') and self.request.user.is_authenticated else None
        db = self.get_db()
        before_state = None
        instance = serializer.save(created_by=user, updated_by=user)
        after_state = snapshot_model_state(instance)
        emit_audit_event(
            action='CREATE',
            resource_type='BranchOperatingException',
            resource_id=str(instance.pk),
            request=self.request,
            instance=instance,
            before_state=before_state,
            after_state=after_state,
            db_alias=db,
        )

    def perform_update(self, serializer):
        user = self.request.user if hasattr(self.request, 'user') and self.request.user.is_authenticated else None
        db = self.get_db()
        before_state = snapshot_model_state(serializer.instance)
        instance = serializer.save(updated_by=user)
        after_state = snapshot_model_state(instance)
        emit_audit_event(
            action='UPDATE',
            resource_type='BranchOperatingException',
            resource_id=str(instance.pk),
            request=self.request,
            instance=instance,
            before_state=before_state,
            after_state=after_state,
            db_alias=db,
        )

    def perform_destroy(self, instance):
        db = self.get_db()
        before_state = snapshot_model_state(instance)
        resource_id = str(instance.pk)
        instance.delete()
        emit_audit_event(
            action='DELETE',
            resource_type='BranchOperatingException',
            resource_id=resource_id,
            request=self.request,
            instance=instance,
            before_state=before_state,
            after_state=None,
            db_alias=db,
        )


