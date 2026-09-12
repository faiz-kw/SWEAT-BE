"""
Focused Unit Tests for Phase 7C: Zata.ai S3 Storage Service Adapter
Validates configuration, HTTPS transport, immutable key generation, tenant isolation,
presigned URLs, physical size and checksum verification, auto-purge on corruption,
and fail-closed error handling.
"""

import datetime
import uuid
from unittest import mock

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from apps.tenant_core.storage import (
    ALLOWED_ENTITY_TYPES,
    ChecksumMismatchError,
    FileSizeMismatchError,
    InvalidEntityError,
    InvalidFileMetadataError,
    StorageConfigurationError,
    StorageNotFoundError,
    StorageProviderError,
    TenantIsolationError,
    ZataS3StorageService,
)


class ZataS3StorageServiceTest(SimpleTestCase):
    """Unit tests for ZataS3StorageService using botocore Stubber."""

    def setUp(self):
        self.endpoint_url = "https://s3.zata.ai"
        self.access_key = "test-zata-key"
        self.secret_key = "test-zata-secret"
        self.bucket_name = "fitness-platform-private"
        self.region = "us-east-1"
        self.upload_expires = 900
        self.download_expires = 3600
        self.max_file_size = 50 * 1024 * 1024

        # Create base client and stubber
        self.client = boto3.client(
            's3',
            region_name=self.region,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            endpoint_url=self.endpoint_url,
            use_ssl=True,
            config=Config(signature_version='s3v4'),
        )
        self.stubber = Stubber(self.client)
        self.stubber.activate()

        self.service = ZataS3StorageService(
            endpoint_url=self.endpoint_url,
            access_key_id=self.access_key,
            secret_access_key=self.secret_key,
            bucket_name=self.bucket_name,
            region_name=self.region,
            upload_expires=self.upload_expires,
            download_expires=self.download_expires,
            max_file_size=self.max_file_size,
            s3_client=self.client,
        )

        self.tenant_a = uuid.uuid4()
        self.tenant_b = uuid.uuid4()
        self.file_id = uuid.uuid4()

    def tearDown(self):
        self.stubber.deactivate()

    # -----------------------------------------------------------------------
    # 1. Initialization & Configuration
    # -----------------------------------------------------------------------

    def test_service_initialization_with_valid_config(self):
        """Service initializes with expected parameters and HTTPS transport."""
        self.assertEqual(self.service.endpoint_url, self.endpoint_url)
        self.assertEqual(self.service.bucket_name, self.bucket_name)
        self.assertEqual(self.service.upload_expires, 900)
        self.assertEqual(self.service.download_expires, 3600)
        self.assertEqual(self.service.max_file_size, 50 * 1024 * 1024)
        self.assertTrue(self.service.use_ssl)

    def test_service_initialization_missing_credentials_fails(self):
        """Service fails closed when credentials or bucket are missing."""
        with self.assertRaises(StorageConfigurationError):
            ZataS3StorageService(
                endpoint_url="https://s3.zata.ai",
                access_key_id="",
                secret_access_key="",
                bucket_name="",
                s3_client=None,
            )

    def test_https_enforcement_rejects_http_endpoint(self):
        """Service rejects unencrypted HTTP endpoints."""
        with self.assertRaises(StorageConfigurationError) as ctx:
            ZataS3StorageService(
                endpoint_url="http://insecure-s3.example.com",
                access_key_id="key",
                secret_access_key="secret",
                bucket_name="bucket",
                s3_client=None,
            )
        self.assertIn("Insecure transport rejected", str(ctx.exception))

    # -----------------------------------------------------------------------
    # 2. Immutable Key Generation & Tenant Namespace Isolation
    # -----------------------------------------------------------------------

    def test_immutable_key_generation(self):
        """Server-side keys are strictly formatted as tenants/{tenant_uuid}/files/{file_uuid}."""
        key = self.service.generate_object_key(self.tenant_a, self.file_id)
        expected = f"tenants/{self.tenant_a}/files/{self.file_id}"
        self.assertEqual(key, expected)

    def test_key_generation_rejects_invalid_uuids(self):
        """Malformed UUIDs are rejected during key generation."""
        with self.assertRaises(InvalidFileMetadataError):
            self.service.generate_object_key("not-a-uuid", self.file_id)
        with self.assertRaises(InvalidFileMetadataError):
            self.service.generate_object_key(self.tenant_a, "../escape")

    def test_tenant_namespace_isolation_enforced(self):
        """Tenant A cannot access or validate Tenant B's object key."""
        key_b = self.service.generate_object_key(self.tenant_b, self.file_id)
        with self.assertRaises(TenantIsolationError) as ctx:
            self.service.validate_tenant_ownership(self.tenant_a, key_b)
        self.assertIn(f"does not belong to tenant {self.tenant_a}", str(ctx.exception))

    # -----------------------------------------------------------------------
    # 3. Entity Allowlist & Pre-Validation
    # -----------------------------------------------------------------------

    def test_entity_allowlist_enforced(self):
        """Allowed entities pass; unknown entities are strictly rejected."""
        for entity in ('USER_AVATAR', 'BRANDING_LOGO', 'MEMBER_DOC', 'INVOICE', 'GENERAL'):
            self.assertIn(entity, ALLOWED_ENTITY_TYPES)
            self.assertEqual(self.service.validate_entity_type(entity), entity)

        with self.assertRaises(InvalidEntityError):
            self.service.validate_entity_type("ARBITRARY_UNKNOWN_TYPE")

        with self.assertRaises(InvalidEntityError):
            self.service.validate_entity_type("")

    def test_upload_params_validation(self):
        """Pre-validation catches path traversals, invalid MIME, and negative/oversized files."""
        # Path traversal filename
        with self.assertRaises(InvalidFileMetadataError):
            self.service.validate_upload_params(
                original_filename="../../etc/passwd",
                mime_type="application/pdf",
                file_size=1024,
                entity_type="MEMBER_DOC",
                classification="INTERNAL",
            )

        # Invalid file size (<= 0)
        with self.assertRaises(InvalidFileMetadataError):
            self.service.validate_upload_params(
                original_filename="doc.pdf",
                mime_type="application/pdf",
                file_size=0,
                entity_type="MEMBER_DOC",
                classification="INTERNAL",
            )

        # Oversized file
        with self.assertRaises(InvalidFileMetadataError):
            self.service.validate_upload_params(
                original_filename="large.zip",
                mime_type="application/zip",
                file_size=60 * 1024 * 1024,
                entity_type="GENERAL",
                classification="INTERNAL",
            )

    # -----------------------------------------------------------------------
    # 4. Presigned Upload & Download URLs
    # -----------------------------------------------------------------------

    def test_presigned_put_generation_and_ttl(self):
        """Presigned PUT URL is generated with 900s TTL and correct method/key."""
        result = self.service.generate_presigned_upload(
            tenant_uuid=self.tenant_a,
            file_uuid=self.file_id,
            original_filename="profile.png",
            mime_type="image/png",
            file_size=2048,
            entity_type="USER_AVATAR",
            classification="INTERNAL",
        )
        self.assertEqual(result['method'], 'PUT')
        self.assertEqual(result['expires_in'], 900)
        self.assertEqual(result['bucket_name'], self.bucket_name)
        self.assertEqual(result['object_key'], f"tenants/{self.tenant_a}/files/{self.file_id}")
        self.assertTrue(result['upload_url'].startswith('https://'))
        self.assertIn('X-Amz-Expires=900', result['upload_url'])

    def test_presigned_get_generation_and_ttl(self):
        """Presigned GET URL is generated with 3600s TTL."""
        key = self.service.generate_object_key(self.tenant_a, self.file_id)
        url = self.service.generate_presigned_download(self.tenant_a, key)
        self.assertTrue(url.startswith('https://'))
        self.assertIn('X-Amz-Expires=3600', url)

    def test_presigned_get_rejects_cross_tenant_key(self):
        """Presigned GET rejects signing another tenant's object key."""
        key_b = self.service.generate_object_key(self.tenant_b, self.file_id)
        with self.assertRaises(TenantIsolationError):
            self.service.generate_presigned_download(self.tenant_a, key_b)

    # -----------------------------------------------------------------------
    # 5. Authoritative File Size & Checksum Verification
    # -----------------------------------------------------------------------

    def test_physical_content_length_verification_success(self):
        """Post-upload verification succeeds when physical size and ETag match."""
        key = self.service.generate_object_key(self.tenant_a, self.file_id)
        expected_size = 4096
        etag = '"098f6bcd4621d373cade4e832627b4f6"'
        client_md5 = '098f6bcd4621d373cade4e832627b4f6'

        response = {
            'ContentLength': expected_size,
            'ETag': etag,
            'ContentType': 'image/png',
            'LastModified': datetime.datetime.now(datetime.timezone.utc),
        }
        self.stubber.add_response(
            'head_object',
            response,
            {'Bucket': self.bucket_name, 'Key': key},
        )

        metadata = self.service.verify_uploaded_object(
            tenant_uuid=self.tenant_a,
            object_key=key,
            expected_size=expected_size,
            client_checksum=client_md5,
        )
        self.assertEqual(metadata['physical_size'], expected_size)
        self.assertEqual(metadata['etag'], client_md5)
        self.assertTrue(metadata['checksum_verified'])

    def test_size_mismatch_purges_object_and_raises(self):
        """Physical ContentLength mismatch immediately purges object from S3 and raises."""
        key = self.service.generate_object_key(self.tenant_a, self.file_id)
        declared_size = 1000
        physical_size = 2500  # Mismatch

        self.stubber.add_response(
            'head_object',
            {'ContentLength': physical_size, 'ETag': '"some-etag"'},
            {'Bucket': self.bucket_name, 'Key': key},
        )
        # Expect automatic deletion
        self.stubber.add_response(
            'delete_object',
            {},
            {'Bucket': self.bucket_name, 'Key': key},
        )

        with self.assertRaises(FileSizeMismatchError) as ctx:
            self.service.verify_uploaded_object(
                tenant_uuid=self.tenant_a,
                object_key=key,
                expected_size=declared_size,
            )
        self.assertIn("does not match declared size", str(ctx.exception))
        self.stubber.assert_no_pending_responses()

    def test_checksum_mismatch_purges_object_and_raises(self):
        """MD5 checksum mismatch immediately purges object and raises."""
        key = self.service.generate_object_key(self.tenant_a, self.file_id)
        expected_size = 512
        actual_etag = '"11111111111111111111111111111111"'
        client_md5 = '99999999999999999999999999999999'

        self.stubber.add_response(
            'head_object',
            {'ContentLength': expected_size, 'ETag': actual_etag},
            {'Bucket': self.bucket_name, 'Key': key},
        )
        self.stubber.add_response(
            'delete_object',
            {},
            {'Bucket': self.bucket_name, 'Key': key},
        )

        with self.assertRaises(ChecksumMismatchError) as ctx:
            self.service.verify_uploaded_object(
                tenant_uuid=self.tenant_a,
                object_key=key,
                expected_size=expected_size,
                client_checksum=client_md5,
            )
        self.assertIn("Checksum mismatch", str(ctx.exception))
        self.stubber.assert_no_pending_responses()

    def test_multipart_etag_detected_and_purged(self):
        """Multipart ETags with hyphens are detected and rejected for single-part PUT flow."""
        key = self.service.generate_object_key(self.tenant_a, self.file_id)
        multipart_etag = '"d41d8cd98f00b204e9800998ecf8427e-2"'

        self.stubber.add_response(
            'head_object',
            {'ContentLength': 1024, 'ETag': multipart_etag},
            {'Bucket': self.bucket_name, 'Key': key},
        )
        self.stubber.add_response(
            'delete_object',
            {},
            {'Bucket': self.bucket_name, 'Key': key},
        )

        with self.assertRaises(ChecksumMismatchError) as ctx:
            self.service.verify_uploaded_object(
                tenant_uuid=self.tenant_a,
                object_key=key,
                expected_size=1024,
                client_checksum="d41d8cd98f00b204e9800998ecf8427e",
            )
        self.assertIn("Multipart ETag", str(ctx.exception))
        self.stubber.assert_no_pending_responses()

    # -----------------------------------------------------------------------
    # 6. Object Existence & Deletion
    # -----------------------------------------------------------------------

    def test_object_exists_true_when_present(self):
        """object_exists returns True when S3 returns 200."""
        key = self.service.generate_object_key(self.tenant_a, self.file_id)
        self.stubber.add_response(
            'head_object',
            {'ContentLength': 100},
            {'Bucket': self.bucket_name, 'Key': key},
        )
        self.assertTrue(self.service.object_exists(self.tenant_a, key))
        self.stubber.assert_no_pending_responses()

    def test_object_exists_false_when_404(self):
        """object_exists returns False when S3 returns 404/NoSuchKey."""
        key = self.service.generate_object_key(self.tenant_a, self.file_id)
        self.stubber.add_client_error(
            'head_object',
            service_error_code='404',
            service_message='Not Found',
            http_status_code=404,
            expected_params={'Bucket': self.bucket_name, 'Key': key},
        )
        self.assertFalse(self.service.object_exists(self.tenant_a, key))
        self.stubber.assert_no_pending_responses()

    def test_delete_object_success(self):
        """delete_object deletes object using server-resolved key."""
        key = self.service.generate_object_key(self.tenant_a, self.file_id)
        self.stubber.add_response(
            'delete_object',
            {},
            {'Bucket': self.bucket_name, 'Key': key},
        )
        self.assertTrue(self.service.delete_object(self.tenant_a, key))
        self.stubber.assert_no_pending_responses()

    def test_delete_object_rejects_cross_tenant_key(self):
        """Tenant A cannot delete Tenant B's object."""
        key_b = self.service.generate_object_key(self.tenant_b, self.file_id)
        with self.assertRaises(TenantIsolationError):
            self.service.delete_object(self.tenant_a, key_b)

    def test_provider_error_handling_fail_closed(self):
        """Provider 500 raises StorageProviderError without leaking credentials."""
        key = self.service.generate_object_key(self.tenant_a, self.file_id)
        self.stubber.add_client_error(
            'head_object',
            service_error_code='InternalError',
            service_message='Internal Server Error',
            http_status_code=500,
            expected_params={'Bucket': self.bucket_name, 'Key': key},
        )
        with self.assertRaises(StorageProviderError) as ctx:
            self.service.verify_uploaded_object(self.tenant_a, key, 1024)
        self.assertIn("InternalError", str(ctx.exception))
        self.assertNotIn(self.secret_key, str(ctx.exception))
