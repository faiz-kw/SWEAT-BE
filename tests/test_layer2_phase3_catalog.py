"""
Layer 2 Phase 3 Tests: Module C (Terms & Acceptance) & Module D (Programs, Packages & Catalog)

Covers:
- Program Categories and Programs
- Packages and Version Numbering
- Mandatory Package Immutability: Published versions are never overwritten; modifications clone to new version
- Version Publishing prerequisites (requires prices & entitlements)
- Terms Documents, Version Retirement, and Legal Acceptance records
- REST API ViewSets and custom actions
"""

import uuid
from decimal import Decimal
from django.utils import timezone
from django.core.exceptions import ValidationError
from rest_framework.test import APITestCase
from rest_framework import status

from apps.authentication.views import _build_tenant_token
from apps.master.models import Tenant, TenantDataSource, ProductModule, TenantModule, SaasPlan, TenantSubscription
from apps.tenant_core.context import set_tenant_db_alias
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_rbac import (
    Role, RoleAssignment, ModuleCatalog, SubmoduleCatalog,
    Permission, RolePermissionSet, RolePermissionSetItem,
    RoleModuleAccess, RoleSubmoduleAccess,
)
from apps.tenant_core.services_crm import CRMLeadService
from apps.tenant_core.models_catalog import (
    TermsDocument, TermsDocumentVersion, TermsAcceptance,
    ProgramCategory, Program, Package, PackageVersion,
    PackagePrice, PackageBranchAvailability, PackageEntitlementDefinition,
)
from apps.tenant_core.services_catalog import PackageCatalogService, TermsLegalService
from apps.tenant_core.models_audit_outbox import BusinessAuditEvent, DomainOutboxEvent


class Layer2Phase3CatalogTestCase(APITestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')

        # 1. Master DB Setup
        self.tenant = Tenant.objects.using('default').create(
            code='CATALOG-TENANT',
            name='Catalog Gym',
            slug='catalog-gym',
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
            name='Catalog Plan',
            code='CATALOG-PLAN',
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
            code='CATALOG-ORG',
            name='Catalog Gym Org',
            status='ACTIVE',
        )
        self.loc = Location.objects.using('tenant_test').create(
            organization=self.org,
            code='CAT-LOC',
            name='Catalog Location',
            status='ACTIVE',
        )
        self.branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            code='BR-MAIN',
            name='Main Downtown Branch',
            status='ACTIVE',
        )

        # 3. Tenant Admin User
        self.admin_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='admin@catalog.test',
            first_name='Catalog',
            last_name='Admin',
            status='ACTIVE',
        )
        self.admin_user.set_password('TestPass123!')
        self.admin_user.save(using='tenant_test')

        # 4. RBAC Setup for core.settings
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
        self.perm_settings_view, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.cat_core,
            submodule=self.csub_settings,
            action='view',
            defaults={'permission_code': 'core.settings.view', 'label': 'View Settings'},
        )
        self.perm_settings_edit, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.cat_core,
            submodule=self.csub_settings,
            action='edit',
            defaults={'permission_code': 'core.settings.edit', 'label': 'Edit Settings'},
        )

        self.perm_set = RolePermissionSet.objects.using('tenant_test').create(
            role=self.role_admin,
            name='Admin Perm Set',
        )
        RoleModuleAccess.objects.using('tenant_test').get_or_create(
            role=self.role_admin, module=self.cat_core, defaults={'can_access': True}
        )
        RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(
            role=self.role_admin, submodule=self.csub_settings, defaults={'can_access': True}
        )
        RolePermissionSetItem.objects.using('tenant_test').get_or_create(
            permission_set=self.perm_set, permission=self.perm_settings_view, defaults={'granted': True}
        )
        RolePermissionSetItem.objects.using('tenant_test').get_or_create(
            permission_set=self.perm_set, permission=self.perm_settings_edit, defaults={'granted': True}
        )

    def tearDown(self):
        set_tenant_db_alias(None)
        super().tearDown()

    def get_token(self, user):
        refresh = _build_tenant_token(
            user=user,
            tenant=self.tenant,
            db_alias='tenant_test',
        )
        return str(refresh.access_token)

    def test_01_create_program_and_category(self):
        """Test creating program categories and programs within an organization."""
        cat = ProgramCategory.objects.using('tenant_test').create(
            organization=self.org,
            code='FITNESS-CAT',
            name='General Fitness',
            display_order=1,
            status='ACTIVE',
        )

        program = PackageCatalogService.create_program(
            organization=self.org,
            code='STRENGTH-PROG',
            name='Strength & Conditioning',
            program_type='FITNESS',
            category_id=str(cat.id),
            actor=self.admin_user,
            db_alias='tenant_test',
        )
        self.assertEqual(program.code, 'STRENGTH-PROG')
        self.assertEqual(program.category, cat)

        audit = BusinessAuditEvent.objects.using('tenant_test').filter(
            action_code='PROGRAM_CREATED', entity_id=program.id
        ).first()
        self.assertIsNotNone(audit)

    def test_02_create_package_and_version(self):
        """Test package creation and initial version creation."""
        pkg = PackageCatalogService.create_package(
            organization=self.org,
            code='GOLD-ANNUAL',
            name='Gold Annual Membership',
            actor=self.admin_user,
            db_alias='tenant_test',
        )

        v1 = PackageCatalogService.create_package_version(
            package=pkg,
            name_snapshot='Gold Annual 2026',
            duration_value=12,
            duration_unit='MONTH',
            created_by_user=self.admin_user,
            validity_days=365,
            status='DRAFT',
            db_alias='tenant_test',
        )
        self.assertEqual(v1.version_number, 1)
        self.assertEqual(v1.status, 'DRAFT')

        v2 = PackageCatalogService.create_package_version(
            package=pkg,
            name_snapshot='Gold Annual 2027 Preview',
            duration_value=12,
            duration_unit='MONTH',
            created_by_user=self.admin_user,
            status='DRAFT',
            db_alias='tenant_test',
        )
        self.assertEqual(v2.version_number, 2)

    def test_03_publish_package_version_requires_price_and_entitlements(self):
        """Publishing must fail without prices and entitlement definitions."""
        pkg = PackageCatalogService.create_package(
            organization=self.org,
            code='SILVER-MONTHLY',
            name='Silver Monthly',
            actor=self.admin_user,
            db_alias='tenant_test',
        )

        v1 = PackageCatalogService.create_package_version(
            package=pkg,
            name_snapshot='Silver Monthly',
            duration_value=1,
            duration_unit='MONTH',
            created_by_user=self.admin_user,
            status='DRAFT',
            db_alias='tenant_test',
        )

        with self.assertRaises(ValidationError):
            PackageCatalogService.publish_package_version(
                package_version_id=str(v1.id),
                actor=self.admin_user,
                db_alias='tenant_test',
            )

        PackageCatalogService.add_package_price(
            package_version=v1,
            base_price=Decimal('2500.00'),
            tax_percent=Decimal('18.000'),
            actor=self.admin_user,
            db_alias='tenant_test',
        )

        with self.assertRaises(ValidationError):
            PackageCatalogService.publish_package_version(
                package_version_id=str(v1.id),
                actor=self.admin_user,
                db_alias='tenant_test',
            )

        PackageCatalogService.add_entitlement_definition(
            package_version=v1,
            entitlement_type='OPEN_ACCESS',
            is_unlimited=True,
            db_alias='tenant_test',
        )

        published = PackageCatalogService.publish_package_version(
            package_version_id=str(v1.id),
            actor=self.admin_user,
            db_alias='tenant_test',
        )
        self.assertEqual(published.status, 'ACTIVE')

        outbox = DomainOutboxEvent.objects.using('tenant_test').filter(
            event_type='PACKAGE_PUBLISHED', aggregate_id=str(v1.id)
        ).first()
        self.assertIsNotNone(outbox)

    def test_04_package_immutability_enforcement(self):
        """
        Published PackageVersions are immutable.
        Commercial changes clone into a new version preserving the original.
        """
        pkg = PackageCatalogService.create_package(
            organization=self.org,
            code='PILATES-PACK',
            name='Pilates 10-Pack',
            actor=self.admin_user,
            db_alias='tenant_test',
        )

        v1 = PackageCatalogService.create_package_version(
            package=pkg,
            name_snapshot='Pilates 10-Pack Original',
            duration_value=3,
            duration_unit='MONTH',
            created_by_user=self.admin_user,
            status='DRAFT',
            db_alias='tenant_test',
        )

        PackageCatalogService.add_package_price(
            package_version=v1,
            base_price=Decimal('5000.00'),
            actor=self.admin_user,
            db_alias='tenant_test',
        )
        PackageCatalogService.add_entitlement_definition(
            package_version=v1,
            entitlement_type='CLASS_SESSION',
            allocated_units=Decimal('10.00'),
            db_alias='tenant_test',
        )

        PackageCatalogService.publish_package_version(
            package_version_id=str(v1.id),
            actor=self.admin_user,
            db_alias='tenant_test',
        )

        new_v2 = PackageCatalogService.modify_package_version_safely(
            package_version_id=str(v1.id),
            actor=self.admin_user,
            modifications={
                'name_snapshot': 'Pilates 12-Pack Super Saver',
                'duration_value': 4,
            },
            db_alias='tenant_test',
        )

        v1.refresh_from_db()
        self.assertEqual(v1.status, 'ACTIVE')
        self.assertEqual(v1.name_snapshot, 'Pilates 10-Pack Original')
        self.assertEqual(v1.duration_value, 3)

        self.assertEqual(new_v2.version_number, 2)
        self.assertEqual(new_v2.name_snapshot, 'Pilates 12-Pack Super Saver')
        self.assertEqual(new_v2.duration_value, 4)
        self.assertEqual(new_v2.status, 'DRAFT')
        self.assertEqual(new_v2.prices.count(), 1)
        self.assertEqual(new_v2.entitlement_definitions.count(), 1)
        self.assertEqual(new_v2.prices.first().base_price, Decimal('5000.00'))

    def test_05_terms_document_versioning_and_acceptance(self):
        """Test legal terms document versioning, publishing, and acceptance tracking."""
        doc = TermsLegalService.create_terms_document(
            organization=self.org,
            code='GYM-TERMS-2026',
            name='Gym Membership Agreement',
            document_type='MEMBERSHIP_TERMS',
            actor=self.admin_user,
            db_alias='tenant_test',
        )

        v1 = TermsLegalService.create_terms_version(
            terms_document=doc,
            content_text='Terms Version 1: You agree to gym safety rules.',
            created_by_user=self.admin_user,
            status='DRAFT',
            db_alias='tenant_test',
        )
        self.assertEqual(v1.version_number, 1)

        TermsLegalService.publish_terms_version(
            terms_document_version_id=str(v1.id),
            actor=self.admin_user,
            db_alias='tenant_test',
        )
        v1.refresh_from_db()
        self.assertEqual(v1.status, 'ACTIVE')

        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Aarav',
            last_name='Sharma',
            phone='+919876500099',
            db_alias='tenant_test',
        )

        acc = TermsLegalService.record_terms_acceptance(
            terms_document_version_id=str(v1.id),
            accepted_via='WEB',
            lead_id=str(lead.id),
            ip_address='127.0.0.1',
            db_alias='tenant_test',
        )
        self.assertEqual(acc.lead, lead)
        self.assertEqual(acc.terms_document_version, v1)

        v2 = TermsLegalService.create_terms_version(
            terms_document=doc,
            content_text='Terms Version 2 draft',
            created_by_user=self.admin_user,
            status='DRAFT',
            db_alias='tenant_test',
        )
        with self.assertRaises(ValidationError):
            TermsLegalService.record_terms_acceptance(
                terms_document_version_id=str(v2.id),
                accepted_via='WEB',
                lead_id=str(lead.id),
                db_alias='tenant_test',
            )

    def test_06_package_api_endpoints(self):
        """Verify PackageViewSet REST APIs and custom actions."""
        token = self.get_token(self.admin_user)

        # 1. Create Package via API
        resp = self.client.post(
            '/api/v1/tenant/packages/',
            {
                'code': 'API-PKG',
                'name': 'API Package',
            },
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        pkg_id = resp.json()['id']

        # 2. Create version via custom action
        resp_ver = self.client.post(
            f'/api/v1/tenant/packages/{pkg_id}/create-version/',
            {
                'name_snapshot': 'API Package v1',
                'duration_value': 6,
                'duration_unit': 'MONTH',
            },
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(resp_ver.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp_ver.json()['version_number'], 1)

        # 3. Query packages list
        resp_list = self.client.get(
            '/api/v1/tenant/packages/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(resp_list.status_code, status.HTTP_200_OK)
        results = resp_list.json().get('results', resp_list.json())
        self.assertTrue(any(p.get('code') == 'API-PKG' for p in results))
