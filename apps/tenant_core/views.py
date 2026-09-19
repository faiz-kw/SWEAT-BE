"""
Tenant core views — Tenant-scoped API endpoints.
All endpoints require Tenant JWT authentication and route to the tenant's dedicated DB.
Enforces real-time database-driven RBAC authorization and branch/resource scoping.
"""

import logging
import uuid
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

    def get_tenant_id(self) -> str:
        """Resolve current tenant ID from auth context or active DB alias."""
        request = getattr(self, 'request', None)
        if request and hasattr(request, 'user'):
            tid = getattr(request.user, '_tenant_id', None)
            if tid:
                return str(tid)
            if hasattr(request, 'auth') and isinstance(request.auth, dict):
                tid = request.auth.get('tid')
                if tid:
                    return str(tid)

        db_alias = self.get_db()
        from apps.master.models_infra import TenantDataSource
        clean_alias = db_alias.replace('tenant_tenant_', 'tenant_').replace('tenant_', '')
        ds = TenantDataSource.objects.using('default').filter(
            db_name__icontains=clean_alias
        ).first()
        if ds and ds.tenant_id:
            return str(ds.tenant_id)
        raise PermissionDenied('Could not resolve authoritative tenant context.')

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
    action_permission_map = {
        'reactivate': 'core.users.edit',
        'deactivate': 'core.users.delete',
        'toggle_active': 'core.users.edit',
    }
    queryset = TenantUser.objects.all()

    def get_queryset(self):
        db = self.get_db()
        qs = TenantUser.objects.using(db).select_related('organization', 'home_branch').all()
        qs = self.filter_queryset_by_scope(qs)

        branch_param = self.request.query_params.get('branch') or self.request.query_params.get('location')
        if branch_param and branch_param != 'all':
            from django.db.models import Q
            try:
                import uuid
                branch_uuid = uuid.UUID(str(branch_param))
                qs = qs.filter(Q(home_branch_id=branch_uuid) | Q(role_assignments__branch_id=branch_uuid, role_assignments__is_active=True)).distinct()
            except (ValueError, TypeError, AttributeError):
                qs = qs.filter(Q(home_branch__name__iexact=str(branch_param)) | Q(role_assignments__branch__name__iexact=str(branch_param), role_assignments__is_active=True)).distinct()

        role_param = self.request.query_params.get('role')
        if role_param and role_param != 'all':
            from django.db.models import Q
            qs = qs.filter(
                Q(role_assignments__role__name__iexact=str(role_param)) |
                Q(role_assignments__role__code__iexact=str(role_param)),
                role_assignments__is_active=True
            ).distinct()

        search_param = self.request.query_params.get('search')
        if search_param:
            from django.db.models import Q
            qs = qs.filter(
                Q(first_name__icontains=search_param) |
                Q(last_name__icontains=search_param) |
                Q(email__icontains=search_param) |
                Q(phone__icontains=search_param)
            )

        return qs

    def get_serializer_class(self):
        if self.action == 'create':
            return TenantUserCreateSerializer
        return TenantUserSerializer

    def perform_update(self, serializer):
        with transaction.atomic(using=self.get_db()):
            instance = serializer.instance
            db = self.get_db()
            data = self.request.data
            from .models_rbac import Role, RoleAssignment
            from .models_users import UserDepartment, Department, UserBranch
            from .models_org import Branch

            from rest_framework.exceptions import ValidationError
            role_id = data.get('role_id') or data.get('role')
            role_obj = None
            if role_id:
                try:
                    role_obj = Role.objects.using(db).filter(id=uuid.UUID(str(role_id))).first()
                except (ValueError, TypeError, AttributeError):
                    role_obj = Role.objects.using(db).filter(code=str(role_id)).first()
                if role_obj is None:
                    raise ValidationError({'role_id': 'Select a valid role.'})

            branch_access = data.get('branch_access')
            resolved_access = []
            if branch_access is not None:
                if not role_obj or role_obj.scope != 'BRANCH' or not role_obj.is_active or role_obj.organization_id != instance.organization_id:
                    raise ValidationError({'branch_access': 'Select an active branch-scoped role.'})
                if not isinstance(branch_access, list):
                    raise ValidationError({'branch_access': 'Expected a list of branch access entries.'})
                seen = set()
                for entry in branch_access:
                    if not isinstance(entry, dict) or type(entry.get('enabled')) is not bool:
                        raise ValidationError({'branch_access': 'Each entry requires branch_id and enabled (true/false).'})
                    try:
                        branch_pk = uuid.UUID(str(entry.get('branch_id')))
                    except (ValueError, TypeError, AttributeError):
                        raise ValidationError({'branch_access': 'Invalid branch ID.'})
                    branch_obj = Branch.objects.using(db).filter(pk=branch_pk, organization_id=instance.organization_id).first()
                    if not branch_obj or branch_pk in seen:
                        raise ValidationError({'branch_access': 'Branch is missing or repeated.'})
                    if entry['enabled'] and branch_obj.status != 'ACTIVE':
                        raise ValidationError({'branch_access': 'Cannot enable access to an inactive branch.'})
                    self.validate_branch_scope(branch_pk)
                    seen.add(branch_pk)
                    resolved_access.append((branch_obj, entry['enabled']))
                if data.get('branch_id') or data.get('branch') or data.get('home_branch'):
                    raise ValidationError({'branch_access': 'Update home branch separately from branch access.'})

            branch_id = data.get('home_branch') or data.get('branch_id') or data.get('branch')
            br_obj = None
            if branch_id:
                try:
                    br_obj = Branch.objects.using(db).filter(id=uuid.UUID(str(branch_id))).first()
                except (ValueError, TypeError, AttributeError):
                    br_obj = Branch.objects.using(db).filter(name__iexact=str(branch_id)).first()
                if br_obj is None:
                    raise ValidationError({'branch_id': 'Select a valid branch.'})
                self.validate_branch_scope(br_obj.id)
            old_branch_id = instance.home_branch_id
            super().perform_update(serializer)

            if br_obj:
                instance.home_branch = br_obj
                instance.save(using=db, update_fields=['home_branch'])
                UserBranch.objects.using(db).filter(user=instance, scope_type='HOME').exclude(branch=br_obj).update(status='INACTIVE', is_active=False, is_primary=False)
                UserBranch.objects.using(db).update_or_create(
                    user=instance, branch=br_obj, scope_type='HOME',
                    defaults={'is_primary': True, 'relationship_type': 'PRIMARY', 'status': 'ACTIVE', 'is_active': True}
                )
                if not role_obj:
                    # Move home-branch access; retain separately assigned additional branches.
                    RoleAssignment.objects.using(db).filter(
                        user=instance, is_active=True, role__scope='BRANCH', branch_id=old_branch_id,
                    ).update(branch=br_obj, scope_type='BRANCH')

            existing_role = role_obj and RoleAssignment.objects.using(db).filter(user=instance, role=role_obj, is_active=True).exists()
            if role_obj and branch_access is None and (br_obj or not existing_role):
                RoleAssignment.objects.using(db).filter(user=instance, is_active=True).update(is_active=False, status='INACTIVE')
                assigned_branch = instance.home_branch if role_obj.scope == 'BRANCH' else None
                RoleAssignment.objects.using(db).create(
                    organization=instance.organization, user=instance, role=role_obj,
                    branch=assigned_branch,
                    scope_type='BRANCH' if assigned_branch else 'ORGANIZATION',
                    status='ACTIVE', is_active=True,
                )

            if branch_access is not None:
                for branch_obj, enabled in resolved_access:
                    assignments = RoleAssignment.objects.using(db).filter(
                        user=instance, role=role_obj, branch=branch_obj,
                    )
                    assignment = assignments.order_by('-created_at').first()
                    assignments.update(is_active=False, status='INACTIVE')
                    if assignment is None:
                        assignment = RoleAssignment(organization=instance.organization,
                                                    user=instance, role=role_obj, branch=branch_obj)
                    assignment.is_active = enabled
                    assignment.status = 'ACTIVE' if enabled else 'INACTIVE'
                    assignment.scope_type = 'BRANCH'
                    assignment.save(using=db)

            # Update department if provided
            dept_id = data.get('department_id') or data.get('department')
            if dept_id:
                dept_obj = None
                try:
                    dept_obj = Department.objects.using(db).filter(id=dept_id).first()
                except Exception:
                    dept_obj = Department.objects.using(db).filter(code=dept_id).first()
                if dept_obj:
                    UserDepartment.objects.using(db).filter(user=instance, status='ACTIVE').update(status='INACTIVE')
                    UserDepartment.objects.using(db).create(
                        user=instance,
                        department=dept_obj,
                        is_primary=True,
                        status='ACTIVE'
                    )


    def perform_destroy(self, instance):
        """
        Lifecycle Deactivation instead of Physical Hard Delete.
        Preserves user row, UUID, historical audit logs, role assignments, bookings, and ledger entries.
        Revokes active sessions immediately.
        """
        db = self.get_db()
        from django.utils import timezone
        from apps.authentication.security import revoke_all_user_sessions
        from .audit import emit_audit_event, snapshot_model_state

        actor = self.request.user if hasattr(self.request, 'user') else None
        before_state = snapshot_model_state(instance)

        with transaction.atomic(using=db):
            instance.status = 'DEACTIVATED'
            instance.is_login_allowed = False
            instance.deactivated_at = timezone.now()
            if actor and hasattr(actor, 'id') and getattr(actor, '_auth_type', None) == 'tenant':
                try:
                    actor_obj = TenantUser.objects.using(db).filter(id=actor.id).first()
                    instance.deactivated_by = actor_obj
                except Exception:
                    pass
            instance.deactivation_reason = self.request.data.get('reason') or 'Deactivated via user management'
            instance.save(using=db, update_fields=[
                'status', 'is_login_allowed', 'deactivated_at', 'deactivated_by', 'deactivation_reason', 'updated_at'
            ])

            # Revoke all active sessions and blacklist tokens
            try:
                revoke_all_user_sessions(
                    user_id=str(instance.id),
                    user_type='tenant',
                    actor_email=actor.email if (actor and hasattr(actor, 'email')) else None,
                    client_ip=self.request.META.get('REMOTE_ADDR'),
                    db_alias=db,
                )
            except Exception as e:
                logger.warning("Failed to revoke sessions for deactivated user %s: %s", instance.id, e)

            after_state = snapshot_model_state(instance)
            emit_audit_event(
                action='DEACTIVATE',
                resource_type='TenantUser',
                resource_id=str(instance.pk),
                request=self.request,
                instance=instance,
                before_state=before_state,
                after_state=after_state,
                db_alias=db,
                description=f"User {instance.email} deactivated (lifecycle preserved)."
            )

    @action(detail=True, methods=['post'], url_path='deactivate')
    def deactivate(self, request, pk=None):
        """Explicit POST action for lifecycle user deactivation."""
        user = self.get_object()
        self.perform_destroy(user)
        serializer = self.get_serializer(user)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='reactivate')
    def reactivate(self, request, pk=None):
        """Lifecycle user reactivation reusing the SAME TenantUser UUID."""
        user = self.get_object()
        db = self.get_db()
        from django.utils import timezone
        from apps.master.quota import QuotaChecker, QuotaExceededError
        from .audit import emit_audit_event, snapshot_model_state

        tenant_id = self.get_tenant_id()
        # Assert user quota before reactivating
        try:
            QuotaChecker.assert_quota_available(
                tenant_id=tenant_id,
                metric_code='ACTIVE_USERS',
                db_alias=db,
                requested_increment=1,
            )
        except QuotaExceededError as qe:
            return Response(
                {
                    'detail': f"Active user quota exceeded ({qe.current_usage}/{qe.limit_value}). Upgrade your plan to add more staff.",
                    'code': 'QUOTA_EXCEEDED',
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        before_state = snapshot_model_state(user)
        with transaction.atomic(using=db):
            user.status = 'ACTIVE'
            user.is_login_allowed = True
            user.activated_at = timezone.now()
            user.deactivated_at = None
            user.deactivated_by = None
            user.deactivation_reason = None
            user.save(using=db, update_fields=[
                'status', 'is_login_allowed', 'activated_at', 'deactivated_at', 'deactivated_by', 'deactivation_reason', 'updated_at'
            ])

            after_state = snapshot_model_state(user)
            emit_audit_event(
                action='REACTIVATE',
                resource_type='TenantUser',
                resource_id=str(user.pk),
                request=request,
                instance=user,
                before_state=before_state,
                after_state=after_state,
                db_alias=db,
                description=f"User {user.email} reactivated."
            )

        serializer = self.get_serializer(user)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='toggle-active')
    def toggle_active(self, request, pk=None):
        """Toggle user active / deactivated status."""
        user = self.get_object()
        if user.status == 'ACTIVE' or user.is_login_allowed:
            return self.deactivate(request, pk)
        else:
            return self.reactivate(request, pk)


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
    action_permission_map = {
        'update_permissions': 'core.roles.edit',
        'matrix': 'core.roles.view',
    }
    serializer_class = RoleSerializer
    queryset = Role.objects.all()

    def get_queryset(self):
        qs = super().get_queryset()
        include_inactive = self.request.query_params.get('include_inactive')
        if include_inactive in ('true', '1'):
            return qs
        return qs.filter(is_active=True)

    def perform_create(self, serializer):
        db = self.get_db()
        from .models_org import Organization
        from .models_rbac import RolePermissionSet
        org = Organization.objects.using(db).first()
        serializer.validated_data['organization'] = org
        super().perform_create(serializer)
        role = serializer.instance
        # Ensure default RolePermissionSet exists for the role
        RolePermissionSet.objects.using(db).get_or_create(
            role=role,
            defaults={
                'name': f"{role.name} Permissions",
                'organization': org,
                'status': 'ACTIVE',
            }
        )

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
            if 'is_active' in data and not data['is_active']:
                raise PermissionDenied('System roles cannot be deactivated.')
        super().perform_update(serializer)

    def perform_destroy(self, instance):
        if instance.is_system:
            raise PermissionDenied('System roles cannot be deleted.')
        db = self.get_db()
        from .models_rbac import RoleAssignment
        if RoleAssignment.objects.using(db).filter(role=instance, is_active=True).exists():
            raise PermissionDenied('Cannot delete role with active user assignments. Deactivate the role instead.')
        # Perform soft-delete by deactivating to preserve audit history
        instance.is_active = False
        instance.save(using=db)

    @action(detail=True, methods=['post', 'put'], url_path='update-permissions')
    def update_permissions(self, request, pk=None):
        """
        Atomic update of role's permissions and module/submodule access.
        Enforces core.roles.edit or core.permissions.manage.
        """
        role = self.get_object()
        db = self.get_db()

        if role.is_system:
            return Response(
                {'error': 'System roles cannot be modified.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        from .models_rbac import (
            RolePermissionSet, RolePermissionSetItem, Permission,
            RoleModuleAccess, RoleSubmoduleAccess, ModuleCatalog, SubmoduleCatalog
        )
        from .models_org import Organization
        from django.db import transaction

        org = role.organization or Organization.objects.using(db).first()
        permission_set, _ = RolePermissionSet.objects.using(db).get_or_create(
            role=role,
            defaults={
                'name': f"{role.name} Permissions",
                'organization': org,
                'status': 'ACTIVE',
            }
        )

        permissions_data = request.data.get('permissions', [])
        module_access_data = request.data.get('module_access', [])
        submodule_access_data = request.data.get('submodule_access', [])

        tenant_id = self.get_tenant_id()
        from apps.master.models_saas import TenantModule
        entitled_module_codes = set(
            TenantModule.objects.using('default').filter(
                tenant_id=tenant_id,
                is_enabled=True,
                status='ENABLED'
            ).values_list('module__code', flat=True)
        )
        # Always allow core module
        entitled_module_codes.add('core')
        entitled_module_codes = {c.lower() for c in entitled_module_codes if c}

        try:
            with transaction.atomic(using=db):
                # 1. Update module access if provided
                for item in module_access_data:
                    m_code = item.get('module_code')
                    if not m_code or m_code.lower() not in entitled_module_codes:
                        raise ValueError(f"Module code '{m_code}' is not entitled for this tenant subscription.")
                    can_access = bool(item.get('is_allowed', item.get('can_access', True)))
                    mod_obj = ModuleCatalog.objects.using(db).filter(module_code=m_code, is_enabled=True).first()
                    if not mod_obj:
                        raise ValueError(f"Module code '{m_code}' not found or not enabled in catalog.")
                    RoleModuleAccess.objects.using(db).update_or_create(
                        role=role,
                        module=mod_obj,
                        defaults={'permission_set': permission_set, 'can_access': can_access, 'is_visible': can_access}
                    )

                # 2. Update submodule access if provided
                for item in submodule_access_data:
                    sm_code = item.get('submodule_code')
                    m_code = item.get('module_code')
                    if m_code and m_code.lower() not in entitled_module_codes:
                        raise ValueError(f"Module code '{m_code}' for submodule '{sm_code}' is not entitled for this tenant subscription.")
                    can_access = bool(item.get('is_allowed', item.get('can_access', True)))
                    sm_filter = {'submodule_code': sm_code, 'module__is_enabled': True}
                    if m_code:
                        sm_filter['module__module_code'] = m_code
                    sm_obj = SubmoduleCatalog.objects.using(db).filter(**sm_filter).first()
                    if not sm_obj:
                        raise ValueError(f"Submodule code '{sm_code}' not found or module not enabled.")
                    if sm_obj.module.module_code.lower() not in entitled_module_codes:
                        raise ValueError(f"Module code '{sm_obj.module.module_code}' for submodule '{sm_code}' is not entitled for this tenant subscription.")
                    RoleSubmoduleAccess.objects.using(db).update_or_create(
                        role=role,
                        submodule=sm_obj,
                        defaults={'permission_set': permission_set, 'can_access': can_access, 'is_visible': can_access}
                    )

                # 3. Update permissions
                for item in permissions_data:
                    if isinstance(item, (str, uuid.UUID)):
                        p_id = str(item)
                        p_code = None
                        granted = True
                    elif isinstance(item, dict):
                        p_id = item.get('permission_id') or item.get('id')
                        p_code = item.get('permission_code') or item.get('code')
                        granted = bool(item.get('granted', item.get('is_granted', True)))
                    else:
                        continue
                    perm_obj = None
                    if p_id:
                        perm_obj = Permission.objects.using(db).filter(id=p_id, module__is_enabled=True).first()
                    elif p_code:
                        perm_obj = Permission.objects.using(db).filter(permission_code=p_code, module__is_enabled=True).first()
                    
                    if not perm_obj:
                        raise ValueError(f"Permission '{p_id or p_code}' not found or module not enabled.")

                    # Authoritative Master TenantModule check
                    perm_mod_code = perm_obj.module.module_code if perm_obj.module else ''
                    if perm_mod_code.lower() not in entitled_module_codes:
                        raise ValueError(f"Permission '{p_id or p_code}' belongs to unentitled module '{perm_mod_code}' (module is not entitled for this tenant subscription).")

                    # Also ensure the corresponding module and submodule are enabled for this role
                    if granted:
                        RoleModuleAccess.objects.using(db).update_or_create(
                            role=role,
                            module=perm_obj.module,
                            defaults={'permission_set': permission_set, 'can_access': True, 'is_visible': True}
                        )
                        RoleSubmoduleAccess.objects.using(db).update_or_create(
                            role=role,
                            submodule=perm_obj.submodule,
                            defaults={'permission_set': permission_set, 'can_access': True, 'is_visible': True}
                        )

                    RolePermissionSetItem.objects.using(db).update_or_create(
                        permission_set=permission_set,
                        permission=perm_obj,
                        defaults={'granted': granted}
                    )

            # Return refreshed role data
            serializer = self.get_serializer(role)
            return Response(serializer.data, status=status.HTTP_200_OK)
        except ValueError as ve:
            return Response({'error': str(ve)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


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
    """Tenant's enabled module catalog filtered by Master TenantModule entitlements."""
    permission_classes = [RequireActiveTenantAndOrg]
    serializer_class = ModuleCatalogSerializer
    queryset = ModuleCatalog.objects.all()

    def get_queryset(self):
        qs = super().get_queryset().filter(is_enabled=True)
        try:
            tenant_id = self.get_tenant_id()
            from apps.master.models_saas import TenantModule
            entitled_codes = set(
                TenantModule.objects.using('default').filter(
                    tenant_id=tenant_id,
                    is_enabled=True,
                    status='ENABLED'
                ).values_list('module__code', flat=True)
            )
            entitled_codes.add('core')
            codes_lower = [c.lower() for c in entitled_codes if c]
            qs = qs.filter(module_code__in=codes_lower)
        except Exception as e:
            logger.warning("Failed to filter ModuleCatalog by Master TenantModule: %s", e)
        return qs


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
    audit_action_map = {
        'create': 'CREATE',
        'update': 'UPDATE',
        'partial_update': 'UPDATE',
        'destroy': 'DELETE',
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

        tenant_id = self.get_tenant_id()
        from apps.master.models_saas import TenantModule
        entitled_module_codes = set(
            TenantModule.objects.using('default').filter(
                tenant_id=tenant_id,
                is_enabled=True,
                status='ENABLED'
            ).values_list('module__code', flat=True)
        )
        entitled_module_codes.add('core')
        entitled_module_codes = {c.lower() for c in entitled_module_codes if c}

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
                    if not m_code or m_code.lower() not in entitled_module_codes:
                        raise ValueError(f"Module code '{m_code}' is not entitled for this tenant subscription.")
                    can_access = bool(item.get('is_allowed', item.get('can_access', True)))
                    mod_obj = ModuleCatalog.objects.using(db).filter(module_code=m_code, is_enabled=True).first()
                    if not mod_obj:
                        raise ValueError(f"Module code '{m_code}' not found or not enabled in catalog.")
                    RoleModuleAccess.objects.using(db).update_or_create(
                        role=role,
                        module=mod_obj,
                        defaults={'permission_set': permission_set, 'can_access': can_access, 'is_visible': can_access}
                    )

                # 2. Update submodule access
                for item in submodule_access_data:
                    sm_code = item.get('submodule_code')
                    m_code = item.get('module_code')
                    if m_code and m_code.lower() not in entitled_module_codes:
                        raise ValueError(f"Module code '{m_code}' for submodule '{sm_code}' is not entitled for this tenant subscription.")
                    can_access = bool(item.get('is_allowed', item.get('can_access', True)))
                    sm_filter = {'submodule_code': sm_code, 'module__is_enabled': True}
                    if m_code:
                        sm_filter['module__module_code'] = m_code
                    sm_obj = SubmoduleCatalog.objects.using(db).filter(**sm_filter).first()
                    if not sm_obj:
                        raise ValueError(f"Submodule code '{sm_code}' not found or module not enabled.")
                    if sm_obj.module.module_code.lower() not in entitled_module_codes:
                        raise ValueError(f"Module code '{sm_obj.module.module_code}' for submodule '{sm_code}' is not entitled for this tenant subscription.")
                    RoleSubmoduleAccess.objects.using(db).update_or_create(
                        role=role,
                        submodule=sm_obj,
                        defaults={'permission_set': permission_set, 'can_access': can_access, 'is_visible': can_access}
                    )

                # 3. Update permission items
                for item in permissions_data:
                    p_code = item.get('permission_code')
                    granted = bool(item.get('is_granted', item.get('granted', True)))
                    perm_obj = Permission.objects.using(db).filter(permission_code=p_code, module__is_enabled=True).first()
                    if not perm_obj:
                        raise ValueError(f"Permission code '{p_code}' not found or module not enabled.")
                    if perm_obj.module.module_code.lower() not in entitled_module_codes:
                        raise ValueError(f"Permission code '{p_code}' belongs to unentitled module '{perm_obj.module.module_code}'.")
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
    """Tenant's enabled submodule catalog (read-only) filtered by Master TenantModule."""
    permission_classes = [TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'permissions'
    permission_prefix = 'core.permissions'
    serializer_class = SubmoduleCatalogSerializer
    queryset = SubmoduleCatalog.objects.all().select_related('module')

    def get_queryset(self):
        qs = super().get_queryset().filter(is_enabled=True, module__is_enabled=True)
        try:
            tenant_id = self.get_tenant_id()
            from apps.master.models_saas import TenantModule
            entitled_codes = set(
                TenantModule.objects.using('default').filter(
                    tenant_id=tenant_id,
                    is_enabled=True,
                    status='ENABLED'
                ).values_list('module__code', flat=True)
            )
            entitled_codes.add('core')
            codes_lower = [c.lower() for c in entitled_codes if c]
            qs = qs.filter(module__module_code__in=codes_lower)
        except Exception as e:
            logger.warning("Failed to filter SubmoduleCatalog by Master TenantModule: %s", e)

        mod_code = self.request.query_params.get('module') or self.request.query_params.get('module_code')
        if mod_code:
            qs = qs.filter(module__module_code=mod_code)
        return qs


class PermissionViewSet(TenantDBMixin, viewsets.ReadOnlyModelViewSet):
    """Tenant's canonical action permission catalog (read-only) filtered by Master TenantModule."""
    permission_classes = [TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'permissions'
    permission_prefix = 'core.permissions'
    serializer_class = PermissionSerializer
    queryset = Permission.objects.all().select_related('module', 'submodule')

    def get_queryset(self):
        qs = super().get_queryset().filter(is_active=True, module__is_enabled=True)
        try:
            tenant_id = self.get_tenant_id()
            from apps.master.models_saas import TenantModule
            entitled_codes = set(
                TenantModule.objects.using('default').filter(
                    tenant_id=tenant_id,
                    is_enabled=True,
                    status='ENABLED'
                ).values_list('module__code', flat=True)
            )
            entitled_codes.add('core')
            codes_lower = [c.lower() for c in entitled_codes if c]
            qs = qs.filter(module__module_code__in=codes_lower)
        except Exception as e:
            logger.warning("Failed to filter Permission by Master TenantModule: %s", e)

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
        'bulk_sync': 'core.settings.edit',
    }
    serializer_class = BranchWorkingHoursSerializer
    queryset = BranchWorkingHours.objects.all().select_related('branch')

    def get_queryset(self):
        qs = super().get_queryset()
        branch_id = self.request.query_params.get('branch') or self.request.query_params.get('branch_id')
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        return qs.order_by('day_of_week')

    @action(detail=False, methods=['post'], url_path='bulk-sync')
    def bulk_sync(self, request):
        """
        Synchronize 7-day weekly schedule for a branch in a single atomic transaction.
        """
        db = self.get_db()
        branch_id = request.data.get('branch_id') or request.data.get('branch')
        schedule = request.data.get('schedule', [])

        if not branch_id:
            return Response({'error': 'branch_id is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(schedule, list):
            return Response({'error': 'schedule must be a list of daily schedules.'}, status=status.HTTP_400_BAD_REQUEST)

        branch = Branch.objects.using(db).filter(id=branch_id).first()
        if not branch:
            return Response({'error': 'Branch not found.'}, status=status.HTTP_404_NOT_FOUND)

        user = request.user if hasattr(request, 'user') and request.user.is_authenticated else None

        saved_items = []
        with transaction.atomic(using=db):
            for day_item in schedule:
                day_num = day_item.get('day_of_week')
                if not day_num or not (1 <= int(day_num) <= 7):
                    continue
                is_open = bool(day_item.get('is_open', True))
                is_24_hours = bool(day_item.get('is_24_hours', False))
                open_time = day_item.get('open_time') if is_open and not is_24_hours else None
                close_time = day_item.get('close_time') if is_open and not is_24_hours else None

                obj, created = BranchWorkingHours.objects.using(db).update_or_create(
                    branch=branch,
                    day_of_week=int(day_num),
                    defaults={
                        'is_open': is_open,
                        'is_24_hours': is_24_hours,
                        'open_time': open_time,
                        'close_time': close_time,
                        'updated_by': user,
                    }
                )
                if created and user:
                    obj.created_by = user
                    obj.save(using=db, update_fields=['created_by'])
                saved_items.append(obj)

        serializer = self.get_serializer(saved_items, many=True)
        return Response({'success': True, 'branch_id': str(branch.id), 'schedule': serializer.data})

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

    def get_queryset(self):
        qs = super().get_queryset()
        branch_id = self.request.query_params.get('branch') or self.request.query_params.get('branch_id')
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        return qs.order_by('exception_date')

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


