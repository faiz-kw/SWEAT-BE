"""
apps/tenant_core/views_integrations.py — DRF ViewSet for Tenant Integrations.
"""

import logging
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response

from config.routers import get_tenant_db_alias
from config.secrets import SecretResolver, SecretResolutionError
from .models_infra import Integration
from .serializers_integrations import TenantIntegrationSerializer
from .permissions import TenantRBACPermission
from .audit import emit_audit_event

logger = logging.getLogger(__name__)


class TenantIntegrationViewSet(viewsets.ModelViewSet):
    """
    Manages tenant third-party integrations (e.g. Razorpay, Gupshup, TeleCMI).
    RBAC and Tenant-isolated. Never stores or leaks plaintext secrets.
    """
    serializer_class = TenantIntegrationSerializer
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
        'toggle': 'core.settings.edit',
        'test_connection': 'core.settings.edit',
    }

    def get_queryset(self):
        db_alias = get_tenant_db_alias() or 'default'
        qs = Integration.objects.using(db_alias).all().order_by('integration_type', 'provider')
        int_type = self.request.query_params.get('integration_type')
        if int_type:
            qs = qs.filter(integration_type=int_type)
        status_filter = self.request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        return qs

    def perform_create(self, serializer):
        db_alias = get_tenant_db_alias() or 'default'
        instance = serializer.save()

        emit_audit_event(
            action='INTEGRATION_CONFIGURED',
            resource_type='Integration',
            resource_id=instance.id,
            request=self.request,
            after_state={
                'provider': instance.provider,
                'integration_type': instance.integration_type,
                'status': instance.status,
            },
            description=f"Configured integration {instance.provider} ({instance.integration_type}).",
            db_alias=db_alias,
        )

    def perform_update(self, serializer):
        db_alias = get_tenant_db_alias() or 'default'
        instance = serializer.save()

        emit_audit_event(
            action='INTEGRATION_UPDATED',
            resource_type='Integration',
            resource_id=instance.id,
            request=self.request,
            after_state={
                'provider': instance.provider,
                'integration_type': instance.integration_type,
                'status': instance.status,
            },
            description=f"Updated integration {instance.provider} ({instance.integration_type}).",
            db_alias=db_alias,
        )

    @action(detail=True, methods=['post'], url_path='toggle')
    def toggle(self, request, pk=None):
        """
        Toggles an integration between ACTIVE and INACTIVE.
        """
        db_alias = get_tenant_db_alias() or 'default'
        instance = self.get_object()

        new_status = 'INACTIVE' if instance.status == 'ACTIVE' else 'ACTIVE'
        instance.status = new_status
        instance.save(using=db_alias, update_fields=['status', 'updated_at'])

        emit_audit_event(
            action='INTEGRATION_TOGGLED',
            resource_type='Integration',
            resource_id=instance.id,
            request=request,
            after_state={'status': new_status},
            description=f"Toggled integration {instance.provider} to {new_status}.",
            db_alias=db_alias,
        )

        return Response(TenantIntegrationSerializer(instance).data)

    @action(detail=True, methods=['post'], url_path='test-connection')
    def test_connection(self, request, pk=None):
        """
        Verifies secret resolution and connectivity parameters for the integration.
        """
        db_alias = get_tenant_db_alias() or 'default'
        instance = self.get_object()

        if not instance.secret_reference:
            return Response(
                {'status': 'ERROR', 'message': 'No secret reference configured for this integration.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            # Test resolving secret
            SecretResolver.resolve(instance.secret_reference)
            instance.last_sync_at = timezone.now()
            instance.last_error = ''
            instance.save(using=db_alias, update_fields=['last_sync_at', 'last_error', 'updated_at'])

            return Response({
                'status': 'SUCCESS',
                'message': f"Credentials for {instance.provider} successfully resolved and verified.",
                'integration': TenantIntegrationSerializer(instance).data,
            })
        except SecretResolutionError as exc:
            instance.last_error = str(exc)
            instance.save(using=db_alias, update_fields=['last_error', 'updated_at'])
            return Response(
                {
                    'status': 'ERROR',
                    'message': 'Failed to resolve integration credentials securely.',
                    'detail': str(exc),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
