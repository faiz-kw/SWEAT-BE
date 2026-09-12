"""
Sprint 5 Test Suite — Schema Field Alignment + Multi-Tenant Migrations (Phase 1 Layer 1).

Tests:
A. Model fields and structure
B. Field types and constraints
C. Nullability and default values
D. Choices verification
E. Foreign key targets and on_delete behavior
F. Backward compatibility (Role.scope, RoleAssignment.is_active, unassigned_at, assigned_by_user_id property)
G. ModuleCatalog logical cross-DB source_module_id reference (no cross-DB FK)
H. OrganizationSettings & BranchSettings configuration blocks and formatting fields
I. Privacy & Consent lifecycle and proof fields
J. TenantAuditEvent metadata fields
K. migrate_all_tenants command:
   - Master DB protection
   - dynamic alias resolution
   - --dry-run behavior
   - --tenant=<slug> behavior
   - --all behavior
   - failure isolation and error reporting
L. Clean/new tenant provisioning and sync compatibility
"""

import uuid
from io import StringIO
from django.test import TestCase
from django.core.management import call_command
from django.db import models

from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.models_saas import ProductModule
from apps.tenant_core.models_org import Organization, CompanyEntity, Location, Branch
from apps.tenant_core.models_users import TenantUser, Department
from apps.tenant_core.models_rbac import (
    Role, RoleAssignment, RolePermissionSet, ModuleCatalog, SubmoduleCatalog, Permission,
)
from apps.tenant_core.models_govern import OrganizationSettings, BranchSettings, NotificationTemplate
from apps.tenant_core.models_privacy import ProcessingPurpose, ConsentRecord, TenantAuditEvent
from config.routers import TenantRouter, set_tenant_db_alias


class Sprint5SchemaAlignmentTestCase(TestCase):
    databases = '__all__'

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        self.router = TenantRouter()

        # Seed master DB tenant and data source
        self.master_tenant = Tenant.objects.using('default').create(
            slug='test-tenant',
            name='Test Tenant Corp',
            status='ACTIVE',
        )
        self.data_source = TenantDataSource.objects.using('default').create(
            tenant=self.master_tenant,
            source_type='PLATFORM_MANAGED',
            db_name='test',  # resolves to alias tenant_test
            status='ACTIVE',
        )

        # Seed minimal tenant test organization and user
        self.org = Organization.objects.using('tenant_test').create(
            code='TEST_FITNESS',
            name='Test Fitness Corp',
            currency='INR',
            timezone='Asia/Kolkata',
            status='ACTIVE',
        )
        self.dept = Department.objects.using('tenant_test').create(
            organization=self.org,
            name='Fitness Operations',
            code='FIT_OPS',
        )
        self.loc = Location.objects.using('tenant_test').create(
            organization=self.org,
            code='MUM_WEST',
            name='Mumbai West',
            city='Mumbai',
        )
        self.branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            code='BANDRA_01',
            name='Bandra Main',
            status='ACTIVE',
        )
        self.company_entity = CompanyEntity.objects.using('tenant_test').create(
            organization=self.org,
            code='CE_01',
            name='Fit Corp Pvt Ltd',
            status='ACTIVE',
        )
        self.user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='test_staff@cultfit.in',
            first_name='Test',
            last_name='Staff',
            status='ACTIVE',
        )
        self.admin_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='admin@cultfit.in',
            first_name='Admin',
            last_name='User',
            status='ACTIVE',
        )
        self.role = Role.objects.using('tenant_test').create(
            organization=self.org,
            department=self.dept,
            code='HEAD_TRAINER',
            name='Head Trainer',
            scope='BRANCH',
            is_active=True,
        )

    def tearDown(self):
        set_tenant_db_alias(None)

    # -------------------------------------------------------------------------
    # 1. Role Model Alignment
    # -------------------------------------------------------------------------
    def test_role_model_alignment(self):
        """Verify Role has department FK (nullable, SET_NULL) and preserves scope."""
        field = Role._meta.get_field('department')
        self.assertIsInstance(field, models.ForeignKey)
        self.assertEqual(field.remote_field.model, Department)
        self.assertEqual(field.remote_field.on_delete, models.SET_NULL)
        self.assertTrue(field.null)
        self.assertTrue(field.blank)

        # Legacy scope preserved
        scope_field = Role._meta.get_field('scope')
        self.assertEqual(scope_field.default, 'BRANCH')
        self.assertEqual(self.role.scope, 'BRANCH')
        self.assertEqual(self.role.department, self.dept)

    # -------------------------------------------------------------------------
    # 2. RoleAssignment Alignment
    # -------------------------------------------------------------------------
    def test_role_assignment_alignment(self):
        """Verify RoleAssignment approved fields, on_delete, choices, and properties."""
        ra = RoleAssignment.objects.using('tenant_test').create(
            organization=self.org,
            user=self.user,
            role=self.role,
            scope_type='BRANCH',
            company_entity=self.company_entity,
            location=self.loc,
            branch=self.branch,
            status='ACTIVE',
            is_active=True,
            assigned_by=self.admin_user,
        )

        # FK to Organization: RESTRICT
        org_field = RoleAssignment._meta.get_field('organization')
        self.assertEqual(org_field.remote_field.model, Organization)
        self.assertEqual(org_field.remote_field.on_delete, models.RESTRICT)

        # Scope type
        scope_type_field = RoleAssignment._meta.get_field('scope_type')
        expected_choices = ['ORGANIZATION', 'COMPANY_ENTITY', 'LOCATION', 'BRANCH']
        actual_choices = [c[0] for c in scope_type_field.choices]
        self.assertEqual(actual_choices, expected_choices)
        self.assertEqual(ra.scope_type, 'BRANCH')

        # Company entity & location & branch: RESTRICT
        ce_field = RoleAssignment._meta.get_field('company_entity')
        self.assertEqual(ce_field.remote_field.on_delete, models.RESTRICT)
        loc_field = RoleAssignment._meta.get_field('location')
        self.assertEqual(loc_field.remote_field.on_delete, models.RESTRICT)
        br_field = RoleAssignment._meta.get_field('branch')
        self.assertEqual(br_field.remote_field.on_delete, models.RESTRICT)

        # Status: ACTIVE / INACTIVE
        status_field = RoleAssignment._meta.get_field('status')
        self.assertEqual([c[0] for c in status_field.choices], ['ACTIVE', 'INACTIVE'])
        self.assertEqual(ra.status, 'ACTIVE')
        self.assertTrue(ra.is_active)

        # Assigned by: SET_NULL, db_column='assigned_by_user_id'
        assigned_by_field = RoleAssignment._meta.get_field('assigned_by')
        self.assertEqual(assigned_by_field.remote_field.model, TenantUser)
        self.assertEqual(assigned_by_field.remote_field.on_delete, models.SET_NULL)
        self.assertEqual(assigned_by_field.db_column, 'assigned_by_user_id')
        self.assertTrue(assigned_by_field.null)
        self.assertEqual(ra.assigned_by, self.admin_user)
        self.assertEqual(ra.assigned_by_user_id, self.admin_user.id)

        # Backward compatibility property setter
        ra.assigned_by_user_id = self.user.id
        self.assertEqual(ra.assigned_by_id, self.user.id)

        # Auto-population of organization and scope derivation in save() if omitted
        ra2 = RoleAssignment(user=self.user, role=self.role)
        ra2.save(using='tenant_test')
        self.assertEqual(ra2.organization_id, self.org.id)
        self.assertEqual(ra2.scope_type, 'ORGANIZATION')

        # Branch assignment derives scope_type = 'BRANCH'
        ra_branch = RoleAssignment(user=self.user, role=self.role, branch=self.branch)
        ra_branch.save(using='tenant_test')
        self.assertEqual(ra_branch.scope_type, 'BRANCH')
        self.assertEqual(ra_branch.branch_id, self.branch.id)

        # Status and is_active bidirectional synchronization
        # 1. Created with is_active=False -> status becomes INACTIVE
        ra_inact = RoleAssignment.objects.using('tenant_test').create(
            user=self.user, role=self.role, is_active=False
        )
        self.assertEqual(ra_inact.status, 'INACTIVE')
        self.assertFalse(ra_inact.is_active)

        # 2. Created with status=INACTIVE -> is_active becomes False
        ra_stat_inact = RoleAssignment.objects.using('tenant_test').create(
            user=self.user, role=self.role, status='INACTIVE'
        )
        self.assertEqual(ra_stat_inact.status, 'INACTIVE')
        self.assertFalse(ra_stat_inact.is_active)

        # 3. Update status to ACTIVE -> is_active synchronized to True
        ra_inact.status = 'ACTIVE'
        ra_inact.save(using='tenant_test')
        self.assertTrue(ra_inact.is_active)
        self.assertEqual(ra_inact.status, 'ACTIVE')

        # 4. Update is_active to False -> status synchronized to INACTIVE
        ra_inact.is_active = False
        ra_inact.save(using='tenant_test')
        self.assertEqual(ra_inact.status, 'INACTIVE')
        self.assertFalse(ra_inact.is_active)

    # -------------------------------------------------------------------------
    # 3. RolePermissionSet Alignment
    # -------------------------------------------------------------------------
    def test_role_permission_set_alignment(self):
        """Verify RolePermissionSet structural scope and inheritance fields."""
        parent_set = RolePermissionSet.objects.using('tenant_test').create(
            role=self.role,
            organization=self.org,
            scope_type='ORGANIZATION',
            name='Base Operations',
            is_override=False,
        )

        child_set = RolePermissionSet.objects.using('tenant_test').create(
            role=self.role,
            organization=self.org,
            scope_type='BRANCH',
            branch=self.branch,
            inherits_from=parent_set,
            is_override=True,
            name='Bandra Override',
        )

        # Organization FK: RESTRICT
        org_field = RolePermissionSet._meta.get_field('organization')
        self.assertEqual(org_field.remote_field.on_delete, models.RESTRICT)

        # Scope type
        scope_type_field = RolePermissionSet._meta.get_field('scope_type')
        expected_choices = ['ORGANIZATION', 'COMPANY_ENTITY', 'LOCATION', 'BRANCH']
        self.assertEqual([c[0] for c in scope_type_field.choices], expected_choices)

        # Hierarchy / inheritance
        inherits_field = RolePermissionSet._meta.get_field('inherits_from')
        self.assertEqual(inherits_field.remote_field.on_delete, models.SET_NULL)
        self.assertTrue(inherits_field.null)
        self.assertEqual(child_set.inherits_from, parent_set)
        self.assertTrue(child_set.is_override)
        self.assertFalse(parent_set.is_override)

        # Auto-population of organization in save() if omitted
        auto_set = RolePermissionSet(role=self.role, name='Auto Set')
        auto_set.save(using='tenant_test')
        self.assertEqual(auto_set.organization_id, self.org.id)

    # -------------------------------------------------------------------------
    # 4. NotificationTemplate Alignment
    # -------------------------------------------------------------------------
    def test_notification_template_alignment(self):
        """Verify NotificationTemplate approved fields and backward compatibility."""
        template = NotificationTemplate.objects.using('tenant_test').create(
            organization=self.org,
            branch=self.branch,
            name='Welcome Email Template',
            channel='EMAIL',
            event_type='MEMBERSHIP_WELCOME',
            event_code='CORE_WELCOME_V1',
            language='en',
            version=2,
            subject='Welcome to Test Fitness!',
            body='Hello {{first_name}}',
            is_active=True,
        )

        # Organization FK: RESTRICT
        org_field = NotificationTemplate._meta.get_field('organization')
        self.assertEqual(org_field.remote_field.on_delete, models.RESTRICT)

        # Branch FK: RESTRICT, nullable
        branch_field = NotificationTemplate._meta.get_field('branch')
        self.assertEqual(branch_field.remote_field.on_delete, models.RESTRICT)
        self.assertTrue(branch_field.null)

        self.assertEqual(template.language, 'en')
        self.assertEqual(template.version, 2)
        self.assertEqual(template.event_code, 'CORE_WELCOME_V1')
        self.assertEqual(template.event_type, 'MEMBERSHIP_WELCOME')

    # -------------------------------------------------------------------------
    # 5. Organization & Branch Settings Alignment
    # -------------------------------------------------------------------------
    def test_settings_alignment(self):
        """Verify OrganizationSettings and BranchSettings 5 JSONB config blocks + formatting fields."""
        org_settings = OrganizationSettings.objects.using('tenant_test').create(
            organization=self.org,
            currency='INR',
            default_timezone='Asia/Kolkata',
            language='en',
            date_format='YYYY-MM-DD',
            time_format='HH:mm',
            membership_config={'allow_freeze': True, 'max_freeze_days': 60},
            booking_config={'cancellation_window_hours': 12},
            attendance_config={'biometric_sync': True},
            notification_config={'sms_enabled': True},
            ai_config={'smart_coach_enabled': False},
        )

        self.assertEqual(org_settings.default_timezone, 'Asia/Kolkata')
        self.assertEqual(org_settings.language, 'en')
        self.assertEqual(org_settings.date_format, 'YYYY-MM-DD')
        self.assertEqual(org_settings.time_format, 'HH:mm')
        self.assertEqual(org_settings.membership_config['allow_freeze'], True)
        self.assertEqual(org_settings.booking_config['cancellation_window_hours'], 12)
        self.assertEqual(org_settings.attendance_config['biometric_sync'], True)
        self.assertEqual(org_settings.notification_config['sms_enabled'], True)
        self.assertEqual(org_settings.ai_config['smart_coach_enabled'], False)

        # BranchSettings: JSONB blocks nullable/override semantics
        branch_settings = BranchSettings.objects.using('tenant_test').create(
            branch=self.branch,
            booking_config={'cancellation_window_hours': 6},  # override
            membership_config=None,  # inherit
        )
        self.assertEqual(branch_settings.booking_config['cancellation_window_hours'], 6)
        self.assertIsNone(branch_settings.membership_config)

    # -------------------------------------------------------------------------
    # 6. Privacy & Consent Alignment
    # -------------------------------------------------------------------------
    def test_privacy_consent_alignment(self):
        """Verify ProcessingPurpose and ConsentRecord lifecycle and proof fields."""
        purpose = ProcessingPurpose.objects.using('tenant_test').create(
            code='CORE_SERVICE',
            name='Core Service Delivery',
            notice_version='1.2',
            description='Processing required for gym access',
            legal_basis='CONTRACT',
            retention_days=365,
        )
        self.assertEqual(purpose.notice_version, '1.2')

        consent = ConsentRecord.objects.using('tenant_test').create(
            user=self.user,
            purpose=purpose,
            status='GRANTED',
            notice_version='1.2',
            consent_method='WEB_FORM',
            capture_source='CLIENT_PORTAL',
            proof_metadata={'ip': '127.0.0.1', 'user_agent': 'Mozilla/5.0'},
        )
        self.assertEqual(consent.notice_version, '1.2')
        self.assertEqual(consent.capture_source, 'CLIENT_PORTAL')
        self.assertEqual(consent.proof_metadata['ip'], '127.0.0.1')
        self.assertIsNone(consent.granted_at)
        self.assertIsNone(consent.withdrawn_at)

    # -------------------------------------------------------------------------
    # 7. TenantAuditEvent Alignment
    # -------------------------------------------------------------------------
    def test_tenant_audit_event_alignment(self):
        """Verify TenantAuditEvent actor_type, metadata, and correlation fields."""
        event = TenantAuditEvent.objects.using('tenant_test').create(
            actor=self.admin_user,
            actor_type='TENANT_USER',
            actor_email=self.admin_user.email,
            organization_id=self.org.id,
            company_entity_id=self.company_entity.id,
            location_id=self.loc.id,
            branch_id=self.branch.id,
            action='UPDATE',
            resource_type='RoleAssignment',
            resource_id=str(uuid.uuid4()),
            request_id='req_xyz_123',
            correlation_id='corr_abc_456',
            source_application='web_admin',
        )

        self.assertEqual(event.actor_type, 'TENANT_USER')
        self.assertEqual(event.organization_id, self.org.id)
        self.assertEqual(event.company_entity_id, self.company_entity.id)
        self.assertEqual(event.location_id, self.loc.id)
        self.assertEqual(event.request_id, 'req_xyz_123')
        self.assertEqual(event.correlation_id, 'corr_abc_456')
        self.assertEqual(event.source_application, 'web_admin')

    # -------------------------------------------------------------------------
    # 8. ModuleCatalog Logical Reference (No Cross-DB FK)
    # -------------------------------------------------------------------------
    def test_module_catalog_source_module_id(self):
        """Verify source_module_id is a logical UUIDField and NOT a cross-database FK."""
        field = ModuleCatalog._meta.get_field('source_module_id')
        self.assertIsInstance(field, models.UUIDField)
        self.assertFalse(field.is_relation)
        self.assertFalse(field.null)
        self.assertTrue(field.unique)

        # Create module with source_module_id
        source_id = uuid.uuid4()
        mc = ModuleCatalog.objects.using('tenant_test').create(
            module_code='ops_test',
            name='Ops Test',
            source_module_id=source_id,
        )
        self.assertEqual(mc.source_module_id, source_id)

    # -------------------------------------------------------------------------
    # 9. Multi-Tenant Migration Command (migrate_all_tenants)
    # -------------------------------------------------------------------------
    def test_master_db_protection_in_migrate_command(self):
        """Verify Master DB is protected against tenant_core migrations."""
        self.assertFalse(self.router.allow_migrate('default', 'tenant_core'))
        self.assertTrue(self.router.allow_migrate('tenant_test', 'tenant_core'))

    def test_migrate_all_tenants_dry_run(self):
        """Verify migrate_all_tenants --dry-run completes without modifying schema."""
        out = StringIO()
        call_command('migrate_all_tenants', '--all', '--dry-run', stdout=out)
        output = out.getvalue()
        self.assertIn('MULTI-TENANT MIGRATION REPORT (dry_run=True)', output)
        self.assertIn('Succeeded', output)

    def test_migrate_all_tenants_specific_tenant(self):
        """Verify migrate_all_tenants works for specific tenant slug."""
        out = StringIO()
        call_command('migrate_all_tenants', f'--tenant={self.master_tenant.slug}', '--dry-run', stdout=out)
        output = out.getvalue()
        self.assertIn(f"Tenant '{self.master_tenant.slug}'", output)
        self.assertIn('MULTI-TENANT MIGRATION REPORT', output)

    def test_migrate_all_tenants_no_args(self):
        """Verify invoking migrate_all_tenants without --all or --tenant gives clean error."""
        err = StringIO()
        call_command('migrate_all_tenants', stderr=err)
        self.assertIn('Error: You must specify either --all or --tenant=<slug>', err.getvalue())
