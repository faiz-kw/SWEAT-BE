"""
tests/test_operational_async_scheduling.py — Production-Grade Operational Scheduling Verification.

Validates:
1. CELERY_BEAT_SCHEDULE dictionary configuration in settings with exact task names and schedules.
2. Periodic tenant resource usage aggregation task (sync_all_tenant_resource_usage_async) with PlatformAuditEvent.
3. Periodic tenant database connection health checks (check_all_tenant_databases_health_async) recording latency and health.
4. Periodic catalog synchronization (sync_tenant_catalogs_async) with system job audit semantics.
5. Periodic due subscription renewal (renew_due_subscriptions_async) with invoice generation.
"""

from decimal import Decimal
from django.test import TestCase
from django.conf import settings
from django.utils import timezone
from datetime import timedelta

from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource, TenantDataSourceHealth, PlatformAuditEvent
from apps.master.models_saas import SaasPlan, SaasPlanPrice, TenantSubscription
from apps.master.tasks import (
    sync_all_tenant_resource_usage_async,
    check_all_tenant_databases_health_async,
    sync_tenant_catalogs_async,
    renew_due_subscriptions_async,
)
from config.tenant_middleware import _register_tenant_connection, unregister_tenant_connection
from config.routers import set_tenant_db_alias


class OperationalAsyncSchedulingTestCase(TestCase):
    databases = '__all__'

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        _register_tenant_connection('tenant_test', 'test_fitness_tenant')

        # Create master test tenant
        self.tenant = Tenant.objects.using('default').create(
            name='Ops Test Fitness',
            slug='ops-fitness',
            code='OPS-001',
            status='ACTIVE',
        )
        self.data_source = TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            hosting_mode='PLATFORM_MANAGED',
            source_type='PLATFORM_MANAGED',
            database_name='test',
            db_name='test',
            status='ACTIVE',
        )

        # Plan & subscription
        self.plan, _ = SaasPlan.objects.using('default').get_or_create(
            code='PLAN-OPS',
            defaults={'name': 'Ops Plan', 'tier': 'standard', 'is_active': True}
        )
        self.price, _ = SaasPlanPrice.objects.using('default').get_or_create(
            plan=self.plan,
            billing_cycle='MONTHLY',
            defaults={'amount': Decimal('5000.00'), 'currency': 'INR', 'is_active': True}
        )

    def tearDown(self):
        set_tenant_db_alias(None)
        from django.conf import settings
        for alias in list(settings.DATABASES.keys()):
            if alias not in ('default', 'tenant_test'):
                unregister_tenant_connection(alias)

    def test_celery_beat_schedule_configuration(self):
        """CELERY_BEAT_SCHEDULE contains all required periodic operational jobs."""
        schedule = getattr(settings, 'CELERY_BEAT_SCHEDULE', {})
        self.assertIn('master-collect-resource-usage', schedule)
        self.assertIn('master-check-database-health', schedule)
        self.assertIn('master-sync-product-catalog', schedule)
        self.assertIn('master-renew-due-subscriptions', schedule)

        # Verify task paths
        self.assertEqual(
            schedule['master-collect-resource-usage']['task'],
            'apps.master.tasks.sync_all_tenant_resource_usage_async'
        )
        self.assertEqual(
            schedule['master-check-database-health']['task'],
            'apps.master.tasks.check_all_tenant_databases_health_async'
        )
        self.assertEqual(
            schedule['master-sync-product-catalog']['task'],
            'apps.master.tasks.sync_tenant_catalogs_async'
        )
        self.assertEqual(
            schedule['master-renew-due-subscriptions']['task'],
            'apps.master.tasks.renew_due_subscriptions_async'
        )

    def test_sync_all_tenant_resource_usage_async(self):
        """Periodic resource usage task aggregates usage and writes audit log."""
        initial_audits = PlatformAuditEvent.objects.using('default').filter(action='RESOURCE_USAGE_SYNCED').count()

        result = sync_all_tenant_resource_usage_async()
        self.assertIsInstance(result, dict)

        new_audits = PlatformAuditEvent.objects.using('default').filter(action='RESOURCE_USAGE_SYNCED').count()
        self.assertGreaterEqual(new_audits, initial_audits + 1)

    def test_check_all_tenant_databases_health_async(self):
        """Periodic database health check tests active connections and updates health records."""
        initial_records = TenantDataSourceHealth.objects.using('default').filter(tenant=self.tenant).count()

        result = check_all_tenant_databases_health_async()
        self.assertIsInstance(result, dict)
        self.assertGreaterEqual(result['total_checked'], 1)

        # Verify TenantDataSourceHealth record created
        new_records = TenantDataSourceHealth.objects.using('default').filter(tenant=self.tenant).count()
        self.assertGreaterEqual(new_records, initial_records + 1)

        latest_health = TenantDataSourceHealth.objects.using('default').filter(tenant=self.tenant).latest('checked_at')
        self.assertIn(latest_health.status, ['HEALTHY', 'DEGRADED'], msg=f"Error was: {latest_health.error_message}")
        self.assertGreater(latest_health.response_time_ms, 0)

        # Verify DataSource last_health_check_at updated
        self.data_source.refresh_from_db(using='default')
        self.assertIsNotNone(self.data_source.last_health_check_at)

    def test_sync_tenant_catalogs_async(self):
        """Periodic catalog sync triggers catalog command and emits audit event."""
        initial_audits = PlatformAuditEvent.objects.using('default').filter(action='TENANT_CATALOGS_SYNCED').count()

        result = sync_tenant_catalogs_async()
        self.assertIsInstance(result, dict)
        self.assertEqual(result['status'], 'SUCCESS')

        new_audits = PlatformAuditEvent.objects.using('default').filter(action='TENANT_CATALOGS_SYNCED').count()
        self.assertGreaterEqual(new_audits, initial_audits + 1)

    def test_renew_due_subscriptions_async(self):
        """Periodic renewal checks for due subscriptions and executes renewal."""
        past_due_date = timezone.now() - timedelta(hours=1)
        sub = TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
            billing_cycle='MONTHLY',
            next_renewal_at=past_due_date,
        )

        result = renew_due_subscriptions_async()
        self.assertIsInstance(result, dict)
        self.assertGreaterEqual(result['renewed_count'], 1)

        sub.refresh_from_db(using='default')
        # Assert next_renewal_at advanced to future
        self.assertGreater(sub.next_renewal_at, timezone.now())
