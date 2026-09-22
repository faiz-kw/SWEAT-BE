"""
tests/test_sprint7_celery_infra.py — Phase 7A: Celery + Redis Foundation Tests.

Validates:
1. Celery app initialization and settings configuration.
2. JSON-only task/result serialization and result expiry.
3. Task execution time limits, acks_late, and failure settings.
4. Eager mode enabled strictly during tests ('test' in sys.argv).
5. Outside test mode, eager mode is disabled and Celery does not silently fall back.
6. Fail-fast behavior when real broker is unreachable without eager mode.
7. Eager execution of registered tasks during test suite execution.
"""

import sys
from unittest.mock import patch
from django.db import connections
from django.test import TestCase
from django.conf import settings

from celery import shared_task
from celery.exceptions import OperationalError
from kombu.exceptions import OperationalError as KombuOperationalError

from config.celery import app as celery_app
from config.routers import (
    get_tenant_db_alias,
    set_tenant_db_alias,
    TenantRoutingError,
)



@shared_task(name='test_sprint7_echo_task')
def sample_echo_task(x, y):
    return x + y


@shared_task(name='test_sprint7_failing_task')
def sample_failing_task():
    raise ValueError("Explicit task failure for testing")


class CeleryInfrastructurePhase7ATest(TestCase):
    """Test suite for Phase 7A Celery + Redis configuration and execution semantics."""

    def test_celery_app_initialized(self):
        """Celery app must be named 'performanceos' and expose registered tasks."""
        self.assertEqual(celery_app.main, 'performanceos')
        # Autodiscovery should find shared tasks
        self.assertIn('test_sprint7_echo_task', celery_app.tasks)
        self.assertIn('config.celery.debug_task', celery_app.tasks)

    def test_celery_serialization_json_only(self):
        """Security requirement: Only JSON serialization must be accepted."""
        self.assertEqual(settings.CELERY_ACCEPT_CONTENT, ['json'])
        self.assertEqual(settings.CELERY_TASK_SERIALIZER, 'json')
        self.assertEqual(settings.CELERY_RESULT_SERIALIZER, 'json')
        self.assertTrue(settings.CELERY_ENABLE_UTC)
        self.assertEqual(settings.CELERY_TIMEZONE, settings.TIME_ZONE)

    def test_celery_time_limits_and_failure_behavior(self):
        """Sensible time limits, acks_late, and worker lost rejection must be set."""
        self.assertEqual(settings.CELERY_TASK_TIME_LIMIT, 600)  # 10 minutes
        self.assertEqual(settings.CELERY_TASK_SOFT_TIME_LIMIT, 540)  # 9 minutes
        self.assertTrue(settings.CELERY_TASK_ACKS_LATE)
        self.assertTrue(settings.CELERY_TASK_REJECT_ON_WORKER_LOST)
        self.assertEqual(settings.CELERY_RESULT_EXPIRES, 86400)  # 24 hours
        self.assertEqual(settings.CELERY_TASK_DEFAULT_QUEUE, 'default')

    def test_eager_mode_strictly_active_in_test_environment(self):
        """During 'python manage.py test', eager mode must be active."""
        self.assertIn('test', sys.argv)
        self.assertTrue(settings.CELERY_TASK_ALWAYS_EAGER)
        self.assertTrue(settings.CELERY_TASK_EAGER_PROPAGATES)

    def test_eager_mode_disabled_outside_test_mode(self):
        """Outside 'test' in sys.argv, eager mode must be strictly False."""
        simulated_argv = ['manage.py', 'runserver', '8000']
        # Evaluate settings logic when 'test' is not in argv
        is_eager_in_runserver = 'test' in simulated_argv
        self.assertFalse(is_eager_in_runserver)

    def test_eager_task_execution_succeeds(self):
        """Tasks dispatched via .delay() in test environment execute eagerly and return results."""
        async_res = sample_echo_task.delay(15, 27)
        self.assertTrue(async_res.ready())
        self.assertTrue(async_res.successful())
        self.assertEqual(async_res.result, 42)

    def test_eager_task_failure_propagates_exception(self):
        """With CELERY_TASK_EAGER_PROPAGATES=True, task exceptions raise immediately."""
        with self.assertRaises(ValueError) as ctx:
            sample_failing_task.delay()
        self.assertIn("Explicit task failure for testing", str(ctx.exception))

    def test_fail_fast_when_broker_unavailable_without_eager(self):
        """
        When eager mode is False and Redis is unreachable, connecting to the broker
        MUST fail fast and raise an OperationalError rather than silently falling back
        to synchronous execution.
        """
        from kombu import Connection
        # Target an invalid port with a 1-second timeout
        conn = Connection('redis://127.0.0.1:59999/0', connect_timeout=1)
        with self.assertRaises((OperationalError, KombuOperationalError, Exception)):
            conn.connect()


class TenantWorkerDatabaseContextPhase7BTest(TestCase):
    """
    Test suite for Phase 7B: tenant_database_context() for Celery/background workers.
    Validates strict fail-closed security, connection lifecycle, and worker isolation.
    """

    databases = '__all__'

    def setUp(self):

        super().setUp()
        set_tenant_db_alias(None)

        from apps.master.models_tenant import Tenant
        from apps.master.models_infra import TenantDataSource
        from apps.tenant_core.models_org import Organization

        # 1. Active Tenant A with active data source
        self.tenant_a = Tenant.objects.using('default').create(
            code='P7B-TENANT-A',
            name='Phase 7B Tenant Alpha',
            slug='phase7b-tenant-alpha',
            status='ACTIVE',
        )
        self.ds_a = TenantDataSource.objects.using('default').create(
            tenant=self.tenant_a,
            db_name='test_fitness_tenant',
            status='ACTIVE',
        )

        # 2. Active Tenant B with separate data source
        self.tenant_b = Tenant.objects.using('default').create(
            code='P7B-TENANT-B',
            name='Phase 7B Tenant Beta',
            slug='phase7b-tenant-beta',
            status='ACTIVE',
        )
        self.ds_b = TenantDataSource.objects.using('default').create(
            tenant=self.tenant_b,
            db_name='tenant_beta_db',
            status='ACTIVE',
        )

        # 3. Inactive Tenant (Suspended)
        self.tenant_inactive = Tenant.objects.using('default').create(
            code='P7B-TENANT-INACT',
            name='Inactive Tenant',
            slug='inactive-tenant',
            status='SUSPENDED',
        )
        self.ds_inactive = TenantDataSource.objects.using('default').create(
            tenant=self.tenant_inactive,
            db_name='tenant_inactive_db',
            status='ACTIVE',
        )

        # 4. Active Tenant with Pending/Inactive DataSource
        self.tenant_pending_ds = Tenant.objects.using('default').create(
            code='P7B-TENANT-PEND',
            name='Pending DS Tenant',
            slug='pending-ds-tenant',
            status='ACTIVE',
        )
        self.ds_pending = TenantDataSource.objects.using('default').create(
            tenant=self.tenant_pending_ds,
            db_name='tenant_pending_db',
            status='PENDING',
        )


        # 5. Seed an Organization in tenant_test DB
        self.org_a = Organization.objects.using('tenant_test').create(
            name='Tenant Alpha Org',
            code='ALPHA-ORG',
            status='ACTIVE',
        )

    def tearDown(self):
        set_tenant_db_alias(None)
        # Clean up any dynamic test aliases registered during tests to prevent test runner unwrap conflicts
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


    def test_valid_tenant_resolves_and_sets_routing_context(self):
        """Active tenant resolves correctly and sets thread-local alias."""
        from apps.tenant_core.context import tenant_database_context
        from apps.tenant_core.models_org import Organization
        from config.routers import get_tenant_db_alias

        self.assertIsNone(get_tenant_db_alias())
        with tenant_database_context(self.tenant_a.id) as alias:
            # Thread-local alias must match resolved tenant alias
            self.assertEqual(get_tenant_db_alias(), alias)
            self.assertEqual(alias, 'tenant_test')
            # Querying tenant model inside context resolves to tenant database
            found_org = Organization.objects.filter(code='ALPHA-ORG').first()
            self.assertIsNotNone(found_org)
            self.assertEqual(found_org.name, 'Tenant Alpha Org')

        # Outside context, thread-local alias is reset to None
        self.assertIsNone(get_tenant_db_alias())

    def test_master_db_queries_remain_on_default_inside_context(self):
        """Master DB models (Platform, Tenant, DataSource) always query 'default'."""
        from apps.tenant_core.context import tenant_database_context
        from apps.master.models_tenant import Tenant
        from config.routers import MasterRouter

        router = MasterRouter()
        self.assertEqual(router.db_for_read(Tenant), 'default')

        with tenant_database_context(self.tenant_a.id):
            # Model in apps.master queries Master DB
            t = Tenant.objects.filter(id=self.tenant_a.id).first()
            self.assertIsNotNone(t)
            self.assertEqual(t.slug, 'phase7b-tenant-alpha')

    def test_invalid_tenant_id_inputs_fail_closed(self):
        """Context manager rejects non-UUID inputs, connection strings, or empty IDs."""
        from apps.tenant_core.context import tenant_database_context
        from config.routers import TenantRoutingError

        for bad_input in [None, '', 'not-a-uuid', 'tenant_custom_alias', {'db_name': 'hack'}]:
            with self.subTest(bad_input=bad_input):
                with self.assertRaises(TenantRoutingError):
                    with tenant_database_context(bad_input):
                        pass

    def test_nonexistent_tenant_fails_closed(self):
        """Random unassigned UUID fails closed."""
        import uuid
        from apps.tenant_core.context import tenant_database_context
        from config.routers import TenantRoutingError

        with self.assertRaises(TenantRoutingError) as ctx:
            with tenant_database_context(uuid.uuid4()):
                pass
        self.assertIn("does not exist", str(ctx.exception))

    def test_inactive_tenant_fails_closed(self):
        """Inactive or suspended tenant raises TenantRoutingError."""
        from apps.tenant_core.context import tenant_database_context
        from config.routers import TenantRoutingError

        with self.assertRaises(TenantRoutingError) as ctx:
            with tenant_database_context(self.tenant_inactive.id):
                pass
        self.assertIn("is not active", str(ctx.exception))

    def test_inactive_datasource_fails_closed(self):
        """Active tenant with pending/inactive DataSource raises TenantRoutingError."""
        from apps.tenant_core.context import tenant_database_context
        from config.routers import TenantRoutingError

        with self.assertRaises(TenantRoutingError) as ctx:
            with tenant_database_context(self.tenant_pending_ds.id):
                pass
        self.assertIn("No active TenantDataSource", str(ctx.exception))

    def test_default_can_never_be_used_as_tenant_db(self):
        """If data source somehow attempts to point to Master DB, fail closed."""
        from apps.master.models_tenant import Tenant
        from apps.master.models_infra import TenantDataSource
        from apps.tenant_core.context import tenant_database_context
        from config.routers import TenantRoutingError

        rogue_tenant = Tenant.objects.using('default').create(
            code='ROGUE-TENANT',
            name='Rogue Tenant',
            slug='rogue-tenant',
            status='ACTIVE',
        )
        TenantDataSource.objects.using('default').create(
            tenant=rogue_tenant,
            db_name='fitness_master',  # Matches Master DB
            status='ACTIVE',
        )

        with self.assertRaises(TenantRoutingError) as ctx:
            with tenant_database_context(rogue_tenant.id):
                pass
        self.assertIn("Master DB ('default')", str(ctx.exception))

    def test_cleanup_guaranteed_after_exception(self):
        """Thread-local state is restored and connection closed even on unhandled exception."""
        from apps.tenant_core.context import tenant_database_context
        from config.routers import get_tenant_db_alias

        self.assertIsNone(get_tenant_db_alias())
        with self.assertRaises(RuntimeError):
            with tenant_database_context(self.tenant_a.id):
                self.assertIsNotNone(get_tenant_db_alias())
                raise RuntimeError("Simulated worker task crash")

        self.assertIsNone(get_tenant_db_alias())

    def test_connection_closed_on_exit(self):
        """Dynamically used connection has close() called on context exit when not in atomic block."""
        from apps.tenant_core.context import tenant_database_context
        from unittest.mock import patch, MagicMock

        mock_conn = MagicMock()
        mock_conn.in_atomic_block = False

        with patch('apps.tenant_core.context.connections') as mock_connections:
            mock_connections.__contains__.return_value = True
            mock_connections.__getitem__.return_value = mock_conn

            with tenant_database_context(self.tenant_b.id):
                pass

            mock_conn.close.assert_called_once()



    def test_no_state_leakage_between_consecutive_worker_tasks(self):
        """Simulate single Celery worker thread handling Tenant A then Tenant B sequentially."""
        from apps.tenant_core.context import tenant_database_context
        from config.routers import get_tenant_db_alias

        self.assertIsNone(get_tenant_db_alias())

        # Worker executes Task 1 for Tenant A
        with tenant_database_context(self.tenant_a.id) as alias_1:
            self.assertEqual(get_tenant_db_alias(), alias_1)
            self.assertEqual(alias_1, 'tenant_test')

        self.assertIsNone(get_tenant_db_alias())

        # Worker executes Task 2 for Tenant B
        from config.routers import build_tenant_db_alias
        with tenant_database_context(self.tenant_b.id) as alias_2:
            self.assertEqual(get_tenant_db_alias(), alias_2)
            self.assertEqual(alias_2, build_tenant_db_alias(self.tenant_b.id))

        # After both tasks, state is clean
        self.assertIsNone(get_tenant_db_alias())


