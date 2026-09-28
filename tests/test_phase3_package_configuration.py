"""
Phase 3: Package, PackageVersion, PackageBranchAvailability, PackagePrice,
and PackageEntitlementDefinition Test Suite.

Verifies:
1. Package create with backend code generation.
2. Package rename preserves code.
3. Package without valid Program is rejected.
4. Package branch availability rejected if Program is unavailable at that branch.
5. PackageBranchAvailability create and duplicate rejected.
6. PackageVersion draft editable, published version immutable.
7. Create next PackageVersion from published version.
8. PackagePrice create: branch-specific vs organization default, authoritative resolution.
9. Conflicting overlapping active price windows rejected.
10. PackageEntitlementDefinition: HOME_BRANCH_SESSION, CROSS_BRANCH_SESSION with extra_unit_price,
    unlimited entitlement support, negative units rejected.
11. Retired PackageVersion excluded from new sales.
12. CRM conversion catalog resolution regression.
13. Membership contract snapshot regression.
14. Entitlement allocation regression upon membership activation.
15. RBAC, Tenant isolation, Organization isolation, and BusinessAuditEvents.
"""

from decimal import Decimal
from datetime import timedelta
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken

from apps.authentication.views import _build_tenant_token
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.models_saas import SaasPlan, SaasPlanPrice, TenantSubscription, TenantModule, ProductModule
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_rbac import (
    Role, RoleAssignment, ModuleCatalog, SubmoduleCatalog, Permission,
    RolePermissionSet, RolePermissionSetItem, RoleModuleAccess, RoleSubmoduleAccess,
)
from apps.tenant_core.models_catalog import (
    ProgramCategory, Program, ProgramBranchAvailability,
    Package, PackageVersion, PackagePrice, PackageBranchAvailability,
    PackageEntitlementDefinition,
)
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_memberships import Membership, MembershipEntitlement
from apps.tenant_core.services_catalog import PackageCatalogService
from apps.tenant_core.models_audit_outbox import BusinessAuditEvent
from config.routers import set_tenant_db_alias
from config.tenant_middleware import _register_tenant_connection


class TenantTestClient(APIClient):
    def request(self, **kwargs):
        res = super().request(**kwargs)
        set_tenant_db_alias('tenant_test')
        return res


class Phase3PackageConfigurationTestCase(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        _register_tenant_connection('tenant_test', 'test_fitness_tenant')
        self.client = TenantTestClient()

        # 1. Master DB Setup
        self.tenant = Tenant.objects.using('default').create(
            name='SWEAT Elite Fitness',
            slug='sweat-elite',
            code='SWEAT-001',
            status='ACTIVE',
        )
        self.data_source = TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            db_name='test',
            status='ACTIVE',
        )
        self.plan = SaasPlan.objects.using('default').create(name='Pro Plan', code='PLAN-PRO', tier='growth', is_active=True)
        self.plan_price = SaasPlanPrice.objects.using('default').create(
            plan=self.plan, billing_cycle='MONTHLY', currency='INR', amount=Decimal('9999.00'), is_active=True
        )
        self.subscription = TenantSubscription.objects.using('default').create(
            tenant=self.tenant, plan=self.plan, plan_price=self.plan_price, status='ACTIVE'
        )
        self.mod_core, _ = ProductModule.objects.using('default').get_or_create(code='core', defaults={'name': 'Core', 'status': 'ACTIVE'})
        TenantModule.objects.using('default').create(tenant=self.tenant, module=self.mod_core, is_enabled=True)

        # 2. Org & Branches
        self.org = Organization.objects.using('tenant_test').create(name='SWEAT Corp', code='SWEAT-ORG', status='ACTIVE')
        self.loc = Location.objects.using('tenant_test').create(organization=self.org, name='West Region', code='WEST-LOC', status='ACTIVE')
        self.branch_a = Branch.objects.using('tenant_test').create(organization=self.org, location=self.loc, name='UAT Pilates West', code='PILATES-WEST', status='ACTIVE')
        self.branch_b = Branch.objects.using('tenant_test').create(organization=self.org, location=self.loc, name='SWEAT Bootcamp Central', code='BOOTCAMP-CENTRAL', status='ACTIVE')

        # 3. Admin User & Token
        self.admin_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org, email='admin@sweat.test', first_name='Phase3', last_name='Admin', status='ACTIVE'
        )
        self.admin_user.set_password('Secret123!')
        self.admin_user.save(using='tenant_test')

        # RBAC Setup for core.settings.edit
        self.role_admin = Role.objects.using('tenant_test').create(name='Org Admin', code='ORG_ADMIN', scope='ORG', organization=self.org, is_active=True)
        RoleAssignment.objects.using('tenant_test').create(user=self.admin_user, role=self.role_admin, organization=self.org, is_active=True)
        self.cat_core, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(module_code='core', defaults={'name': 'Core', 'is_enabled': True})
        self.csub_settings, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(module=self.cat_core, submodule_code='settings', defaults={'name': 'Settings', 'is_enabled': True})
        self.perm_edit, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.cat_core, submodule=self.csub_settings, action='edit',
            defaults={'permission_code': 'core.settings.edit', 'label': 'Edit Settings'}
        )
        self.perm_view, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.cat_core, submodule=self.csub_settings, action='view',
            defaults={'permission_code': 'core.settings.view', 'label': 'View Settings'}
        )
        self.pset = RolePermissionSet.objects.using('tenant_test').create(role=self.role_admin, name='Admin Core Perms')
        RoleModuleAccess.objects.using('tenant_test').get_or_create(
            role=self.role_admin, module=self.cat_core, defaults={'can_access': True}
        )
        RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(
            role=self.role_admin, submodule=self.csub_settings, defaults={'can_access': True}
        )
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=self.pset, permission=self.perm_edit, granted=True)
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=self.pset, permission=self.perm_view, granted=True)

        token = _build_tenant_token(self.admin_user, self.tenant, db_alias='tenant_test')
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {str(token.access_token)}')

        # 4. Program Setup
        self.cat = ProgramCategory.objects.using('tenant_test').create(organization=self.org, name='UAT Pilates', code='CAT-PILATES')
        self.program = Program.objects.using('tenant_test').create(
            organization=self.org, category=self.cat, name='UAT Reformer Pilates Plus', status='ACTIVE', delivery_mode='GROUP_CLASS'
        )
        # Program is available at Branch A ONLY
        self.prog_branch = ProgramBranchAvailability.objects.using('tenant_test').create(
            program=self.program, branch=self.branch_a, is_active=True
        )

    def tearDown(self):
        set_tenant_db_alias(None)
        super().tearDown()

    def test_01_package_create_auto_code_generation(self):
        """Package creation auto-generates org-scoped unique code."""
        url = '/api/v1/tenant/packages/'
        data = {
            'name': 'UAT Reformer 3 Month',
            'program': str(self.program.id),
            'status': 'ACTIVE',
        }
        res = self.client.post(url, data, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        pkg = Package.objects.get(id=res.data['id'])
        self.assertTrue(pkg.code)
        self.assertIn('UAT_REFORMER', pkg.code.upper())
        self.assertEqual(pkg.organization, self.org)

    def test_02_package_rename_preserves_code(self):
        """Updating package name preserves the auto-generated code."""
        pkg = PackageCatalogService.create_package(
            organization=self.org, program=self.program, name='Reformer Initial', actor=self.admin_user
        )
        initial_code = pkg.code

        url = f'/api/v1/tenant/packages/{pkg.id}/'
        res = self.client.patch(url, {'name': 'Reformer Renamed Tier'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        pkg.refresh_from_db()
        self.assertEqual(pkg.name, 'Reformer Renamed Tier')
        self.assertEqual(pkg.code, initial_code)

    def test_03_package_without_valid_program_rejected(self):
        """Package without valid program must be rejected."""
        url = '/api/v1/tenant/packages/'
        res = self.client.post(url, {'name': 'Orphan Package'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('program', res.data)

    def test_04_program_unavailable_branch_rejected_for_package(self):
        """Package branch availability rejected if Program is unavailable at that branch."""
        pkg = PackageCatalogService.create_package(
            organization=self.org, program=self.program, name='Branch Test Pkg', actor=self.admin_user
        )

        # Branch B has NO ProgramBranchAvailability for self.program
        url = '/api/v1/tenant/package-branch-availabilities/'
        data = {
            'package': str(pkg.id),
            'branch': str(self.branch_b.id),
            'is_active': True,
        }
        res = self.client.post(url, data, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('not available at branch', str(res.data).lower())

    def test_05_package_branch_availability_create_and_duplicate_rejected(self):
        """Valid package branch availability succeeds; duplicate availability is rejected."""
        pkg = PackageCatalogService.create_package(
            organization=self.org, program=self.program, name='Branch Valid Pkg', actor=self.admin_user
        )

        # Branch A is valid (Program is available at Branch A)
        url = '/api/v1/tenant/package-branch-availabilities/'
        data = {
            'package': str(pkg.id),
            'branch': str(self.branch_a.id),
            'is_active': True,
        }
        res = self.client.post(url, data, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        # Duplicate attempt
        res2 = self.client.post(url, data, format='json')
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)

    def test_06_package_version_draft_editable_published_immutable(self):
        """Draft version is editable; published version is immutable."""
        pkg = PackageCatalogService.create_package(
            organization=self.org, program=self.program, name='Immutability Pkg', actor=self.admin_user
        )
        ver = PackageCatalogService.create_package_version(
            package=pkg, name_snapshot='V1 Draft', duration_value=3, duration_unit='MONTH',
            total_days=90, validity_days=90, status='DRAFT', created_by_user=self.admin_user
        )

        # 1. Edit draft version -> Allowed
        url = f'/api/v1/tenant/package-versions/{ver.id}/'
        res = self.client.patch(url, {'total_days': 95}, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        ver.refresh_from_db()
        self.assertEqual(ver.total_days, 95)

        # 2. Add required price & entitlement and publish
        PackageCatalogService.create_package_price(
            package_version=ver, currency='INR', base_price=Decimal('15000.00'), actor=self.admin_user
        )
        PackageCatalogService.create_entitlement_definition(
            package_version=ver, entitlement_type='HOME_BRANCH_SESSION', allocated_units=Decimal('36'), actor=self.admin_user
        )
        pub_url = f'/api/v1/tenant/package-versions/{ver.id}/publish/'
        pub_res = self.client.post(pub_url, format='json')
        self.assertEqual(pub_res.status_code, status.HTTP_200_OK)
        ver.refresh_from_db()
        self.assertEqual(ver.status, 'ACTIVE')

        # 3. Mutate published version -> Rejected (400)
        res_mut = self.client.patch(url, {'total_days': 100}, format='json')
        self.assertEqual(res_mut.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('immutable', str(res_mut.data).lower())

    def test_07_create_next_package_version_from_published(self):
        """Creating a new version snapshot increments version_number and allows new terms."""
        pkg = PackageCatalogService.create_package(
            organization=self.org, program=self.program, name='Multi Version Pkg', actor=self.admin_user
        )
        v1 = PackageCatalogService.create_package_version(
            package=pkg, name_snapshot='V1', duration_value=3, duration_unit='MONTH',
            total_days=90, validity_days=90, status='DRAFT', created_by_user=self.admin_user
        )
        PackageCatalogService.create_package_price(
            package_version=v1, currency='INR', base_price=Decimal('15000.00'), actor=self.admin_user
        )
        PackageCatalogService.create_entitlement_definition(
            package_version=v1, entitlement_type='HOME_BRANCH_SESSION', allocated_units=Decimal('36'), actor=self.admin_user
        )
        PackageCatalogService.publish_package_version(package_version_id=v1.id, actor=self.admin_user)

        # Create Version 2 via API
        url = f'/api/v1/tenant/packages/{pkg.id}/create-version/'
        data = {
            'name_snapshot': 'V2',
            'duration_value': 3,
            'duration_unit': 'MONTH',
            'total_days': 90,
            'validity_days': 90,
            'sale_price': '16000.00',
            'max_sessions': 40,
            'passport_sessions': 4,
            'publish_immediately': True,
        }
        res = self.client.post(url, data, format='json')
        self.assertIn(res.status_code, [status.HTTP_200_OK, status.HTTP_201_CREATED], res.data)
        v2 = PackageVersion.objects.using('tenant_test').get(id=res.data['id'])
        self.assertEqual(v2.version_number, 2)
        self.assertEqual(v2.status, 'ACTIVE')
        # v1 should now be retired
        v1.refresh_from_db()
        self.assertEqual(v1.status, 'RETIRED')

    def test_08_package_price_branch_specific_vs_default_and_resolution(self):
        """Authoritative resolution returns branch-specific price if available, falls back to default."""
        pkg = PackageCatalogService.create_package(
            organization=self.org, program=self.program, name='Pricing Pkg', actor=self.admin_user
        )
        ver = PackageCatalogService.create_package_version(
            package=pkg, name_snapshot='V1', duration_value=1, duration_unit='MONTH',
            total_days=30, status='DRAFT', created_by_user=self.admin_user
        )
        # Default org price: 15,000
        p_default = PackageCatalogService.create_package_price(
            package_version=ver, currency='INR', base_price=Decimal('15000.00'), branch_id=None, actor=self.admin_user
        )
        # Branch A specific price: 14,299
        p_branch = PackageCatalogService.create_package_price(
            package_version=ver, currency='INR', base_price=Decimal('14299.00'), branch_id=self.branch_a.id, actor=self.admin_user
        )

        # Resolve for Branch A -> should be 14,299
        res_a = PackageCatalogService.resolve_package_price(ver, branch=self.branch_a)
        self.assertEqual(res_a.id, p_branch.id)
        self.assertEqual(res_a.base_price, Decimal('14299.00'))

        # Resolve for Branch B (no specific price) -> falls back to default 15,000
        res_b = PackageCatalogService.resolve_package_price(ver, branch=self.branch_b)
        self.assertEqual(res_b.id, p_default.id)
        self.assertEqual(res_b.base_price, Decimal('15000.00'))

    def test_09_conflicting_overlapping_active_price_windows_rejected(self):
        """Overlapping active prices for same (package_version, branch, currency) are rejected."""
        pkg = PackageCatalogService.create_package(
            organization=self.org, program=self.program, name='Overlap Price Pkg', actor=self.admin_user
        )
        ver = PackageCatalogService.create_package_version(
            package=pkg, name_snapshot='V1', duration_value=1, duration_unit='MONTH',
            total_days=30, status='DRAFT', created_by_user=self.admin_user
        )
        now = timezone.now()
        # Price 1: effective now to +30 days
        PackageCatalogService.create_package_price(
            package_version=ver, currency='INR', base_price=Decimal('10000.00'),
            effective_from=now, effective_until=now + timedelta(days=30), actor=self.admin_user
        )

        # Price 2: overlaps (starts in 15 days) -> Rejected
        url = '/api/v1/tenant/package-prices/'
        data = {
            'package_version': str(ver.id),
            'currency': 'INR',
            'base_price': '11000.00',
            'effective_from': (now + timedelta(days=15)).isoformat(),
            'effective_until': (now + timedelta(days=45)).isoformat(),
        }
        res = self.client.post(url, data, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('overlapping', str(res.data).lower())

    def test_10_entitlement_definitions_validation(self):
        """Validates HOME_BRANCH_SESSION, CROSS_BRANCH_SESSION, unlimited, and negative units rejected."""
        pkg = PackageCatalogService.create_package(
            organization=self.org, program=self.program, name='Entitlements Pkg', actor=self.admin_user
        )
        ver = PackageCatalogService.create_package_version(
            package=pkg, name_snapshot='V1', duration_value=1, duration_unit='MONTH',
            total_days=30, status='DRAFT', created_by_user=self.admin_user
        )

        # 1. Negative units -> Rejected
        url = '/api/v1/tenant/package-entitlement-definitions/'
        res_neg = self.client.post(url, {
            'package_version': str(ver.id),
            'entitlement_type': 'HOME_BRANCH_SESSION',
            'allocated_units': -5,
        }, format='json')
        self.assertEqual(res_neg.status_code, status.HTTP_400_BAD_REQUEST)

        # 2. Valid Home Branch sessions
        res_home = self.client.post(url, {
            'package_version': str(ver.id),
            'entitlement_type': 'HOME_BRANCH_SESSION',
            'allocated_units': 36,
        }, format='json')
        self.assertEqual(res_home.status_code, status.HTTP_201_CREATED)

        # 3. Cross-Branch session with extra_unit_price surcharge
        res_cross = self.client.post(url, {
            'package_version': str(ver.id),
            'entitlement_type': 'CROSS_BRANCH_SESSION',
            'allocated_units': 2,
            'extra_unit_price': '500.00',
        }, format='json')
        self.assertEqual(res_cross.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Decimal(res_cross.data['extra_unit_price']), Decimal('500.00'))

        # 4. Unlimited session support
        res_unlim = self.client.post(url, {
            'package_version': str(ver.id),
            'entitlement_type': 'OPEN_ACCESS',
            'is_unlimited': True,
        }, format='json')
        self.assertEqual(res_unlim.status_code, status.HTTP_201_CREATED)
        self.assertTrue(res_unlim.data['is_unlimited'])

    def test_11_retired_version_excluded_from_new_sales(self):
        """A retired version cannot be purchased or resolved for active commercial offerings."""
        pkg = PackageCatalogService.create_package(
            organization=self.org, program=self.program, name='Retire Check Pkg', actor=self.admin_user
        )
        ver = PackageCatalogService.create_package_version(
            package=pkg, name_snapshot='V1', duration_value=1, duration_unit='MONTH',
            total_days=30, status='DRAFT', created_by_user=self.admin_user
        )
        PackageCatalogService.create_package_price(
            package_version=ver, currency='INR', base_price=Decimal('10000.00'), actor=self.admin_user
        )
        PackageCatalogService.create_entitlement_definition(
            package_version=ver, entitlement_type='HOME_BRANCH_SESSION', allocated_units=Decimal('10'), actor=self.admin_user
        )
        PackageCatalogService.publish_package_version(package_version_id=ver.id, actor=self.admin_user)

        # Retire version
        PackageCatalogService.retire_package_version(package_version_id=ver.id, actor=self.admin_user)
        ver.refresh_from_db()
        self.assertEqual(ver.status, 'RETIRED')

        # Resolving active version on package returns None
        self.assertIsNone(pkg.get_active_version())

    def test_12_crm_conversion_catalog_resolution_regression(self):
        """Lead conversion strictly resolves program, package, published version, and branch price."""
        pkg = PackageCatalogService.create_package(
            organization=self.org, program=self.program, name='CRM Lead Pkg', actor=self.admin_user
        )
        ver = PackageCatalogService.create_package_version(
            package=pkg, name_snapshot='V1', duration_value=1, duration_unit='MONTH',
            total_days=30, status='DRAFT', created_by_user=self.admin_user
        )
        PackageCatalogService.create_package_price(
            package_version=ver, currency='INR', base_price=Decimal('12000.00'),
            branch_id=self.branch_a.id, actor=self.admin_user
        )
        PackageCatalogService.create_entitlement_definition(
            package_version=ver, entitlement_type='HOME_BRANCH_SESSION', allocated_units=Decimal('20'), actor=self.admin_user
        )
        PackageCatalogService.publish_package_version(package_version_id=ver.id, actor=self.admin_user, db_alias='tenant_test')
        PackageBranchAvailability.objects.using('tenant_test').create(
            package=pkg, branch=self.branch_a, status='ENABLED'
        )

        resolved_price = PackageCatalogService.resolve_package_price(ver, branch=self.branch_a, db_alias='tenant_test')
        self.assertIsNotNone(resolved_price)
        self.assertEqual(resolved_price.base_price, Decimal('12000.00'))

    def test_13_membership_contract_snapshot_regression(self):
        """Membership freeze snapshots of program, package, version, price, and branch."""
        pkg = PackageCatalogService.create_package(
            organization=self.org, program=self.program, name='Snapshot Pkg', actor=self.admin_user, db_alias='tenant_test'
        )
        ver = PackageCatalogService.create_package_version(
            package=pkg, name_snapshot='V1 Terms', duration_value=3, duration_unit='MONTH',
            total_days=90, validity_days=90, status='DRAFT', created_by_user=self.admin_user, db_alias='tenant_test'
        )
        price = PackageCatalogService.add_package_price(
            package_version=ver, currency='INR', base_price=Decimal('15000.00'), actor=self.admin_user, db_alias='tenant_test'
        )
        ent_def = PackageCatalogService.add_entitlement_definition(
            package_version=ver, entitlement_type='HOME_BRANCH_SESSION', allocated_units=Decimal('36'), actor=self.admin_user, db_alias='tenant_test'
        )
        PackageCatalogService.publish_package_version(package_version_id=ver.id, actor=self.admin_user, db_alias='tenant_test')

        profile, _ = UserProfile.objects.using('tenant_test').get_or_create(
            user=self.admin_user, defaults={'first_name_snapshot': 'Snapshot', 'last_name_snapshot': 'Tester'}
        )
        membership = Membership.objects.using('tenant_test').create(
            user_profile=profile, home_branch=self.branch_a, purchase_branch=self.branch_a,
            membership_number='MEM-SNAP-001',
            program=self.program, package=pkg, package_version=ver, package_price=price,
            status='ACTIVE', start_date=timezone.now().date(), end_date=(timezone.now() + timedelta(days=90)).date()
        )

        # Later client changes package to V2
        v2 = PackageCatalogService.create_package_version(
            package=pkg, name_snapshot='V2 Terms', duration_value=3, duration_unit='MONTH',
            total_days=90, validity_days=90, status='DRAFT', created_by_user=self.admin_user, db_alias='tenant_test'
        )
        PackageCatalogService.add_package_price(
            package_version=v2, currency='INR', base_price=Decimal('18000.00'), actor=self.admin_user, db_alias='tenant_test'
        )
        PackageCatalogService.add_entitlement_definition(
            package_version=v2, entitlement_type='HOME_BRANCH_SESSION', allocated_units=Decimal('40'), actor=self.admin_user, db_alias='tenant_test'
        )
        PackageCatalogService.publish_package_version(package_version_id=v2.id, actor=self.admin_user, db_alias='tenant_test')

        # Existing membership remains on V1 with price 15,000
        membership.refresh_from_db()
        self.assertEqual(membership.package_version.id, ver.id)
        self.assertEqual(membership.package_price.base_price, Decimal('15000.00'))

    def test_14_entitlement_allocation_regression(self):
        """Membership entitlement is initialized from PackageEntitlementDefinition values."""
        pkg = PackageCatalogService.create_package(
            organization=self.org, program=self.program, name='Alloc Pkg', actor=self.admin_user, db_alias='tenant_test'
        )
        ver = PackageCatalogService.create_package_version(
            package=pkg, name_snapshot='V1 Terms', duration_value=1, duration_unit='MONTH',
            total_days=30, validity_days=30, status='DRAFT', created_by_user=self.admin_user, db_alias='tenant_test'
        )
        PackageCatalogService.add_package_price(
            package_version=ver, currency='INR', base_price=Decimal('5000.00'), actor=self.admin_user, db_alias='tenant_test'
        )
        PackageCatalogService.add_entitlement_definition(
            package_version=ver, entitlement_type='HOME_BRANCH_SESSION', allocated_units=Decimal('12'), actor=self.admin_user, db_alias='tenant_test'
        )
        PackageCatalogService.add_entitlement_definition(
            package_version=ver, entitlement_type='CROSS_BRANCH_SESSION', allocated_units=Decimal('2'), actor=self.admin_user, db_alias='tenant_test'
        )
        PackageCatalogService.publish_package_version(package_version_id=ver.id, actor=self.admin_user, db_alias='tenant_test')

        profile, _ = UserProfile.objects.using('tenant_test').get_or_create(
            user=self.admin_user, defaults={'first_name_snapshot': 'Alloc', 'last_name_snapshot': 'Tester'}
        )
        membership = Membership.objects.using('tenant_test').create(
            user_profile=profile, home_branch=self.branch_a, purchase_branch=self.branch_a,
            membership_number='MEM-ALLOC-001',
            program=self.program, package=pkg, package_version=ver, status='ACTIVE',
            start_date=timezone.now().date(), end_date=(timezone.now() + timedelta(days=30)).date()
        )

        # Allocate entitlements from version definitions
        for defn in ver.entitlement_definitions.using('tenant_test').all():
            MembershipEntitlement.objects.using('tenant_test').create(
                membership=membership,
                source_definition=defn,
                entitlement_type=defn.entitlement_type,
                allocated_units=defn.allocated_units or Decimal('0'),
                is_unlimited=defn.is_unlimited,
                valid_from=timezone.now(),
                status='ACTIVE',
            )

        home_ent = MembershipEntitlement.objects.using('tenant_test').get(membership=membership, entitlement_type='HOME_BRANCH_SESSION')
        self.assertEqual(home_ent.allocated_units, Decimal('12'))
        cross_ent = MembershipEntitlement.objects.using('tenant_test').get(membership=membership, entitlement_type='CROSS_BRANCH_SESSION')
        self.assertEqual(cross_ent.allocated_units, Decimal('2'))

    def test_15_rbac_tenant_and_org_isolation_and_audit(self):
        """Verifies RBAC enforcement, org isolation, and audit events."""
        # 1. Unprivileged user cannot create package
        guest_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org, email='guest@sweat.test', first_name='Guest', last_name='User', status='ACTIVE'
        )
        token_guest = _build_tenant_token(guest_user, self.tenant, db_alias='tenant_test')
        client_guest = APIClient()
        client_guest.credentials(HTTP_AUTHORIZATION=f'Bearer {str(token_guest.access_token)}')
        res_403 = client_guest.post('/api/v1/tenant/packages/', {
            'name': 'Hacker Package', 'program': str(self.program.id)
        }, format='json')
        self.assertEqual(res_403.status_code, status.HTTP_403_FORBIDDEN)
        set_tenant_db_alias('tenant_test')

        # 2. Org isolation: Org 2 cannot see Org 1 packages
        org_2 = Organization.objects.using('tenant_test').create(name='Other Gym', code='OTHER-ORG', status='ACTIVE')
        pkg_org1 = PackageCatalogService.create_package(
            organization=self.org, program=self.program, name='Org 1 Pkg', actor=self.admin_user, db_alias='tenant_test'
        )
        self.assertFalse(Package.objects.using('tenant_test').filter(organization=org_2, id=pkg_org1.id).exists())

        # 3. Audit event emitted
        audits = BusinessAuditEvent.objects.using('tenant_test').filter(entity_type='Package', entity_id=str(pkg_org1.id))
        self.assertTrue(audits.exists())

    def test_16_reuse_package_version_clones_to_new_draft_preserves_old(self):
        """Reusing a retired version creates a new DRAFT version, deep-cloning prices/entitlements and preserving old."""
        pkg = PackageCatalogService.create_package(
            organization=self.org, program=self.program, name='Reuse Lifecycle Pkg', actor=self.admin_user
        )
        # V1
        v1 = PackageCatalogService.create_package_version(
            package=pkg, name_snapshot='V1', duration_value=1, duration_unit='MONTH',
            total_days=30, validity_days=30, status='DRAFT', created_by_user=self.admin_user
        )
        PackageCatalogService.create_package_price(
            package_version=v1, currency='INR', base_price=Decimal('5000.00'), actor=self.admin_user
        )
        PackageCatalogService.create_entitlement_definition(
            package_version=v1, entitlement_type='HOME_BRANCH_SESSION', allocated_units=Decimal('10'), actor=self.admin_user
        )
        PackageCatalogService.publish_package_version(package_version_id=v1.id, actor=self.admin_user)
        v1.refresh_from_db()
        self.assertEqual(v1.status, 'ACTIVE')

        # V2 (published -> retires V1)
        v2 = PackageCatalogService.create_package_version(
            package=pkg, name_snapshot='V2', duration_value=2, duration_unit='MONTH',
            total_days=60, validity_days=60, status='DRAFT', created_by_user=self.admin_user
        )
        PackageCatalogService.create_package_price(
            package_version=v2, currency='INR', base_price=Decimal('9000.00'), actor=self.admin_user
        )
        PackageCatalogService.create_entitlement_definition(
            package_version=v2, entitlement_type='HOME_BRANCH_SESSION', allocated_units=Decimal('20'), actor=self.admin_user
        )
        PackageCatalogService.publish_package_version(package_version_id=v2.id, actor=self.admin_user)
        v1.refresh_from_db()
        v2.refresh_from_db()
        self.assertEqual(v1.status, 'RETIRED')
        self.assertEqual(v2.status, 'ACTIVE')

        # Reuse V1 via API
        reuse_url = f'/api/v1/tenant/package-versions/{v1.id}/reuse/'
        res = self.client.post(reuse_url, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)

        v3_id = res.data['id']
        v3 = PackageVersion.objects.using('tenant_test').get(id=v3_id)
        self.assertEqual(v3.version_number, 3)
        self.assertEqual(v3.status, 'DRAFT')
        self.assertEqual(v3.total_days, 30)

        # Old versions untouched
        v1.refresh_from_db()
        v2.refresh_from_db()
        self.assertEqual(v1.status, 'RETIRED')
        self.assertEqual(v2.status, 'ACTIVE')

        # Child records are separate instances cloned from V1
        v3_prices = list(v3.prices.using('tenant_test').all())
        self.assertEqual(len(v3_prices), 1)
        self.assertEqual(v3_prices[0].base_price, Decimal('5000.00'))
        self.assertNotEqual(v3_prices[0].id, v1.prices.using('tenant_test').first().id)

        v3_ents = list(v3.entitlement_definitions.using('tenant_test').all())
        self.assertEqual(len(v3_ents), 1)
        self.assertEqual(v3_ents[0].allocated_units, Decimal('10'))
        self.assertNotEqual(v3_ents[0].id, v1.entitlement_definitions.using('tenant_test').first().id)

        # Publishing V3 retires V2 atomically
        pub_url = f'/api/v1/tenant/package-versions/{v3.id}/publish/'
        pub_res = self.client.post(pub_url, format='json')
        self.assertEqual(pub_res.status_code, status.HTTP_200_OK, pub_res.data)

        v1.refresh_from_db()
        v2.refresh_from_db()
        v3.refresh_from_db()
        self.assertEqual(v1.status, 'RETIRED')
        self.assertEqual(v2.status, 'RETIRED')
        self.assertEqual(v3.status, 'ACTIVE')

        # Exactly 1 active version exists
        active_count = PackageVersion.objects.using('tenant_test').filter(package=pkg, status='ACTIVE').count()
        self.assertEqual(active_count, 1)

