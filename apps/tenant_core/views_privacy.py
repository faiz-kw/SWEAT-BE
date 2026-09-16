"""
apps/tenant_core/views_privacy.py — DRF ViewSets for Processing Purposes, Consents, and Privacy Requests.
"""

import uuid
import logging
from datetime import timedelta
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError, PermissionDenied

from config.routers import get_tenant_db_alias
from .models_privacy import ProcessingPurpose, ConsentRecord, PrivacyRequest
from .models_users import TenantUser
from .serializers_privacy import (
    ProcessingPurposeSerializer,
    ConsentRecordSerializer,
    ConsentWithdrawSerializer,
    PrivacyRequestSerializer,
    PrivacyRequestAdminActionSerializer,
)
from .permissions import TenantRBACPermission
from .audit import emit_audit_event

logger = logging.getLogger(__name__)


class ProcessingPurposeViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only view for documented data processing purposes.
    """
    serializer_class = ProcessingPurposeSerializer
    permission_classes = [TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    action_permission_map = {
        'list': 'core.settings.view',
        'retrieve': 'core.settings.view',
    }

    def get_queryset(self):
        db_alias = get_tenant_db_alias() or 'default'
        return ProcessingPurpose.objects.using(db_alias).filter(is_active=True).order_by('name')


class ConsentRecordViewSet(viewsets.ModelViewSet):
    """
    Manages tenant user consent lifecycle.
    Append-only: Updates create new records with status changes.
    """
    serializer_class = ConsentRecordSerializer
    permission_classes = [TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    action_permission_map = {
        'list': 'core.settings.view',
        'retrieve': 'core.settings.view',
        'create': 'core.settings.edit',
        'withdraw': 'core.settings.edit',
    }
    http_method_names = ['get', 'post', 'head', 'options']

    def get_queryset(self):
        db_alias = get_tenant_db_alias() or 'default'
        qs = ConsentRecord.objects.using(db_alias).select_related('purpose', 'user').order_by('-recorded_at', '-created_at')
        user_id = self.request.query_params.get('user_id')
        if user_id:
            qs = qs.filter(user_id=user_id)
        purpose_code = self.request.query_params.get('purpose_code')
        if purpose_code:
            qs = qs.filter(purpose__code=purpose_code)
        return qs

    def perform_create(self, serializer):
        db_alias = get_tenant_db_alias() or 'default'
        now = timezone.now()
        ip_addr = self.request.META.get('REMOTE_ADDR')

        consent = serializer.save(
            status='GRANTED',
            granted_at=now,
            recorded_at=now,
            ip_address=ip_addr,
        )

        emit_audit_event(
            action='CONSENT_GRANTED',
            resource_type='ConsentRecord',
            resource_id=consent.id,
            request=self.request,
            after_state={
                'user_id': str(consent.user_id),
                'purpose_id': str(consent.purpose_id),
                'status': consent.status,
            },
            description=f"User {consent.user.email} granted consent for purpose {consent.purpose.code}.",
            db_alias=db_alias,
        )

    @action(detail=False, methods=['post'], url_path='withdraw')
    def withdraw(self, request):
        """
        Withdraw consent for a user + purpose. Creates an append-only WITHDRAWN record.
        """
        db_alias = get_tenant_db_alias() or 'default'
        serializer = ConsentWithdrawSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        purpose_id = serializer.validated_data['purpose_id']
        user_id = serializer.validated_data.get('user_id') or getattr(request.user, 'id', None)

        if not user_id:
            raise ValidationError({'user_id': 'Target user is required for consent withdrawal.'})

        purpose = ProcessingPurpose.objects.using(db_alias).filter(id=purpose_id).first()
        if not purpose:
            raise ValidationError({'purpose_id': 'Processing purpose not found.'})

        if purpose.is_mandatory:
            raise ValidationError({'purpose_id': f"Cannot withdraw consent for mandatory purpose '{purpose.name}'."})

        target_user = TenantUser.objects.using(db_alias).filter(id=user_id).first()
        if not target_user:
            raise ValidationError({'user_id': 'User not found.'})

        now = timezone.now()
        withdrawn_record = ConsentRecord.objects.using(db_alias).create(
            user=target_user,
            purpose=purpose,
            status='WITHDRAWN',
            notice_version=purpose.notice_version,
            withdrawn_at=now,
            recorded_at=now,
            capture_source='WEB_FORM',
            ip_address=request.META.get('REMOTE_ADDR'),
            notes=serializer.validated_data.get('notes', ''),
        )

        emit_audit_event(
            action='CONSENT_WITHDRAWN',
            resource_type='ConsentRecord',
            resource_id=withdrawn_record.id,
            request=request,
            after_state={
                'user_id': str(target_user.id),
                'purpose_id': str(purpose.id),
                'status': 'WITHDRAWN',
            },
            description=f"User {target_user.email} withdrew consent for purpose {purpose.code}.",
            db_alias=db_alias,
        )

        return Response(
            ConsentRecordSerializer(withdrawn_record).data,
            status=status.HTTP_201_CREATED,
        )


class PrivacyRequestViewSet(viewsets.ModelViewSet):
    """
    Manages Data Subject Requests (DSR) under DPDP & GDPR.
    """
    serializer_class = PrivacyRequestSerializer
    permission_classes = [TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    action_permission_map = {
        'list': 'core.settings.view',
        'retrieve': 'core.settings.view',
        'create': 'core.settings.edit',
        'update': 'core.settings.edit',
        'partial_update': 'core.settings.edit',
        'execute': 'core.settings.edit',
        'reject': 'core.settings.edit',
    }
    http_method_names = ['get', 'post', 'patch', 'head', 'options']

    def get_queryset(self):
        db_alias = get_tenant_db_alias() or 'default'
        qs = PrivacyRequest.objects.using(db_alias).select_related('user', 'assigned_to').order_by('-created_at')
        user_id = self.request.query_params.get('user_id')
        if user_id:
            qs = qs.filter(user_id=user_id)
        req_type = self.request.query_params.get('request_type')
        if req_type:
            qs = qs.filter(request_type=req_type)
        status_filter = self.request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        return qs

    def perform_create(self, serializer):
        db_alias = get_tenant_db_alias() or 'default'
        now = timezone.now()
        due = now + timedelta(days=30)  # DPDP/GDPR standard statutory timeline

        instance = serializer.save(
            status='RECEIVED',
            received_at=now,
            due_at=due,
            due_date=due.date(),
        )

        emit_audit_event(
            action='PRIVACY_REQUEST_CREATED',
            resource_type='PrivacyRequest',
            resource_id=instance.id,
            request=self.request,
            after_state={
                'user_id': str(instance.user_id),
                'request_type': instance.request_type,
                'status': instance.status,
            },
            description=f"Data subject request {instance.request_type} submitted for user {instance.user.email}.",
            db_alias=db_alias,
        )

    @action(detail=True, methods=['post'], url_path='execute')
    def execute(self, request, pk=None):
        """
        Dispatches DSR execution asynchronously via Celery worker.
        """
        db_alias = get_tenant_db_alias() or 'default'
        instance = self.get_object()

        if instance.status == 'COMPLETED':
            return Response(
                {'error': 'REQUEST_ALREADY_COMPLETED', 'detail': 'This privacy request has already been completed.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        tenant_id = getattr(request, 'tenant_id', None) or getattr(request.user, 'tenant_id', None)
        if not tenant_id:
            return Response(
                {'error': 'MISSING_TENANT_CONTEXT', 'detail': 'Active tenant context is required for DSR execution.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from .tasks import process_dsr_export_task, process_dsr_erasure_task

        if instance.request_type in ('ACCESS', 'PORTABILITY'):
            task = process_dsr_export_task.delay(str(tenant_id), str(instance.id))
            task_name = 'process_dsr_export_task'
        elif instance.request_type == 'ERASURE':
            task = process_dsr_erasure_task.delay(str(tenant_id), str(instance.id))
            task_name = 'process_dsr_erasure_task'
        else:
            # Rectification, Restriction, Objection - marked resolved by admin action
            instance.status = 'COMPLETED'
            instance.completed_at = timezone.now()
            instance.resolution = request.data.get('resolution', 'Manual compliance workflow completed.')
            instance.save(using=db_alias, update_fields=['status', 'completed_at', 'resolution', 'updated_at'])
            return Response(PrivacyRequestSerializer(instance).data)

        emit_audit_event(
            action='PRIVACY_REQUEST_DISPATCHED',
            resource_type='PrivacyRequest',
            resource_id=instance.id,
            request=request,
            after_state={'status': 'IN_PROGRESS', 'celery_task_id': getattr(task, 'id', '')},
            description=f"Dispatched async {task_name} for request {instance.id}.",
            db_alias=db_alias,
        )

        instance.refresh_from_db()
        return Response({
            'status': instance.status,
            'celery_task_id': getattr(task, 'id', ''),
            'message': f"Privacy request {instance.request_type} dispatched for asynchronous execution.",
            'request': PrivacyRequestSerializer(instance).data,
        })

    @action(detail=True, methods=['post'], url_path='reject')
    def reject(self, request, pk=None):
        """
        Reject a privacy request with a lawful justification reason.
        """
        db_alias = get_tenant_db_alias() or 'default'
        instance = self.get_object()

        reason = request.data.get('reason') or request.data.get('rejection_reason')
        if not reason:
            raise ValidationError({'reason': 'A lawful rejection reason is required.'})

        instance.status = 'REJECTED'
        instance.rejection_reason = reason
        instance.resolution = f"Rejected: {reason}"
        instance.save(using=db_alias, update_fields=['status', 'rejection_reason', 'resolution', 'updated_at'])

        emit_audit_event(
            action='PRIVACY_REQUEST_REJECTED',
            resource_type='PrivacyRequest',
            resource_id=instance.id,
            request=request,
            after_state={'status': 'REJECTED', 'reason': reason},
            description=f"Privacy request {instance.id} rejected.",
            db_alias=db_alias,
        )

        return Response(PrivacyRequestSerializer(instance).data)
