"""
Views for Tenant File Subsystem (Sprint 7 Phase 7D).
Implements storage presign-upload, confirm-upload, presign-download,
and the File metadata CRUD API with canonical RBAC and Sprint 6 audit trail.
"""

import logging
import uuid
from typing import Optional

from django.db import transaction
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from config.routers import get_tenant_db_alias
from .audit import emit_audit_event, snapshot_model_state
from .models_infra import File
from .permissions import TenantRBACPermission
from .serializers_storage import (
    ConfirmUploadRequestSerializer,
    FileMetadataSerializer,
    PresignedUploadRequestSerializer,
)
from .storage import (
    ChecksumMismatchError,
    FileSizeMismatchError,
    StorageError,
    StorageNotFoundError,
    TenantIsolationError,
    ZataS3StorageService,
)

logger = logging.getLogger('apps.tenant_core.views_storage')


def resolve_tenant_id(request) -> uuid.UUID:
    """
    Safely resolves the authenticated tenant UUID from server-side context.
    Never accepts client-supplied query parameters or headers for tenant identity.
    """
    user = getattr(request, 'user', None)
    tenant_id_str = getattr(user, '_tenant_id', None)
    if tenant_id_str:
        try:
            return uuid.UUID(str(tenant_id_str))
        except (ValueError, AttributeError):
            pass

    # Resolve from active tenant datasource
    db_alias = get_tenant_db_alias()
    if db_alias:
        from apps.master.models_infra import TenantDataSource
        # Look up datasource matching tenant db or active datasource
        ds = TenantDataSource.objects.using('default').filter(
            status='ACTIVE'
        ).first()
        if ds and ds.tenant_id:
            return ds.tenant_id

    raise PermissionDenied("Active tenant context could not be resolved.")


def get_storage_service() -> ZataS3StorageService:
    """Factory helper to obtain an instance of ZataS3StorageService."""
    return ZataS3StorageService()


class StoragePresignUploadView(APIView):
    """
    POST /api/v1/tenant/storage/presign-upload/
    Generates a presigned S3 PUT upload URL and reserves a File record in active tenant DB.
    Requires permission: core.files.create
    """
    permission_classes = [TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'files'
    required_permission = 'core.files.create'

    def post(self, request, *args, **kwargs):
        serializer = PresignedUploadRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        tenant_id = resolve_tenant_id(request)
        db_alias = get_tenant_db_alias()
        if not db_alias:
            raise PermissionDenied("Tenant database context missing.")

        file_id = uuid.uuid4()
        try:
            storage = get_storage_service()
        except StorageError as exc:
            logger.error("Storage service configuration error: %s", exc)
            return Response(
                {'error': 'Storage configuration error', 'detail': 'Storage service is not configured.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        object_key = storage.generate_object_key(tenant_id, file_id)

        # Create reserved File record in active tenant DB (pending confirmation)
        file_record = File.objects.using(db_alias).create(
            id=file_id,
            object_key=object_key,
            bucket_name=storage.bucket_name,
            original_filename=data['original_filename'],
            mime_type=data['mime_type'],
            file_size=data['file_size'],
            checksum=data.get('checksum', ''),
            classification=data.get('classification', 'INTERNAL'),
            entity_type=data['entity_type'],
            entity_id=data.get('entity_id'),
            uploaded_by=request.user if hasattr(request.user, 'pk') else None,
            uploaded_at=timezone.now(),
            is_deleted=True,  # Inactive/pending until confirm-upload succeeds
            deleted_at=None,
        )

        presign_data = storage.generate_presigned_upload(
            tenant_uuid=tenant_id,
            file_uuid=file_id,
            original_filename=data['original_filename'],
            mime_type=data['mime_type'],
            file_size=data['file_size'],
            entity_type=data['entity_type'],
            classification=data.get('classification', 'INTERNAL'),
        )

        return Response(
            {
                'file_id': str(file_id),
                'upload_url': presign_data['upload_url'],
                'expires_in': presign_data['expires_in'],
                'object_key': object_key,
                'method': 'PUT',
            },
            status=status.HTTP_201_CREATED,
        )


class StorageConfirmUploadView(APIView):
    """
    POST /api/v1/tenant/storage/confirm-upload/
    Authoritative post-upload verification: calls head_object via ZataS3StorageService,
    validates physical ContentLength and ETag MD5 checksum, and marks File active.
    Requires permission: core.files.create
    """
    permission_classes = [TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'files'
    required_permission = 'core.files.create'

    def post(self, request, *args, **kwargs):
        serializer = ConfirmUploadRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        file_id = data['file_id']
        client_checksum = data.get('checksum', '')

        tenant_id = resolve_tenant_id(request)
        db_alias = get_tenant_db_alias()
        if not db_alias:
            raise PermissionDenied("Tenant database context missing.")

        try:
            file_record = File.objects.using(db_alias).get(id=file_id)
        except File.DoesNotExist:
            raise NotFound("File record not found in tenant database.")

        try:
            storage = get_storage_service()
        except StorageError as exc:
            logger.error("Storage configuration error: %s", exc)
            return Response(
                {'error': 'Storage configuration error', 'detail': 'Storage service is not configured.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # Enforce server-side tenant isolation
        try:
            storage.validate_tenant_ownership(tenant_id, file_record.object_key)
        except TenantIsolationError as exc:
            raise PermissionDenied(str(exc))

        # Perform authoritative physical size and checksum verification
        try:
            verified_metadata = storage.verify_uploaded_object(
                tenant_uuid=tenant_id,
                object_key=file_record.object_key,
                expected_size=file_record.file_size,
                client_checksum=client_checksum or file_record.checksum,
            )
        except (FileSizeMismatchError, ChecksumMismatchError, StorageNotFoundError) as exc:
            logger.warning(
                "Upload verification failed for file_id=%s: %s",
                file_id,
                exc,
            )
            with transaction.atomic(using=db_alias):
                file_record.deleted_at = timezone.now()
                file_record.save(using=db_alias)
            return Response(
                {'error': 'Upload verification failed', 'detail': str(exc)},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        except StorageError as exc:
            logger.error("Storage provider error during verification: %s", exc)
            return Response(
                {'error': 'Storage provider error', 'detail': 'Unable to verify uploaded object with storage provider.'},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # Transition to active in an atomic transaction and emit audit event
        with transaction.atomic(using=db_alias):
            before_state = snapshot_model_state(file_record)
            file_record.is_deleted = False
            file_record.deleted_at = None
            file_record.uploaded_at = timezone.now()
            if verified_metadata.get('etag'):
                file_record.checksum = verified_metadata['etag']
            file_record.save(using=db_alias)

            emit_audit_event(
                action='FILE_UPLOAD_CONFIRMED',
                resource_type='File',
                resource_id=str(file_record.id),
                request=request,
                instance=file_record,
                before_state=before_state,
                after_state=snapshot_model_state(file_record),
                description=f"File '{file_record.original_filename}' verified and activated in S3.",
                db_alias=db_alias,
            )

        return Response(
            FileMetadataSerializer(file_record).data,
            status=status.HTTP_200_OK,
        )


class StoragePresignDownloadView(APIView):
    """
    GET /api/v1/tenant/storage/presign-download/{id}/
    Generates a presigned S3 GET download URL for an active File.
    Requires permission: core.files.view
    """
    permission_classes = [TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'files'
    required_permission = 'core.files.view'

    def get(self, request, id=None, *args, **kwargs):
        if not id:
            raise ValidationError("File ID is required.")

        tenant_id = resolve_tenant_id(request)
        db_alias = get_tenant_db_alias()
        if not db_alias:
            raise PermissionDenied("Tenant database context missing.")

        try:
            file_record = File.objects.using(db_alias).get(id=id)
        except File.DoesNotExist:
            raise NotFound("File not found in tenant database.")

        if file_record.is_deleted or file_record.deleted_at is not None:
            raise NotFound("File not found or has been deleted.")

        try:
            storage = get_storage_service()
        except StorageError as exc:
            logger.error("Storage configuration error: %s", exc)
            return Response(
                {'error': 'Storage configuration error', 'detail': 'Storage service is not configured.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        try:
            storage.validate_tenant_ownership(tenant_id, file_record.object_key)
            download_url = storage.generate_presigned_download(tenant_id, file_record.object_key)
        except TenantIsolationError as exc:
            raise PermissionDenied(str(exc))
        except StorageError as exc:
            logger.error("Failed to generate download URL: %s", exc)
            return Response(
                {'error': 'Storage provider error', 'detail': 'Unable to generate download URL.'},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(
            {
                'file_id': str(file_record.id),
                'download_url': download_url,
                'expires_in': storage.download_expires,
                'original_filename': file_record.original_filename,
                'mime_type': file_record.mime_type,
                'file_size': file_record.file_size,
            },
            status=status.HTTP_200_OK,
        )


class FileViewSet(viewsets.ReadOnlyModelViewSet):
    """
    GET /api/v1/tenant/files/
    GET /api/v1/tenant/files/{id}/
    DELETE /api/v1/tenant/files/{id}/

    Tenant File Metadata CRUD API:
    - Lists active, confirmed files (excluding is_deleted=True).
    - Supports pagination, entity_type and classification filters, and filename search.
    - Soft-deletes files on DELETE and emits Sprint 6 audit event.
    Permissions:
      - list, retrieve -> core.files.view
      - destroy -> core.files.delete
    """
    permission_classes = [TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'files'
    permission_prefix = 'core.files'
    serializer_class = FileMetadataSerializer
    http_method_names = ['get', 'delete', 'head', 'options']

    def get_queryset(self):
        db_alias = get_tenant_db_alias()
        if not db_alias:
            return File.objects.none()

        qs = File.objects.using(db_alias).filter(is_deleted=False)

        # Approved filtering
        entity_type = self.request.query_params.get('entity_type')
        if entity_type:
            qs = qs.filter(entity_type=entity_type.strip())

        classification = self.request.query_params.get('classification')
        if classification:
            qs = qs.filter(classification=classification.strip().upper())

        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(original_filename__icontains=search.strip())

        return qs

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        db_alias = get_tenant_db_alias()

        with transaction.atomic(using=db_alias):
            before_state = snapshot_model_state(instance)
            instance.is_deleted = True
            instance.deleted_at = timezone.now()
            instance.save(using=db_alias)

            emit_audit_event(
                action='FILE_DELETED',
                resource_type='File',
                resource_id=str(instance.id),
                request=request,
                instance=instance,
                before_state=before_state,
                after_state=snapshot_model_state(instance),
                description=f"File '{instance.original_filename}' soft-deleted.",
                db_alias=db_alias,
            )

        return Response(status=status.HTTP_204_NO_CONTENT)
