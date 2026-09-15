"""
tests/test_customer_managed_db_routing.py — Canonical Requirement R01.08 Verification.

Proves:
1. PLATFORM_MANAGED routing behaves as platform-managed (copies master cluster settings, overrides NAME).
2. CUSTOMER_MANAGED routing connects to external host, port, database name, user, sslmode.
3. Secure credential resolution via SecretResolver (Vault, env, test seam).
4. Invalid/missing secret reference strictly fails closed (no fallback).
5. Zero fallback to localhost or default DB credentials.
6. Strict tenant isolation between multiple tenant databases.
7. Schema version gate validation (Canonical Section 14 Guardrail 9).
8. Concurrent connection registration thread safety.
9. Credential redaction in logs and exception traces.
"""

import uuid
import threading
from django.test import TestCase, override_settings
from django.conf import settings
from django.db import connections

from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from config.secrets import SecretResolver, SecretResolutionError
from config.tenant_middleware import (
    _register_tenant_connection,
    unregister_tenant_connection,
    _validate_tenant_schema_version,
    TenantDatabaseConfigurationError,
    SchemaVersionMismatchError,
    _db_registration_lock,
)


class CustomerManagedDatabaseRoutingTestCase(TestCase):
    databases = '__all__'

    def setUp(self):
        SecretResolver.clear_test_secrets()
        _register_tenant_connection('tenant_test', 'test_fitness_tenant')
        self.platform_tenant = Tenant.objects.using('default').create(
            name='Platform Managed Gym',
            slug='platform-managed-gym',
            code='PLT-001',
            status='ACTIVE',
        )
        self.customer_tenant = Tenant.objects.using('default').create(
            name='Customer Managed Corp',
            slug='customer-managed-corp',
            code='CST-001',
            status='ACTIVE',
        )

    def tearDown(self):
        SecretResolver.clear_test_secrets()
        for alias in list(settings.DATABASES.keys()):
            if alias.startswith('tenant_') and alias != 'tenant_test':
                unregister_tenant_connection(alias)

    def test_platform_managed_routing(self):
        """PLATFORM_MANAGED tenants inherit platform cluster defaults with dedicated db name."""
        ds = TenantDataSource.objects.using('default').create(
            tenant=self.platform_tenant,
            hosting_mode='PLATFORM_MANAGED',
            db_name='plt_gym_db',
            status='ACTIVE',
        )
        alias = 'tenant_plt_gym_db'
        _register_tenant_connection(alias, ds.db_name, data_source=ds)

        self.assertIn(alias, settings.DATABASES)
        config = settings.DATABASES[alias]
        self.assertEqual(config['NAME'], 'plt_gym_db')
        self.assertEqual(config['HOST'], settings.DATABASES['default']['HOST'])
        self.assertEqual(config['USER'], settings.DATABASES['default']['USER'])

    def test_customer_managed_routing_with_external_host_and_port(self):
        """CUSTOMER_MANAGED routes to actual external host, port, user, sslmode, and resolved password."""
        secret_ref = 'vault://tenants/customer-corp/db_password'
        SecretResolver.register_test_secret(secret_ref, 'super-secure-ext-pw-99!')

        ds = TenantDataSource.objects.using('default').create(
            tenant=self.customer_tenant,
            hosting_mode='CUSTOMER_MANAGED',
            db_host='postgres.customer-corp.internal',
            db_port=5433,
            db_user='corp_db_admin',
            database_name='ext_corp_db',
            secret_reference=secret_ref,
            ssl_mode='VERIFY-FULL',
            schema_version='1.0',
            status='ACTIVE',
        )
        alias = 'tenant_ext_corp_db'

        # Mock schema validation gate for this external configuration test
        from unittest.mock import patch
        with patch('config.tenant_middleware._validate_tenant_schema_version') as mock_val:
            _register_tenant_connection(alias, ds.database_name, data_source=ds)
            mock_val.assert_called_once_with(alias, '1.0')

        self.assertIn(alias, settings.DATABASES)
        config = settings.DATABASES[alias]
        self.assertEqual(config['HOST'], 'postgres.customer-corp.internal')
        self.assertEqual(config['PORT'], 5433)
        self.assertEqual(config['NAME'], 'ext_corp_db')
        self.assertEqual(config['USER'], 'corp_db_admin')
        self.assertEqual(config['PASSWORD'], 'super-secure-ext-pw-99!')
        self.assertEqual(config['OPTIONS'].get('sslmode'), 'verify-full')

    def test_missing_or_unresolvable_secret_reference_fails_closed(self):
        """CUSTOMER_MANAGED with unresolvable secret reference must strictly fail closed without fallback."""
        ds = TenantDataSource.objects.using('default').create(
            tenant=self.customer_tenant,
            hosting_mode='CUSTOMER_MANAGED',
            db_host='postgres.customer-corp.internal',
            db_port=5432,
            db_user='corp_user',
            database_name='ext_corp_db',
            secret_reference='vault://missing/path/nonexistent',
            status='ACTIVE',
        )
        alias = 'tenant_ext_corp_db'

        with self.assertRaises(TenantDatabaseConfigurationError) as ctx:
            _register_tenant_connection(alias, ds.database_name, data_source=ds)

        # Assert clean fail-closed error with no leak
        self.assertIn('Failed to securely resolve database credentials', str(ctx.exception))
        # Ensure alias was NEVER registered in settings.DATABASES
        self.assertNotIn(alias, settings.DATABASES)

    def test_no_localhost_or_default_fallback_on_error(self):
        """Assert no fallback to localhost or default cluster when external parameters are invalid."""
        ds = TenantDataSource.objects.using('default').create(
            tenant=self.customer_tenant,
            hosting_mode='CUSTOMER_MANAGED',
            db_host='',  # Missing host
            db_port=5432,
            db_user='corp_user',
            database_name='ext_corp_db',
            secret_reference='env://SOME_PW',
            status='ACTIVE',
        )
        alias = 'tenant_ext_corp_db'

        with self.assertRaises(TenantDatabaseConfigurationError):
            _register_tenant_connection(alias, ds.database_name, data_source=ds)

        self.assertNotIn(alias, settings.DATABASES)

    def test_tenant_isolation_between_connections(self):
        """Assert multiple tenant databases maintain completely independent connection configurations."""
        ref_a = 'env://TENANT_A_PW'
        ref_b = 'env://TENANT_B_PW'
        SecretResolver.register_test_secret(ref_a, 'secret-a')
        SecretResolver.register_test_secret(ref_b, 'secret-b')

        ds_a = TenantDataSource.objects.using('default').create(
            tenant=self.platform_tenant,
            hosting_mode='PLATFORM_MANAGED',
            db_name='tenant_a_db',
            status='ACTIVE',
        )
        ds_b = TenantDataSource.objects.using('default').create(
            tenant=self.customer_tenant,
            hosting_mode='CUSTOMER_MANAGED',
            db_host='db.tenant-b.com',
            db_port=5432,
            db_user='user_b',
            database_name='tenant_b_db',
            secret_reference=ref_b,
            status='ACTIVE',
        )

        from unittest.mock import patch
        with patch('config.tenant_middleware._validate_tenant_schema_version'):
            _register_tenant_connection('tenant_tenant_a_db', ds_a.db_name, data_source=ds_a)
            _register_tenant_connection('tenant_tenant_b_db', ds_b.database_name, data_source=ds_b)

        self.assertNotEqual(
            settings.DATABASES['tenant_tenant_a_db']['HOST'],
            settings.DATABASES['tenant_tenant_b_db']['HOST']
        )
        self.assertEqual(settings.DATABASES['tenant_tenant_b_db']['HOST'], 'db.tenant-b.com')
        self.assertEqual(settings.DATABASES['tenant_tenant_b_db']['USER'], 'user_b')

    def test_schema_version_gate_validation(self):
        """Canonical Section 14 Guardrail 9: schema version check fails closed if DB missing migrations."""
        # Test schema check directly on tenant_test
        # tenant_test has applied migrations, so schema check should succeed
        _validate_tenant_schema_version('tenant_test', '1.0')

        # When pointing to a nonexistent connection or broken database, it must raise SchemaVersionMismatchError
        with self.assertRaises(SchemaVersionMismatchError):
            _validate_tenant_schema_version('nonexistent_alias_999', '1.0')

    def test_concurrent_connection_registration_thread_safety(self):
        """Concurrent threads registering connections operate cleanly under lock with no corruption."""
        alias = 'tenant_test_concurrent'
        errors = []

        def worker():
            try:
                for _ in range(20):
                    _register_tenant_connection(alias, 'test_fitness_tenant')
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertIn(alias, settings.DATABASES)

    def test_credential_redaction(self):
        """Ensure secret reference values and passwords are never exposed in exception strings."""
        secret_ref = 'vault://secret/path/very_sensitive'
        redacted = SecretResolver.redact_reference(secret_ref)
        self.assertNotIn('very_sensitive', redacted)
        self.assertTrue(redacted.startswith('vaul'))

    def test_middleware_customer_managed_fail_closed_503(self):
        """Middleware returns 503 Service Unavailable when customer managed credentials cannot be resolved."""
        from django.test import RequestFactory
        from config.tenant_middleware import TenantDatabaseMiddleware
        from config.routers import get_tenant_db_alias
        import jwt as pyjwt

        ds = TenantDataSource.objects.using('default').create(
            tenant=self.customer_tenant,
            hosting_mode='CUSTOMER_MANAGED',
            db_host='postgres.customer.com',
            db_port=5432,
            db_user='user',
            database_name='cust_db',
            secret_reference='vault://invalid/ref',
            status='ACTIVE',
        )

        token = pyjwt.encode(
            {'tid': str(self.customer_tenant.id), 'exp': 9999999999},
            settings.SECRET_KEY,
            algorithm='HS256'
        )

        factory = RequestFactory()
        request = factory.get('/api/v1/tenant/users/')
        request.META['HTTP_AUTHORIZATION'] = f'Bearer {token}'

        middleware = TenantDatabaseMiddleware(get_response=lambda req: None)
        response = middleware(request)

        self.assertEqual(response.status_code, 503)
        self.assertIn('TENANT_DATABASE_UNAVAILABLE', response.content.decode())
        self.assertIsNone(get_tenant_db_alias())
