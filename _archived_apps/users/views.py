from django.db import models
from rest_framework import viewsets, permissions, status, filters
from rest_framework.decorators import action
from rest_framework.views import APIView
from rest_framework.response import Response
import uuid

from .models import User, RoleDefinition, PermissionDefinition, RolePermission, RoleScope
from .serializers import (
    UserSerializer, RoleDefinitionSerializer, PermissionDefinitionSerializer,
    RolePermissionSerializer, UserInviteSerializer
)
from apps.tenants.models import Tenant, Location
from .permissions import IsTenantAdminOrSuperAdmin, IsPlatformSuperAdmin


class UserViewSet(viewsets.ModelViewSet):
    """
    CRUD ViewSet for Staff and Organization Users.
    Enforces strict tenant isolation and Super Admin protections.
    """
    permission_classes = [permissions.IsAuthenticated, IsTenantAdminOrSuperAdmin]
    serializer_class = UserSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['first_name', 'last_name', 'email', 'phone', 'role']
    ordering_fields = ['date_joined', 'first_name', 'role', 'status']
    ordering = ['first_name', 'last_name']

    def get_queryset(self):
        user = self.request.user
        tenant_param = self.request.query_params.get('tenant')

        if user.is_platform_admin:
            if tenant_param and tenant_param not in ['all', '']:
                qs = User.objects.filter(tenant_id=tenant_param)
            else:
                qs = User.objects.all()
        else:
            qs = User.objects.filter(tenant=user.tenant)

        role = self.request.query_params.get('role')
        if role and role != 'all':
            qs = qs.filter(models.Q(role=role) | models.Q(role_definition__id=role) | models.Q(role_definition__code=role))

        location_id = self.request.query_params.get('location')
        if location_id and location_id != 'all':
            qs = qs.filter(allowed_locations__id=location_id)

        status_param = self.request.query_params.get('status')
        if status_param and status_param != 'all':
            qs = qs.filter(status=status_param)

        return qs.select_related('tenant', 'active_location', 'role_definition').prefetch_related('allowed_locations').distinct()

    def perform_create(self, serializer):
        user = self.request.user
        target_tenant_id = self.request.data.get('tenant_id') or self.request.data.get('tenant')
        
        if user.is_platform_admin and target_tenant_id and target_tenant_id not in ['platform', 'none', 'all', '']:
            tenant = Tenant.objects.filter(id=target_tenant_id).first()
        elif user.is_platform_admin:
            tenant = None
        else:
            tenant = user.tenant

        role_name = serializer.validated_data.get('role', 'Trainer')
        role_def = serializer.validated_data.get('role_definition')
        if not role_def:
            # Try to resolve RoleDefinition matching role name
            role_def = RoleDefinition.objects.filter(
                models.Q(tenant=tenant) | models.Q(tenant__isnull=True),
                name__iexact=role_name
            ).first()

        user_id = serializer.validated_data.get('id') or f"USR-{uuid.uuid4().hex[:6].upper()}"
        is_platform = (tenant is None) or ('Super' in role_name) or ('Platform' in role_name)
        is_staff = is_platform or ('Admin' in role_name) or ('Manager' in role_name)
        is_superuser = ('Super' in role_name)
        serializer.save(
            id=user_id,
            tenant=tenant,
            role_definition=role_def,
            is_staff=is_staff,
            is_superuser=is_superuser
        )

    def perform_update(self, serializer):
        target_user = self.get_object()
        req_user = self.request.user

        # Prevent non-superadmins from escalating to Super Admin
        if not req_user.is_platform_admin:
            new_role = serializer.validated_data.get('role', target_user.role)
            if 'Super' in new_role or (serializer.validated_data.get('role_definition') and serializer.validated_data['role_definition'].code == 'super_admin'):
                return Response({'detail': 'You cannot assign Super Admin privileges.'}, status=status.HTTP_403_FORBIDDEN)

        # Sync role_definition if role string was changed
        role_name = serializer.validated_data.get('role')
        role_def = serializer.validated_data.get('role_definition')
        if role_name and not role_def:
            role_def = RoleDefinition.objects.filter(
                models.Q(tenant=target_user.tenant) | models.Q(tenant__isnull=True),
                name__iexact=role_name
            ).first()
            if role_def:
                serializer.save(role_definition=role_def)
                return

        serializer.save()

    def destroy(self, request, *args, **kwargs):
        target_user = self.get_object()
        if target_user.id in ['USR-ADMIN', 'admin'] or (target_user.email == 'admin'):
            return Response(
                {'detail': 'Root Super Admin account is immutable and cannot be deleted.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if target_user.id == request.user.id:
            return Response(
                {'detail': 'You cannot delete your own active account.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=['post'], url_path='toggle-active')
    def toggle_active(self, request, pk=None):
        target_user = self.get_object()
        if target_user.id in ['USR-ADMIN', 'admin'] or target_user.email == 'admin':
            return Response(
                {'detail': 'Root Super Admin account cannot be deactivated.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        target_user.is_active = not target_user.is_active
        target_user.status = 'Active' if target_user.is_active else 'Inactive'
        target_user.save()
        return Response(UserSerializer(target_user).data)


class RoleDefinitionViewSet(viewsets.ModelViewSet):
    """
    CRUD ViewSet for RBAC Roles.
    Supports system templates and custom tenant roles.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = RoleDefinitionSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'code', 'description']
    ordering_fields = ['name', 'created_at', 'scope']
    ordering = ['scope', 'name']

    def get_queryset(self):
        user = self.request.user
        tenant_param = self.request.query_params.get('tenant')
        scope_param = self.request.query_params.get('scope')

        if user.is_platform_admin:
            qs = RoleDefinition.objects.all()
            if tenant_param and tenant_param != 'all':
                qs = qs.filter(models.Q(tenant_id=tenant_param) | models.Q(tenant__isnull=True))
        else:
            qs = RoleDefinition.objects.filter(
                models.Q(tenant=user.tenant) | models.Q(is_system=True)
            )

        if scope_param:
            qs = qs.filter(scope=scope_param)

        return qs.prefetch_related('permissions__permission', 'assigned_users', 'tenant')

    def perform_create(self, serializer):
        user = self.request.user
        name = serializer.validated_data.get('name', '')
        code = serializer.validated_data.get('code')
        if not code:
            code = name.lower().replace(' ', '_').replace('/', '_').replace('-', '_')

        target_tenant_id = self.request.data.get('tenant') or self.request.data.get('tenant_id')
        if user.is_platform_admin and target_tenant_id and target_tenant_id != 'platform':
            tenant = Tenant.objects.filter(id=target_tenant_id).first()
            scope = RoleScope.TENANT
        elif user.is_platform_admin and not target_tenant_id:
            tenant = None
            scope = serializer.validated_data.get('scope', RoleScope.PLATFORM)
        else:
            tenant = user.tenant
            scope = RoleScope.TENANT

        role_id = serializer.validated_data.get('id') or f"ROLE-{uuid.uuid4().hex[:6].upper()}"
        role = serializer.save(id=role_id, code=code, tenant=tenant, scope=scope, is_system=False)

        # Initialize RolePermission records for all existing permission definitions
        all_perms = PermissionDefinition.objects.all()
        requested_perms = self.request.data.get('permissions', [])
        granted_map = {}
        for p in requested_perms:
            if isinstance(p, dict):
                p_id = p.get('permission_id') or (p.get('permission', {}).get('id') if isinstance(p.get('permission'), dict) else None)
                if p_id:
                    granted_map[p_id] = p.get('granted', True)

        for perm in all_perms:
            RolePermission.objects.get_or_create(
                role=role,
                permission=perm,
                defaults={'granted': granted_map.get(perm.id, False)}
            )

    def destroy(self, request, *args, **kwargs):
        role = self.get_object()
        if role.is_system or role.code == 'super_admin':
            return Response(
                {'detail': 'Built-in system and platform roles are protected and cannot be deleted.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if role.assigned_users.count() > 0:
            return Response(
                {'detail': f'Cannot delete role "{role.name}" because {role.assigned_users.count()} user(s) are currently assigned to it.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=['post'], url_path='update-permissions')
    def update_permissions(self, request, pk=None):
        role = self.get_object()
        permissions_data = request.data.get('permissions', [])  # list of { permission_id, granted }
        
        for p in permissions_data:
            perm_id = p.get('permission_id') or (p.get('permission', {}).get('id') if isinstance(p.get('permission'), dict) else None)
            granted = p.get('granted', True)
            if perm_id:
                perm_obj = PermissionDefinition.objects.filter(id=perm_id).first()
                if perm_obj:
                    RolePermission.objects.update_or_create(
                        role=role,
                        permission=perm_obj,
                        defaults={'granted': granted}
                    )
        return Response(RoleDefinitionSerializer(role).data)


class PermissionDefinitionViewSet(viewsets.ReadOnlyModelViewSet):
    """
    List and retrieve all system permissions.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = PermissionDefinitionSerializer
    queryset = PermissionDefinition.objects.all().order_by('module', 'action')
    filter_backends = [filters.SearchFilter]
    search_fields = ['module', 'action', 'label', 'id']

    def get_queryset(self):
        qs = super().get_queryset()
        module = self.request.query_params.get('module')
        if module and module != 'all':
            qs = qs.filter(module=module)
        scope = self.request.query_params.get('scope')
        if scope:
            qs = qs.filter(scope=scope)
        return qs


class UserInviteView(APIView):
    """
    Invite a new team member with tenant scoping and role assignment.
    """
    permission_classes = [permissions.IsAuthenticated, IsTenantAdminOrSuperAdmin]

    def post(self, request):
        serializer = UserInviteSerializer(data=request.data)
        if serializer.is_valid():
            data = serializer.validated_data
            target_tenant_id = request.data.get('tenant_id') or request.data.get('tenant')
            
            if request.user.is_platform_admin and target_tenant_id and target_tenant_id not in ['platform', 'none', 'all', '']:
                tenant = Tenant.objects.filter(id=target_tenant_id).first()
            elif request.user.is_platform_admin:
                tenant = None
            else:
                tenant = request.user.tenant

            user_id = f"USR-{uuid.uuid4().hex[:6].upper()}"
            role_name = data.get('role', 'Trainer')
            
            role_def = RoleDefinition.objects.filter(
                models.Q(tenant=tenant) | models.Q(tenant__isnull=True),
                name__iexact=role_name
            ).first()

            password = data.get('password') or request.data.get('password') or uuid.uuid4().hex[:12]
            status_val = 'Active' if (data.get('password') or request.data.get('password')) else 'Invited'
            is_platform = (tenant is None) or ('Super' in role_name) or ('Platform' in role_name)
            is_staff = is_platform or ('Admin' in role_name) or ('Manager' in role_name)
            is_superuser = ('Super' in role_name)

            user = User.objects.create_user(
                id=user_id,
                email=data['email'],
                password=password,
                first_name=data['first_name'],
                last_name=data.get('last_name', ''),
                phone=data.get('phone', ''),
                role=role_name,
                role_definition=role_def,
                tenant=tenant,
                status=status_val,
                is_staff=is_staff,
                is_superuser=is_superuser
            )
            location_ids = data.get('location_ids', [])
            if location_ids:
                locations = Location.objects.filter(id__in=location_ids)
                user.allowed_locations.set(locations)
                user.active_location = locations.first()
                user.save()

            return Response({
                'user': UserSerializer(user).data,
                'message': f"User {user.email} created successfully ({status_val})"
            }, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
