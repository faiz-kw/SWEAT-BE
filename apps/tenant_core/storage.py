"""
Zata.ai S3-Compatible Private Object Storage Adapter (Sprint 7 Phase 7C)
Provides secure, tenant-isolated, server-side verified object storage interactions.
"""

import logging
import os
import re
import uuid
from typing import Any, Dict, Optional, Union

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings

logger = logging.getLogger('apps.tenant_core.storage')

# Approved server-side allowlists
ALLOWED_ENTITY_TYPES = frozenset({
    'USER_AVATAR',
    'BRANDING_LOGO',
    'BRANDING_FAVICON',
    'MEMBER_DOC',
    'INVOICE',
    'EQUIPMENT_ATTACHMENT',
    'FACILITY_PHOTO',
    'GENERAL',
    'TenantUser',
    'Lead',
    'Member',
})

ALLOWED_CLASSIFICATIONS = frozenset({
    'PUBLIC',
    'INTERNAL',
    'CONFIDENTIAL',
    'RESTRICTED',
})

FILENAME_TRAVERSAL_REGEX = re.compile(r'[/\\.]\.')


class StorageError(Exception):
    """Base exception for all Zata S3 storage operations."""
    pass


class StorageConfigurationError(StorageError):
    """Raised when S3 credentials, endpoints, or required settings are missing or invalid."""
    pass


class StorageProviderError(StorageError):
    """Raised when the S3 provider returns an unexpected error or is unavailable."""
    pass


class StorageNotFoundError(StorageError):
    """Raised when the requested S3 object does not exist."""
    pass


class FileSizeMismatchError(StorageError):
    """Raised when the physical S3 ContentLength does not match the declared file size."""
    pass


class ChecksumMismatchError(StorageError):
    """Raised when the client-supplied checksum does not match the physical S3 ETag."""
    pass


class TenantIsolationError(StorageError):
    """Raised when a tenant attempts to access or construct keys outside their namespace."""
    pass


class InvalidEntityError(StorageError):
    """Raised when an unrecognized or unauthorized entity_type is supplied."""
    pass


class InvalidFileMetadataError(StorageError):
    """Raised when filename, MIME type, or size parameters fail validation."""
    pass


class ZataS3StorageService:
    """
    Dedicated production-grade storage adapter for Zata.ai S3-compatible private object storage.

    Guarantees:
    - HTTPS/TLS transport required for all communications.
    - Server-side generated immutable keys: tenants/{tenant_uuid}/files/{file_uuid}.
    - Strict tenant isolation: Tenant A cannot read, sign, or delete Tenant B's keys.
    - Entity allowlist enforcement without altering the S3 key structure.
    - Authoritative physical ContentLength verification via head_object().
    - Single-part MD5 ETag checksum verification against client-provided digest.
    - Automatic physical purge of corrupted or mismatched uploads.
    - Fail-closed error handling (no fallback to local disk or unvalidated buckets).
    """

    def __init__(
        self,
        endpoint_url: Optional[str] = None,
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
        bucket_name: Optional[str] = None,
        region_name: Optional[str] = None,
        upload_expires: Optional[int] = None,
        download_expires: Optional[int] = None,
        max_file_size: Optional[int] = None,
        s3_client: Optional[Any] = None,
    ):
        self.endpoint_url = endpoint_url or getattr(settings, 'ZATA_S3_ENDPOINT_URL', None) or os.getenv('ZATA_S3_ENDPOINT_URL', '')
        self.access_key_id = access_key_id or getattr(settings, 'ZATA_S3_ACCESS_KEY_ID', None) or os.getenv('ZATA_S3_ACCESS_KEY_ID', '')
        self.secret_access_key = secret_access_key or getattr(settings, 'ZATA_S3_SECRET_ACCESS_KEY', None) or os.getenv('ZATA_S3_SECRET_ACCESS_KEY', '')
        self.bucket_name = bucket_name or getattr(settings, 'ZATA_S3_BUCKET_NAME', None) or os.getenv('ZATA_S3_BUCKET_NAME', 'fitness-platform-private')
        self.region_name = region_name or getattr(settings, 'ZATA_S3_REGION_NAME', None) or os.getenv('ZATA_S3_REGION_NAME', 'us-east-1')
        self.upload_expires = upload_expires or getattr(settings, 'ZATA_S3_UPLOAD_EXPIRES', 900)
        self.download_expires = download_expires or getattr(settings, 'ZATA_S3_DOWNLOAD_EXPIRES', 3600)
        self.max_file_size = max_file_size or getattr(settings, 'ZATA_S3_MAX_FILE_SIZE', 50 * 1024 * 1024)
        self.use_ssl = getattr(settings, 'ZATA_S3_USE_SSL', True)

        # Enforce HTTPS/TLS transport
        if self.use_ssl and self.endpoint_url and self.endpoint_url.startswith('http://'):
            raise StorageConfigurationError(
                f"Insecure transport rejected: endpoint '{self.endpoint_url}' must use HTTPS/TLS."
            )

        if s3_client is not None:
            self._client = s3_client
        else:
            if not self.access_key_id or not self.secret_access_key or not self.bucket_name:
                raise StorageConfigurationError(
                    "Zata S3 storage configuration incomplete: access_key_id, secret_access_key, "
                    "and bucket_name must be configured."
                )

            client_kwargs: Dict[str, Any] = {
                'service_name': 's3',
                'aws_access_key_id': self.access_key_id,
                'aws_secret_access_key': self.secret_access_key,
                'region_name': self.region_name,
                'use_ssl': self.use_ssl,
                'config': Config(signature_version='s3v4'),
            }
            if self.endpoint_url:
                client_kwargs['endpoint_url'] = self.endpoint_url

            try:
                self._client = boto3.client(**client_kwargs)
            except Exception as exc:
                raise StorageConfigurationError(f"Failed to initialize boto3 S3 client: {exc}") from exc

    @property
    def s3_client(self) -> Any:
        return self._client

    # -----------------------------------------------------------------------
    # Key Generation & Tenant Isolation
    # -----------------------------------------------------------------------

    @staticmethod
    def clean_uuid(val: Union[str, uuid.UUID], param_name: str = 'identifier') -> str:
        """Validates and standardizes a UUID string."""
        if isinstance(val, uuid.UUID):
            return str(val).lower()
        if not isinstance(val, str):
            raise InvalidFileMetadataError(f"Invalid {param_name}: must be a UUID string or uuid.UUID instance.")
        try:
            return str(uuid.UUID(val.strip())).lower()
        except (ValueError, AttributeError) as exc:
            raise InvalidFileMetadataError(f"Invalid {param_name} '{val}': not a valid UUID format.") from exc

    def generate_object_key(
        self,
        tenant_uuid: Union[str, uuid.UUID],
        file_uuid: Union[str, uuid.UUID],
    ) -> str:
        """
        Generates an immutable, server-controlled object key.
        Format: tenants/{tenant_uuid}/files/{file_uuid}
        Clients have ZERO control over the bucket, prefix, or final object key.
        """
        clean_tenant = self.clean_uuid(tenant_uuid, 'tenant_uuid')
        clean_file = self.clean_uuid(file_uuid, 'file_uuid')
        return f"tenants/{clean_tenant}/files/{clean_file}"

    def validate_tenant_ownership(
        self,
        tenant_uuid: Union[str, uuid.UUID],
        object_key: str,
    ) -> str:
        """
        Enforces tenant isolation. Ensures that object_key begins with
        the tenant's dedicated namespace: tenants/{tenant_uuid}/files/{file_uuid}.
        """
        clean_tenant = self.clean_uuid(tenant_uuid, 'tenant_uuid')
        expected_prefix = f"tenants/{clean_tenant}/files/"
        if not object_key or not isinstance(object_key, str) or not object_key.startswith(expected_prefix):
            logger.warning(
                "Tenant isolation violation attempt: tenant=%s requested key=%s",
                clean_tenant,
                object_key,
            )
            raise TenantIsolationError(
                f"Tenant isolation error: object_key '{object_key}' does not belong to tenant {clean_tenant}."
            )

        suffix = object_key[len(expected_prefix):]
        # Validate that the remaining suffix is a valid UUID without traversal characters
        self.clean_uuid(suffix, 'file_uuid in object_key')
        return clean_tenant

    def validate_entity_type(self, entity_type: str) -> str:
        """
        Validates entity_type against approved server-side allowlist.
        Entity type does not modify the S3 object key.
        """
        if not entity_type or not isinstance(entity_type, str):
            raise InvalidEntityError("entity_type is required and must be a non-empty string.")
        trimmed = entity_type.strip()
        if trimmed not in ALLOWED_ENTITY_TYPES:
            raise InvalidEntityError(
                f"Invalid entity_type '{trimmed}'. Allowed types: {sorted(ALLOWED_ENTITY_TYPES)}"
            )
        return trimmed

    def validate_classification(self, classification: str) -> str:
        """Validates classification against approved server-side allowlist."""
        if not classification or not isinstance(classification, str):
            return 'INTERNAL'
        upper = classification.strip().upper()
        if upper not in ALLOWED_CLASSIFICATIONS:
            raise InvalidFileMetadataError(
                f"Invalid classification '{classification}'. Allowed choices: {sorted(ALLOWED_CLASSIFICATIONS)}"
            )
        return upper

    def validate_upload_params(
        self,
        original_filename: str,
        mime_type: str,
        file_size: int,
        entity_type: str,
        classification: str,
    ) -> None:
        """Pre-validation before generating a presigned upload URL."""
        if not original_filename or not isinstance(original_filename, str) or not original_filename.strip():
            raise InvalidFileMetadataError("original_filename must be a non-empty string.")

        clean_filename = os.path.basename(original_filename.strip())
        if not clean_filename or FILENAME_TRAVERSAL_REGEX.search(original_filename):
            raise InvalidFileMetadataError("original_filename contains invalid or directory traversal characters.")

        if not mime_type or not isinstance(mime_type, str) or '/' not in mime_type:
            raise InvalidFileMetadataError("mime_type must be a valid MIME type string (e.g. 'image/png').")

        if not isinstance(file_size, int) or file_size <= 0:
            raise InvalidFileMetadataError("file_size must be a positive integer in bytes.")

        if file_size > self.max_file_size:
            raise InvalidFileMetadataError(
                f"file_size ({file_size} bytes) exceeds maximum allowed size ({self.max_file_size} bytes)."
            )

        self.validate_entity_type(entity_type)
        self.validate_classification(classification)

    # -----------------------------------------------------------------------
    # Presigned Upload
    # -----------------------------------------------------------------------

    def generate_presigned_upload(
        self,
        tenant_uuid: Union[str, uuid.UUID],
        file_uuid: Union[str, uuid.UUID],
        original_filename: str,
        mime_type: str,
        file_size: int,
        entity_type: str,
        classification: str = 'INTERNAL',
    ) -> Dict[str, Any]:
        """
        Generates a presigned PUT URL with 900-second (15-minute) TTL.
        Pre-validates filename, MIME type, declared size, entity type, and classification.
        Note: Presigned PUT alone does NOT guarantee physical size enforcement;
        confirm_upload must call head_object to perform authoritative verification.
        """
        self.validate_upload_params(
            original_filename=original_filename,
            mime_type=mime_type,
            file_size=file_size,
            entity_type=entity_type,
            classification=classification,
        )

        object_key = self.generate_object_key(tenant_uuid, file_uuid)

        try:
            upload_url = self.s3_client.generate_presigned_url(
                ClientMethod='put_object',
                Params={
                    'Bucket': self.bucket_name,
                    'Key': object_key,
                    'ContentType': mime_type,
                },
                ExpiresIn=self.upload_expires,
                HttpMethod='PUT',
            )
        except (BotoCoreError, ClientError) as exc:
            logger.error("Failed to generate presigned upload URL: %s", exc)
            raise StorageProviderError("Failed to generate presigned upload URL from storage provider.") from exc

        return {
            'upload_url': upload_url,
            'object_key': object_key,
            'bucket_name': self.bucket_name,
            'method': 'PUT',
            'expires_in': self.upload_expires,
            'content_type': mime_type,
            'declared_file_size': file_size,
        }

    # -----------------------------------------------------------------------
    # Object Existence & Inspection
    # -----------------------------------------------------------------------

    def object_exists(self, tenant_uuid: Union[str, uuid.UUID], object_key: str) -> bool:
        """
        Safely verifies whether an object exists in S3 using head_object.
        Enforces tenant isolation on object_key.
        """
        self.validate_tenant_ownership(tenant_uuid, object_key)
        try:
            self.s3_client.head_object(Bucket=self.bucket_name, Key=object_key)
            return True
        except ClientError as exc:
            error_code = str(exc.response.get('Error', {}).get('Code', ''))
            if error_code in ('404', 'NoSuchKey', 'NotFound'):
                return False
            logger.error("Provider error checking object existence: %s", error_code)
            raise StorageProviderError(f"Storage provider error checking object: {error_code}") from exc
        except BotoCoreError as exc:
            logger.error("BotoCore error checking object existence: %s", exc)
            raise StorageProviderError("Storage provider communication failure.") from exc

    # -----------------------------------------------------------------------
    # Authoritative File Size & Checksum Verification
    # -----------------------------------------------------------------------

    def verify_uploaded_object(
        self,
        tenant_uuid: Union[str, uuid.UUID],
        object_key: str,
        expected_size: int,
        client_checksum: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Authoritative post-upload verification:
        1. Validates tenant namespace isolation.
        2. Calls head_object() to retrieve physical metadata.
        3. Compares physical ContentLength against declared expected_size.
        4. Verifies physical MD5 checksum against client_checksum if provided.
        5. On mismatch or failure: purges the object from S3 via delete_object()
           and raises FileSizeMismatchError or ChecksumMismatchError.
        """
        self.validate_tenant_ownership(tenant_uuid, object_key)

        try:
            head_response = self.s3_client.head_object(Bucket=self.bucket_name, Key=object_key)
        except ClientError as exc:
            error_code = str(exc.response.get('Error', {}).get('Code', ''))
            if error_code in ('404', 'NoSuchKey', 'NotFound'):
                raise StorageNotFoundError(f"Uploaded file not found in storage at key '{object_key}'.") from exc
            logger.error("S3 head_object error: %s", error_code)
            raise StorageProviderError(f"Storage provider error inspecting object: {error_code}") from exc
        except BotoCoreError as exc:
            logger.error("BotoCore error inspecting object: %s", exc)
            raise StorageProviderError("Storage provider communication failure.") from exc

        # 1. Authoritative Physical File Size Verification
        physical_size = head_response.get('ContentLength', 0)
        if physical_size != expected_size or physical_size <= 0:
            logger.warning(
                "File size mismatch: key=%s expected=%s physical=%s. Purging object.",
                object_key,
                expected_size,
                physical_size,
            )
            # Delete corrupted/mismatched object from S3
            self._safe_delete_object(object_key)
            raise FileSizeMismatchError(
                f"Physical file size ({physical_size} bytes) does not match declared size ({expected_size} bytes)."
            )

        if physical_size > self.max_file_size:
            logger.warning("File exceeds maximum allowed size: key=%s size=%s. Purging.", object_key, physical_size)
            self._safe_delete_object(object_key)
            raise FileSizeMismatchError(
                f"Physical file size ({physical_size} bytes) exceeds maximum limit ({self.max_file_size} bytes)."
            )

        # 2. Checksum Verification (Single-Part S3 PUT ETag is MD5 hex)
        raw_etag = head_response.get('ETag', '')
        normalized_etag = raw_etag.strip('"').strip().lower()
        checksum_verified = False

        if client_checksum and client_checksum.strip():
            normalized_client_checksum = client_checksum.strip().lower()

            # Inspect whether ETag represents a multipart upload (contains hyphen)
            if '-' in normalized_etag:
                logger.warning(
                    "Multipart ETag detected (%s) for key %s. Multipart ETags are not direct MD5 digests.",
                    normalized_etag,
                    object_key,
                )
                self._safe_delete_object(object_key)
                raise ChecksumMismatchError(
                    f"Multipart ETag '{normalized_etag}' cannot be validated against single-part MD5 checksum."
                )

            # Standard single-part S3 PUT ETag comparison
            if normalized_client_checksum != normalized_etag:
                logger.warning(
                    "Checksum mismatch for key %s: client=%s, physical_etag=%s. Purging object.",
                    object_key,
                    normalized_client_checksum,
                    normalized_etag,
                )
                self._safe_delete_object(object_key)
                raise ChecksumMismatchError(
                    f"Checksum mismatch: declared '{client_checksum}' does not match physical ETag '{normalized_etag}'."
                )
            checksum_verified = True

        return {
            'object_key': object_key,
            'physical_size': physical_size,
            'etag': normalized_etag,
            'content_type': head_response.get('ContentType', ''),
            'checksum_verified': checksum_verified,
            'last_modified': head_response.get('LastModified'),
        }

    # -----------------------------------------------------------------------
    # Object Deletion
    # -----------------------------------------------------------------------

    def _safe_delete_object(self, object_key: str) -> None:
        """Internal helper to delete an object without raising if deletion fails."""
        try:
            self.s3_client.delete_object(Bucket=self.bucket_name, Key=object_key)
        except Exception as exc:
            logger.error("Failed to delete object '%s' during cleanup: %s", object_key, exc)

    def delete_object(self, tenant_uuid: Union[str, uuid.UUID], object_key: str) -> bool:
        """
        Safely deletes an object from S3.
        Enforces tenant isolation; deletion uses server-resolved bucket and key only.
        """
        self.validate_tenant_ownership(tenant_uuid, object_key)
        try:
            self.s3_client.delete_object(Bucket=self.bucket_name, Key=object_key)
            logger.info("Deleted object key '%s' for tenant %s", object_key, tenant_uuid)
            return True
        except ClientError as exc:
            error_code = str(exc.response.get('Error', {}).get('Code', ''))
            logger.error("S3 delete_object failed: %s", error_code)
            raise StorageProviderError(f"Storage provider error deleting object: {error_code}") from exc
        except BotoCoreError as exc:
            logger.error("BotoCore error deleting object: %s", exc)
            raise StorageProviderError("Storage provider communication failure during deletion.") from exc

    # -----------------------------------------------------------------------
    # Presigned Download
    # -----------------------------------------------------------------------

    def generate_presigned_download(
        self,
        tenant_uuid: Union[str, uuid.UUID],
        object_key: str,
        expires_in: Optional[int] = None,
    ) -> str:
        """
        Generates a presigned GET URL with 3600-second (60-minute) TTL.
        Validates tenant ownership of the key before signing.
        """
        self.validate_tenant_ownership(tenant_uuid, object_key)
        ttl = expires_in or self.download_expires

        try:
            download_url = self.s3_client.generate_presigned_url(
                ClientMethod='get_object',
                Params={
                    'Bucket': self.bucket_name,
                    'Key': object_key,
                },
                ExpiresIn=ttl,
                HttpMethod='GET',
            )
            return download_url
        except (BotoCoreError, ClientError) as exc:
            logger.error("Failed to generate presigned download URL: %s", exc)
            raise StorageProviderError("Failed to generate presigned download URL from storage provider.") from exc
