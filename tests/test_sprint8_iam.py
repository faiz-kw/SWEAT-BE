"""
Sprint 8 Test Suite — Master & Tenant IAM / RBAC Schema Expansion & Verification
Tests cover:
- Master Platform IAM model expansions, constraints, and backward compatibility
- Tenant Operational IAM & Catalogs model expansions, constraints, and backward compatibility
- Avatar ingestion management command (migrate_tenant_user_avatars):
  SSRF protection, non-HTTPS rejection, image validation, S3 upload, File row creation, idempotency
- BranchModule dual resolution (module_id <-> module_code)
- Physical constraint rules (RESTRICT, NOT NULL, UNIQUE composite)
"""

import uuid
from io import BytesIO
from unittest.mock import patch, MagicMock
from django.test import TestCase, override_settings
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.master.models_iam import (
    PlatformUser, PlatformDepartment, PlatformUserDepartment,
    PlatformRole, PlatformModule, PlatformSubmodule,
    PlatformPermission, PlatformRoleModuleAccess,
    PlatformRoleSubmoduleAccess, PlatformRolePermission,
    PlatformUserRole
)
from apps.tenant_core.models_users import TenantUser, UserDepartment, UserBranch, Department
from apps.tenant_core.models_rbac import (
    Organization, Role, RolePermissionSet, RolePermissionSetItem,
    RoleAssignment, ModuleCatalog, SubmoduleCatalog, Permission,
    BranchModule, Branch, Location
)
from apps.tenant_core.models_infra import File
from apps.tenant_core.management.commands.migrate_tenant_user_avatars import is_private_ip
from config.routers import set_tenant_db_alias


class Sprint8MasterIAMTestCase(TestCase):
    """Test Master Platform IAM models and compatibility."""
    databases = {'default'}

    def setUp(self):
        self.user = PlatformUser.objects.create(
            email='iam_tester@performanceos.io',
            first_name='IAM',
            last_name='Tester',
            status='ACTIVE'
        )
        self.dept = PlatformDepartment.objects.create(
            code='ENG',
            name='Engineering',
            status='ACTIVE'
        )
        self.role = PlatformRole.objects.create(
            code='LEAD_ENG',
            name='Lead Engineer',
            department=self.dept,
            is_system=False,
            status='ACTIVE'
        )
        self.module = PlatformModule.objects.create(
            code='tenants_mgmt',
            name='Tenant Management',
            sort_order=10,
            status='ACTIVE'
        )
        self.submod = PlatformSubmodule.objects.create(
            module=self.module,
            code='onboarding',
            name='Onboarding',
            sort_order=1,
            status='ACTIVE'
        )
        self.perm = PlatformPermission.objects.create(
            module=self.module,
            submodule=self.submod,
            action='view',
            code='tenants_mgmt.onboarding.view'
        )

    def test_platform_user_department_compatibility(self):
        """Verify PlatformUserDepartment supports user/platform_user and joined_at/assigned_at."""
        ud = PlatformUserDepartment.objects.create(
            user=self.user,
            department=self.dept,
            is_primary=True,
            status='ACTIVE'
        )
        self.assertEqual(ud.platform_user, self.user)
        self.assertEqual(ud.user, self.user)
        self.assertEqual(ud.platform_user_id, self.user.id)
        self.assertEqual(ud.assigned_at, ud.joined_at)

        # Test querying via user
        found = PlatformUserDepartment.objects.filter(user=self.user).first()
        self.assertIsNotNone(found)
        self.assertEqual(found.id, ud.id)

    def test_platform_user_role_compatibility(self):
        """Verify PlatformUserRole supports user/platform_user and is_active/status."""
        ur = PlatformUserRole.objects.create(
            user=self.user,
            role=self.role,
            status='ACTIVE'
        )
        self.assertEqual(ur.platform_user, self.user)
        self.assertEqual(ur.user, self.user)
        self.assertTrue(ur.is_active)

        # Test query
        found = PlatformUserRole.objects.filter(user=self.user, role=self.role).first()
        self.assertIsNotNone(found)

    def test_platform_role_properties(self):
        """Verify PlatformRole is_system and is_system_role property bridge."""
        self.assertFalse(self.role.is_system_role)
        self.assertFalse(self.role.is_system)
        self.role.is_system = True
        self.assertTrue(self.role.is_system_role)

    def test_platform_module_sort_order(self):
        """Verify sort_order and display_order bridge."""
        self.assertEqual(self.module.display_order, 10)
        self.assertEqual(self.module.sort_order, 10)
        self.assertEqual(self.submod.display_order, 1)

    def test_platform_access_and_permissions(self):
        """Verify PlatformRoleModuleAccess, SubmoduleAccess, and RolePermission."""
        ma = PlatformRoleModuleAccess.objects.create(
            role=self.role,
            module=self.module,
            can_access=True
        )
        self.assertTrue(ma.is_visible)

        sa = PlatformRoleSubmoduleAccess.objects.create(
            role=self.role,
            submodule=self.submod,
            can_access=True
        )
        self.assertTrue(sa.is_visible)

        rp = PlatformRolePermission.objects.create(
            role=self.role,
            permission=self.perm,
            granted=True
        )
        self.assertTrue(rp.is_allowed)

        # Querying via granted and can_access
        self.assertEqual(PlatformRolePermission.objects.filter(role=self.role, granted=True).count(), 1)
        self.assertEqual(PlatformRoleModuleAccess.objects.filter(role=self.role, can_access=True).count(), 1)
        self.assertEqual(PlatformRoleSubmoduleAccess.objects.filter(role=self.role, can_access=True).count(), 1)


class Sprint8TenantIAMTestCase(TestCase):
    """Test Tenant Operational IAM & Catalog models, dual resolution, and constraints."""
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        self.org = Organization.objects.using('tenant_test').create(
            name='Sprint 8 Gym Org',
            code='SPRINT8_GYM',
            status='ACTIVE'
        )
        self.location = Location.objects.using('tenant_test').create(
            organization=self.org,
            code='LOC_FLAGSHIP',
            name='Flagship Location',
            city='Mumbai',
            status='ACTIVE'
        )
        self.branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.location,
            name='Flagship Gym',
            code='FLAGSHIP',
            status='ACTIVE'
        )
        self.user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='member@sprint8gym.com',
            first_name='Gym',
            last_name='Member',
            display_name='Gym Member',
            status='ACTIVE'
        )
        self.dept = Department.objects.using('tenant_test').create(
            organization=self.org,
            code='FITNESS',
            name='Fitness Operations',
            status='ACTIVE'
        )
        self.role = Role.objects.using('tenant_test').create(
            organization=self.org,
            code='FITNESS_COACH',
            name='Fitness Coach',
            is_system_role=False,
            status='ACTIVE'
        )
        self.mc = ModuleCatalog.objects.using('tenant_test').create(
            code='workouts',
            module_code='workouts',
            name='Workout Tracker',
            is_enabled=True,
            display_order=1,
            status='ACTIVE'
        )
        self.sc = SubmoduleCatalog.objects.using('tenant_test').create(
            module=self.mc,
            code='routines',
            submodule_code='routines',
            name='Routines',
            display_order=1,
            status='ACTIVE'
        )
        self.perm = Permission.objects.using('tenant_test').create(
            module=self.mc,
            submodule=self.sc,
            code='workouts.routines.view',
            action='view',
            status='ACTIVE'
        )

    def test_user_department_compatibility(self):
        """Verify UserDepartment joined_at/assigned_at compatibility."""
        ud = UserDepartment.objects.using('tenant_test').create(
            user=self.user,
            department=self.dept,
            is_primary=True,
            status='ACTIVE'
        )
        self.assertEqual(ud.assigned_at, ud.joined_at)
        found = UserDepartment.objects.using('tenant_test').filter(user=self.user).first()
        self.assertIsNotNone(found)

    def test_branch_module_dual_resolution(self):
        """Verify BranchModule auto-resolves module_id from module_code."""
        bm = BranchModule.objects.using('tenant_test').create(
            branch=self.branch,
            module_code='workouts',
            is_enabled=True
        )
        self.assertEqual(bm.module, self.mc)
        self.assertEqual(bm.module_id, self.mc.id)
        self.assertEqual(bm.module_code, 'workouts')

    def test_branch_module_composite_unique(self):
        """Verify composite unique constraint on (branch, module_code) or (branch_id, module_id)."""
        BranchModule.objects.using('tenant_test').create(
            branch=self.branch,
            module_code='workouts',
            is_enabled=True
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic(using='tenant_test'):
                BranchModule.objects.using('tenant_test').create(
                    branch=self.branch,
                    module_code='workouts',
                    is_enabled=True
                )

    def test_role_assignment_restrict_and_assigned_by(self):
        """Verify RoleAssignment RESTRICT and assigned_by_user_id db_column."""
        ra = RoleAssignment.objects.using('tenant_test').create(
            organization=self.org,
            user=self.user,
            role=self.role,
            scope_type='BRANCH',
            branch=self.branch,
            assigned_by=self.user,
            status='ACTIVE'
        )
        self.assertEqual(ra.assigned_by, self.user)
        self.assertEqual(ra.assigned_by_user_id, self.user.id)

        # Deleting branch should be RESTRICTed
        with self.assertRaises(IntegrityError):
            with transaction.atomic(using='tenant_test'):
                self.branch.delete()

    def tearDown(self):
        set_tenant_db_alias(None)


@override_settings(
    ZATA_S3_ACCESS_KEY_ID='test-access-key',
    ZATA_S3_SECRET_ACCESS_KEY='test-secret-key',
    ZATA_S3_BUCKET_NAME='test-bucket',
    ZATA_S3_REGION_NAME='us-east-1',
)
class Sprint8AvatarIngestionCommandTestCase(TestCase):
    """Test migrate_tenant_user_avatars management command safety and ingestion."""
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        self.org = Organization.objects.using('tenant_test').create(
            name='Avatar Org',
            code='AVATAR_ORG',
            status='ACTIVE'
        )
        self.user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='avatar_user@testfitness.com',
            first_name='Avatar',
            last_name='User',
            display_name='Avatar User',
            avatar_url='https://images.example.com/avatar.png',
            status='ACTIVE'
        )

    def tearDown(self):
        set_tenant_db_alias(None)

    def test_ssrf_ip_detection(self):
        """Verify private and loopback IPs are blocked."""
        self.assertTrue(is_private_ip('127.0.0.1'))
        self.assertTrue(is_private_ip('10.0.0.1'))
        self.assertTrue(is_private_ip('192.168.1.1'))
        self.assertTrue(is_private_ip('169.254.169.254'))
        self.assertTrue(is_private_ip('172.16.0.1'))
        self.assertFalse(is_private_ip('8.8.8.8'))
        self.assertFalse(is_private_ip('93.184.216.34'))

    @patch('apps.tenant_core.management.commands.migrate_tenant_user_avatars.socket.getaddrinfo')
    @patch('apps.tenant_core.management.commands.migrate_tenant_user_avatars.requests.Session.get')
    @patch('apps.tenant_core.storage.ZataS3StorageService.s3_client')
    def test_successful_avatar_ingestion(self, mock_s3_client, mock_get, mock_dns):
        """Test successful download, validation, S3 upload, and File row creation."""
        mock_dns.return_value = [(None, None, None, None, ('93.184.216.34', 443))]

        # Minimal valid 1x1 PNG bytes
        png_bytes = (
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
            b'\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc`\x00\x00\x00'
            b'\x02\x00\x01H\xaf\xa4q\x00\x00\x00\x00IEND\xaeB`\x82'
        )
        mock_resp = MagicMock()
        mock_resp.is_redirect = False
        mock_resp.status_code = 200
        mock_resp.headers = {'Content-Length': str(len(png_bytes))}
        mock_resp.iter_content.return_value = [png_bytes]
        mock_get.return_value = mock_resp

        mock_s3_client.put_object.return_value = {'ETag': '"mock-etag"'}

        call_command('migrate_tenant_user_avatars', tenant='tenant_test')

        self.user.refresh_from_db(using='tenant_test')
        self.assertIsNotNone(self.user.profile_file_id)

        # File record should exist
        file_obj = File.objects.using('tenant_test').filter(id=self.user.profile_file_id_id).first()
        self.assertIsNotNone(file_obj)
        self.assertEqual(file_obj.mime_type, 'image/png')
        self.assertEqual(file_obj.entity_type, 'TenantUser')
        self.assertEqual(file_obj.entity_id, self.user.id)
        # Legacy avatar_url preserved
        self.assertEqual(self.user.avatar_url, 'https://images.example.com/avatar.png')

    def test_ssrf_rejection(self):
        """Verify command rejects private network URLs."""
        self.user.avatar_url = 'http://169.254.169.254/latest/meta-data/'
        self.user.save(using='tenant_test')

        call_command('migrate_tenant_user_avatars', tenant='tenant_test')

        self.user.refresh_from_db(using='tenant_test')
        self.assertIsNone(self.user.profile_file_id)
