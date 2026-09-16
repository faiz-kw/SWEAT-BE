"""
apps/tenant_core/views_audit_outbox.py — ViewSets for Layer 2 Module N
"""

from rest_framework import viewsets, filters
from django.db.models import Q
from config.routers import get_tenant_db_alias
from .models_audit_outbox import BusinessAuditEvent, IdempotencyRecord, DomainOutboxEvent, ReasonCode
from .serializers_audit_outbox import (
    BusinessAuditEventSerializer,
    IdempotencyRecordSerializer,
    DomainOutboxEventSerializer,
    ReasonCodeSerializer,
)
from .permissions import RequireActiveTenantAndOrg, TenantRBACPermission


def _get_request_db_alias(request):
    return (
        get_tenant_db_alias()
        or getattr(getattr(request, 'user', None), '_db_alias', None)
        or getattr(request, '_tenant_db_alias', None)
        or 'default'
    )


class BusinessAuditEventViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Append-only business audit trail for privileged administrators.
    """
    serializer_class = BusinessAuditEventSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'audit'
    required_permission = 'core.audit.view'
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['event_description', 'action_code', 'entity_type', 'session_id']
    ordering_fields = ['occurred_at', 'created_at']
    ordering = ['-occurred_at']

    def get_queryset(self):
        alias = _get_request_db_alias(self.request)
        org = getattr(self.request, 'organization', None)
        qs = BusinessAuditEvent.objects.using(alias).all()
        if org:
            qs = qs.filter(organization=org)
        module = self.request.query_params.get('module')
        if module:
            qs = qs.filter(module=module)
        entity_type = self.request.query_params.get('entity_type')
        if entity_type:
            qs = qs.filter(entity_type=entity_type)
        return qs


class IdempotencyRecordViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only introspection of idempotency lock status and response replays.
    """
    serializer_class = IdempotencyRecordSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'audit'
    required_permission = 'core.audit.view'
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['idempotency_key', 'operation_type']
    ordering = ['-created_at']

    def get_queryset(self):
        alias = _get_request_db_alias(self.request)
        org = getattr(self.request, 'organization', None)
        qs = IdempotencyRecord.objects.using(alias).all()
        if org:
            qs = qs.filter(organization=org)
        op_type = self.request.query_params.get('operation_type')
        if op_type:
            qs = qs.filter(operation_type=op_type)
        status_val = self.request.query_params.get('status')
        if status_val:
            qs = qs.filter(status=status_val)
        return qs


class DomainOutboxEventViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only inspection of transactional outbox dispatch status.
    """
    serializer_class = DomainOutboxEventSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'audit'
    required_permission = 'core.audit.view'
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['event_type', 'aggregate_type']
    ordering = ['-created_at']

    def get_queryset(self):
        alias = _get_request_db_alias(self.request)
        org = getattr(self.request, 'organization', None)
        qs = DomainOutboxEvent.objects.using(alias).all()
        if org:
            qs = qs.filter(organization=org)
        status_val = self.request.query_params.get('status')
        if status_val:
            qs = qs.filter(status=status_val)
        event_type = self.request.query_params.get('event_type')
        if event_type:
            qs = qs.filter(event_type=event_type)
        return qs


class ReasonCodeViewSet(viewsets.ModelViewSet):
    """
    Configurable reason codes for business operations (cancellations, refunds, freezes).
    """
    serializer_class = ReasonCodeSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'
    permission_action_map = {
        'create': 'core.settings.create',
        'update': 'core.settings.edit',
        'partial_update': 'core.settings.edit',
        'destroy': 'core.settings.delete',
    }
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['code', 'label', 'description']
    ordering = ['module', 'action_type', 'code']

    def get_queryset(self):
        alias = _get_request_db_alias(self.request)
        org = getattr(self.request, 'organization', None)
        qs = ReasonCode.objects.using(alias).all()
        if org:
            qs = qs.filter(Q(organization=org) | Q(organization__isnull=True))
        module = self.request.query_params.get('module')
        if module:
            qs = qs.filter(module=module)
        action_type = self.request.query_params.get('action_type')
        if action_type:
            qs = qs.filter(action_type=action_type)
        return qs

    def perform_create(self, serializer):
        alias = _get_request_db_alias(self.request)
        org = getattr(self.request, 'organization', None)
        serializer.save(organization=org)

