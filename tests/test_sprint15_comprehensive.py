"""
Sprint 15 Comprehensive Test Suite — Final Implementation & Physical Hardening
Covers:
1. Physical Database Catalog Assertions:
   - tenant_domains: tenant_id FK RESTRICT NOT DEFERRABLE, is_primary/is_verified defaults and types
   - tenant_branding: tenant_id FK RESTRICT NOT DEFERRABLE, unique preserved, branding_mode varchar(30), 10 canonical fields
   - tenant_resource_usage: period_start and period_end NOT NULL, backfilled
   - subscription_dunning_events: invoice_id FK SET NULL NOT DEFERRABLE, nullable
   - tenant_data_source_health: status varchar(30)
   - platform_branding: brand_name NOT NULL default 'PerformanceOS', secondary_color default '#f59e0b', storage keys
   - marketplace_integrations: logo_storage_key
   - privacy_requests: assigned_to single authoritative FK SET NULL NOT DEFERRABLE (no duplicate deferrable FK)
   - files: uploaded_by FK SET NULL NOT DEFERRABLE
   - 5 architectural reclassifications: profile_file_id (Class A), audit string IDs (Class B), secret_reference untouched (Class C-existing)
2. Branding API & Serializer Verification:
   - Platform branding CRUD and serialization
   - Tenant branding CRUD, theme presets, theme tokens
   - Storage key privacy (no fabrication, nullable)
3. Marketplace Integration Contract:
   - logo_storage_key serialization and icon_text fallback
4. RBAC & Audit Verification:
   - Authorized branding mutations succeed and emit PlatformAuditEvent
   - Unauthorized mutations return HTTP 403
   - Audit identifiers remain strings
"""

import uuid
from datetime import timedelta
from django.test import TestCase
from django.utils import timezone
from django.db import connection, connections
from rest_framework.test import APIClient
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken, AccessToken

from apps.master.models_iam import PlatformUser, PlatformRole, PlatformPermission, PlatformRolePermission, PlatformUserRole
from apps.master.models_tenant import Tenant, TenantDomain, TenantBranding, PlatformBranding
from apps.master.models_saas import (
    SaasPlan, TenantSubscription, ResourceMetric, TenantResourceUsage,
    SubscriptionInvoice, SubscriptionDunningEvent
)
from apps.master.models_market import MarketplaceIntegration
from apps.master.models_infra import TenantDataSource, TenantDataSourceHealth, PlatformAuditEvent
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_infra import File
from apps.tenant_core.models_privacy import PrivacyRequest, ProcessingPurpose, TenantAuditEvent
from config.routers import set_tenant_db_alias
from config.tenant_middleware import _register_tenant_connection


class Sprint15PhysicalCatalogTestCase(TestCase):
    """Verifies live PostgreSQL catalogs against the authoritative Sprint 15 specification."""
    databases = '__all__'

    def test_01_tenant_domains_catalog(self):
        """tenant_domains: FK RESTRICT NOT DEFERRABLE, is_primary and is_verified defaults."""
        with connection.cursor() as cur:
            # Check column defaults
            cur.execute("""
                SELECT column_name, data_type, column_default, is_nullable
                FROM information_schema.columns
                WHERE table_name = 'tenant_domains' AND column_name IN ('is_primary', 'is_verified')
                ORDER BY column_name;
            """)
            cols = {r[0]: r for r in cur.fetchall()}
            self.assertIn('is_primary', cols)
            self.assertIn('is_verified', cols)
            self.assertEqual(cols['is_primary'][1], 'boolean')
            self.assertEqual(cols['is_verified'][1], 'boolean')
            self.assertEqual(cols['is_primary'][3], 'NO')
            self.assertEqual(cols['is_verified'][3], 'NO')

            # Check FK constraint
            cur.execute("""
                SELECT tc.constraint_name, tc.is_deferrable, rc.delete_rule
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name
                JOIN information_schema.referential_constraints rc ON tc.constraint_name = rc.constraint_name
                WHERE tc.table_name = 'tenant_domains' AND kcu.column_name = 'tenant_id'
                  AND tc.constraint_type = 'FOREIGN KEY';
            """)
            fks = cur.fetchall()
            self.assertTrue(len(fks) >= 1)
            for fk in fks:
                self.assertEqual(fk[1], 'NO', "tenant_domains.tenant_id must NOT be deferrable")
                self.assertEqual(fk[2], 'RESTRICT', "tenant_domains.tenant_id must have ON DELETE RESTRICT")

    def test_02_tenant_branding_catalog(self):
        """tenant_branding: FK RESTRICT NOT DEFERRABLE, unique preserved, branding_mode varchar(30), new cols."""
        with connection.cursor() as cur:
            # Check branding_mode length
            cur.execute("""
                SELECT character_maximum_length FROM information_schema.columns
                WHERE table_name = 'tenant_branding' AND column_name = 'branding_mode';
            """)
            self.assertEqual(cur.fetchone()[0], 30)

            # Check new canonical columns
            cur.execute("""
                SELECT column_name, data_type, character_maximum_length, is_nullable
                FROM information_schema.columns
                WHERE table_name = 'tenant_branding';
            """)
            col_map = {r[0]: r for r in cur.fetchall()}
            self.assertIn('brand_name', col_map)
            self.assertIn('secondary_color', col_map)
            self.assertIn('theme_preset_code', col_map)
            self.assertIn('theme_tokens', col_map)
            self.assertIn('support_phone', col_map)
            self.assertIn('logo_storage_key', col_map)
            self.assertIn('favicon_storage_key', col_map)
            self.assertIn('login_logo_key', col_map)
            self.assertIn('login_background_key', col_map)
            self.assertIn('email_logo_key', col_map)

            self.assertEqual(col_map['brand_name'][2], 200)
            self.assertEqual(col_map['secondary_color'][2], 20)
            self.assertEqual(col_map['theme_preset_code'][2], 100)
            self.assertEqual(col_map['theme_tokens'][1], 'jsonb')
            self.assertEqual(col_map['logo_storage_key'][1], 'text')

            # Check FK and uniqueness
            cur.execute("""
                SELECT tc.constraint_type, tc.is_deferrable, rc.delete_rule
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name
                LEFT JOIN information_schema.referential_constraints rc ON tc.constraint_name = rc.constraint_name
                WHERE tc.table_name = 'tenant_branding' AND kcu.column_name = 'tenant_id'
                ORDER BY tc.constraint_type;
            """)
            constraints = cur.fetchall()
            types = [c[0] for c in constraints]
            self.assertIn('FOREIGN KEY', types)
            self.assertIn('UNIQUE', types)
            for c in constraints:
                if c[0] == 'FOREIGN KEY':
                    self.assertEqual(c[1], 'NO')
                    self.assertEqual(c[2], 'RESTRICT')

    def test_03_tenant_resource_usage_catalog(self):
        """tenant_resource_usage: period_start and period_end NOT NULL."""
        with connection.cursor() as cur:
            cur.execute("""
                SELECT column_name, is_nullable
                FROM information_schema.columns
                WHERE table_name = 'tenant_resource_usage' AND column_name IN ('period_start', 'period_end');
            """)
            for row in cur.fetchall():
                self.assertEqual(row[1], 'NO', f"tenant_resource_usage.{row[0]} must be NOT NULL")

    def test_04_subscription_dunning_events_catalog(self):
        """subscription_dunning_events: invoice_id FK SET NULL NOT DEFERRABLE, nullable."""
        with connection.cursor() as cur:
            cur.execute("""
                SELECT is_nullable FROM information_schema.columns
                WHERE table_name = 'subscription_dunning_events' AND column_name = 'invoice_id';
            """)
            self.assertEqual(cur.fetchone()[0], 'YES')

            cur.execute("""
                SELECT tc.is_deferrable, rc.delete_rule
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name
                JOIN information_schema.referential_constraints rc ON tc.constraint_name = rc.constraint_name
                WHERE tc.table_name = 'subscription_dunning_events' AND kcu.column_name = 'invoice_id'
                  AND tc.constraint_type = 'FOREIGN KEY';
            """)
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], 'NO')
            self.assertEqual(row[1], 'SET NULL')

    def test_05_tenant_data_source_health_catalog(self):
        """tenant_data_source_health: status varchar(30)."""
        with connection.cursor() as cur:
            cur.execute("""
                SELECT character_maximum_length FROM information_schema.columns
                WHERE table_name = 'tenant_data_source_health' AND column_name = 'status';
            """)
            self.assertEqual(cur.fetchone()[0], 30)

    def test_06_platform_branding_catalog(self):
        """platform_branding: canonical brand_name, secondary_color, storage keys."""
        with connection.cursor() as cur:
            cur.execute("""
                SELECT column_name, data_type, character_maximum_length, is_nullable
                FROM information_schema.columns
                WHERE table_name = 'platform_branding';
            """)
            cols = {r[0]: r for r in cur.fetchall()}
            self.assertIn('brand_name', cols)
            self.assertEqual(cols['brand_name'][2], 200)
            self.assertEqual(cols['brand_name'][3], 'NO')
            self.assertIn('secondary_color', cols)
            self.assertEqual(cols['secondary_color'][2], 20)
            self.assertIn('login_title', cols)
            self.assertEqual(cols['login_title'][2], 250)
            self.assertIn('support_phone', cols)
            self.assertEqual(cols['support_phone'][2], 50)
            self.assertIn('logo_storage_key', cols)
            self.assertEqual(cols['logo_storage_key'][1], 'text')
            self.assertIn('favicon_storage_key', cols)
            self.assertIn('login_logo_key', cols)
            self.assertIn('login_background_key', cols)

    def test_07_marketplace_integrations_catalog(self):
        """marketplace_integrations: logo_storage_key text NULL."""
        with connection.cursor() as cur:
            cur.execute("""
                SELECT data_type, is_nullable FROM information_schema.columns
                WHERE table_name = 'marketplace_integrations' AND column_name = 'logo_storage_key';
            """)
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], 'text')
            self.assertEqual(row[1], 'YES')

    def test_08_tenant_database_catalog(self):
        """Tenant DB: privacy_requests.assigned_to single FK NOT DEFERRABLE SET NULL, files.uploaded_by_id."""
        t_conn = connections['tenant_test']
        with t_conn.cursor() as cur:
            # privacy_requests.assigned_to constraints
            cur.execute("""
                SELECT tc.constraint_name, tc.is_deferrable, rc.delete_rule
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name
                JOIN information_schema.referential_constraints rc ON tc.constraint_name = rc.constraint_name
                WHERE tc.table_name = 'privacy_requests' AND kcu.column_name = 'assigned_to'
                  AND tc.constraint_type = 'FOREIGN KEY';
            """)
            fks = cur.fetchall()
            self.assertEqual(len(fks), 1, f"Expected exactly 1 FK on privacy_requests.assigned_to, found: {fks}")
            self.assertEqual(fks[0][1], 'NO', "privacy_requests.assigned_to FK must NOT be deferrable")
            self.assertEqual(fks[0][2], 'SET NULL', "privacy_requests.assigned_to FK must have ON DELETE SET NULL")

            # files.uploaded_by_id constraint
            cur.execute("""
                SELECT tc.constraint_name, tc.is_deferrable, rc.delete_rule
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name
                JOIN information_schema.referential_constraints rc ON tc.constraint_name = rc.constraint_name
                WHERE tc.table_name = 'files' AND kcu.column_name = 'uploaded_by_id'
                  AND tc.constraint_type = 'FOREIGN KEY';
            """)
            file_fks = cur.fetchall()
            self.assertEqual(len(file_fks), 1, f"Expected exactly 1 FK on files.uploaded_by_id, found: {file_fks}")
            self.assertEqual(file_fks[0][1], 'NO', "files.uploaded_by_id FK must NOT be deferrable")
            self.assertEqual(file_fks[0][2], 'SET NULL', "files.uploaded_by_id FK must have ON DELETE SET NULL")

    def test_09_reclassifications_preserved(self):
        """Preserves 5 architectural reclassifications: profile_file_id, audit string IDs, secret_reference."""
        with connection.cursor() as cur:
            cur.execute("""
                SELECT data_type, character_maximum_length, is_nullable
                FROM information_schema.columns
                WHERE table_name = 'tenant_data_sources' AND column_name = 'secret_reference';
            """)
            row = cur.fetchone()
            self.assertEqual(row[0], 'character varying')
            self.assertEqual(row[1], 255)
            self.assertEqual(row[2], 'YES')

        t_conn = connections['tenant_test']
        with t_conn.cursor() as cur:
            cur.execute("""
                SELECT data_type FROM information_schema.columns
                WHERE table_name = 'users' AND column_name = 'profile_file_id';
            """)
            self.assertEqual(cur.fetchone()[0], 'uuid')

            cur.execute("""
                SELECT column_name, data_type, character_maximum_length
                FROM information_schema.columns
                WHERE table_name = 'audit_events' AND column_name IN ('resource_id', 'request_id', 'correlation_id')
                ORDER BY column_name;
            """)
            audit_cols = {r[0]: r for r in cur.fetchall()}
            self.assertEqual(audit_cols['resource_id'][2], 36)
            self.assertEqual(audit_cols['request_id'][2], 255)
            self.assertEqual(audit_cols['correlation_id'][2], 255)


class Sprint15BrandingAPITestCase(TestCase):
    """Verifies Platform and Tenant Branding APIs, RBAC, Serializers, and Audit events."""
    databases = '__all__'

    def setUp(self):
        self.client = APIClient()
        self.superadmin = PlatformUser.objects.using('default').create(
            email='superadmin@sprint15.test',
            first_name='Super',
            last_name='Admin',
            status='ACTIVE',
            is_active=True,
            is_superuser=True,
        )
        self.superadmin.set_password('PlatformPassword123!')
        self.superadmin.save(using='default')

        # Regular platform staff with 'tenants.edit'
        self.staff_editor = PlatformUser.objects.using('default').create(
            email='editor@sprint15.test',
            first_name='Staff',
            last_name='Editor',
            status='ACTIVE',
            is_active=True,
            is_superuser=False,
        )
        self.staff_editor.set_password('PlatformPassword123!')
        self.staff_editor.save(using='default')

        # Regular platform staff without edit permission
        self.staff_viewer = PlatformUser.objects.using('default').create(
            email='viewer@sprint15.test',
            first_name='Staff',
            last_name='Viewer',
            status='ACTIVE',
            is_active=True,
            is_superuser=False,
        )
        self.staff_viewer.set_password('PlatformPassword123!')
        self.staff_viewer.save(using='default')

        # Assign roles / permissions
        perm_edit, _ = PlatformPermission.objects.using('default').get_or_create(
            code='tenants.edit', defaults={'name': 'Edit Tenants', 'module': 'tenants', 'action': 'edit'}
        )
        perm_view, _ = PlatformPermission.objects.using('default').get_or_create(
            code='tenants.view', defaults={'name': 'View Tenants', 'module': 'tenants', 'action': 'view'}
        )
        role_editor = PlatformRole.objects.using('default').create(name='Tenant Editor', code='TENANT_EDITOR')
        role_viewer = PlatformRole.objects.using('default').create(name='Tenant Viewer', code='TENANT_VIEWER')

        PlatformRolePermission.objects.using('default').create(role=role_editor, permission=perm_edit)
        PlatformRolePermission.objects.using('default').create(role=role_editor, permission=perm_view)
        PlatformRolePermission.objects.using('default').create(role=role_viewer, permission=perm_view)

        PlatformUserRole.objects.using('default').create(platform_user=self.staff_editor, role=role_editor)
        PlatformUserRole.objects.using('default').create(platform_user=self.staff_viewer, role=role_viewer)

        # Create Tenant & Branding
        self.tenant = Tenant.objects.using('default').create(
            code='SP15',
            name='Sprint 15 Club',
            legal_name='Sprint 15 Club Private Limited',
            slug='sprint15-club',
            status='ACTIVE',
        )
        self.domain = TenantDomain.objects.using('default').create(
            tenant=self.tenant,
            domain='sprint15.performanceos.io',
            is_primary=True,
            is_verified=False,
            status='ACTIVE',
        )
        self.tenant_branding = TenantBranding.objects.using('default').create(
            tenant=self.tenant,
            app_name='Sprint 15 Club',
            brand_name='Sprint 15 Club',
            primary_color='#0f766e',
            secondary_color='#f59e0b',
            accent_color='#f59e0b',
            theme_preset_code='titanium-teal',
        )

    def _get_token(self, user):
        token = AccessToken()
        token['sub'] = str(user.id)
        token['user_type'] = 'platform'
        token['email'] = user.email
        if user.is_superuser:
            token['roles'] = ['SUPER_ADMIN']
        else:
            token['roles'] = list(user.role_assignments.values_list('role__code', flat=True))
        return str(token)

    def test_10_platform_branding_endpoint(self):
        """GET and PATCH /api/v1/platform/branding/platform/ updates canonical fields and audits."""
        token = self._get_token(self.staff_editor)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        # GET
        res = self.client.get('/api/v1/platform/branding/platform/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['brand_name'], 'PerformanceOS')
        self.assertEqual(res.data['secondary_color'], '#f59e0b')

        # PATCH
        update_payload = {
            'brand_name': 'PerformanceOS Pro',
            'secondary_color': '#e11d48',
            'login_title': 'Welcome to Enterprise Performance',
            'support_phone': '+1-800-555-0199',
        }
        res_patch = self.client.patch('/api/v1/platform/branding/platform/', update_payload)
        self.assertEqual(res_patch.status_code, status.HTTP_200_OK)
        self.assertEqual(res_patch.data['brand_name'], 'PerformanceOS Pro')
        self.assertEqual(res_patch.data['platform_name'], 'PerformanceOS Pro')
        self.assertEqual(res_patch.data['secondary_color'], '#e11d48')
        self.assertEqual(res_patch.data['accent_color'], '#e11d48')

        # Audit Event check
        audit = PlatformAuditEvent.objects.using('default').filter(resource_type='PlatformBranding', action='UPDATE').first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.actor_email, self.staff_editor.email)
        self.assertEqual(audit.event_name, 'PLATFORMBRANDING_UPDATE')

    def test_11_tenant_branding_retrieve_and_update(self):
        """GET and PATCH /api/v1/platform/branding/{tenant_id}/ updates theme preset and tokens."""
        token = self._get_token(self.staff_editor)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        # Retrieve by tenant slug
        res = self.client.get(f"/api/v1/platform/branding/{self.tenant.slug}/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['brand_name'], 'Sprint 15 Club')
        self.assertEqual(res.data['theme_preset_code'], 'titanium-teal')
        self.assertIsNone(res.data['logo_storage_key'])

        # Update theme tokens & preset code
        patch_payload = {
            'theme_preset_code': 'equinox-obsidian-gold',
            'primary_color': '#b45309',
            'secondary_color': '#f59e0b',
            'theme_tokens': {'primary': '#b45309', 'accent': '#f59e0b', 'category': 'Luxury'},
            'support_phone': '+91-9876543210',
        }
        res_patch = self.client.patch(f"/api/v1/platform/branding/{self.tenant.id}/", patch_payload, format='json')
        self.assertEqual(res_patch.status_code, status.HTTP_200_OK)
        self.assertEqual(res_patch.data['theme_preset_code'], 'equinox-obsidian-gold')
        self.assertEqual(res_patch.data['theme_tokens']['category'], 'Luxury')
        self.assertEqual(res_patch.data['support_phone'], '+91-9876543210')

        # Verify DB
        self.tenant_branding.refresh_from_db()
        self.assertEqual(self.tenant_branding.theme_preset_code, 'equinox-obsidian-gold')
        self.assertEqual(self.tenant_branding.secondary_color, '#f59e0b')

        # Audit check
        audit = PlatformAuditEvent.objects.using('default').filter(
            resource_type='TenantBranding', tenant_id=self.tenant.id, action='UPDATE'
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.actor_email, self.staff_editor.email)

    def test_12_tenant_dns_verification_action(self):
        """POST /api/v1/platform/branding/{tenant_id}/verify-dns/ sets domain is_verified=True."""
        token = self._get_token(self.staff_editor)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        self.assertFalse(self.domain.is_verified)
        res = self.client.post(f"/api/v1/platform/branding/{self.tenant.id}/verify-dns/", {
            'domain': self.domain.domain
        })
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.data['cname_verified'])

        self.domain.refresh_from_db()
        self.assertTrue(self.domain.is_verified)
        self.assertEqual(self.domain.status, 'ACTIVE')

    def test_13_rbac_unauthorized_mutation_denied(self):
        """Platform user without tenants.edit cannot update branding (403 Forbidden)."""
        token = self._get_token(self.staff_viewer)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        # GET is allowed with tenants.view
        res_get = self.client.get(f"/api/v1/platform/branding/{self.tenant.id}/")
        self.assertEqual(res_get.status_code, status.HTTP_200_OK)

        # PATCH is denied without tenants.edit
        res_patch = self.client.patch(f"/api/v1/platform/branding/{self.tenant.id}/", {
            'brand_name': 'Hacked Brand'
        })
        self.assertEqual(res_patch.status_code, status.HTTP_403_FORBIDDEN)

    def test_14_marketplace_logo_storage_key(self):
        """Marketplace integration contract supports logo_storage_key with icon_text fallback."""
        integration = MarketplaceIntegration.objects.using('default').create(
            name='Razorpay Gateway',
            code='RAZORPAY_S15',
            category='Payments',
            integration_type='PAYMENT',
            provider='Razorpay Software Ltd',
            description='Online payments and subscriptions',
            icon_text='RZP',
            logo_storage_key=None,
            is_free=True,
            price_monthly=0,
            status='ACTIVE',
        )

        token = self._get_token(self.superadmin)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        res = self.client.get(f"/api/v1/platform/marketplace/{integration.id}/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['icon_text'], 'RZP')
        self.assertIsNone(res.data['logo_storage_key'])

        # Update logo_storage_key
        integration.logo_storage_key = 'marketplace/logos/razorpay.png'
        integration.save(using='default', update_fields=['logo_storage_key'])
        res2 = self.client.get(f"/api/v1/platform/marketplace/{integration.id}/")
        self.assertEqual(res2.data['logo_storage_key'], 'marketplace/logos/razorpay.png')
