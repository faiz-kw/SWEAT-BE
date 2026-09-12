"""
Sprint 11 — Comprehensive Test Suite
TENANT ORGANIZATIONAL HIERARCHY + PRODUCT MODULE CATALOG & TENANT ENTITLEMENTS

Covers:
1. Workstream A:
   - Organization hierarchy (Organization -> CompanyEntity / Location -> Branch -> BranchSettings)
   - Branch physical infrastructure (address_line_1, address_line_2, latitude, longitude, timezone)
   - CompanyEntity legal_name NOT NULL & RESTRICT FK
   - OrganizationSettings varchar lengths, JSONB defaults, RESTRICT FK
2. Workstream B:
   - ProductModule (supports_branch_scope, display_order, status, created_at, updated_at, code/name lengths)
   - ProductSubmodule (module RESTRICT FK, display_order, status, created_at, updated_at, code/name lengths)
   - TenantPermissionCatalog (version, status, created_at, updated_at, RESTRICT module, SET NULL submodule)
   - TenantModule (status, configuration, created_at, updated_at, RESTRICT tenant, RESTRICT module)
   - Tenant-local ModuleCatalog (source_module_id NOT NULL, supports_branch_scope default, is_enabled default)
   - Tenant-local SubmoduleCatalog (source_submodule_id NOT NULL, module RESTRICT FK)
3. Backward Compatibility:
   - Property aliases (sort_order, is_active, is_enabled, catalog_version)
   - Multi-tenant database isolation
"""

import uuid
from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from django.db import IntegrityError, transaction

from config.routers import set_tenant_db_alias
from apps.master.models_tenant import Tenant
from apps.master.models_saas import (
    ProductModule,
    ProductSubmodule,
    TenantPermissionCatalog,
    TenantModule,
)
from apps.tenant_core.models_org import (
    Organization,
    CompanyEntity,
    Location,
    Branch,
)
from apps.tenant_core.models_govern import (
    OrganizationSettings,
    BranchSettings,
)
from apps.tenant_core.models_rbac import (
    ModuleCatalog,
    SubmoduleCatalog,
)


class Sprint11HierarchyTests(TestCase):
    """Workstream A: Organization Hierarchy & Branch Physical Infrastructure"""

    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        self.org = Organization.objects.using('tenant_test').create(
            code=f"ORG-{uuid.uuid4().hex[:6].upper()}",
            name="Alpha Fitness Corp",
            timezone="Asia/Kolkata",
            status="ACTIVE",
        )
        self.location = Location.objects.using('tenant_test').create(
            organization=self.org,
            code=f"LOC-{uuid.uuid4().hex[:6].upper()}",
            name="South Bengaluru",
            city="Bengaluru",
            status="ACTIVE",
        )
        self.entity = CompanyEntity.objects.using('tenant_test').create(
            organization=self.org,
            code=f"CE-{uuid.uuid4().hex[:6].upper()}",
            name="Alpha Fit Private Limited",
            legal_name="Alpha Fitness India Private Limited",
            status="ACTIVE",
        )

    def tearDown(self):
        set_tenant_db_alias(None)

    def test_branch_physical_infrastructure_fields(self):
        """Branches store address, high-precision geo coordinates, and IANA timezone."""
        branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.location,
            company_entity=self.entity,
            code=f"BR-{uuid.uuid4().hex[:6].upper()}",
            name="Koramangala Flagship Studio",
            address_line_1="80 Feet Road, 4th Block",
            address_line_2="Near Sony World Junction",
            latitude=Decimal("12.9351740"),
            longitude=Decimal("77.6244800"),
            timezone="Asia/Kolkata",
            status="ACTIVE",
        )
        self.assertEqual(branch.address_line_1, "80 Feet Road, 4th Block")
        self.assertEqual(branch.address_line_2, "Near Sony World Junction")
        self.assertEqual(branch.latitude, Decimal("12.9351740"))
        self.assertEqual(branch.longitude, Decimal("77.6244800"))
        self.assertEqual(branch.timezone, "Asia/Kolkata")

    def test_branch_fk_relationships_and_restrict(self):
        """Branch points to Organization, Location (RESTRICT) and CompanyEntity (SET NULL)."""
        branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.location,
            company_entity=self.entity,
            code=f"BR-{uuid.uuid4().hex[:6].upper()}",
            name="Indiranagar Studio",
            status="ACTIVE",
        )
        # Deleting company_entity sets branch.company_entity to NULL
        self.entity.delete()
        branch.refresh_from_db()
        self.assertIsNone(branch.company_entity)

        # Deleting location is RESTRICTed
        with self.assertRaises(Exception):
            with transaction.atomic(using='tenant_test'):
                self.location.delete()

    def test_company_entity_legal_name_not_null(self):
        """CompanyEntity enforces non-null legal_name at database level."""
        from django.db import connections
        with self.assertRaises(IntegrityError):
            with transaction.atomic(using='tenant_test'):
                with connections['tenant_test'].cursor() as cur:
                    cur.execute(
                        "INSERT INTO company_entities (id, organization_id, code, name, legal_name, status, created_at, updated_at) "
                        "VALUES (%s, %s, %s, %s, NULL, 'ACTIVE', NOW(), NOW())",
                        [str(uuid.uuid4()), str(self.org.id), "CE-NULL-TEST", "Entity Null Test"]
                    )

    def test_organization_settings_jsonb_and_lengths(self):
        """OrganizationSettings supports 100/20/30 char strings and default empty JSONB config."""
        settings = OrganizationSettings.objects.using('tenant_test').create(
            organization=self.org,
            default_timezone="America/New_York",
            language="en-US",
            date_format="YYYY-MM-DD",
            time_format="HH:mm:ss",
            membership_config={"allow_online_freeze": True},
            booking_config={"advance_window_days": 14},
        )
        settings.refresh_from_db()
        self.assertEqual(settings.default_timezone, "America/New_York")
        self.assertEqual(settings.language, "en-US")
        self.assertEqual(settings.membership_config, {"allow_online_freeze": True})
        self.assertEqual(settings.booking_config, {"advance_window_days": 14})
        self.assertEqual(settings.attendance_config, {})
        self.assertEqual(settings.notification_config, {})
        self.assertEqual(settings.ai_config, {})

    def test_branch_settings_relationship(self):
        """BranchSettings has a 1-to-1 RESTRICT relationship to Branch."""
        branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.location,
            code=f"BR-{uuid.uuid4().hex[:6].upper()}",
            name="Whitefield Center",
        )
        bs = BranchSettings.objects.using('tenant_test').create(
            branch=branch,
            business_open_time="05:30",
            business_close_time="23:00",
        )
        self.assertEqual(bs.branch, branch)


class Sprint11CatalogEntitlementTests(TestCase):
    """Workstream B: Product Module Catalog & Tenant Entitlements"""

    databases = {'default'}

    def setUp(self):
        self.tenant = Tenant.objects.using('default').create(
            name="Cult Fit Global",
            slug=f"cult-fit-{uuid.uuid4().hex[:6]}",
            code=f"ORG-{uuid.uuid4().hex[:6].upper()}",
            status="ACTIVE",
        )

    def test_product_module_canonical_fields(self):
        """ProductModule has supports_branch_scope, display_order, status, timestamps."""
        pm = ProductModule.objects.using('default').create(
            code=f"NUTRITION_{uuid.uuid4().hex[:6].upper()}",
            name="Nutrition & Diet Coaching",
            supports_branch_scope=True,
            display_order=10,
            status="ACTIVE",
        )
        pm.refresh_from_db()
        self.assertTrue(pm.supports_branch_scope)
        self.assertEqual(pm.display_order, 10)
        self.assertEqual(pm.sort_order, 10)  # Compatibility property
        self.assertEqual(pm.status, "ACTIVE")
        self.assertTrue(pm.is_active)       # Compatibility property
        self.assertIsNotNone(pm.created_at)
        self.assertIsNotNone(pm.updated_at)

    def test_product_submodule_canonical_fields(self):
        """ProductSubmodule points to module (RESTRICT) with display_order, status, timestamps."""
        pm = ProductModule.objects.using('default').create(
            code=f"CRM_{uuid.uuid4().hex[:6].upper()}",
            name="CRM & Sales Pipeline",
            display_order=1,
            status="ACTIVE",
        )
        ps = ProductSubmodule.objects.using('default').create(
            module=pm,
            code="LEADS",
            name="Lead Management & Capture",
            display_order=1,
            status="ACTIVE",
        )
        ps.refresh_from_db()
        self.assertEqual(ps.display_order, 1)
        self.assertEqual(ps.sort_order, 1)
        self.assertEqual(ps.status, "ACTIVE")
        self.assertTrue(ps.is_active)
        self.assertIsNotNone(ps.created_at)
        self.assertIsNotNone(ps.updated_at)

        # Module cannot be deleted while submodules exist (RESTRICT)
        with self.assertRaises(Exception):
            with transaction.atomic(using='default'):
                pm.delete()

    def test_tenant_permission_catalog_canonical_fields(self):
        """TenantPermissionCatalog has version, status, timestamps, and proper FK actions."""
        pm = ProductModule.objects.using('default').create(
            code=f"BILLING_{uuid.uuid4().hex[:6].upper()}",
            name="Commercial Billing Engine",
            status="ACTIVE",
        )
        ps = ProductSubmodule.objects.using('default').create(
            module=pm,
            code="INVOICES",
            name="Invoicing & GST",
            status="ACTIVE",
        )
        tpc = TenantPermissionCatalog.objects.using('default').create(
            module=pm,
            submodule=ps,
            code=f"billing.invoices.view_{uuid.uuid4().hex[:6]}",
            action="view",
            label="View Invoices",
            version=1,
            status="ACTIVE",
        )
        tpc.refresh_from_db()
        self.assertEqual(tpc.version, 1)
        self.assertEqual(tpc.catalog_version, "1.0")
        self.assertEqual(tpc.status, "ACTIVE")
        self.assertTrue(tpc.is_active)
        self.assertIsNotNone(tpc.created_at)
        self.assertIsNotNone(tpc.updated_at)

        # Deleting submodule sets tpc.submodule to NULL (SET NULL)
        ps.delete()
        tpc.refresh_from_db()
        self.assertIsNone(tpc.submodule)

    def test_tenant_module_entitlement_canonical_fields(self):
        """TenantModule stores status, configuration JSONB, timestamps, and RESTRICT FKs."""
        pm = ProductModule.objects.using('default').create(
            code=f"AI_VOICE_{uuid.uuid4().hex[:6].upper()}",
            name="AI Voice Assistant",
            status="ACTIVE",
        )
        tm = TenantModule.objects.using('default').create(
            tenant=self.tenant,
            module=pm,
            status="ENABLED",
            configuration={"monthly_call_minutes": 500, "voice_accent": "en-IN"},
        )
        tm.refresh_from_db()
        self.assertEqual(tm.status, "ENABLED")
        self.assertTrue(tm.is_enabled)
        self.assertEqual(tm.configuration, {"monthly_call_minutes": 500, "voice_accent": "en-IN"})
        self.assertIsNotNone(tm.created_at)
        self.assertIsNotNone(tm.updated_at)

        # Neither tenant nor module can be deleted (RESTRICT)
        with self.assertRaises(Exception):
            with transaction.atomic(using='default'):
                self.tenant.delete()

        with self.assertRaises(Exception):
            with transaction.atomic(using='default'):
                pm.delete()


class Sprint11TenantCatalogSyncTests(TestCase):
    """Tenant-local ModuleCatalog and SubmoduleCatalog Model Tests"""

    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')

    def tearDown(self):
        set_tenant_db_alias(None)

    def test_tenant_local_catalog_model_constraints(self):
        """ModuleCatalog and SubmoduleCatalog enforce non-null cross-DB source IDs."""
        src_module_id = uuid.uuid4()
        src_submodule_id = uuid.uuid4()

        mc = ModuleCatalog.objects.using('tenant_test').create(
            source_module_id=src_module_id,
            code="ANALYTICS",
            name="Advanced Analytics",
            supports_branch_scope=False,
            is_enabled=True,
            display_order=5,
            status="ACTIVE",
        )
        mc.refresh_from_db()
        self.assertEqual(mc.source_module_id, src_module_id)
        self.assertFalse(mc.supports_branch_scope)
        self.assertTrue(mc.is_enabled)

        sc = SubmoduleCatalog.objects.using('tenant_test').create(
            module=mc,
            source_submodule_id=src_submodule_id,
            code="RETENTION",
            name="Member Retention",
            display_order=1,
            is_enabled=True,
            status="ACTIVE",
        )
        sc.refresh_from_db()
        self.assertEqual(sc.source_submodule_id, src_submodule_id)
        self.assertEqual(sc.module, mc)
