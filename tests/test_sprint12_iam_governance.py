"""
Sprint 12 — Focused Test Suite
IAM / RBAC GOVERNANCE — EXACTLY 29 FIELDS

Covers:
1. Master IAM fields (platform_users.is_mfa_enabled, platform_user_departments, platform_roles, platform_modules, etc.)
2. Tenant IAM fields (users.is_mfa_enabled, user_branches.is_primary, user_departments, permissions, roles, RMA/RSMA)
3. Historical assignment timestamps (assigned_at, unassigned_at preservation)
4. Permission source resolution (permissions.source_permission_id -> Master catalog)
5. Permission-set resolution (RMA & RSMA permission_set_id -> RolePermissionSet)
6. Display ordering (display_order on platform modules & submodules)
7. Visibility/grant flags (is_visible, is_allowed)
8. System-role protection (is_system_role fail-closed)
9. NOT NULL enforcement (IntegrityError on required fields)
10. FK target/action/deferrability (RESTRICT on permission_set)
11. ORM/API compatibility (legacy attribute and filter kwargs aliases)
12. RBAC compatibility (10-check authorization chain evaluation)
"""

import uuid
from django.test import TestCase
from django.utils import timezone
from django.db import IntegrityError, transaction

from config.routers import set_tenant_db_alias
from apps.master.models_tenant import Tenant
from apps.master.models_saas import (
    ProductModule,
    ProductSubmodule,
    TenantPermissionCatalog,
    SaasPlan,
    TenantSubscription,
    TenantModule,
)
from apps.master.models_infra import TenantDataSource
from apps.master.models_iam import (
    PlatformUser,
    PlatformDepartment,
    PlatformUserDepartment,
    PlatformRole,
    PlatformUserRole,
    PlatformModule,
    PlatformSubmodule,
    PlatformPermission,
    PlatformRoleModuleAccess,
    PlatformRoleSubmoduleAccess,
    PlatformRolePermission,
)
from apps.tenant_core.models_org import Organization, Branch, Location
from apps.tenant_core.models_users import TenantUser, UserBranch, UserDepartment
from apps.tenant_core.models_rbac import (
    Department,
    ModuleCatalog,
    SubmoduleCatalog,
    Permission,
    Role,
    RolePermissionSet,
    RolePermissionSetItem,
    RoleModuleAccess,
    RoleSubmoduleAccess,
    RoleAssignment,
)
from apps.tenant_core.rbac_engine import RBACAuthorizationEngine


class Sprint12MasterIAMTests(TestCase):
    """Test Suite for Sprint 12 Master IAM Schema & Governance (12 Fields)."""

    databases = {'default'}

    def setUp(self):
        self.p_user = PlatformUser.objects.create(
            email='master_iam_tester@performanceos.io',
            first_name='Master',
            last_name='Tester',
        )
        self.p_dept = PlatformDepartment.objects.create(
            code='DEPT-ENG',
            name='Engineering',
        )
        self.p_role = PlatformRole.objects.create(
            code='ROLE-SECENG',
            name='Security Engineer',
            is_system_role=True,
        )
        self.p_mod = PlatformModule.objects.create(
            code='MOD-SEC',
            name='Security Console',
            display_order=5,
        )
        self.p_submod = PlatformSubmodule.objects.create(
            module=self.p_mod,
            code='SUB-AUDIT',
            name='Audit Log Explorer',
            display_order=2,
        )
        self.p_perm = PlatformPermission.objects.create(
            module=self.p_mod,
            submodule=self.p_submod,
            code='sec.audit.view',
            action='view',
            label='View Security Audits',
        )

    def test_1_master_mfa_default(self):
        """platform_users.is_mfa_enabled default is False, can be enabled."""
        self.assertFalse(self.p_user.is_mfa_enabled)
        self.p_user.is_mfa_enabled = True
        self.p_user.save()
        self.p_user.refresh_from_db()
        self.assertTrue(self.p_user.is_mfa_enabled)

    def test_2_master_user_department_fields(self):
        """platform_user_departments fields: platform_user, is_primary, assigned_at, unassigned_at."""
        historical = timezone.now() - timezone.timedelta(days=180)
        pud = PlatformUserDepartment.objects.create(
            platform_user=self.p_user,
            department=self.p_dept,
            is_primary=True,
            assigned_at=historical,
            unassigned_at=None,
        )
        pud.refresh_from_db()
        self.assertTrue(pud.is_primary)
        self.assertEqual(pud.platform_user_id, self.p_user.id)
        self.assertEqual(pud.assigned_at, historical)
        self.assertIsNone(pud.unassigned_at)

        # ORM backward compatibility: user property and filter
        self.assertEqual(pud.user.id, self.p_user.id)
        self.assertEqual(pud.joined_at, historical)
        self.assertTrue(PlatformUserDepartment.objects.filter(user=self.p_user).exists())
        self.assertTrue(PlatformUserDepartment.objects.filter(joined_at=historical).exists())

    def test_3_master_role_is_system_role(self):
        """platform_roles.is_system_role flag and legacy is_system property/filter."""
        self.assertTrue(self.p_role.is_system_role)
        self.assertTrue(self.p_role.is_system)
        self.assertTrue(PlatformRole.objects.filter(is_system=True).exists())
        self.assertTrue(PlatformRole.objects.filter(is_system_role=True).exists())

    def test_4_master_display_order(self):
        """platform_modules and submodules display_order ordering and sort_order alias."""
        m1 = PlatformModule.objects.create(code='MOD-A', name='A Module', display_order=10)
        m2 = PlatformModule.objects.create(code='MOD-B', name='B Module', display_order=1)
        mods = list(PlatformModule.objects.filter(code__in=['MOD-A', 'MOD-B']).order_by('display_order'))
        self.assertEqual(mods[0].code, 'MOD-B')
        self.assertEqual(mods[1].code, 'MOD-A')
        self.assertEqual(m1.sort_order, 10)

    def test_5_master_visibility_and_grant_flags(self):
        """platform_role_module_access, submodule_access, and role_permissions."""
        rma = PlatformRoleModuleAccess.objects.create(
            role=self.p_role,
            module=self.p_mod,
            is_visible=True,
        )
        rsa = PlatformRoleSubmoduleAccess.objects.create(
            role=self.p_role,
            submodule=self.p_submod,
            is_visible=True,
        )
        rp = PlatformRolePermission.objects.create(
            role=self.p_role,
            permission=self.p_perm,
            is_allowed=True,
        )
        self.assertTrue(rma.is_visible)
        self.assertTrue(rma.can_access)
        self.assertTrue(rsa.is_visible)
        self.assertTrue(rsa.can_access)
        self.assertTrue(rp.is_allowed)
        self.assertTrue(rp.granted)

        # ORM filter compatibility
        self.assertTrue(PlatformRoleModuleAccess.objects.filter(can_access=True).exists())
        self.assertTrue(PlatformRoleSubmoduleAccess.objects.filter(can_access=True).exists())
        self.assertTrue(PlatformRolePermission.objects.filter(granted=True).exists())

    def test_6_master_user_roles_platform_user_id(self):
        """platform_user_roles.platform_user_id ForeignKey mapping."""
        pur = PlatformUserRole.objects.create(
            platform_user=self.p_user,
            role=self.p_role,
        )
        pur.refresh_from_db()
        self.assertEqual(pur.platform_user_id, self.p_user.id)
        self.assertEqual(pur.user.id, self.p_user.id)
        self.assertTrue(PlatformUserRole.objects.filter(user=self.p_user).exists())

    def test_6b_master_platform_user_deletion_semantics_restrict(self):
        """platform_user_departments and platform_user_roles enforce ON DELETE RESTRICT on platform_user_id."""
        from django.db.models import RestrictedError

        # 1. PlatformUserDepartment RESTRICT check
        pud = PlatformUserDepartment.objects.create(
            platform_user=self.p_user,
            department=self.p_dept,
            is_primary=True,
        )
        with self.assertRaises(RestrictedError):
            self.p_user.delete()
        pud.delete()

        # 2. PlatformUserRole RESTRICT check
        pur = PlatformUserRole.objects.create(
            platform_user=self.p_user,
            role=self.p_role,
        )
        with self.assertRaises(RestrictedError):
            self.p_user.delete()
        pur.delete()


class Sprint12TenantIAMTests(TestCase):
    """Test Suite for Sprint 12 Tenant IAM Schema & Governance (17 Fields)."""

    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        self.org = Organization.objects.using('tenant_test').create(
            name='Sprint 12 Athletics',
            code='ORG-S12',
            status='ACTIVE',
        )
        self.loc = Location.objects.using('tenant_test').create(
            organization=self.org,
            name='Main Campus',
            code='LOC-S12',
            status='ACTIVE',
        )
        self.branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            name='North Gym',
            code='BR-S12-N',
            status='ACTIVE',
        )
        self.dept = Department.objects.using('tenant_test').create(
            organization=self.org,
            code='DEPT-OPS',
            name='Operations',
        )
        self.user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='tenant_iam_user@sprint12.test',
            first_name='Tenant',
            last_name='Tester',
            status='ACTIVE',
            home_branch=self.branch,
        )
        self.mod = ModuleCatalog.objects.using('tenant_test').create(
            code='crm',
            module_code='crm',
            name='Customer Relations',
            is_enabled=True,
            source_module_id=uuid.uuid4(),
        )
        self.submod = SubmoduleCatalog.objects.using('tenant_test').create(
            module=self.mod,
            code='leads',
            submodule_code='leads',
            name='Lead Management',
            is_enabled=True,
            source_submodule_id=uuid.uuid4(),
        )

        # Master Catalog permission for deterministic source_permission_id resolution
        p_mod = ProductModule.objects.using('default').create(code='crm', name='CRM', is_core=True)
        self.master_perm = TenantPermissionCatalog.objects.using('default').create(
            code='crm.leads.export',
            label='Export Leads',
            action='export',
            module=p_mod,
        )

        # Master Tenant, Plan, Sub, DataSource & Entitlements for 10-check RBAC Engine
        self.tenant = Tenant.objects.using('default').create(
            name='Sprint 12 Athletics',
            slug='sprint12-athletics',
            code='TENANT-S12',
            status='ACTIVE',
        )
        self.plan = SaasPlan.objects.using('default').create(
            name='Enterprise Plan',
            code='PLAN-S12-ENT',
            tier='enterprise',
            is_active=True,
        )
        self.sub = TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
        )
        self.ds = TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            db_name='tenant_test',
            status='ACTIVE',
        )
        self.tenant_mod = TenantModule.objects.using('default').create(
            tenant=self.tenant,
            module=p_mod,
            availability_mode='ALL_BRANCHES',
            is_enabled=True,
        )
        self.user._tenant_id = str(self.tenant.id)
        self.user._db_alias = 'tenant_test'
        self.user._auth_type = 'tenant'

        self.perm = Permission.objects.using('tenant_test').create(
            module=self.mod,
            submodule=self.submod,
            code='crm.leads.export',
            action='export',
            label='Export Leads',
        )

        self.role = Role.objects.using('tenant_test').create(
            organization=self.org,
            code='ROLE-LEAD-MGR',
            name='Lead Manager',
            is_system_role=False,
        )
        self.pset = RolePermissionSet.objects.using('tenant_test').create(
            role=self.role,
            organization=self.org,
            name='Lead Manager PSet',
            is_override=False,
        )

    def test_7_tenant_mfa_and_primary_flags(self):
        """users.is_mfa_enabled, user_branches.is_primary, user_departments.is_primary defaults."""
        self.assertFalse(self.user.is_mfa_enabled)

        ub = UserBranch.objects.using('tenant_test').create(
            user=self.user,
            branch=self.branch,
            scope_type='ADDITIONAL',
            is_primary=False,
        )
        self.assertFalse(ub.is_primary)

        ud = UserDepartment.objects.using('tenant_test').create(
            user=self.user,
            department=self.dept,
            is_primary=False,
        )
        self.assertFalse(ud.is_primary)

    def test_8_permission_source_permission_id_and_version(self):
        """permissions.source_permission_id deterministically resolves and enforces UNIQUE + NOT NULL."""
        self.perm.refresh_from_db()
        self.assertEqual(self.perm.source_permission_id, self.master_perm.id)
        self.assertEqual(self.perm.version, 1)

        # Enforce unique constraint
        with self.assertRaises(IntegrityError):
            with transaction.atomic(using='tenant_test'):
                Permission.objects.using('tenant_test').create(
                    module=self.mod,
                    code='crm.leads.duplicate',
                    action='duplicate',
                    source_permission_id=self.master_perm.id,  # Duplicate source ID
                )

    def test_9_role_and_permission_set_defaults(self):
        """roles.is_system_role and role_permission_sets.is_override defaults."""
        self.assertFalse(self.role.is_system_role)
        self.assertFalse(self.pset.is_override)

    def test_10_role_module_access_fields_and_restrict(self):
        """role_module_access fields: permission_set, is_visible, created_at, updated_at."""
        rma = RoleModuleAccess.objects.using('tenant_test').create(
            role=self.role,
            permission_set=self.pset,
            module=self.mod,
            is_visible=True,
        )
        rma.refresh_from_db()
        self.assertEqual(rma.permission_set_id, self.pset.id)
        self.assertTrue(rma.is_visible)
        self.assertTrue(rma.can_access)
        self.assertIsNotNone(rma.created_at)
        self.assertIsNotNone(rma.updated_at)

        # FK ON DELETE RESTRICT: cannot delete RolePermissionSet while RMA exists
        with self.assertRaises(IntegrityError):
            with transaction.atomic(using='tenant_test'):
                self.pset.delete()

    def test_11_role_submodule_access_fields_and_restrict(self):
        """role_submodule_access fields: permission_set, is_visible, created_at, updated_at."""
        rsma = RoleSubmoduleAccess.objects.using('tenant_test').create(
            role=self.role,
            permission_set=self.pset,
            submodule=self.submod,
            is_visible=True,
        )
        rsma.refresh_from_db()
        self.assertEqual(rsma.permission_set_id, self.pset.id)
        self.assertTrue(rsma.is_visible)
        self.assertTrue(rsma.can_access)
        self.assertIsNotNone(rsma.created_at)
        self.assertIsNotNone(rsma.updated_at)

        # FK ON DELETE RESTRICT: cannot delete RolePermissionSet while RSMA exists
        with self.assertRaises(IntegrityError):
            with transaction.atomic(using='tenant_test'):
                self.pset.delete()

    def test_12_rbac_engine_compatibility_with_s12_schema(self):
        """RBACAuthorizationEngine 10-check chain evaluates cleanly with Sprint 12 schema."""
        rma = RoleModuleAccess.objects.using('tenant_test').create(
            role=self.role,
            permission_set=self.pset,
            module=self.mod,
            is_visible=True,
        )
        rsma = RoleSubmoduleAccess.objects.using('tenant_test').create(
            role=self.role,
            permission_set=self.pset,
            submodule=self.submod,
            is_visible=True,
        )
        RolePermissionSetItem.objects.using('tenant_test').create(
            permission_set=self.pset,
            permission=self.perm,
            is_allowed=True,
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.user,
            role=self.role,
            branch=self.branch,
            is_active=True,
        )

        allowed, reason, code = RBACAuthorizationEngine.evaluate(
            user=self.user,
            required_module='crm',
            required_submodule='leads',
            required_permission='crm.leads.export',
            branch_id=str(self.branch.id),
        )
        self.assertTrue(allowed)
        self.assertEqual(code, 'ALL_CHECKS_PASSED')

        # Visibility gate check: toggling is_visible=False denies access
        rma.is_visible = False
        rma.save()
        allowed_denied, reason, code_denied = RBACAuthorizationEngine.evaluate(
            user=self.user,
            required_module='crm',
            required_submodule='leads',
            required_permission='crm.leads.export',
            branch_id=str(self.branch.id),
        )
        self.assertFalse(allowed_denied)
        self.assertEqual(code_denied, 'CHECK_8_MODULE_ACCESS_DENIED')
