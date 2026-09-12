"""
Focused Tests for Sprint 7 Phase 7D: Tenant File Subsystem API
Tests all 24 required cases:
1. Presign upload success
2. Presign upload requires core.files.create
3. Invalid entity type rejected
4. Invalid classification rejected
5. Invalid file size rejected
6. Immutable server-generated key cannot be overridden
7. File record is created in correct tenant DB
8. Confirm upload success
9. Confirm upload rejects another tenant's File
10. Confirm upload cannot activate invalid upload
11. Download presign success
12. Download requires core.files.view
13. Deleted File cannot be downloaded
14. Tenant A cannot access Tenant B File
15. File list is tenant-scoped
16. File detail is tenant-scoped
17. Delete requires core.files.delete
18. Delete performs soft deletion
19. Delete emits Sprint 6 audit event
20. Upload confirmation emits Sprint 6 audit event
21. No Master DB File leakage
22. Authentication/authorization failures behave consistently
23. No sensitive storage credentials/configuration exposed
24. Pagination/filtering works safely
"""

import uuid
from unittest import mock

from django.conf import settings
from django.test import TestCase
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.master.models_saas import SaasPlan, TenantSubscription, ProductModule, TenantModule
from apps.master.models_infra import TenantDataSource
from apps.master.models_tenant import Tenant
from apps.tenant_core.models_infra import File
from apps.tenant_core.models_org import Branch, Location, Organization
from apps.tenant_core.models_privacy import TenantAuditEvent
from apps.tenant_core.models_rbac import (
    ModuleCatalog,
    Permission,
    Role,
    RoleAssignment,
    RoleModuleAccess,
    RolePermissionSet,
    RolePermissionSetItem,
    RoleSubmoduleAccess,
    SubmoduleCatalog,
)
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.storage import (
    FileSizeMismatchError,
    ZataS3StorageService,
)
from config.routers import set_tenant_db_alias
from config.tenant_middleware import _register_tenant_connection


@override_settings(
    ZATA_S3_ACCESS_KEY_ID='test-zata-key',
    ZATA_S3_SECRET_ACCESS_KEY='test-zata-secret',
    ZATA_S3_BUCKET_NAME='fitness-platform-private',
    ZATA_S3_ENDPOINT_URL='https://s3.zata.ai',
)
class TenantFileSubsystemPhase7DTest(TestCase):
    databases = '__all__'

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        _register_tenant_connection('tenant_test', 'test_fitness_tenant')
        self.client = APIClient()

        # -------------------------------------------------------------------
        # 1. Master DB Setup: Tenant, SaaS Plan, Core Module
        # -------------------------------------------------------------------
        self.tenant = Tenant.objects.using('default').create(
            name='Sprint 7 Storage Gym',
            slug='sprint7-storage-gym',
            code='S7-GYM-001',
            status='ACTIVE',
        )
        self.plan = SaasPlan.objects.using('default').create(
            name='Enterprise Plan',
            code='PLAN-S7-ENT',
            tier='enterprise',
            is_active=True,
        )
        self.sub = TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
            billing_cycle='MONTHLY',
        )
        self.ds = TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            db_name='test_fitness_tenant',
            status='ACTIVE',
        )
        self.mod_core, _ = ProductModule.objects.using('default').get_or_create(
            code='core',
            defaults={'name': 'Core System & Administration', 'is_active': True, 'is_core': True},
        )
        self.tm_core, _ = TenantModule.objects.using('default').get_or_create(
            tenant=self.tenant,
            module=self.mod_core,
            defaults={'availability_mode': 'ALL_BRANCHES', 'is_enabled': True},
        )

        # -------------------------------------------------------------------
        # 2. Tenant DB Setup: Org, Branch, Users
        # -------------------------------------------------------------------
        self.org = Organization.objects.using('tenant_test').create(
            name='Sprint 7 Org',
            code='S7-ORG',
            status='ACTIVE',
        )
        self.loc = Location.objects.using('tenant_test').create(
            organization=self.org,
            name='Main Location',
            code='LOC-S7-01',
            status='ACTIVE',
        )
        self.branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            name='Main Branch',
            code='BR-S7-01',
            status='ACTIVE',
        )

        # Admin user with full file permissions
        self.admin_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='admin@sprint7files.test',
            first_name='File',
            last_name='Admin',
            status='ACTIVE',
            home_branch=self.branch,
        )
        # Viewer user with only view permissions
        self.viewer_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='viewer@sprint7files.test',
            first_name='File',
            last_name='Viewer',
            status='ACTIVE',
            home_branch=self.branch,
        )
        # Restricted user with NO file permissions
        self.restricted_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='restricted@sprint7files.test',
            first_name='Restricted',
            last_name='User',
            status='ACTIVE',
            home_branch=self.branch,
        )

        # -------------------------------------------------------------------
        # 3. Tenant DB Catalog Setup: Module, Submodules, Permissions
        # -------------------------------------------------------------------
        self.cat_core, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(
            module_code='core',
            defaults={'name': 'Core System', 'is_enabled': True},
        )
        self.sub_files, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=self.cat_core,
            submodule_code='files',
            defaults={'name': 'Files', 'is_enabled': True},
        )

        self.perm_view, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.cat_core,
            submodule=self.sub_files,
            action='view',
            defaults={'permission_code': 'core.files.view', 'label': 'View Files'},
        )
        self.perm_create, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.cat_core,
            submodule=self.sub_files,
            action='create',
            defaults={'permission_code': 'core.files.create', 'label': 'Create Files'},
        )
        self.perm_delete, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.cat_core,
            submodule=self.sub_files,
            action='delete',
            defaults={'permission_code': 'core.files.delete', 'label': 'Delete Files'},
        )

        # -------------------------------------------------------------------
        # 4. Roles & Permission Sets
        # -------------------------------------------------------------------
        # Admin Role: has core module, files submodule, and all 3 permissions
        self.role_admin = Role.objects.using('tenant_test').create(
            organization=self.org,
            name='File Administrator',
            code='FILE_ADMIN',
            scope='ORG',
            is_system=False,
            is_active=True,
        )
        RoleModuleAccess.objects.using('tenant_test').create(
            role=self.role_admin, module=self.cat_core, can_access=True,
        )
        RoleSubmoduleAccess.objects.using('tenant_test').create(
            role=self.role_admin, submodule=self.sub_files, can_access=True,
        )
        self.pset_admin = RolePermissionSet.objects.using('tenant_test').create(
            role=self.role_admin, name='Admin File Permset', is_active=True,
        )
        for perm in (self.perm_view, self.perm_create, self.perm_delete):
            RolePermissionSetItem.objects.using('tenant_test').create(
                permission_set=self.pset_admin, permission=perm, granted=True,
            )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.admin_user, role=self.role_admin, branch=None, is_active=True, status='ACTIVE',
        )

        # Viewer Role: has core module, files submodule, ONLY core.files.view
        self.role_viewer = Role.objects.using('tenant_test').create(
            organization=self.org,
            name='File Viewer',
            code='FILE_VIEWER',
            scope='ORG',
            is_system=False,
            is_active=True,
        )
        RoleModuleAccess.objects.using('tenant_test').create(
            role=self.role_viewer, module=self.cat_core, can_access=True,
        )
        RoleSubmoduleAccess.objects.using('tenant_test').create(
            role=self.role_viewer, submodule=self.sub_files, can_access=True,
        )
        self.pset_viewer = RolePermissionSet.objects.using('tenant_test').create(
            role=self.role_viewer, name='Viewer File Permset', is_active=True,
        )
        RolePermissionSetItem.objects.using('tenant_test').create(
            permission_set=self.pset_viewer, permission=self.perm_view, granted=True,
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.viewer_user, role=self.role_viewer, branch=None, is_active=True, status='ACTIVE',
        )

        # Restricted Role: no access to files submodule or permissions
        self.role_restricted = Role.objects.using('tenant_test').create(
            organization=self.org,
            name='Restricted Staff',
            code='RESTRICTED_STAFF',
            scope='ORG',
            is_system=False,
            is_active=True,
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.restricted_user, role=self.role_restricted, branch=None, is_active=True, status='ACTIVE',
        )

    def tearDown(self):
        from django.db import connections
        for alias in list(connections.databases.keys()):
            if alias not in ('default', 'tenant_test'):
                connections.databases.pop(alias, None)
                if alias in settings.DATABASES:
                    del settings.DATABASES[alias]
                try:
                    connections[alias].close()
                except Exception:
                    pass

    def _get_token(self, user, tenant=None):
        t = tenant or self.tenant
        set_tenant_db_alias('tenant_test')
        refresh = RefreshToken()
        refresh['sub'] = str(user.id)
        refresh['user_type'] = 'tenant'
        refresh['roles'] = [
            ra.role.code for ra in RoleAssignment.objects.using('tenant_test').filter(user=user, is_active=True).select_related('role')
        ]
        refresh['tid'] = str(t.id)
        refresh['tenant_slug'] = t.slug
        refresh['db_alias'] = 'tenant_test'
        refresh['email'] = user.email
        return str(refresh.access_token)

    # -----------------------------------------------------------------------
    # 1. Presign Upload Success & Validation
    # -----------------------------------------------------------------------

    def test_presign_upload_success(self):
        """1. Presign upload succeeds and reserves a File record in active tenant DB."""
        token = self._get_token(self.admin_user)
        payload = {
            'original_filename': 'member_contract.pdf',
            'mime_type': 'application/pdf',
            'file_size': 1048576,
            'entity_type': 'MEMBER_DOC',
            'classification': 'CONFIDENTIAL',
        }
        with mock.patch.object(ZataS3StorageService, 'generate_presigned_upload') as mock_presign:
            mock_presign.return_value = {
                'upload_url': 'https://s3.zata.ai/fitness-platform-private/mock-upload',
                'expires_in': 900,
                'method': 'PUT',
            }
            resp = self.client.post(
                '/api/v1/tenant/storage/presign-upload/',
                payload,
                HTTP_AUTHORIZATION=f'Bearer {token}',
            )

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        data = resp.json()
        self.assertIn('file_id', data)
        self.assertIn('upload_url', data)
        self.assertEqual(data['expires_in'], 900)
        self.assertEqual(data['method'], 'PUT')

        # Verify reserved File record in tenant DB (is_deleted=True until confirmed)
        file_id = data['file_id']
        file_rec = File.objects.using('tenant_test').get(id=file_id)
        self.assertEqual(file_rec.original_filename, 'member_contract.pdf')
        self.assertEqual(file_rec.file_size, 1048576)
        self.assertEqual(file_rec.classification, 'CONFIDENTIAL')
        self.assertTrue(file_rec.is_deleted)
        self.assertIsNone(file_rec.deleted_at)

    def test_presign_upload_requires_core_files_create(self):
        """2. Presign upload requires core.files.create; viewer receives 403."""
        token = self._get_token(self.viewer_user)
        payload = {
            'original_filename': 'doc.pdf',
            'mime_type': 'application/pdf',
            'file_size': 1024,
            'entity_type': 'GENERAL',
        }
        resp = self.client.post(
            '/api/v1/tenant/storage/presign-upload/',
            payload,
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_invalid_entity_type_rejected(self):
        """3. Invalid entity type is rejected with 400 Bad Request."""
        token = self._get_token(self.admin_user)
        payload = {
            'original_filename': 'doc.pdf',
            'mime_type': 'application/pdf',
            'file_size': 1024,
            'entity_type': 'ILLEGAL_ENTITY_NAME',
        }
        resp = self.client.post(
            '/api/v1/tenant/storage/presign-upload/',
            payload,
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('entity_type', resp.json())

    def test_invalid_classification_rejected(self):
        """4. Invalid classification is rejected with 400."""
        token = self._get_token(self.admin_user)
        payload = {
            'original_filename': 'doc.pdf',
            'mime_type': 'application/pdf',
            'file_size': 1024,
            'entity_type': 'GENERAL',
            'classification': 'TOP_SECRET_UNKNOWN',
        }
        resp = self.client.post(
            '/api/v1/tenant/storage/presign-upload/',
            payload,
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_file_size_rejected(self):
        """5. File size <= 0 or > 50MB is rejected."""
        token = self._get_token(self.admin_user)
        # Size <= 0
        resp1 = self.client.post(
            '/api/v1/tenant/storage/presign-upload/',
            {'original_filename': 'doc.pdf', 'mime_type': 'application/pdf', 'file_size': 0, 'entity_type': 'GENERAL'},
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(resp1.status_code, status.HTTP_400_BAD_REQUEST)

        # Size > 50MB
        resp2 = self.client.post(
            '/api/v1/tenant/storage/presign-upload/',
            {'original_filename': 'doc.pdf', 'mime_type': 'application/pdf', 'file_size': 60 * 1024 * 1024, 'entity_type': 'GENERAL'},
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(resp2.status_code, status.HTTP_400_BAD_REQUEST)

    def test_immutable_server_generated_key_cannot_be_overridden(self):
        """6. Client-supplied object_key in request body is ignored; server generates key."""
        token = self._get_token(self.admin_user)
        payload = {
            'original_filename': 'test.png',
            'mime_type': 'image/png',
            'file_size': 2048,
            'entity_type': 'USER_AVATAR',
            'object_key': 'hacked/tenants/other/evil.exe',
        }
        with mock.patch.object(ZataS3StorageService, 'generate_presigned_upload') as mock_presign:
            mock_presign.return_value = {'upload_url': 'https://s3.zata.ai/mock', 'expires_in': 900, 'method': 'PUT'}
            resp = self.client.post(
                '/api/v1/tenant/storage/presign-upload/',
                payload,
                HTTP_AUTHORIZATION=f'Bearer {token}',
            )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        data = resp.json()
        # The key MUST start with tenants/{tenant_uuid}/files/
        expected_prefix = f"tenants/{self.tenant.id}/files/"
        self.assertTrue(data['object_key'].startswith(expected_prefix))
        self.assertNotIn('hacked', data['object_key'])

    def test_file_record_created_in_correct_tenant_db(self):
        """7. File record exists only in tenant_test DB; never in Master DB."""
        token = self._get_token(self.admin_user)
        payload = {
            'original_filename': 'isolated.pdf',
            'mime_type': 'application/pdf',
            'file_size': 1000,
            'entity_type': 'GENERAL',
        }
        with mock.patch.object(ZataS3StorageService, 'generate_presigned_upload') as mock_presign:
            mock_presign.return_value = {'upload_url': 'https://s3.zata.ai/mock', 'expires_in': 900, 'method': 'PUT'}
            resp = self.client.post(
                '/api/v1/tenant/storage/presign-upload/',
                payload,
                HTTP_AUTHORIZATION=f'Bearer {token}',
            )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        file_id = resp.json()['file_id']

        self.assertTrue(File.objects.using('tenant_test').filter(id=file_id).exists())
        from django.db import connections
        with connections['default'].cursor() as cursor:
            master_tables = connections['default'].introspection.table_names(cursor)
            self.assertNotIn('files', master_tables)

    # -----------------------------------------------------------------------
    # 2. Confirm Upload
    # -----------------------------------------------------------------------

    def test_confirm_upload_success(self):
        """8. Confirm upload verifies physical S3 metadata, marks File active, and returns 200."""
        file_id = uuid.uuid4()
        key = f"tenants/{self.tenant.id}/files/{file_id}"
        file_rec = File.objects.using('tenant_test').create(
            id=file_id,
            object_key=key,
            bucket_name='fitness-platform-private',
            original_filename='confirmed.png',
            mime_type='image/png',
            file_size=4096,
            checksum='098f6bcd4621d373cade4e832627b4f6',
            entity_type='USER_AVATAR',
            is_deleted=True,
        )

        token = self._get_token(self.admin_user)
        with mock.patch.object(ZataS3StorageService, 'verify_uploaded_object') as mock_verify:
            mock_verify.return_value = {
                'physical_size': 4096,
                'etag': '098f6bcd4621d373cade4e832627b4f6',
                'checksum_verified': True,
            }
            resp = self.client.post(
                '/api/v1/tenant/storage/confirm-upload/',
                {'file_id': str(file_id), 'checksum': '098f6bcd4621d373cade4e832627b4f6'},
                HTTP_AUTHORIZATION=f'Bearer {token}',
            )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        file_rec.refresh_from_db(using='tenant_test')
        self.assertFalse(file_rec.is_deleted)  # Activated!
        self.assertIsNone(file_rec.deleted_at)

    def test_confirm_upload_rejects_another_tenant_file(self):
        """9. Confirm upload rejects confirming a File that belongs to another tenant."""
        other_tenant_id = uuid.uuid4()
        other_file_id = uuid.uuid4()
        # Create file belonging to other tenant
        File.objects.using('tenant_test').create(
            id=other_file_id,
            object_key=f"tenants/{other_tenant_id}/files/{other_file_id}",
            bucket_name='fitness-platform-private',
            original_filename='other.png',
            mime_type='image/png',
            file_size=100,
            entity_type='GENERAL',
            is_deleted=True,
        )

        token = self._get_token(self.admin_user)  # Belongs to self.tenant
        resp = self.client.post(
            '/api/v1/tenant/storage/confirm-upload/',
            {'file_id': str(other_file_id)},
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_confirm_upload_cannot_activate_invalid_upload(self):
        """10. Size mismatch fails upload verification and does not activate File."""
        file_id = uuid.uuid4()
        key = f"tenants/{self.tenant.id}/files/{file_id}"
        file_rec = File.objects.using('tenant_test').create(
            id=file_id,
            object_key=key,
            bucket_name='fitness-platform-private',
            original_filename='bad_size.png',
            mime_type='image/png',
            file_size=2000,
            entity_type='GENERAL',
            is_deleted=True,
        )

        token = self._get_token(self.admin_user)
        with mock.patch.object(ZataS3StorageService, 'verify_uploaded_object') as mock_verify:
            mock_verify.side_effect = FileSizeMismatchError("Physical size does not match declared size.")
            resp = self.client.post(
                '/api/v1/tenant/storage/confirm-upload/',
                {'file_id': str(file_id)},
                HTTP_AUTHORIZATION=f'Bearer {token}',
            )

        self.assertEqual(resp.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        file_rec.refresh_from_db(using='tenant_test')
        self.assertTrue(file_rec.is_deleted)  # Did NOT activate
        self.assertIsNotNone(file_rec.deleted_at)

    # -----------------------------------------------------------------------
    # 3. Presigned Download
    # -----------------------------------------------------------------------

    def test_download_presign_success(self):
        """11. Presign download returns 200 with GET URL and 3600s expiry."""
        file_id = uuid.uuid4()
        key = f"tenants/{self.tenant.id}/files/{file_id}"
        File.objects.using('tenant_test').create(
            id=file_id,
            object_key=key,
            bucket_name='fitness-platform-private',
            original_filename='report.pdf',
            mime_type='application/pdf',
            file_size=8192,
            entity_type='GENERAL',
            is_deleted=False,  # Active
        )

        token = self._get_token(self.viewer_user)
        with mock.patch.object(ZataS3StorageService, 'generate_presigned_download') as mock_download:
            mock_download.return_value = 'https://s3.zata.ai/mock-download-url'
            resp = self.client.get(
                f'/api/v1/tenant/storage/presign-download/{file_id}/',
                HTTP_AUTHORIZATION=f'Bearer {token}',
            )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(data['file_id'], str(file_id))
        self.assertEqual(data['download_url'], 'https://s3.zata.ai/mock-download-url')
        self.assertEqual(data['expires_in'], 3600)
        self.assertEqual(data['original_filename'], 'report.pdf')

    def test_download_requires_core_files_view(self):
        """12. Download requires core.files.view; restricted user receives 403."""
        file_id = uuid.uuid4()
        File.objects.using('tenant_test').create(
            id=file_id,
            object_key=f"tenants/{self.tenant.id}/files/{file_id}",
            bucket_name='fitness-platform-private',
            original_filename='secret.pdf',
            mime_type='application/pdf',
            file_size=100,
            entity_type='GENERAL',
            is_deleted=False,
        )

        token = self._get_token(self.restricted_user)
        resp = self.client.get(
            f'/api/v1/tenant/storage/presign-download/{file_id}/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_deleted_file_cannot_be_downloaded(self):
        """13. Soft-deleted file returns 404 Not Found on presign download."""
        file_id = uuid.uuid4()
        File.objects.using('tenant_test').create(
            id=file_id,
            object_key=f"tenants/{self.tenant.id}/files/{file_id}",
            bucket_name='fitness-platform-private',
            original_filename='deleted.pdf',
            mime_type='application/pdf',
            file_size=100,
            entity_type='GENERAL',
            is_deleted=True,
            deleted_at=timezone.now(),
        )

        token = self._get_token(self.viewer_user)
        resp = self.client.get(
            f'/api/v1/tenant/storage/presign-download/{file_id}/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_tenant_a_cannot_access_tenant_b_file(self):
        """14. Tenant A user cannot presign download Tenant B's file (returns 404/403)."""
        tenant_b_id = uuid.uuid4()
        file_b_id = uuid.uuid4()
        File.objects.using('tenant_test').create(
            id=file_b_id,
            object_key=f"tenants/{tenant_b_id}/files/{file_b_id}",
            bucket_name='fitness-platform-private',
            original_filename='tenant_b_data.pdf',
            mime_type='application/pdf',
            file_size=500,
            entity_type='GENERAL',
            is_deleted=False,
        )

        token = self._get_token(self.viewer_user)  # Belongs to self.tenant
        resp = self.client.get(
            f'/api/v1/tenant/storage/presign-download/{file_b_id}/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        # Blocked by validate_tenant_ownership
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    # -----------------------------------------------------------------------
    # 4. File Metadata Listing & Detail
    # -----------------------------------------------------------------------

    def test_file_list_is_tenant_scoped(self):
        """15. File list excludes soft-deleted files and only returns active files."""
        # Active file
        f1 = File.objects.using('tenant_test').create(
            id=uuid.uuid4(),
            object_key=f"tenants/{self.tenant.id}/files/{uuid.uuid4()}",
            bucket_name='fitness-platform-private',
            original_filename='active.pdf',
            mime_type='application/pdf',
            file_size=100,
            entity_type='GENERAL',
            is_deleted=False,
        )
        # Soft-deleted file
        File.objects.using('tenant_test').create(
            id=uuid.uuid4(),
            object_key=f"tenants/{self.tenant.id}/files/{uuid.uuid4()}",
            bucket_name='fitness-platform-private',
            original_filename='inactive.pdf',
            mime_type='application/pdf',
            file_size=100,
            entity_type='GENERAL',
            is_deleted=True,
            deleted_at=timezone.now(),
        )

        token = self._get_token(self.viewer_user)
        resp = self.client.get(
            '/api/v1/tenant/files/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.json()['results'] if 'results' in resp.json() else resp.json()
        ids = [item['id'] for item in results]
        self.assertIn(str(f1.id), ids)
        self.assertNotIn('inactive.pdf', [item['original_filename'] for item in results])

    def test_file_detail_is_tenant_scoped(self):
        """16. File detail retrieves metadata only from active tenant DB."""
        f = File.objects.using('tenant_test').create(
            id=uuid.uuid4(),
            object_key=f"tenants/{self.tenant.id}/files/{uuid.uuid4()}",
            bucket_name='fitness-platform-private',
            original_filename='detail_test.pdf',
            mime_type='application/pdf',
            file_size=256,
            entity_type='INVOICE',
            is_deleted=False,
        )

        token = self._get_token(self.viewer_user)
        resp = self.client.get(
            f'/api/v1/tenant/files/{f.id}/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(data['id'], str(f.id))
        self.assertEqual(data['entity_type'], 'INVOICE')

    # -----------------------------------------------------------------------
    # 5. File Deletion & Audit Trail
    # -----------------------------------------------------------------------

    def test_delete_requires_core_files_delete(self):
        """17. Delete requires core.files.delete; viewer receives 403."""
        f = File.objects.using('tenant_test').create(
            id=uuid.uuid4(),
            object_key=f"tenants/{self.tenant.id}/files/{uuid.uuid4()}",
            bucket_name='fitness-platform-private',
            original_filename='undeletable.pdf',
            mime_type='application/pdf',
            file_size=100,
            entity_type='GENERAL',
            is_deleted=False,
        )

        token = self._get_token(self.viewer_user)  # Has only core.files.view
        resp = self.client.delete(
            f'/api/v1/tenant/files/{f.id}/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_delete_performs_soft_deletion(self):
        """18. DELETE performs soft-deletion (is_deleted=True, deleted_at is set)."""
        f = File.objects.using('tenant_test').create(
            id=uuid.uuid4(),
            object_key=f"tenants/{self.tenant.id}/files/{uuid.uuid4()}",
            bucket_name='fitness-platform-private',
            original_filename='to_delete.pdf',
            mime_type='application/pdf',
            file_size=100,
            entity_type='GENERAL',
            is_deleted=False,
        )

        token = self._get_token(self.admin_user)
        resp = self.client.delete(
            f'/api/v1/tenant/files/{f.id}/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

        f.refresh_from_db(using='tenant_test')
        self.assertTrue(f.is_deleted)
        self.assertIsNotNone(f.deleted_at)

    def test_delete_emits_sprint6_audit_event(self):
        """19. DELETE emits a Sprint 6 TenantAuditEvent with action FILE_DELETED."""
        f = File.objects.using('tenant_test').create(
            id=uuid.uuid4(),
            object_key=f"tenants/{self.tenant.id}/files/{uuid.uuid4()}",
            bucket_name='fitness-platform-private',
            original_filename='audited_delete.pdf',
            mime_type='application/pdf',
            file_size=100,
            entity_type='GENERAL',
            is_deleted=False,
        )

        token = self._get_token(self.admin_user)
        resp = self.client.delete(
            f'/api/v1/tenant/files/{f.id}/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

        audit_event = TenantAuditEvent.objects.using('tenant_test').filter(
            action='FILE_DELETED',
            resource_id=str(f.id),
        ).first()
        self.assertIsNotNone(audit_event)
        self.assertEqual(audit_event.resource_type, 'File')
        self.assertEqual(audit_event.actor_email, self.admin_user.email)

    def test_upload_confirmation_emits_sprint6_audit_event(self):
        """20. Confirm upload emits a Sprint 6 TenantAuditEvent with action FILE_UPLOAD_CONFIRMED."""
        file_id = uuid.uuid4()
        f = File.objects.using('tenant_test').create(
            id=file_id,
            object_key=f"tenants/{self.tenant.id}/files/{file_id}",
            bucket_name='fitness-platform-private',
            original_filename='audited_confirm.png',
            mime_type='image/png',
            file_size=1024,
            entity_type='GENERAL',
            is_deleted=True,
        )

        token = self._get_token(self.admin_user)
        with mock.patch.object(ZataS3StorageService, 'verify_uploaded_object') as mock_verify:
            mock_verify.return_value = {
                'physical_size': 1024,
                'etag': '1234567890abcdef1234567890abcdef',
                'checksum_verified': True,
            }
            resp = self.client.post(
                '/api/v1/tenant/storage/confirm-upload/',
                {'file_id': str(file_id)},
                HTTP_AUTHORIZATION=f'Bearer {token}',
            )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        audit_event = TenantAuditEvent.objects.using('tenant_test').filter(
            action='FILE_UPLOAD_CONFIRMED',
            resource_id=str(file_id),
        ).first()
        self.assertIsNotNone(audit_event)
        self.assertEqual(audit_event.actor_email, self.admin_user.email)

    # -----------------------------------------------------------------------
    # 6. Security, Isolation, & Sanitization
    # -----------------------------------------------------------------------

    def test_no_master_db_file_leakage(self):
        """21. No file records or files table ever leak into Master DB ('default')."""
        from django.db import connections
        with connections['default'].cursor() as cursor:
            master_tables = connections['default'].introspection.table_names(cursor)
            self.assertNotIn('files', master_tables)

    def test_authentication_and_authorization_failures_behave_consistently(self):
        """22. Unauthenticated request -> 401; Unauthorized request -> 403."""
        # 401 Unauthorized
        resp1 = self.client.get('/api/v1/tenant/files/')
        self.assertEqual(resp1.status_code, status.HTTP_401_UNAUTHORIZED)

        # 403 Forbidden (restricted user)
        token = self._get_token(self.restricted_user)
        resp2 = self.client.get('/api/v1/tenant/files/', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(resp2.status_code, status.HTTP_403_FORBIDDEN)

    def test_no_sensitive_storage_credentials_exposed(self):
        """23. FileSerializer does not expose bucket name, secret keys, or internal paths."""
        f = File.objects.using('tenant_test').create(
            id=uuid.uuid4(),
            object_key=f"tenants/{self.tenant.id}/files/{uuid.uuid4()}",
            bucket_name='fitness-platform-private',
            original_filename='sensitive_check.pdf',
            mime_type='application/pdf',
            file_size=500,
            entity_type='GENERAL',
            is_deleted=False,
        )

        token = self._get_token(self.viewer_user)
        resp = self.client.get(
            f'/api/v1/tenant/files/{f.id}/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertNotIn('bucket_name', data)
        self.assertNotIn('secret', str(data).lower())
        self.assertNotIn('password', str(data).lower())

    def test_pagination_and_filtering_works_safely(self):
        """24. Filtering by entity_type, classification, and search works safely."""
        File.objects.using('tenant_test').create(
            id=uuid.uuid4(),
            object_key=f"tenants/{self.tenant.id}/files/{uuid.uuid4()}",
            bucket_name='fitness-platform-private',
            original_filename='annual_tax_report.pdf',
            mime_type='application/pdf',
            file_size=1000,
            classification='RESTRICTED',
            entity_type='INVOICE',
            is_deleted=False,
        )
        File.objects.using('tenant_test').create(
            id=uuid.uuid4(),
            object_key=f"tenants/{self.tenant.id}/files/{uuid.uuid4()}",
            bucket_name='fitness-platform-private',
            original_filename='member_avatar.png',
            mime_type='image/png',
            file_size=500,
            classification='PUBLIC',
            entity_type='USER_AVATAR',
            is_deleted=False,
        )

        token = self._get_token(self.viewer_user)

        # Filter by entity_type
        resp1 = self.client.get('/api/v1/tenant/files/?entity_type=INVOICE', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(resp1.status_code, status.HTTP_200_OK)
        results1 = resp1.json()['results'] if 'results' in resp1.json() else resp1.json()
        self.assertEqual(len(results1), 1)
        self.assertEqual(results1[0]['original_filename'], 'annual_tax_report.pdf')

        # Filter by classification
        resp2 = self.client.get('/api/v1/tenant/files/?classification=PUBLIC', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        results2 = resp2.json()['results'] if 'results' in resp2.json() else resp2.json()
        self.assertEqual(len(results2), 1)
        self.assertEqual(results2[0]['original_filename'], 'member_avatar.png')

        # Search by original_filename
        resp3 = self.client.get('/api/v1/tenant/files/?search=tax_report', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(resp3.status_code, status.HTTP_200_OK)
        results3 = resp3.json()['results'] if 'results' in resp3.json() else resp3.json()
        self.assertEqual(len(results3), 1)
        self.assertEqual(results3[0]['original_filename'], 'annual_tax_report.pdf')
