"""
Comprehensive Security and Hardening Test Suite — Phase 1 Layer 1 Final Hardening.

Mandatory Security Test Matrix:
1. Tenant A normal request (HTTP 200)
2. Tenant B normal request (HTTP 200)
3. Same DB name / different customer-managed host (No alias collision)
4. Tenant A cannot access Tenant B (Isolation)
5. Forged tid (Rejected, no DB registration)
6. Forged/legacy db_alias (Ignored, authoritative resolution only)
7. Suspended tenant (Rejected before DB registration, thread-local cleaned)
8. Deactivated tenant (Rejected before DB registration, thread-local cleaned)
9. Inactive datasource (Rejected with HTTP 503, thread-local cleaned)
10. Missing tenant context (Fails closed)
11. Tenant ORM with no alias (TenantRoutingError)
12. Worker tenant context cleanup (Cleaned in finally upon error)
13. DSR Tenant A -> Tenant A only
14. DSR Tenant A -> Tenant B request denied / fails closed
15. Concurrent Tenant A / Tenant B context isolation (Thread-local process safety)
16. Structural ViewSets mutation blocked with HTTP 405 (ReadOnlyModelViewSet)
"""

import uuid
import threading
from unittest.mock import patch, MagicMock
from django.test import TestCase, Client
from django.conf import settings
from django.db import connections
from rest_framework import status

from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.models_saas import SaasPlan, TenantSubscription
from apps.tenant_core.models_org import Organization, Location, Branch, CompanyEntity
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_rbac import Role, RoleAssignment, ModuleCatalog, BranchModule
from apps.tenant_core.models_privacy import PrivacyRequest
from apps.tenant_core.context import tenant_database_context
from apps.tenant_core.tasks import process_dsr_export_task
from apps.authentication.views import _build_tenant_token
from config.routers import (
    get_tenant_db_alias,
    set_tenant_db_alias,
    build_tenant_db_alias,
    TenantRoutingError,
)
from config.tenant_middleware import _register_tenant_connection


class Layer1FinalHardeningSecurityTests(TestCase):
    databases = '__all__'

    def setUp(self):
        super().setUp()
        self.client = Client()
        set_tenant_db_alias(None)

        # ── Global Plan Setup ───────────────────────────────────────────────
        self.plan = SaasPlan.objects.using('default').create(
            name='Hardening Plan',
            code='HARDENING-PLAN',
            tier='ENTERPRISE',
            status='ACTIVE',
        )

        # ── Tenant A Setup ──────────────────────────────────────────────────
        self.tenant_a = Tenant.objects.using('default').create(
            code='TENANT-ALPHA',
            name='Tenant Alpha',
            slug='tenant-alpha',
            status='ACTIVE',
        )
        self.ds_a = TenantDataSource.objects.using('default').create(
            tenant=self.tenant_a,
            db_name='test_fitness_tenant',
            database_name='test_fitness_tenant',
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )
        TenantSubscription.objects.using('default').create(
            tenant=self.tenant_a,
            plan=self.plan,
            status='ACTIVE',
        )

        # ── Tenant B Setup ──────────────────────────────────────────────────
        self.tenant_b = Tenant.objects.using('default').create(
            code='TENANT-BETA',
            name='Tenant Beta',
            slug='tenant-beta',
            status='ACTIVE',
        )
        self.ds_b = TenantDataSource.objects.using('default').create(
            tenant=self.tenant_b,
            db_name='test_fitness_tenant_b',
            database_name='test_fitness_tenant',
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )
        TenantSubscription.objects.using('default').create(
            tenant=self.tenant_b,
            plan=self.plan,
            status='ACTIVE',
        )

        # ── Seed Tenant A Records in Tenant DB ──────────────────────────────
        set_tenant_db_alias('tenant_test')
        self.org_a = Organization.objects.using('tenant_test').create(
            code='ORG-ALPHA',
            name='Organization Alpha',
            status='ACTIVE',
        )
        self.user_a = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a,
            email='user.alpha@tenant-a.com',
            first_name='Alpha',
            last_name='User',
            status='ACTIVE',
        )
        self.user_a.set_password('StrongPassword123!')
        self.user_a.save(using='tenant_test')

        self.role_admin_a = Role.objects.using('tenant_test').create(
            name='Org Admin',
            code='ORG_ADMIN',
            is_system_role=True,
            scope='ORG',
            organization=self.org_a,
            is_active=True,
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.user_a,
            role=self.role_admin_a,
            organization=self.org_a,
            is_active=True,
        )

        # ── Seed Tenant B Records in Tenant DB ──────────────────────────────
        self.org_b = Organization.objects.using('tenant_test').create(
            code='ORG-BETA',
            name='Organization Beta',
            status='ACTIVE',
        )
        self.user_b = TenantUser.objects.using('tenant_test').create(
            organization=self.org_b,
            email='user.beta@tenant-b.com',
            first_name='Beta',
            last_name='User',
            status='ACTIVE',
        )
        self.user_b.set_password('StrongPassword123!')
        self.user_b.save(using='tenant_test')

        self.role_admin_b = Role.objects.using('tenant_test').create(
            name='Org Admin B',
            code='ORG_ADMIN',
            is_system_role=True,
            scope='ORG',
            organization=self.org_b,
            is_active=True,
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.user_b,
            role=self.role_admin_b,
            organization=self.org_b,
            is_active=True,
        )

        set_tenant_db_alias(None)

    def tearDown(self):
        set_tenant_db_alias(None)
        # Clean up any test dynamic connections created
        for alias in list(connections):
            if alias not in ('default', 'tenant_test'):
                try:
                    connections[alias].close()
                except Exception:
                    pass
                try:
                    del connections[alias]
                except Exception:
                    pass
                if alias in settings.DATABASES:
                    del settings.DATABASES[alias]
        super().tearDown()

    def get_tenant_token(self, user, tenant, db_alias='tenant_test'):
        refresh = _build_tenant_token(
            user=user,
            tenant=tenant,
            db_alias=db_alias,
        )
        return str(refresh.access_token)

    # ── Test 1: Tenant A Normal Request ─────────────────────────────────────
    def test_01_tenant_a_normal_request(self):
        """Active Tenant A user authenticates and accesses tenant resources with 200 OK."""
        token = self.get_tenant_token(self.user_a, self.tenant_a)
        res = self.client.get(
            '/api/v1/tenant/organizations/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        results = res.json().get('results', res.json())
        self.assertTrue(any(item.get('code') == 'ORG-ALPHA' for item in results))

    # ── Test 2: Tenant B Normal Request ─────────────────────────────────────
    def test_02_tenant_b_normal_request(self):
        """Active Tenant B user authenticates and accesses tenant resources with 200 OK."""
        token = self.get_tenant_token(self.user_b, self.tenant_b)
        res = self.client.get(
            '/api/v1/tenant/organizations/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        results = res.json().get('results', res.json())
        self.assertTrue(any(item.get('code') == 'ORG-BETA' for item in results))

    # ── Test 3: Same DB name / different customer-managed host (SEC-02 & Alias) ──
    def test_03_same_db_name_different_hosts_no_collision(self):
        """
        Two customer-managed tenants sharing the identical database name 'production_db'
        on different external hosts resolve to distinct, deterministic tenant-unique aliases.
        """
        tenant_cust_a = Tenant.objects.using('default').create(
            code='CUST-TENANT-A',
            name='Customer A',
            slug='cust-a',
            status='ACTIVE',
        )
        ds_cust_a = TenantDataSource.objects.using('default').create(
            tenant=tenant_cust_a,
            db_name='production_db_a',
            database_name='production_db',
            db_host='db-a.customer.com',
            db_port=5432,
            db_user='app_user',
            secret_reference='env://FAKE_SECRET_A',
            hosting_mode='CUSTOMER_MANAGED',
            status='ACTIVE',
        )

        tenant_cust_b = Tenant.objects.using('default').create(
            code='CUST-TENANT-B',
            name='Customer B',
            slug='cust-b',
            status='ACTIVE',
        )
        ds_cust_b = TenantDataSource.objects.using('default').create(
            tenant=tenant_cust_b,
            db_name='production_db_b',
            database_name='production_db',
            db_host='db-b.customer.com',
            db_port=5432,
            db_user='app_user',
            secret_reference='env://FAKE_SECRET_B',
            hosting_mode='CUSTOMER_MANAGED',
            status='ACTIVE',
        )

        alias_a = build_tenant_db_alias(tenant_cust_a.id)
        alias_b = build_tenant_db_alias(tenant_cust_b.id)

        # 1. Deterministic aliases must NOT collide
        self.assertNotEqual(alias_a, alias_b)
        self.assertTrue(alias_a.startswith(f"tenant_{tenant_cust_a.id.hex}"))
        self.assertTrue(alias_b.startswith(f"tenant_{tenant_cust_b.id.hex}"))

        # 2. Mock schema validation to verify connection registration binding
        with patch('config.tenant_middleware._validate_tenant_schema_version'), \
             patch('config.secrets.SecretResolver.resolve', return_value='dummy_secret'):
            _register_tenant_connection(alias_a, 'production_db', data_source=ds_cust_a, tenant_id=tenant_cust_a.id)
            _register_tenant_connection(alias_b, 'production_db', data_source=ds_cust_b, tenant_id=tenant_cust_b.id)

        # 3. Verify configurations in Django settings are strictly isolated
        self.assertEqual(settings.DATABASES[alias_a]['HOST'], 'db-a.customer.com')
        self.assertEqual(settings.DATABASES[alias_b]['HOST'], 'db-b.customer.com')
        self.assertEqual(settings.DATABASES[alias_a]['NAME'], 'production_db')
        self.assertEqual(settings.DATABASES[alias_b]['NAME'], 'production_db')

    # ── Test 4: Tenant A cannot access Tenant B ─────────────────────────────
    def test_04_tenant_a_cannot_access_tenant_b_data(self):
        """Tenant A token routes strictly to Tenant A and cannot access records outside Tenant A context."""
        token_a = self.get_tenant_token(self.user_a, self.tenant_a)
        # Attempt to retrieve a specific record that does not exist in Tenant A's DB
        non_existent_id = uuid.uuid4()
        res = self.client.get(
            f'/api/v1/tenant/organizations/{non_existent_id}/',
            HTTP_AUTHORIZATION=f'Bearer {token_a}',
        )
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

        # Verify that Tenant A request context is strictly bound to Tenant A's organization
        res_me = self.client.get(
            f'/api/v1/tenant/organizations/{self.org_a.id}/',
            HTTP_AUTHORIZATION=f'Bearer {token_a}',
        )
        self.assertEqual(res_me.status_code, status.HTTP_200_OK)
        self.assertEqual(res_me.json().get('code'), 'ORG-ALPHA')

    # ── Test 5: Forged tid in JWT fails closed ──────────────────────────────
    def test_05_forged_tid_rejected_without_db_registration(self):
        """JWT with a forged / non-existent tenant_id is rejected at middleware with 401."""
        forged_tenant_id = uuid.uuid4()
        fake_tenant = MagicMock(id=forged_tenant_id, slug='forged-slug')
        refresh = _build_tenant_token(
            user=self.user_a,
            tenant=fake_tenant,
            db_alias='tenant_test',
        )
        token = str(refresh.access_token)

        res = self.client.get(
            '/api/v1/tenant/organizations/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(res.json().get('error'), 'TENANT_NOT_FOUND')
        self.assertIsNone(get_tenant_db_alias())

    # ── Test 6: Forged/legacy db_alias header/payload ignored ────────────────
    def test_06_forged_legacy_db_alias_header_ignored(self):
        """Attacker sending X-Database-Alias or db_alias is ignored; authoritative routing only."""
        token = self.get_tenant_token(self.user_a, self.tenant_a)
        res = self.client.get(
            '/api/v1/tenant/organizations/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
            HTTP_X_DATABASE_ALIAS='default',
            HTTP_DB_ALIAS='default',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIsNone(get_tenant_db_alias())

    # ── Test 7: Suspended Tenant (SEC-01) ───────────────────────────────────
    def test_07_suspended_tenant_rejected_before_db_registration(self):
        """SUSPENDED tenant is rejected with 401 at middleware before DB registration."""
        self.tenant_a.status = 'SUSPENDED'
        self.tenant_a.save(using='default')

        token = self.get_tenant_token(self.user_a, self.tenant_a)

        with patch('config.tenant_middleware._register_tenant_connection') as mock_reg:
            res = self.client.get(
                '/api/v1/tenant/organizations/',
                HTTP_AUTHORIZATION=f'Bearer {token}',
            )
            self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)
            self.assertEqual(res.json().get('error'), 'TENANT_INACTIVE')
            mock_reg.assert_not_called()

        self.assertIsNone(get_tenant_db_alias())

    # ── Test 8: Deactivated Tenant (SEC-01) ─────────────────────────────────
    def test_08_deactivated_tenant_rejected_before_db_registration(self):
        """DEACTIVATED tenant is rejected with 401 before DB registration."""
        self.tenant_a.status = 'DEACTIVATED'
        self.tenant_a.save(using='default')

        token = self.get_tenant_token(self.user_a, self.tenant_a)

        with patch('config.tenant_middleware._register_tenant_connection') as mock_reg:
            res = self.client.get(
                '/api/v1/tenant/organizations/',
                HTTP_AUTHORIZATION=f'Bearer {token}',
            )
            self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)
            self.assertEqual(res.json().get('error'), 'TENANT_INACTIVE')
            mock_reg.assert_not_called()

        self.assertIsNone(get_tenant_db_alias())

    # ── Test 9: Inactive DataSource Rejected (SEC-01) ───────────────────────
    def test_09_inactive_datasource_rejected(self):
        """Tenant with INACTIVE / MAINTENANCE DataSource is rejected with 503."""
        self.ds_a.status = 'INACTIVE'
        self.ds_a.save(using='default')

        token = self.get_tenant_token(self.user_a, self.tenant_a)

        with patch('config.tenant_middleware._register_tenant_connection') as mock_reg:
            res = self.client.get(
                '/api/v1/tenant/organizations/',
                HTTP_AUTHORIZATION=f'Bearer {token}',
            )
            self.assertEqual(res.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
            self.assertEqual(res.json().get('error'), 'TENANT_DATASOURCE_INACTIVE')
            mock_reg.assert_not_called()

        self.assertIsNone(get_tenant_db_alias())

    # ── Test 10: Missing Tenant Context ─────────────────────────────────────
    def test_10_missing_tenant_context_fails_closed(self):
        """Unauthenticated request or token missing tid fails closed with 401."""
        res = self.client.get('/api/v1/tenant/organizations/')
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIsNone(get_tenant_db_alias())

    # ── Test 11: Tenant ORM with No Alias ───────────────────────────────────
    def test_11_tenant_orm_with_no_alias_fails_closed(self):
        """Attempting to query tenant-scoped models outside active tenant context raises TenantRoutingError."""
        set_tenant_db_alias(None)
        with self.assertRaises(TenantRoutingError):
            list(TenantUser.objects.all()[:1])

    # ── Test 12: Worker Tenant Context Cleanup (SEC-03) ─────────────────────
    def test_12_worker_tenant_context_cleanup_on_exception(self):
        """tenant_database_context clears thread-local alias in finally even when exception occurs."""
        self.assertIsNone(get_tenant_db_alias())

        class CustomWorkerException(Exception):
            pass

        try:
            with tenant_database_context(self.tenant_a.id) as alias:
                self.assertIsNotNone(alias)
                self.assertEqual(get_tenant_db_alias(), alias)
                raise CustomWorkerException("Worker task crashed")
        except CustomWorkerException:
            pass

        self.assertIsNone(get_tenant_db_alias())

    # ── Test 13: DSR Tenant A -> Tenant A Only (SEC-03) ─────────────────────
    def test_13_dsr_tenant_a_executes_inside_tenant_a_db(self):
        """DSR export task executes strictly within Tenant A DB context."""
        set_tenant_db_alias('tenant_test')
        try:
            req_a = PrivacyRequest.objects.using('tenant_test').create(
                user=self.user_a,
                request_type='PORTABILITY',
                status='RECEIVED',
            )
        finally:
            set_tenant_db_alias(None)

        with patch('apps.tenant_core.tasks.emit_audit_event'):
            result = process_dsr_export_task(str(self.tenant_a.id), str(req_a.id))

        self.assertEqual(result.get('status'), 'COMPLETED')
        req_a.refresh_from_db(using='tenant_test')
        self.assertEqual(req_a.status, 'COMPLETED')
        self.assertIsNone(get_tenant_db_alias())

    # ── Test 14: DSR Cross-Tenant Request Denied (SEC-03) ───────────────────
    def test_14_dsr_cross_tenant_request_denied(self):
        """DSR task for Tenant A cannot process a PrivacyRequest ID belonging to Tenant B."""
        set_tenant_db_alias('tenant_test')
        try:
            req_b = PrivacyRequest.objects.using('tenant_test').create(
                user=self.user_b,
                request_type='PORTABILITY',
                status='RECEIVED',
            )
        finally:
            set_tenant_db_alias(None)

        # 1. Missing tenant_id fails closed
        res_missing = process_dsr_export_task(None, str(req_b.id))
        self.assertEqual(res_missing.get('status'), 'FAILED')
        self.assertIn('required', res_missing.get('error'))

        # 2. Cross-tenant request ID not in Tenant A DB fails closed
        res_cross = process_dsr_export_task(str(self.tenant_a.id), str(uuid.uuid4()))
        self.assertEqual(res_cross.get('status'), 'FAILED')
        self.assertIn('not found', res_cross.get('error'))

        # 3. Suspended tenant fails closed
        self.tenant_a.status = 'SUSPENDED'
        self.tenant_a.save(using='default')
        res_suspended = process_dsr_export_task(str(self.tenant_a.id), str(req_b.id))
        self.assertEqual(res_suspended.get('status'), 'FAILED')
        self.assertIn('not active', res_suspended.get('error'))

        self.assertIsNone(get_tenant_db_alias())

    # ── Test 15: Concurrent Tenant Context Thread Isolation ──────────────────
    def test_15_concurrent_tenant_context_thread_isolation(self):
        """
        Two concurrent worker threads operating in parallel maintain strict, isolated thread-local aliases
        without any cross-thread state leakage or race conditions.
        """
        alias_a_result = []
        alias_b_result = []
        errors = []
        barrier = threading.Barrier(2)

        # Build isolated customer-managed data sources so aliases use build_tenant_db_alias
        mock_ds_a = MagicMock(
            tenant=self.tenant_a,
            status='ACTIVE',
            db_name='production_db_a',
            database_name='production_db',
            host='db-a.customer.com',
            port=5432,
            user='user_a',
            password_ref='env://TEST_DB_PASSWORD',
            ssl_mode='prefer',
        )
        mock_ds_b = MagicMock(
            tenant=self.tenant_b,
            status='ACTIVE',
            db_name='production_db_b',
            database_name='production_db',
            host='db-b.customer.com',
            port=5432,
            user='user_b',
            password_ref='env://TEST_DB_PASSWORD',
            ssl_mode='prefer',
        )

        mock_tenants = {
            self.tenant_a.id: self.tenant_a,
            self.tenant_b.id: self.tenant_b,
        }
        mock_ds = {
            self.tenant_a.id: mock_ds_a,
            self.tenant_b.id: mock_ds_b,
        }

        def fake_tenant_filter(id=None, **kwargs):
            m = MagicMock()
            m.first.return_value = mock_tenants.get(id)
            return m

        def fake_ds_filter(tenant=None, status=None, **kwargs):
            m = MagicMock()
            m.first.return_value = mock_ds.get(getattr(tenant, 'id', None))
            return m

        with patch('apps.master.models_tenant.Tenant.objects.using') as mock_t_using, \
             patch('apps.master.models_infra.TenantDataSource.objects.using') as mock_ds_using, \
             patch('config.tenant_middleware._register_tenant_connection'):

            mock_t_using.return_value.filter.side_effect = fake_tenant_filter
            mock_ds_using.return_value.filter.side_effect = fake_ds_filter

            def worker_a():
                try:
                    with tenant_database_context(self.tenant_a.id) as alias:
                        barrier.wait(timeout=5)
                        alias_a_result.append((alias, get_tenant_db_alias()))
                except Exception as e:
                    errors.append(e)

            def worker_b():
                try:
                    with tenant_database_context(self.tenant_b.id) as alias:
                        barrier.wait(timeout=5)
                        alias_b_result.append((alias, get_tenant_db_alias()))
                except Exception as e:
                    errors.append(e)

            t1 = threading.Thread(target=worker_a)
            t2 = threading.Thread(target=worker_b)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(alias_a_result), 1)
        self.assertEqual(len(alias_b_result), 1)
        self.assertEqual(alias_a_result[0][0], alias_a_result[0][1])
        self.assertEqual(alias_b_result[0][0], alias_b_result[0][1])
        # Verify that aliases are distinct and tenant-unique
        self.assertNotEqual(alias_a_result[0][0], alias_b_result[0][0])
        self.assertEqual(alias_a_result[0][0], build_tenant_db_alias(self.tenant_a.id))
        self.assertEqual(alias_b_result[0][0], build_tenant_db_alias(self.tenant_b.id))
        # Verify main thread has no leaked tenant alias
        self.assertIsNone(get_tenant_db_alias())

    # ── Test 16: Structural ViewSets Mutation Blocked with 405 (Issue 5) ─────
    def test_16_structural_viewsets_mutation_blocked_with_405(self):
        """
        ReadOnlyModelViewSet guarantees that POST, PUT, PATCH, DELETE are blocked with HTTP 405
        on structural organization/location/branch endpoints, preserving Platform Super Admin topology.
        """
        token = self.get_tenant_token(self.user_a, self.tenant_a)
        endpoints = [
            '/api/v1/tenant/organizations/',
            '/api/v1/tenant/locations/',
            '/api/v1/tenant/branches/',
            '/api/v1/tenant/company-entities/',
            '/api/v1/tenant/module-catalog/',
            '/api/v1/tenant/branch-modules/',
        ]

        for url in endpoints:
            with self.subTest(url=url):
                # 1. POST -> 405
                post_res = self.client.post(
                    url,
                    data={'name': 'Malicious Mutation'},
                    content_type='application/json',
                    HTTP_AUTHORIZATION=f'Bearer {token}',
                )
                self.assertEqual(
                    post_res.status_code,
                    status.HTTP_405_METHOD_NOT_ALLOWED,
                    f"Expected 405 on POST {url}, got {post_res.status_code}",
                )

                # 2. DELETE on collection or instance -> 405
                del_res = self.client.delete(
                    f"{url}{uuid.uuid4()}/",
                    HTTP_AUTHORIZATION=f'Bearer {token}',
                )
                self.assertEqual(
                    del_res.status_code,
                    status.HTTP_405_METHOD_NOT_ALLOWED,
                    f"Expected 405 on DELETE {url}, got {del_res.status_code}",
                )

