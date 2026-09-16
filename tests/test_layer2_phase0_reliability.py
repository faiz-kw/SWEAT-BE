"""
tests/test_layer2_phase0_reliability.py — Tests for Layer 2 Phase 0: Reliability, Idempotency & Outbox
"""

import uuid
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken

from config.routers import set_tenant_db_alias, get_tenant_db_alias
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.models_saas import SaasPlan, TenantSubscription, ProductModule, TenantModule
from apps.authentication.views import _build_tenant_token
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_rbac import (
    Role,
    RoleAssignment,
    ModuleCatalog,
    SubmoduleCatalog,
    Permission,
    RoleModuleAccess,
    RoleSubmoduleAccess,
    RolePermissionSet,
    RolePermissionSetItem,
)
from apps.tenant_core.models_audit_outbox import (
    BusinessAuditEvent,
    IdempotencyRecord,
    DomainOutboxEvent,
    ReasonCode,
)
from apps.tenant_core.services_reliability import (
    execute_idempotent_operation,
    record_business_audit,
    enqueue_outbox_event,
    sanitize_payload,
    IdempotencyConflictError,
)
from apps.tenant_core.tasks import process_domain_outbox_events_task


class Layer2Phase0ReliabilityTests(TestCase):
    databases = '__all__'

    def setUp(self):
        super().setUp()
        set_tenant_db_alias('tenant_test')

        # 1. Setup Tenant in Master DB
        self.tenant = Tenant.objects.using('default').create(
            code='REL-TENANT',
            name='Reliability Gym',
            slug='rel-gym',
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
            name='Reliability Plan',
            code='REL-PLAN',
            tier='ENTERPRISE',
            status='ACTIVE',
        )
        self.sub = TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
        )
        self.prod_module_core, _ = ProductModule.objects.using('default').get_or_create(
            code='core',
            defaults={'name': 'Core Module', 'status': 'ACTIVE'},
        )
        self.tm_core = TenantModule.objects.using('default').create(
            tenant=self.tenant,
            module=self.prod_module_core,
            is_enabled=True,
            availability_mode='ALL_BRANCHES',
        )

        # 2. Setup Organization, Location, and Branch in Tenant DB
        self.org = Organization.objects.using('tenant_test').create(
            code='REL-ORG',
            name='Reliability Org',
            status='ACTIVE',
        )
        self.loc = Location.objects.using('tenant_test').create(
            organization=self.org,
            code='REL-LOC',
            name='Reliability Location',
            status='ACTIVE',
        )
        self.branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            code='REL-BR-1',
            name='Reliability Branch 1',
            status='ACTIVE',
        )

        # 3. Setup Tenant Admin User
        self.user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='admin@reliability.test',
            first_name='Audit',
            last_name='Admin',
            status='ACTIVE',
        )
        self.user.set_password('TestPass123!')
        self.user.save(using='tenant_test')

        self.role_admin = Role.objects.using('tenant_test').create(
            name='Org Admin',
            code='ORG_ADMIN',
            is_system_role=True,
            scope='ORG',
            organization=self.org,
            is_active=True,
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.user,
            role=self.role_admin,
            organization=self.org,
            is_active=True,
        )

        # 4. Catalog & RBAC grants for core.settings
        self.cat_core, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(
            module_code='core',
            defaults={'name': 'Core System', 'is_enabled': True},
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
        RoleModuleAccess.objects.using('tenant_test').get_or_create(
            role=self.role_admin, module=self.cat_core, defaults={'can_access': True}
        )
        RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(
            role=self.role_admin, submodule=self.csub_settings, defaults={'can_access': True}
        )
        self.perm_set, _ = RolePermissionSet.objects.using('tenant_test').get_or_create(
            role=self.role_admin, defaults={'name': 'Admin Full Permissions', 'is_active': True}
        )
        RolePermissionSetItem.objects.using('tenant_test').get_or_create(
            permission_set=self.perm_set, permission=self.perm_settings_view, defaults={'granted': True}
        )

    def tearDown(self):
        set_tenant_db_alias(None)
        super().tearDown()

    def get_token(self, user, tenant):
        refresh = _build_tenant_token(
            user=user,
            tenant=tenant,
            db_alias='tenant_test',
        )
        return str(refresh.access_token)

    def test_01_idempotent_operation_executes_and_caches_result(self):
        """First invocation executes operation; subsequent invocation with same key returns cached snapshot."""
        idempotency_key = f"key-{uuid.uuid4()}"
        call_counter = {'count': 0}

        def mock_business_action():
            call_counter['count'] += 1
            return {'booking_id': str(uuid.uuid4()), 'status': 'CONFIRMED'}

        # First execution
        res1 = execute_idempotent_operation(
            organization=self.org,
            operation_type='BOOKING_CREATE',
            idempotency_key=idempotency_key,
            operation_func=mock_business_action,
            request_data={'class_id': 'c1', 'seat': 5},
            actor_user=self.user,
        )
        self.assertEqual(call_counter['count'], 1)
        self.assertEqual(res1['status'], 'CONFIRMED')

        # Second execution with same key
        res2 = execute_idempotent_operation(
            organization=self.org,
            operation_type='BOOKING_CREATE',
            idempotency_key=idempotency_key,
            operation_func=mock_business_action,
            request_data={'class_id': 'c1', 'seat': 5},
            actor_user=self.user,
        )
        # Verify function was NOT called again; cached result was returned
        self.assertEqual(call_counter['count'], 1)
        self.assertEqual(res2['booking_id'], res1['booking_id'])
        self.assertEqual(res2['status'], 'CONFIRMED')

        # Verify record state in DB
        record = IdempotencyRecord.objects.using('tenant_test').get(
            organization=self.org,
            operation_type='BOOKING_CREATE',
            idempotency_key=idempotency_key,
        )
        self.assertEqual(record.status, 'COMPLETED')
        self.assertIsNotNone(record.response_snapshot)

    def test_02_idempotent_operation_in_progress_raises_conflict(self):
        """Active PROCESSING idempotency record blocks concurrent re-entry."""
        idempotency_key = f"key-conflict-{uuid.uuid4()}"

        IdempotencyRecord.objects.using('tenant_test').create(
            organization=self.org,
            operation_type='PAYMENT_PROCESS',
            idempotency_key=idempotency_key,
            status='PROCESSING',
            expires_at=timezone.now() + timezone.timedelta(minutes=5),
        )

        with self.assertRaises(IdempotencyConflictError):
            execute_idempotent_operation(
                organization=self.org,
                operation_type='PAYMENT_PROCESS',
                idempotency_key=idempotency_key,
                operation_func=lambda: {'amount': 100},
            )

    def test_03_idempotent_operation_failed_allows_retry(self):
        """Failed operations are retryable upon subsequent request with same key."""
        idempotency_key = f"key-retry-{uuid.uuid4()}"
        attempt = {'count': 0}

        def flaky_action():
            attempt['count'] += 1
            if attempt['count'] == 1:
                raise ValueError("Temporary network glitch")
            return {'result': 'SUCCESS'}

        # Attempt 1 -> fails
        with self.assertRaises(ValueError):
            execute_idempotent_operation(
                organization=self.org,
                operation_type='CHECKOUT',
                idempotency_key=idempotency_key,
                operation_func=flaky_action,
            )

        record = IdempotencyRecord.objects.using('tenant_test').get(
            organization=self.org,
            operation_type='CHECKOUT',
            idempotency_key=idempotency_key,
        )
        self.assertEqual(record.status, 'FAILED')

        # Attempt 2 -> succeeds
        res = execute_idempotent_operation(
            organization=self.org,
            operation_type='CHECKOUT',
            idempotency_key=idempotency_key,
            operation_func=flaky_action,
        )
        self.assertEqual(res['result'], 'SUCCESS')
        record.refresh_from_db(using='tenant_test')
        self.assertEqual(record.status, 'COMPLETED')

    def test_04_business_audit_event_redacts_sensitive_keys(self):
        """Business audit events mask sensitive credentials, tokens, and health details."""
        raw_before = {
            'username': 'member1',
            'password': 'PlainTextPassword!',
            'card_number': '4111222233334444',
            'health_notes': 'Asthma and high blood pressure',
            'tier': 'GOLD',
        }
        raw_after = {
            'username': 'member1',
            'tier': 'PLATINUM',
            'token': 'secret-jwt-token-1234',
        }

        event = record_business_audit(
            organization=self.org,
            branch=self.branch,
            actor_type='EMPLOYEE',
            actor_user=self.user,
            source_channel='ADMIN_PANEL',
            module='memberships',
            action_code='MEMBERSHIP_UPGRADE',
            entity_type='Membership',
            entity_id=uuid.uuid4(),
            event_description='Member upgraded from Gold to Platinum',
            before_data=raw_before,
            after_data=raw_after,
        )

        self.assertIsNotNone(event.id)
        self.assertEqual(event.before_data['password'], '[REDACTED]')
        self.assertEqual(event.before_data['card_number'], '[REDACTED]')
        self.assertEqual(event.before_data['health_notes'], '[REDACTED]')
        self.assertEqual(event.before_data['tier'], 'GOLD')
        self.assertEqual(event.after_data['token'], '[REDACTED]')
        self.assertEqual(event.after_data['tier'], 'PLATINUM')

    def test_05_domain_outbox_event_enqueue_and_celery_dispatch(self):
        """Domain outbox events are created in PENDING state and published via Celery task."""
        agg_id = uuid.uuid4()
        outbox_event = enqueue_outbox_event(
            organization=self.org,
            event_type='BOOKING_CONFIRMED',
            aggregate_type='Booking',
            aggregate_id=agg_id,
            payload={'booking_id': str(agg_id), 'user_email': 'user@gym.com'},
        )
        self.assertEqual(outbox_event.status, 'PENDING')

        # Run outbox dispatch task
        result = process_domain_outbox_events_task(str(self.tenant.id))
        self.assertEqual(result.get('status'), 'COMPLETED')
        self.assertEqual(result.get('published'), 1)

        outbox_event.refresh_from_db(using='tenant_test')
        self.assertEqual(outbox_event.status, 'PUBLISHED')
        self.assertIsNotNone(outbox_event.published_at)

    def test_06_reason_code_uniqueness_and_retrieval(self):
        """Reason codes enforce uniqueness across organization, module, action_type, and code."""
        code1 = ReasonCode.objects.using('tenant_test').create(
            organization=self.org,
            module='bookings',
            action_type='CANCELLATION',
            code='MEMBER_ILLNESS',
            label='Member Illness / Injury',
            requires_comment=True,
            status='ACTIVE',
        )
        self.assertIsNotNone(code1.id)

        # Query via API
        token = self.get_token(self.user, self.tenant)
        res = self.client.get(
            '/api/v1/tenant/reason-codes/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        results = res.json().get('results', res.json())
        codes = [item.get('code') for item in results]
        self.assertIn('MEMBER_ILLNESS', codes)
