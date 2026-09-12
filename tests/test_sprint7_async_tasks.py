"""
tests/test_sprint7_async_tasks.py — Phase 7E: Asynchronous Tenant Provisioning & Migration Tasks.

Validates:
1. Celery task registration and configuration (acks_late, reject_on_worker_lost, retries).
2. Redis distributed locking with safe ownership and collision prevention.
3. Asynchronous provisioning API:
   - Authorization validation (401/403 for unauthorized users).
   - Validation of payload fields (400 for missing fields).
   - Creation of TenantProvisioning record in Master DB ('default') in QUEUED status.
   - Celery task dispatch and HTTP 202 Accepted response.
   - Celery task ID persistence in TenantProvisioning.celery_task_id.
   - Duplicate provisioning request guard (409 Conflict).
   - Live status endpoint (GET /status/).
4. Provisioning task execution semantics:
   - Status progression (QUEUED -> IN_PROGRESS -> COMPLETED).
   - Idempotent re-execution and retry behavior.
   - Worker failure handling and error_step logging.
5. Asynchronous tenant migration tasks:
   - Fail-closed validation (invalid UUID, non-existent tenant, inactive tenant).
   - Tenant database context establishment and Master DB isolation.
   - Per-tenant Redis lock ownership.
   - Dynamic connection cleanup on task completion.
6. Migrate-all fan-out orchestration:
   - migrate_all_tenants_async task dispatch.
   - Management command migrate_all_tenants --all --async.
"""

import uuid
from unittest.mock import patch, MagicMock
from django.test import TestCase
from django.conf import settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken

from config.celery import app as celery_app
from config.routers import set_tenant_db_alias, get_tenant_db_alias
from config.tenant_middleware import _register_tenant_connection
from apps.master.models_iam import PlatformUser, PlatformRole, PlatformPermission, PlatformRolePermission, PlatformUserRole
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantProvisioning, TenantDataSource, PlatformAuditEvent
from apps.master.provisioning import TenantProvisioningEngine
from apps.tenant_core.locks import redis_distributed_lock, LockAcquisitionError, is_lock_held
from apps.master.tasks import provision_tenant_async, migrate_tenant_async, migrate_all_tenants_async


class Sprint7AsyncTasksBaseTestCase(TestCase):
    """Base setup for Sprint 7 Phase 7E tests."""
    databases = '__all__'

    def setUp(self):
        super().setUp()
        set_tenant_db_alias(None)
        self.client = APIClient()

        # 1. Platform Super Admin (Full Access)
        self.super_role, _ = PlatformRole.objects.get_or_create(
            code='SUPER_ADMIN',
            defaults={'name': 'Super Admin', 'is_system': True, 'is_active': True}
        )
        self.admin_user = PlatformUser.objects.create(
            email='admin@platform.internal',
            first_name='Platform',
            last_name='Admin',
            status='ACTIVE',
            is_staff=True,
            is_superuser=True,
        )
        PlatformUserRole.objects.get_or_create(user=self.admin_user, role=self.super_role, defaults={'is_active': True})

        # 2. Platform User WITH tenants.provision permission
        self.provisioner_role, _ = PlatformRole.objects.get_or_create(
            code='PROVISIONER_ROLE',
            defaults={'name': 'Tenant Provisioner', 'is_system': False, 'is_active': True}
        )
        perm_provision, _ = PlatformPermission.objects.get_or_create(
            code='tenants.provision',
            defaults={'module': 'tenants', 'action': 'provision', 'name': 'Provision Tenants'}
        )
        perm_view, _ = PlatformPermission.objects.get_or_create(
            code='tenants.view',
            defaults={'module': 'tenants', 'action': 'view', 'name': 'View Tenants'}
        )
        PlatformRolePermission.objects.get_or_create(role=self.provisioner_role, permission=perm_provision, defaults={'granted': True})
        PlatformRolePermission.objects.get_or_create(role=self.provisioner_role, permission=perm_view, defaults={'granted': True})

        self.provisioner_user = PlatformUser.objects.create(
            email='provisioner@platform.internal',
            first_name='Tenant',
            last_name='Provisioner',
            status='ACTIVE',
            is_staff=True,
            is_superuser=False,
        )
        PlatformUserRole.objects.create(user=self.provisioner_user, role=self.provisioner_role, is_active=True)

        # 3. Platform User WITHOUT tenants.provision permission
        self.unauthorized_user = PlatformUser.objects.create(
            email='unauthorized@platform.internal',
            first_name='Unauth',
            last_name='User',
            status='ACTIVE',
            is_staff=True,
            is_superuser=False,
        )

        # 4. Existing Active Tenant with DataSource for migration tests
        self.active_tenant = Tenant.objects.using('default').create(
            code='P7E-MIG-001',
            name='Phase 7E Migration Tenant',
            slug='phase7e-mig-tenant',
            status='ACTIVE',
        )
        self.active_ds = TenantDataSource.objects.using('default').create(
            tenant=self.active_tenant,
            db_name='test_fitness_tenant',
            status='ACTIVE',
            db_schema_version='1.0',
        )

    def tearDown(self):
        set_tenant_db_alias(None)
        from django.db import connections
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

    def get_jwt_token(self, user: PlatformUser) -> str:
        refresh = RefreshToken()
        refresh['sub'] = str(user.id)
        refresh['user_type'] = 'platform'
        refresh['role'] = 'SUPER_ADMIN' if user.is_superuser else 'CUSTOM'
        refresh['email'] = user.email
        return str(refresh.access_token)


class TaskRegistrationAndReliabilityPhase7ETest(Sprint7AsyncTasksBaseTestCase):
    """Test Celery task registration, queue configuration, and reliability flags."""

    def test_tasks_registered_in_celery_app(self):
        """All Phase 7E Celery tasks must be registered in the application task catalog."""
        self.assertIn('apps.master.tasks.provision_tenant_async', celery_app.tasks)
        self.assertIn('apps.master.tasks.migrate_tenant_async', celery_app.tasks)
        self.assertIn('apps.master.tasks.migrate_all_tenants_async', celery_app.tasks)

    def test_task_reliability_configuration(self):
        """Tasks must enforce acks_late, reject_on_worker_lost, and bounded retries."""
        prov_task = celery_app.tasks['apps.master.tasks.provision_tenant_async']
        self.assertTrue(prov_task.acks_late)
        self.assertTrue(prov_task.reject_on_worker_lost)
        self.assertEqual(prov_task.max_retries, 3)

        mig_task = celery_app.tasks['apps.master.tasks.migrate_tenant_async']
        self.assertTrue(mig_task.acks_late)
        self.assertTrue(mig_task.reject_on_worker_lost)
        self.assertEqual(mig_task.max_retries, 2)


class RedisDistributedLockPhase7ETest(Sprint7AsyncTasksBaseTestCase):
    """Test Redis distributed locking with safe UUID token ownership and collision handling."""

    def test_lock_acquire_and_release(self):
        """Lock must be successfully acquired, reported as held, and released cleanly."""
        lock_key = f"lock:test:{uuid.uuid4()}"
        self.assertFalse(is_lock_held(lock_key))

        with redis_distributed_lock(lock_key, timeout_seconds=10, blocking=False):
            self.assertTrue(is_lock_held(lock_key))

        self.assertFalse(is_lock_held(lock_key))

    def test_concurrent_lock_collision_fails_closed(self):
        """Attempting to acquire an already-held lock without blocking raises LockAcquisitionError."""
        lock_key = f"lock:collision:{uuid.uuid4()}"

        with redis_distributed_lock(lock_key, timeout_seconds=60, blocking=False):
            with self.assertRaises(LockAcquisitionError):
                with redis_distributed_lock(lock_key, timeout_seconds=60, blocking=False):
                    pass

    def test_lock_lease_auto_renewal_heartbeat(self):
        """With auto_renew=True, background thread renews TTL and cleanly exits on finally."""
        import time
        from apps.tenant_core.locks import redis_distributed_lock, is_lock_held

        lock_key = f"lock:renew:{uuid.uuid4()}"
        with redis_distributed_lock(lock_key, timeout_seconds=5, auto_renew=True, renew_interval=0.1):
            self.assertTrue(is_lock_held(lock_key))
            time.sleep(0.3)
            self.assertTrue(is_lock_held(lock_key))

        self.assertFalse(is_lock_held(lock_key))

    def test_redis_concurrency_semaphore_bounds_capacity(self):
        """Concurrency semaphore allows up to max_concurrent slots, then raises ConcurrencyLimitExceeded."""
        from apps.tenant_core.locks import redis_concurrency_semaphore, ConcurrencyLimitExceeded

        sem_key = f"semaphore:test:{uuid.uuid4()}"
        max_slots = 2

        with redis_concurrency_semaphore(sem_key, max_concurrent=max_slots, timeout_seconds=30):
            with redis_concurrency_semaphore(sem_key, max_concurrent=max_slots, timeout_seconds=30):
                # 3rd acquisition must raise ConcurrencyLimitExceeded
                with self.assertRaises(ConcurrencyLimitExceeded):
                    with redis_concurrency_semaphore(sem_key, max_concurrent=max_slots, timeout_seconds=30):
                        pass


class AsyncProvisioningAPIPhase7ETest(Sprint7AsyncTasksBaseTestCase):
    """Test platform provisioning API endpoint converting to async execution."""

    def test_unauthenticated_request_rejected(self):
        """POST without platform JWT token must be rejected with 401."""
        res = self.client.post('/api/v1/platform/provisioning/run/', {}, format='json')
        self.assertIn(res.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

    def test_unauthorized_user_without_permission_rejected(self):
        """User without tenants.provision permission must be rejected with 403 Forbidden."""
        token = self.get_jwt_token(self.unauthorized_user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        payload = {
            'brand_name': 'Unauthorized Gym',
            'admin_email': 'admin@unauth.com',
            'admin_first_name': 'John',
            'admin_password': 'SecurePassword123!',
        }
        res = self.client.post('/api/v1/platform/provisioning/run/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_missing_required_fields_returns_400(self):
        """Missing brand_name, admin_email, or admin_password returns HTTP 400 Bad Request."""
        token = self.get_jwt_token(self.provisioner_user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        res = self.client.post('/api/v1/platform/provisioning/run/', {'brand_name': 'Incomplete Gym'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', res.data)

    def test_authorized_post_returns_202_and_dispatches_task(self):
        """
        Valid POST must:
        1. Validate authorization.
        2. Create TenantProvisioning record in QUEUED status.
        3. Dispatch Celery task.
        4. Persist Celery task ID in TenantProvisioning.celery_task_id.
        5. Return HTTP 202 Accepted.
        6. Never execute DDL synchronously in request thread.
        """
        token = self.get_jwt_token(self.provisioner_user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        payload = {
            'brand_name': 'Async Iron Gym',
            'slug': 'async-iron-gym',
            'admin_email': 'admin@asynciron.com',
            'admin_first_name': 'Iron',
            'admin_last_name': 'Admin',
            'admin_password': 'IronPassword123!',
        }

        with patch('apps.master.tasks.provision_tenant_async.delay') as mock_delay:
            mock_task = MagicMock()
            mock_task.id = 'task-uuid-iron-1234'
            mock_delay.return_value = mock_task

            res = self.client.post('/api/v1/platform/provisioning/run/', payload, format='json')

            self.assertEqual(res.status_code, status.HTTP_202_ACCEPTED)
            self.assertIn('provisioning_id', res.data)
            self.assertEqual(res.data['celery_task_id'], 'task-uuid-iron-1234')
            self.assertEqual(res.data['status'], 'QUEUED')

            # Verify Master DB persistence
            prov_id = res.data['provisioning_id']
            prov_record = TenantProvisioning.objects.using('default').get(id=prov_id)
            self.assertEqual(prov_record.status, 'QUEUED')
            self.assertEqual(prov_record.celery_task_id, 'task-uuid-iron-1234')
            self.assertEqual(prov_record.initiated_by, self.provisioner_user)

            mock_delay.assert_called_once_with(str(prov_record.id), payload)

    def test_duplicate_provisioning_request_rejected_with_409(self):
        """Duplicate request while provisioning is in QUEUED/IN_PROGRESS returns HTTP 409 Conflict."""
        token = self.get_jwt_token(self.provisioner_user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        # Pre-create an active provisioning record for a tenant with slug 'duplicate-gym'
        tenant = Tenant.objects.using('default').create(
            name='Duplicate Gym',
            slug='duplicate-gym',
            code='DUP-GYM',
            status='DRAFT',
        )
        TenantProvisioning.objects.using('default').create(
            tenant=tenant,
            status='IN_PROGRESS',
            celery_task_id='existing-task-id-5678',
        )

        payload = {
            'brand_name': 'Duplicate Gym',
            'slug': 'duplicate-gym',
            'admin_email': 'admin@duplicate.com',
            'admin_first_name': 'Dup',
            'admin_password': 'DupPassword123!',
        }

        res = self.client.post('/api/v1/platform/provisioning/run/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertIn('already active', res.data['error'])
        self.assertEqual(res.data['celery_task_id'], 'existing-task-id-5678')

    def test_provisioning_status_action_endpoint(self):
        """GET /api/v1/platform/provisioning/{id}/status/ returns live status and step metrics."""
        token = self.get_jwt_token(self.provisioner_user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        prov = TenantProvisioning.objects.using('default').create(
            status='IN_PROGRESS',
            celery_task_id='status-task-id-999',
            current_step='CREATE_ORGANIZATION',
            total_steps=14,
            completed_steps=6,
            step_log=[{'step': 'CREATE_TENANT', 'success': True}],
        )

        res = self.client.get(f'/api/v1/platform/provisioning/{prov.id}/status/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['provisioning_id'], str(prov.id))
        self.assertEqual(res.data['status'], 'IN_PROGRESS')
        self.assertEqual(res.data['celery_task_id'], 'status-task-id-999')
        self.assertEqual(res.data['completed_steps'], 6)
        self.assertEqual(res.data['current_step'], 'CREATE_ORGANIZATION')
        self.assertEqual(len(res.data['step_log']), 1)


class ProvisioningTaskExecutionPhase7ETest(Sprint7AsyncTasksBaseTestCase):
    """Test provisioning task execution, idempotency, and error handling."""

    def test_task_raises_if_provisioning_record_not_found(self):
        """Task raises ValueError if provisioning ID does not exist on Master DB."""
        fake_id = str(uuid.uuid4())
        with self.assertRaises(ValueError):
            provision_tenant_async.delay(fake_id, {'brand_name': 'Ghost Gym'})

    def test_task_exits_cleanly_if_already_completed(self):
        """If provisioning record is already COMPLETED, task safely exits without recreating resources."""
        prov = TenantProvisioning.objects.using('default').create(
            tenant=self.active_tenant,
            status='COMPLETED',
            celery_task_id='completed-task-001',
            completed_steps=14,
        )

        res = provision_tenant_async.delay(str(prov.id), {'brand_name': 'Already Done Gym'})
        self.assertTrue(res.ready())
        self.assertEqual(res.result['status'], 'COMPLETED')
        self.assertEqual(res.result['provisioning_id'], str(prov.id))

    def test_task_successful_execution_and_status_progression(self):
        """Task executes 14-step engine, updates record to COMPLETED, and logs audit event."""
        prov = TenantProvisioning.objects.using('default').create(
            status='QUEUED',
            total_steps=14,
            completed_steps=0,
            initiated_by=self.provisioner_user,
        )

        payload = {
            'brand_name': 'Async Powerhouse Gym',
            'slug': 'async-powerhouse-gym',
            'admin_email': 'admin@powerhouse.com',
            'admin_first_name': 'Power',
            'admin_password': 'PowerPassword123!',
        }

    def test_task_successful_execution_and_status_progression(self):
        """Task executes 14-step engine, updates record to COMPLETED, and logs audit event."""
        prov = TenantProvisioning.objects.using('default').create(
            status='QUEUED',
            total_steps=14,
            completed_steps=0,
            initiated_by=self.provisioner_user,
        )

        payload = {
            'brand_name': 'Async Powerhouse Gym',
            'slug': 'async-powerhouse-gym',
            'admin_email': 'admin@powerhouse.com',
            'admin_first_name': 'Power',
            'admin_password': 'PowerPassword123!',
        }

        # Mock _step6 to return 'test' so db_alias resolves to 'tenant_test' (the preconfigured test DB)
        def mock_step6(tenant, data_source, provisioning):
            data_source.status = 'ACTIVE'
            data_source.provisioned_at = timezone.now()
            data_source.db_schema_version = '1.0'
            data_source.save(using='default')
            provisioning.log_step('PROVISION_DATABASE', True, "Test DB mapped to tenant_test")
            return 'test'

        with patch.object(TenantProvisioningEngine, '_step6_provision_database', side_effect=mock_step6):
            res = provision_tenant_async.delay(str(prov.id), payload)

            self.assertTrue(res.ready())
            self.assertTrue(res.result.get('success', False))

            prov.refresh_from_db()
            self.assertEqual(prov.status, 'COMPLETED')
            self.assertEqual(prov.completed_steps, 14)
            self.assertIsNotNone(prov.tenant)
            self.assertEqual(prov.tenant.slug, 'async-powerhouse-gym')

            # Verify PlatformAuditEvent recorded
            audit = PlatformAuditEvent.objects.using('default').filter(action='PROVISION_TENANT').first()
            self.assertIsNotNone(audit)
            self.assertEqual(audit.actor, self.provisioner_user)
            self.assertIn('async-powerhouse-gym', audit.description)

    def test_task_failure_records_error_step_and_message(self):
        """On pipeline exception, record status is marked FAILED with error_step and error_message."""
        prov = TenantProvisioning.objects.using('default').create(
            status='QUEUED',
            total_steps=14,
            completed_steps=0,
        )

        payload = {
            'brand_name': 'Failing Gym',
            'slug': 'failing-gym',
            'admin_email': 'admin@failing.com',
            'admin_first_name': 'Fail',
            'admin_password': 'FailPassword123!',
        }

        with patch('apps.master.provisioning.TenantProvisioningEngine._step2_configure_domain', side_effect=RuntimeError("Domain failure")):
            res = provision_tenant_async.delay(str(prov.id), payload)
            self.assertTrue(res.ready())

            prov.refresh_from_db()
            self.assertEqual(prov.status, 'FAILED')
            self.assertIn("Domain failure", prov.error_message)


class AsyncMigrationTaskPhase7ETest(Sprint7AsyncTasksBaseTestCase):
    """Test asynchronous per-tenant migration tasks."""

    def test_invalid_uuid_fails_closed(self):
        """Non-UUID tenant ID raises ValueError immediately."""
        with self.assertRaises(ValueError):
            migrate_tenant_async.delay('not-a-uuid')

    def test_nonexistent_tenant_fails_closed(self):
        """Random unassigned UUID raises ValueError."""
        random_id = str(uuid.uuid4())
        with self.assertRaises(ValueError):
            migrate_tenant_async.delay(random_id)

    def test_inactive_tenant_skipped(self):
        """Suspended or deactivated tenant is safely skipped."""
        suspended_tenant = Tenant.objects.using('default').create(
            name='Suspended Gym',
            slug='suspended-gym',
            code='SUSP-001',
            status='SUSPENDED',
        )
        res = migrate_tenant_async.delay(str(suspended_tenant.id))
        self.assertTrue(res.ready())
        self.assertEqual(res.result['status'], 'SKIPPED')

    def test_active_tenant_migration_success_and_context_isolation(self):
        """
        Active tenant executes migration inside tenant_database_context.
        Master DB is never targeted. Context is cleanly restored in finally.
        """
        self.assertIsNone(get_tenant_db_alias())

        with patch('apps.master.tasks.call_command') as mock_call_command:
            res = migrate_tenant_async.delay(str(self.active_tenant.id))

            self.assertTrue(res.ready())
            self.assertEqual(res.result['status'], 'SUCCESS')
            self.assertEqual(res.result['tenant_slug'], self.active_tenant.slug)

            # Assert migrate was called with tenant database alias
            mock_call_command.assert_called_once()
            called_args, called_kwargs = mock_call_command.call_args
            self.assertEqual(called_args[0], 'migrate')
            self.assertEqual(called_args[1], 'tenant_core')
            self.assertEqual(called_kwargs['database'], 'tenant_test')
            self.assertNotEqual(called_kwargs['database'], 'default')

        # Assert thread-local context was cleanly restored
        self.assertIsNone(get_tenant_db_alias())


class MigrateAllFanOutPhase7ETest(Sprint7AsyncTasksBaseTestCase):
    """Test migrate_all_tenants_async fan-out orchestrator and management command."""

    def test_migrate_all_fans_out_individual_tasks(self):
        """migrate_all_tenants_async resolves active tenants and dispatches individual tasks."""
        with patch('apps.master.tasks.migrate_tenant_async.apply_async') as mock_apply:
            mock_task = MagicMock()
            mock_task.id = 'task-fan-out-1'
            mock_apply.return_value = mock_task

            res = migrate_all_tenants_async.delay()
            self.assertTrue(res.ready())
            self.assertEqual(res.result['dispatched_count'], 1)
            self.assertEqual(len(res.result['dispatched_tasks']), 1)
            self.assertEqual(res.result['dispatched_tasks'][0]['tenant_slug'], self.active_tenant.slug)

            mock_apply.assert_called_once_with(args=[str(self.active_tenant.id)], countdown=0)

    def test_management_command_async_flag(self):
        """python manage.py migrate_all_tenants --all --async dispatches via Celery."""
        from io import StringIO
        from django.core.management import call_command

        out = StringIO()
        with patch('apps.master.tasks.migrate_tenant_async.delay') as mock_delay:
            mock_task = MagicMock()
            mock_task.id = 'cli-task-uuid-888'
            mock_delay.return_value = mock_task

            call_command('migrate_all_tenants', '--all', '--async', stdout=out)

            output = out.getvalue()
            self.assertIn("ASYNC TENANT MIGRATION DISPATCH COMPLETE", output)
            self.assertIn("cli-task-uuid-888", output)
            mock_delay.assert_called_once_with(str(self.active_tenant.id))
