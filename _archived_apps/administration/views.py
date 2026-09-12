"""
ViewSets for Administration models: Service, TenantSettings, CustomForm, ApiKey, AuditLog, and SecurityPolicy.
"""

from rest_framework import viewsets, permissions, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
import uuid
import secrets
import hashlib

from apps.tenants.models import Tenant
from .models import Service, TenantSettings, CustomForm, ApiKey, AuditLog, SecurityPolicy
from .serializers import (
    ServiceSerializer, TenantSettingsSerializer, CustomFormSerializer,
    ApiKeySerializer, AuditLogSerializer, SecurityPolicySerializer
)


class ServiceViewSet(viewsets.ModelViewSet):
    """
    CRUD ViewSet for Gym Offerings and Amenities.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ServiceSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'category', 'description']
    ordering_fields = ['name', 'price', 'duration_minutes']
    ordering = ['category', 'name']

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser or (user.role and 'Super' in user.role):
            qs = Service.objects.all()
            tenant_id = self.request.query_params.get('tenant')
            if tenant_id:
                qs = qs.filter(tenant_id=tenant_id)
        else:
            qs = Service.objects.filter(tenant=user.tenant)
        
        category = self.request.query_params.get('category')
        if category:
            qs = qs.filter(category=category)
            
        location_id = self.request.query_params.get('location')
        if location_id and location_id != 'all':
            qs = qs.filter(location_id=location_id)

        return qs.select_related('location')

    def perform_create(self, serializer):
        user = self.request.user
        tenant = serializer.validated_data.get('tenant') or (user.tenant if hasattr(user, 'tenant') and user.tenant else Tenant.objects.first())
        service_id = serializer.validated_data.get('id') or f"SVC-{uuid.uuid4().hex[:6].upper()}"
        serializer.save(id=service_id, tenant=tenant)


class TenantSettingsViewSet(viewsets.ModelViewSet):
    """
    Get or Update global tenant settings and policies.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = TenantSettingsSerializer

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser or (user.role and 'Super' in user.role):
            return TenantSettings.objects.all()
        return TenantSettings.objects.filter(tenant=user.tenant)

    @action(detail=False, methods=['get', 'put', 'patch'], url_path='current')
    def current_settings(self, request):
        user = request.user
        tenant = user.tenant if hasattr(user, 'tenant') and user.tenant else Tenant.objects.first()
        settings_obj, _ = TenantSettings.objects.get_or_create(tenant=tenant)
        if request.method in ['PUT', 'PATCH']:
            serializer = TenantSettingsSerializer(settings_obj, data=request.data, partial=True)
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        return Response(TenantSettingsSerializer(settings_obj).data)


class CustomFormViewSet(viewsets.ModelViewSet):
    """
    CRUD ViewSet for Dynamic Forms, PAR-Q, and Waivers.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = CustomFormSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ['title', 'code', 'description']

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser or (user.role and 'Super' in user.role):
            return CustomForm.objects.all()
        return CustomForm.objects.filter(tenant=user.tenant)

    def perform_create(self, serializer):
        user = self.request.user
        tenant = serializer.validated_data.get('tenant') or (user.tenant if hasattr(user, 'tenant') and user.tenant else Tenant.objects.first())
        form_id = serializer.validated_data.get('id') or f"FORM-{uuid.uuid4().hex[:6].upper()}"
        serializer.save(id=form_id, tenant=tenant)


class ApiKeyViewSet(viewsets.ModelViewSet):
    """
    Generate and manage Developer API Keys and Webhooks.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ApiKeySerializer

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser or (user.role and 'Super' in user.role):
            return ApiKey.objects.all()
        return ApiKey.objects.filter(tenant=user.tenant)

    def perform_create(self, serializer):
        user = self.request.user
        raw_secret = f"pk_live_{secrets.token_urlsafe(32)}"
        key_hash = hashlib.sha256(raw_secret.encode()).hexdigest()
        key_id = f"KEY-{uuid.uuid4().hex[:6].upper()}"
        tenant = serializer.validated_data.get('tenant') or (user.tenant if hasattr(user, 'tenant') and user.tenant else Tenant.objects.first())
        
        instance = serializer.save(
            id=key_id,
            tenant=tenant,
            key_prefix=raw_secret[:12],
            key_hash=key_hash,
            webhook_secret=secrets.token_hex(16)
        )
        instance.raw_key = raw_secret # Return raw key only once upon creation


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Search and inspect immutable audit trail logs.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = AuditLogSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['user_email', 'module', 'entity_type', 'description']
    ordering = ['-created_at']

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser or (user.role and 'Super' in user.role):
            qs = AuditLog.objects.all()
        else:
            qs = AuditLog.objects.filter(tenant=user.tenant)

        action_param = self.request.query_params.get('action')
        if action_param:
            qs = qs.filter(action=action_param)

        module = self.request.query_params.get('module')
        if module:
            qs = qs.filter(module=module)

        return qs.select_related('user')


class SecurityPolicyViewSet(viewsets.ModelViewSet):
    """
    Security policies management (MFA, session timeout, lockout).
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = SecurityPolicySerializer

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser or (user.role and 'Super' in user.role):
            return SecurityPolicy.objects.all()
        return SecurityPolicy.objects.filter(tenant=user.tenant)

    @action(detail=False, methods=['get', 'put', 'patch'], url_path='current')
    def current_policy(self, request):
        user = request.user
        tenant = user.tenant if hasattr(user, 'tenant') and user.tenant else Tenant.objects.first()
        policy, _ = SecurityPolicy.objects.get_or_create(tenant=tenant)
        if request.method in ['PUT', 'PATCH']:
            serializer = SecurityPolicySerializer(policy, data=request.data, partial=True)
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        return Response(SecurityPolicySerializer(policy).data)
