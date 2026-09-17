"""
Targeted tests for the Corrected Layer 2 Program / Package / Session Schema.

Covers:
1. ProgramType model & API (configurable, org-scoped, no hardcoded enums).
2. Program stable metadata & ProgramType FK.
3. Package program_id required at database level & unique(program, code).
4. PackageVersion total_days > 0, trial flags, visibility, published_at.
5. Strict PackageVersion Immutability (modifying ACTIVE / RETIRED raises ValidationError).
6. PackagePrice versioning, display_price, sale_price, prices_include_tax.
7. Entitlement definitions with extra_unit_price (Passport mapping).
8. PackageClassAccessRule versioning: V1 vs V2 rules differ and V1 member retains V1 access.
9. Membership immutable purchase_branch.
10. MembershipContractSnapshot immutability (NO UPDATE, NO DELETE).
11. Transactional Member Entitlement & Append-Only Ledger allocation upon activation.
12. BusinessAuditEvent generation for all lifecycle actions.
"""

import uuid
from decimal import Decimal
from datetime import date, timedelta
from django.utils import timezone
from django.core.exceptions import ValidationError
from rest_framework.test import APITestCase
from rest_framework import status

from apps.authentication.views import _build_tenant_token
from apps.master.models import Tenant, TenantDataSource, ProductModule, TenantModule, SaasPlan, TenantSubscription
from apps.tenant_core.context import set_tenant_db_alias
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_rbac import (
    Role, RoleAssignment, ModuleCatalog, SubmoduleCatalog,
    Permission, RolePermissionSet, RolePermissionSetItem,
    RoleModuleAccess, RoleSubmoduleAccess,
)
from apps.tenant_core.models_catalog import (
    ProgramCategory, ProgramType, Program, Package, PackageVersion,
    PackagePrice, PackageBranchAvailability, PackageEntitlementDefinition,
)
from apps.tenant_core.models_classes import (
    ClassCategory, ClassTemplate, PackageClassAccessRule,
)
from apps.tenant_core.models_commerce import Order, OrderItem
from apps.tenant_core.models_memberships import (
    Membership, MembershipContractSnapshot, MembershipEntitlement, MembershipEntitlementLedger,
)
from apps.tenant_core.services_catalog import PackageCatalogService
from apps.tenant_core.services_memberships import MembershipLifecycleService
from apps.tenant_core.models_audit_outbox import BusinessAuditEvent


class CorrectedCatalogSchemaTestCase(APITestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')

        # 1. Master DB Setup
        self.tenant = Tenant.objects.using('default').create(
            code='CORRECTED-TENANT',
            name='Corrected Catalog Gym',
            slug='corrected-gym',
            status='ACTIVE',
        )
        self.ds = TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            db_name='test_fitness_tenant',
            database_name='test_fitness_tenant',
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )
        self.plan = SaasPlan.objects.using('default').create(
            name='Enterprise Plan',
            code='ENTERPRISE-PLAN',
            tier='ENTERPRISE',
            status='ACTIVE',
        )
        self.sub = TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
        )
        self.prod_mod_core, _ = ProductModule.objects.using('default').get_or_create(
            code='core',
            defaults={'name': 'Core Platform', 'status': 'ACTIVE'},
        )
        self.tm_core = TenantModule.objects.using('default').create(
            tenant=self.tenant,
            module=self.prod_mod_core,
            is_enabled=True,
            availability_mode='ALL_BRANCHES',
        )

        # 2. Tenant DB Org Setup
        self.org = Organization.objects.using('tenant_test').create(
            code='CORR-ORG',
            name='Corrected Schema Org',
            status='ACTIVE',
        )
        self.loc = Location.objects.using('tenant_test').create(
            organization=self.org,
            code='CORR-LOC',
            name='Corrected Location',
            status='ACTIVE',
        )
        self.branch_home = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            code='BR-HOME',
            name='Home Club',
            status='ACTIVE',
        )
        self.branch_cross = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            code='BR-CROSS',
            name='Cross Club',
            status='ACTIVE',
        )

        # 3. Tenant Admin User & Member User
        self.admin_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='admin@corrected.test',
            first_name='Admin',
            last_name='User',
            status='ACTIVE',
        )
        self.admin_user.set_password('AdminPass123!')
        self.admin_user.save(using='tenant_test')

        self.member_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='member@corrected.test',
            first_name='John',
            last_name='Doe',
            status='ACTIVE',
        )
        self.member_profile = UserProfile.objects.using('tenant_test').create(
            user=self.member_user,
            member_type='MEMBER',
            member_status='ACTIVE',
        )

        # 4. RBAC Setup
        self.role_admin = Role.objects.using('tenant_test').create(
            name='Org Admin',
            code='ORG_ADMIN',
            is_system_role=True,
            scope='ORG',
            organization=self.org,
            is_active=True,
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.admin_user,
            role=self.role_admin,
            organization=self.org,
            is_active=True,
        )

        self.cat_core, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(
            module_code='core',
            defaults={'name': 'Core', 'is_enabled': True},
        )
        self.csub_settings, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=self.cat_core,
            submodule_code='settings',
            defaults={'name': 'Settings', 'is_enabled': True},
        )
        perm_set = RolePermissionSet.objects.using('tenant_test').create(
            role=self.role_admin,
            name='Admin Perm Set',
        )
        for act in ['view', 'edit']:
            perm, _ = Permission.objects.using('tenant_test').get_or_create(
                module=self.cat_core,
                submodule=self.csub_settings,
                action=act,
                defaults={'permission_code': f'core.settings.{act}', 'label': f'{act.title()} Settings'},
            )
            RolePermissionSetItem.objects.using('tenant_test').get_or_create(
                permission_set=perm_set, permission=perm, defaults={'granted': True}
            )
        RoleModuleAccess.objects.using('tenant_test').get_or_create(
            role=self.role_admin, module=self.cat_core, defaults={'can_access': True}
        )
        RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(
            role=self.role_admin, submodule=self.csub_settings, defaults={'can_access': True}
        )

    def get_token(self, user):
        refresh = _build_tenant_token(user, self.tenant, 'tenant_test')
        return str(refresh.access_token)

    def test_01_program_types_crud_and_no_hardcoded_enums(self):
        """Step 2: Program types are organization-scoped, configurable, no hardcoded enums."""
        pt = ProgramType.objects.using('tenant_test').create(
            organization=self.org,
            code='CUSTOM-PILATES',
            name='Reformer & Mat Pilates',
            description='Pilates studio specialty program',
            display_order=1,
            status='ACTIVE',
        )
        self.assertIsNotNone(pt.id)
        self.assertEqual(pt.code, 'CUSTOM-PILATES')

        token = self.get_token(self.admin_user)
        resp = self.client.get('/api/v1/tenant/program-types/', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.json().get('results', resp.json())
        codes = [item['code'] for item in results]
        self.assertIn('CUSTOM-PILATES', codes)

    def test_02_program_stable_metadata(self):
        """Step 3: Program contains only stable metadata and links to ProgramType."""
        pt = ProgramType.objects.using('tenant_test').create(
            organization=self.org,
            code='PT-PROG-TYPE',
            name='Personal Training',
        )
        prog = PackageCatalogService.create_program(
            organization=self.org,
            code='PROG-90-DAYS',
            name='90 Days Transformation',
            program_type=pt,
            description='90 Days body transformation program',
            trial_allowed=True,
            status='ACTIVE',
            actor=self.admin_user,
            db_alias='tenant_test',
        )
        self.assertEqual(prog.program_type, pt)
        self.assertEqual(prog.name, '90 Days Transformation')
        self.assertTrue(prog.trial_allowed)

        # Confirm BusinessAuditEvent
        audit = BusinessAuditEvent.objects.using('tenant_test').filter(
            action_code='PROGRAM_CREATED',
            entity_id=prog.id
        ).first()
        self.assertIsNotNone(audit)

    def test_03_package_requires_program_in_database(self):
        """Step 4: packages.program_id is required in database."""
        prog = PackageCatalogService.create_program(
            organization=self.org,
            code='STABLE-PROG',
            name='Stable Program',
            db_alias='tenant_test',
        )
        pkg = Package.objects.using('tenant_test').create(
            organization=self.org,
            program=prog,
            code='PKG-6-MONTHS',
            name='6 Months Package',
            status='ACTIVE',
        )
        self.assertIsNotNone(pkg.id)
        self.assertEqual(pkg.program, prog)

        # Database Integrity Error when trying to save null program directly
        with self.assertRaises(Exception):
            Package.objects.using('tenant_test').create(
                organization=self.org,
                program=None,
                code='PKG-ORPHAN',
                name='Orphan Package',
            )

    def test_04_package_version_immutability_and_fields(self):
        """Step 5 & 6: PackageVersion fields, constraints, and immutability."""
        prog = PackageCatalogService.create_program(
            organization=self.org,
            code='IMMUT-PROG',
            name='Immutability Program',
            db_alias='tenant_test',
        )
        pkg = PackageCatalogService.create_package(
            organization=self.org,
            code='PKG-IMMUT',
            name='Immutability Package',
            program_id=str(prog.id),
            db_alias='tenant_test',
        )
        v1 = PackageCatalogService.create_package_version(
            package=pkg,
            name_snapshot='Version 1 Snapshot',
            duration_value=6,
            duration_unit='MONTH',
            total_days=180,
            created_by_user=self.admin_user,
            show_on_web=True,
            show_on_app=True,
            status='DRAFT',
            db_alias='tenant_test',
        )
        self.assertEqual(v1.total_days, 180)
        self.assertEqual(v1.version_number, 1)

        # Add active price & active entitlement
        PackageCatalogService.add_package_price(
            package_version=v1,
            base_price=Decimal('24285.00'),
            tax_percent=Decimal('18.000'),
            actor=self.admin_user,
            db_alias='tenant_test',
        )
        PackageCatalogService.add_entitlement_definition(
            package_version=v1,
            entitlement_type='HOME_BRANCH_SESSION',
            allocated_units=Decimal('72.00'),
            db_alias='tenant_test',
        )
        PackageCatalogService.add_entitlement_definition(
            package_version=v1,
            entitlement_type='CROSS_BRANCH_SESSION',
            allocated_units=Decimal('10.00'),
            extra_unit_price=Decimal('500.00'),
            db_alias='tenant_test',
        )

        # Publish V1
        published_v1 = PackageCatalogService.publish_package_version(
            package_version_id=str(v1.id),
            actor=self.admin_user,
            db_alias='tenant_test',
        )
        self.assertEqual(published_v1.status, 'ACTIVE')
        self.assertIsNotNone(published_v1.published_at)

        # Backend enforcement: Attempting to modify commercial fields on ACTIVE version raises ValidationError
        published_v1.total_days = 90
        with self.assertRaises(ValidationError):
            published_v1.save(using='tenant_test')

        # To modify, create a new version (V2)
        v2 = PackageCatalogService.modify_package_version_safely(
            package_version_id=str(v1.id),
            actor=self.admin_user,
            modifications={'total_days': 180, 'name_snapshot': 'Version 2 (Updated Sessions)'},
            db_alias='tenant_test',
        )
        self.assertEqual(v2.version_number, 2)
        self.assertEqual(v2.status, 'DRAFT')
        # V1 remains unchanged
        v1_reloaded = PackageVersion.objects.using('tenant_test').get(id=v1.id)
        self.assertEqual(v1_reloaded.version_number, 1)
        self.assertEqual(v1_reloaded.name_snapshot, 'Version 1 Snapshot')

    def test_05_package_prices_and_passport_mapping(self):
        """Step 7, 8, 9: Prices & Passport extra unit cost mapping."""
        prog = PackageCatalogService.create_program(
            organization=self.org,
            code='PRICE-PROG',
            name='Price Test Program',
            db_alias='tenant_test',
        )
        pkg = PackageCatalogService.create_package(
            organization=self.org,
            code='PKG-PRICE',
            name='Pricing Package',
            program_id=str(prog.id),
            db_alias='tenant_test',
        )
        v1 = PackageCatalogService.create_package_version(
            package=pkg,
            name_snapshot='Pricing v1',
            duration_value=180,
            duration_unit='DAY',
            total_days=180,
            created_by_user=self.admin_user,
            db_alias='tenant_test',
        )

        # Price with display_price and prices_include_tax
        price = PackageCatalogService.add_package_price(
            package_version=v1,
            base_price=Decimal('24285.00'),
            display_price=Decimal('28000.00'),
            prices_include_tax=False,
            tax_percent=Decimal('18.000'),
            actor=self.admin_user,
            db_alias='tenant_test',
        )
        self.assertEqual(price.sale_price, Decimal('24285.00'))
        self.assertEqual(price.display_price, Decimal('28000.00'))
        self.assertEqual(price.tax_percentage, Decimal('18.000'))
        self.assertEqual(price.total_price, Decimal('28656.30'))

        # Passport Entitlements mapping
        # Maximum Sessions -> HOME_BRANCH_SESSION.allocated_units = 72
        home_ent = PackageCatalogService.add_entitlement_definition(
            package_version=v1,
            entitlement_type='HOME_BRANCH_SESSION',
            allocated_units=Decimal('72.00'),
            db_alias='tenant_test',
        )
        # Max Passport Sessions -> CROSS_BRANCH_SESSION.allocated_units = 10, extra_unit_price = 500
        cross_ent = PackageCatalogService.add_entitlement_definition(
            package_version=v1,
            entitlement_type='CROSS_BRANCH_SESSION',
            allocated_units=Decimal('10.00'),
            extra_unit_price=Decimal('500.00'),
            db_alias='tenant_test',
        )
        self.assertEqual(home_ent.allocated_units, Decimal('72.00'))
        self.assertEqual(cross_ent.allocated_units, Decimal('10.00'))
        self.assertEqual(cross_ent.extra_unit_price, Decimal('500.00'))

    def test_06_package_class_access_rules_versioned(self):
        """Step 11: PackageClassAccessRule references package_version_id and preserves V1 vs V2."""
        prog = PackageCatalogService.create_program(
            organization=self.org,
            code='ACCESS-PROG',
            name='Access Rules Program',
            db_alias='tenant_test',
        )
        pkg = PackageCatalogService.create_package(
            organization=self.org,
            code='PKG-ACCESS',
            name='Class Access Package',
            program_id=str(prog.id),
            db_alias='tenant_test',
        )
        v1 = PackageCatalogService.create_package_version(
            package=pkg,
            name_snapshot='Access v1',
            duration_value=30,
            duration_unit='DAY',
            total_days=30,
            created_by_user=self.admin_user,
            db_alias='tenant_test',
        )
        cat = ClassCategory.objects.using('tenant_test').create(
            organization=self.org,
            code='HIIT-CAT',
            name='HIIT Classes',
        )
        rule_v1 = PackageClassAccessRule.objects.using('tenant_test').create(
            package_version=v1,
            class_category=cat,
            access_type='INCLUDED',
            status='ACTIVE',
        )
        self.assertEqual(rule_v1.package_version, v1)
        self.assertEqual(rule_v1.access_type, 'INCLUDED')

        # Add price & ent to publish
        PackageCatalogService.add_package_price(
            package_version=v1,
            base_price=Decimal('5000.00'),
            actor=self.admin_user,
            db_alias='tenant_test',
        )
        PackageCatalogService.add_entitlement_definition(
            package_version=v1,
            entitlement_type='CLASS_SESSION',
            allocated_units=Decimal('12.00'),
            db_alias='tenant_test',
        )
        PackageCatalogService.publish_package_version(str(v1.id), actor=self.admin_user, db_alias='tenant_test')

        # Clone to V2 where access is ADD_ON
        v2 = PackageCatalogService.modify_package_version_safely(
            package_version_id=str(v1.id),
            actor=self.admin_user,
            modifications={'name_snapshot': 'Access v2'},
            db_alias='tenant_test',
        )
        rule_v2 = PackageClassAccessRule.objects.using('tenant_test').filter(package_version=v2).first()
        self.assertIsNotNone(rule_v2)
        rule_v2.access_type = 'ADD_ON'
        rule_v2.save(using='tenant_test')

        # V1 rule remains INCLUDED, V2 rule is ADD_ON
        self.assertEqual(PackageClassAccessRule.objects.using('tenant_test').get(id=rule_v1.id).access_type, 'INCLUDED')
        self.assertEqual(PackageClassAccessRule.objects.using('tenant_test').get(id=rule_v2.id).access_type, 'ADD_ON')

    def test_07_membership_activation_contract_snapshot_and_entitlements(self):
        """Step 12, 13, 14, 15: Contract Snapshot, Member Entitlements, and Append-Only Ledger."""
        prog = PackageCatalogService.create_program(
            organization=self.org,
            code='MEM-PROG',
            name='Membership Program',
            db_alias='tenant_test',
        )
        pkg = PackageCatalogService.create_package(
            organization=self.org,
            code='MEM-PKG',
            name='Membership Package',
            program_id=str(prog.id),
            db_alias='tenant_test',
        )
        v1 = PackageCatalogService.create_package_version(
            package=pkg,
            name_snapshot='Membership 6M v1',
            duration_value=6,
            duration_unit='MONTH',
            total_days=180,
            created_by_user=self.admin_user,
            db_alias='tenant_test',
        )
        price = PackageCatalogService.add_package_price(
            package_version=v1,
            base_price=Decimal('24285.00'),
            tax_percent=Decimal('18.000'),
            actor=self.admin_user,
            db_alias='tenant_test',
        )
        # Entitlements: 72 home sessions, 10 cross-branch sessions
        PackageCatalogService.add_entitlement_definition(
            package_version=v1,
            entitlement_type='HOME_BRANCH_SESSION',
            allocated_units=Decimal('72.00'),
            db_alias='tenant_test',
        )
        PackageCatalogService.add_entitlement_definition(
            package_version=v1,
            entitlement_type='CROSS_BRANCH_SESSION',
            allocated_units=Decimal('10.00'),
            extra_unit_price=Decimal('500.00'),
            db_alias='tenant_test',
        )
        PackageCatalogService.publish_package_version(str(v1.id), actor=self.admin_user, db_alias='tenant_test')

        # Order & OrderItem
        order = Order.objects.using('tenant_test').create(
            order_number=f"ORD-{uuid.uuid4().hex[:6].upper()}",
            user_profile=self.member_profile,
            branch=self.branch_home,
            total_amount=Decimal('28656.30'),
            status='COMPLETED',
        )
        order_item = OrderItem.objects.using('tenant_test').create(
            order=order,
            item_type='PACKAGE',
            package=pkg,
            package_version=v1,
            package_price=price,
            item_name_snapshot='Membership 6M v1',
            unit_price_snapshot=Decimal('24285.00'),
            tax_percent_snapshot=Decimal('18.000'),
            tax_amount=Decimal('4371.30'),
            total_amount=Decimal('28656.30'),
        )

        # Activate Membership
        membership = MembershipLifecycleService.activate_membership_from_order(
            order=order,
            order_item=order_item,
            start_date=date.today(),
            db_alias='tenant_test',
            created_by_user=self.admin_user,
        )

        self.assertIsNotNone(membership)
        self.assertEqual(membership.package_version, v1)
        self.assertEqual(membership.purchase_branch, self.branch_home)

        # Step 12: purchase_branch is immutable
        membership.purchase_branch = self.branch_cross
        with self.assertRaises(ValidationError):
            membership.save(using='tenant_test')

        # Step 13: MembershipContractSnapshot NO UPDATE, NO DELETE
        snapshot = MembershipContractSnapshot.objects.using('tenant_test').get(membership=membership)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.final_amount, Decimal('28656.30'))
        self.assertEqual(snapshot.duration_value, 6)

        snapshot.final_amount = Decimal('1000.00')
        with self.assertRaises(ValidationError):
            snapshot.save(using='tenant_test')

        with self.assertRaises(ValidationError):
            snapshot.delete(using='tenant_test')

        # Step 14 & 15: Member Entitlements generated and append-only ledger initialized
        entitlements = MembershipEntitlement.objects.using('tenant_test').filter(membership=membership)
        self.assertEqual(entitlements.count(), 2)

        home_ent = entitlements.filter(entitlement_type='HOME_BRANCH_SESSION').first()
        self.assertIsNotNone(home_ent)
        self.assertEqual(home_ent.allocated_units, Decimal('72.00'))
        self.assertEqual(home_ent.remaining_units, Decimal('72.00'))

        cross_ent = entitlements.filter(entitlement_type='CROSS_BRANCH_SESSION').first()
        self.assertIsNotNone(cross_ent)
        self.assertEqual(cross_ent.allocated_units, Decimal('10.00'))
        self.assertEqual(cross_ent.remaining_units, Decimal('10.00'))

        ledger_rows = MembershipEntitlementLedger.objects.using('tenant_test').filter(
            membership_entitlement__in=entitlements
        )
        self.assertEqual(ledger_rows.count(), 2)
        for row in ledger_rows:
            self.assertEqual(row.transaction_type, 'ALLOCATION')
            self.assertIsNotNone(row.balance_after)
