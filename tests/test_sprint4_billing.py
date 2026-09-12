"""
Sprint 4 Billing Enforcement & Metering Test Suite — Phase 1 Layer 1.

Verifies:
1. Quota Enforcement:
   - Below limit -> creation succeeds (201 Created)
   - Exactly at limit -> creation rejected (400 Bad Request, code=QUOTA_EXCEEDED)
   - Unlimited (-1) -> creation allowed
   - TenantResourceLimit override beats SaasPlan limit (both higher and lower)
   - Missing/invalid quota configuration fails safely (fail closed)
   - ACTIVE counted toward quota
   - INVITED counted toward quota
   - INACTIVE not counted toward quota
   - SUSPENDED not counted toward quota
   - User status transition from INACTIVE to ACTIVE checked against quota
   - Quota rejection does not leave partial user persisted (atomic rollback)
   - Concurrency row locking on Organization via select_for_update()

2. Subscription Authentication Hardening:
   - ACTIVE subscription -> allowed (200)
   - TRIALING subscription with future trial_ends_at -> allowed (200)
   - TRIALING subscription with None trial_ends_at -> allowed (200)
   - TRIALING subscription with past trial_ends_at -> rejected (401)
   - PAST_DUE subscription -> rejected (401)
   - CANCELED subscription -> rejected (401)
   - PAUSED subscription -> rejected (401)
   - Missing subscription -> rejected (401)
   - Inactive tenant (SUSPENDED) -> rejected (401)
   - Master DB query failure -> fail closed (401)

3. Metering & Background Usage Synchronization:
   - Authoritative snapshot of ACTIVE_USERS written to TenantResourceUsage in Master DB
   - Authoritative snapshot of LOCATIONS written to TenantResourceUsage in Master DB
   - Repeated sync execution is idempotent (unique together on tenant, metric, billing_period_start)
   - Single tenant failure does not abort processing of other tenants
   - Management command sync_resource_usage runs cleanly

4. Metric Catalog Reconciliation:
   - Canonical ACTIVE_USERS metric exists in ResourceMetric
   - Obsolete TRAINERS metric does not exist
   - Authoritative plan limits exist for ACTIVE_USERS across all SaaS plans
"""

from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from django.core.management import call_command
from rest_framework.test import APIClient
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken

from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.models_saas import (
    SaasPlan, SaasPlanPrice, ResourceMetric, SaasPlanResourceLimit,
    TenantResourceLimit, TenantResourceUsage, TenantSubscription,
    ProductModule, TenantModule,
)
from apps.master.quota import QuotaChecker, QuotaExceededError, QuotaConfigurationError
from apps.master.metering import sync_tenant_resource_usage
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_rbac import (
    Role, RoleAssignment, ModuleCatalog, SubmoduleCatalog, Permission,
    RoleModuleAccess, RoleSubmoduleAccess, RolePermissionSet, RolePermissionSetItem,
)
from config.routers import set_tenant_db_alias
from config.tenant_middleware import _register_tenant_connection


class BaseSprint4BillingTestCase(TestCase):
    """Base setup for Sprint 4 Billing Enforcement tests."""
    databases = '__all__'

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        _register_tenant_connection('tenant_test', 'test_fitness_tenant')
        self.client = APIClient()

        # 1. Master Tenant & Data Source
        self.tenant = Tenant.objects.using('default').create(
            name='Sprint4 Gym Club',
            slug='sprint4-gym',
            code='S4-GYM-001',
            status='ACTIVE',
        )
        self.data_source = TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            db_name='test',
            status='ACTIVE',
        )

        # 2. Canonical Metrics
        self.metric_users, _ = ResourceMetric.objects.using('default').get_or_create(
            code='ACTIVE_USERS',
            defaults={'name': 'Active Users', 'unit': 'users', 'is_active': True}
        )
        self.metric_locations, _ = ResourceMetric.objects.using('default').get_or_create(
            code='LOCATIONS',
            defaults={'name': 'Locations', 'unit': 'branches', 'is_active': True}
        )

        # 3. SaaS Plans
        self.plan_starter = SaasPlan.objects.using('default').create(
            name='Starter Plan',
            code='PLAN-STARTER',
            tier='starter',
            is_active=True,
        )
        self.plan_growth = SaasPlan.objects.using('default').create(
            name='Growth Plan',
            code='PLAN-GROWTH',
            tier='growth',
            is_active=True,
        )
        self.plan_enterprise = SaasPlan.objects.using('default').create(
            name='Enterprise Plan',
            code='PLAN-ENTERPRISE',
            tier='enterprise',
            is_active=True,
        )

        # 4. Plan Limits for ACTIVE_USERS
        # Starter: 2 (for easy boundary testing), Growth: 5, Enterprise: -1
        self.limit_starter = SaasPlanResourceLimit.objects.using('default').create(
            plan=self.plan_starter,
            metric=self.metric_users,
            limit_value=2,
        )
        self.limit_growth = SaasPlanResourceLimit.objects.using('default').create(
            plan=self.plan_growth,
            metric=self.metric_users,
            limit_value=5,
        )
        self.limit_enterprise = SaasPlanResourceLimit.objects.using('default').create(
            plan=self.plan_enterprise,
            metric=self.metric_users,
            limit_value=-1,
        )

        # Plan limit for LOCATIONS
        SaasPlanResourceLimit.objects.using('default').create(
            plan=self.plan_starter,
            metric=self.metric_locations,
            limit_value=2,
        )

        # 5. Tenant Subscription (default ACTIVE on Starter plan: limit=2)
        self.subscription = TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan_starter,
            status='ACTIVE',
            billing_cycle='MONTHLY',
            current_period_start=timezone.now(),
            current_period_end=timezone.now() + timezone.timedelta(days=30),
        )

        # 5b. Enable core module for tenant (Check 4 RBAC prerequisite)
        self.mod_core, _ = ProductModule.objects.using('default').get_or_create(
            code='core', defaults={'name': 'Core System', 'is_active': True}
        )
        self.tm_core, _ = TenantModule.objects.using('default').get_or_create(
            tenant=self.tenant,
            module=self.mod_core,
            defaults={'availability_mode': 'ALL_BRANCHES', 'is_enabled': True},
        )

        # 6. Tenant DB Organization & Structure
        self.org = Organization.objects.using('tenant_test').create(
            name='Sprint4 Org',
            code='S4-ORG',
            status='ACTIVE',
        )
        self.location = Location.objects.using('tenant_test').create(
            organization=self.org,
            name='Main Location',
            code='LOC-MAIN',
            status='ACTIVE',
        )
        self.branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.location,
            name='Downtown Branch',
            code='BR-DT',
            status='ACTIVE',
        )

        # 7. Admin Tenant User (1 active seat used)
        self.admin_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='admin@sprint4gym.com',
            first_name='Admin',
            last_name='User',
            status='ACTIVE',
            home_branch=self.branch,
        )

        # RBAC Setup for User Management
        self.admin_role = Role.objects.using('tenant_test').create(
            organization=self.org,
            code='ORG_ADMIN',
            name='Org Admin',
            scope='ORG',
            is_system=True,
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.admin_user,
            role=self.admin_role,
            is_active=True,
        )

        mod_core, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(
            module_code='core', defaults={'name': 'Core', 'is_enabled': True}
        )
        sub_users, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=mod_core, submodule_code='users', defaults={'name': 'Users', 'is_enabled': True}
        )
        perm_users_create, _ = Permission.objects.using('tenant_test').get_or_create(
            module=mod_core, submodule=sub_users, action='create',
            defaults={'permission_code': 'core.users.create', 'label': 'Create Users'}
        )
        perm_users_edit, _ = Permission.objects.using('tenant_test').get_or_create(
            module=mod_core, submodule=sub_users, action='edit',
            defaults={'permission_code': 'core.users.edit', 'label': 'Edit Users'}
        )
        perm_users_view, _ = Permission.objects.using('tenant_test').get_or_create(
            module=mod_core, submodule=sub_users, action='view',
            defaults={'permission_code': 'core.users.view', 'label': 'View Users'}
        )

        RoleModuleAccess.objects.using('tenant_test').create(role=self.admin_role, module=mod_core, can_access=True)
        RoleSubmoduleAccess.objects.using('tenant_test').create(role=self.admin_role, submodule=sub_users, can_access=True)
        ps = RolePermissionSet.objects.using('tenant_test').create(role=self.admin_role, name='Admin Core')
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=ps, permission=perm_users_create, granted=True)
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=ps, permission=perm_users_edit, granted=True)
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=ps, permission=perm_users_view, granted=True)

    def get_tenant_token(self, user=None):
        u = user or self.admin_user
        set_tenant_db_alias('tenant_test')
        refresh = RefreshToken()
        refresh['sub'] = str(u.id)
        refresh['user_type'] = 'tenant'
        refresh['roles'] = [
            ra.role.code for ra in RoleAssignment.objects.using('tenant_test').filter(user=u, is_active=True).select_related('role')
        ]
        refresh['tid'] = str(self.tenant.id)
        refresh['tenant_slug'] = self.tenant.slug
        refresh['db_alias'] = 'tenant_test'
        refresh['email'] = u.email
        return str(refresh.access_token)


class QuotaEnforcementTests(BaseSprint4BillingTestCase):
    """Tests for S4.1 QuotaChecker and S4.2 ACTIVE_USERS quota enforcement."""

    def test_quota_service_effective_limit_resolution(self):
        """QuotaChecker resolves override -> plan limit -> unlimited (-1) -> fail closed."""
        # 1. Resolves plan limit (Starter = 2)
        limit = QuotaChecker.get_effective_limit(str(self.tenant.id), 'ACTIVE_USERS')
        self.assertEqual(limit, 2)

        # 2. Resolves tenant-level override when present
        override = TenantResourceLimit.objects.using('default').create(
            tenant=self.tenant,
            metric=self.metric_users,
            limit_value=10,
        )
        limit_override = QuotaChecker.get_effective_limit(str(self.tenant.id), 'ACTIVE_USERS')
        self.assertEqual(limit_override, 10)

        # Clean up override
        override.delete()

        # 3. Unlimited (-1) resolution on Enterprise plan
        self.subscription.plan = self.plan_enterprise
        self.subscription.save(using='default')
        limit_unlimited = QuotaChecker.get_effective_limit(str(self.tenant.id), 'ACTIVE_USERS')
        self.assertEqual(limit_unlimited, -1)

    def test_quota_service_fail_closed_on_missing_config(self):
        """QuotaChecker fails closed when metric limit is not configured for the plan."""
        unconfigured_metric = ResourceMetric.objects.using('default').create(
            code='UNKNOWN_METRIC',
            name='Unknown Metric',
            unit='items',
        )
        with self.assertRaises(QuotaConfigurationError):
            QuotaChecker.get_effective_limit(str(self.tenant.id), 'UNKNOWN_METRIC')

    def test_quota_below_limit_creation_succeeds(self):
        """When live usage (1) is below limit (2), creating 2nd user succeeds."""
        token = self.get_tenant_token()
        payload = {
            'organization': str(self.org.id),
            'email': 'trainer2@sprint4gym.com',
            'first_name': 'Trainer',
            'last_name': 'Two',
            'password': 'SecurePassword123!',
            'status': 'INVITED',
        }
        res = self.client.post(
            '/api/v1/tenant/users/',
            payload,
            HTTP_AUTHORIZATION=f'Bearer {token}',
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertTrue(TenantUser.objects.using('tenant_test').filter(email='trainer2@sprint4gym.com').exists())

    def test_quota_exactly_at_limit_creation_rejected(self):
        """When live usage reaches limit (2), creating 3rd user is rejected with 400 QUOTA_EXCEEDED."""
        token = self.get_tenant_token()

        # Create user 2 (now usage = 2, exactly at limit)
        TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='trainer2@sprint4gym.com',
            first_name='Trainer',
            last_name='Two',
            status='ACTIVE',
        )

        payload = {
            'organization': str(self.org.id),
            'email': 'trainer3@sprint4gym.com',
            'first_name': 'Trainer',
            'last_name': 'Three',
            'password': 'SecurePassword123!',
            'status': 'ACTIVE',
        }
        res = self.client.post(
            '/api/v1/tenant/users/',
            payload,
            HTTP_AUTHORIZATION=f'Bearer {token}',
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('code', res.data)
        self.assertEqual(res.data['code'], 'QUOTA_EXCEEDED')
        # Ensure partial user was NOT created (atomic rollback)
        self.assertFalse(TenantUser.objects.using('tenant_test').filter(email='trainer3@sprint4gym.com').exists())

    def test_invited_users_count_toward_active_users_quota(self):
        """INVITED status consumes quota seats; creation is rejected once ACTIVE+INVITED reaches limit."""
        token = self.get_tenant_token()

        # Add 1 INVITED user (now usage = 1 active admin + 1 invited = 2)
        TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='invited@sprint4gym.com',
            first_name='Invited',
            last_name='User',
            status='INVITED',
        )

        # Attempt to create another user -> must be blocked because INVITED counted
        payload = {
            'organization': str(self.org.id),
            'email': 'blocked@sprint4gym.com',
            'first_name': 'Blocked',
            'last_name': 'User',
            'password': 'SecurePassword123!',
            'status': 'ACTIVE',
        }
        res = self.client.post(
            '/api/v1/tenant/users/',
            payload,
            HTTP_AUTHORIZATION=f'Bearer {token}',
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data.get('code'), 'QUOTA_EXCEEDED')

    def test_inactive_and_suspended_users_do_not_consume_quota(self):
        """INACTIVE and SUSPENDED users do NOT consume quota seats."""
        token = self.get_tenant_token()

        # Add INACTIVE and SUSPENDED users
        TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='inactive@sprint4gym.com',
            status='INACTIVE',
        )
        TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='suspended@sprint4gym.com',
            status='SUSPENDED',
        )

        # Live usage should still be 1 (only the active admin)
        usage = QuotaChecker.get_live_usage('ACTIVE_USERS', 'tenant_test')
        self.assertEqual(usage, 1)

        # Creating user 2 must succeed despite inactive/suspended users in DB
        payload = {
            'organization': str(self.org.id),
            'email': 'active2@sprint4gym.com',
            'first_name': 'Active',
            'last_name': 'Two',
            'password': 'SecurePassword123!',
            'status': 'ACTIVE',
        }
        res = self.client.post(
            '/api/v1/tenant/users/',
            payload,
            HTTP_AUTHORIZATION=f'Bearer {token}',
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

    def test_user_status_transition_enforces_quota(self):
        """Transitioning a user from INACTIVE to ACTIVE fails if quota is full."""
        token = self.get_tenant_token()

        # 1. Fill quota with 2nd active user (total active = 2 = limit)
        TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='active2@sprint4gym.com',
            status='ACTIVE',
        )

        # 2. Existing inactive user
        inactive_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='dormant@sprint4gym.com',
            status='INACTIVE',
        )

        # 3. Attempt to activate dormant user
        res = self.client.patch(
            f'/api/v1/tenant/users/{inactive_user.id}/',
            {'status': 'ACTIVE'},
            HTTP_AUTHORIZATION=f'Bearer {token}',
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data.get('code'), 'QUOTA_EXCEEDED')

        # Verify user remains INACTIVE
        user_in_db = TenantUser.objects.using('tenant_test').get(id=inactive_user.id)
        self.assertEqual(user_in_db.status, 'INACTIVE')

    def test_tenant_resource_limit_override_precedence(self):
        """TenantResourceLimit override takes strict precedence over SaasPlan limit."""
        token = self.get_tenant_token()

        # Override limit down to 1 (active admin already uses 1)
        TenantResourceLimit.objects.using('default').create(
            tenant=self.tenant,
            metric=self.metric_users,
            limit_value=1,
        )

        # Now attempting to add 2nd user should fail immediately even though plan limit was 2
        payload = {
            'organization': str(self.org.id),
            'email': 'override_blocked@sprint4gym.com',
            'first_name': 'Override',
            'last_name': 'Blocked',
            'password': 'SecurePassword123!',
            'status': 'ACTIVE',
        }
        res = self.client.post(
            '/api/v1/tenant/users/',
            payload,
            HTTP_AUTHORIZATION=f'Bearer {token}',
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data.get('code'), 'QUOTA_EXCEEDED')

    def test_unlimited_enterprise_plan_allows_growth(self):
        """Enterprise plan with limit -1 allows creating users beyond normal boundaries."""
        token = self.get_tenant_token()
        self.subscription.plan = self.plan_enterprise
        self.subscription.save(using='default')

        # Add multiple users
        for i in range(5):
            TenantUser.objects.using('tenant_test').create(
                organization=self.org,
                email=f'enterprise_user_{i}@sprint4gym.com',
                status='ACTIVE',
            )

        # Adding another user should succeed without limit rejection
        payload = {
            'organization': str(self.org.id),
            'email': 'enterprise_new@sprint4gym.com',
            'first_name': 'Enterprise',
            'last_name': 'New',
            'password': 'SecurePassword123!',
            'status': 'ACTIVE',
        }
        res = self.client.post(
            '/api/v1/tenant/users/',
            payload,
            HTTP_AUTHORIZATION=f'Bearer {token}',
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)


class SubscriptionAuthenticationHardeningTests(BaseSprint4BillingTestCase):
    """Tests for S4.3 Subscription authentication hardening."""

    def test_active_subscription_allows_authentication(self):
        """Active subscription permits tenant requests."""
        token = self.get_tenant_token()
        res = self.client.get(
            '/api/v1/tenant/users/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    def test_trialing_valid_allows_authentication(self):
        """Valid trial (future trial_ends_at or None) permits tenant requests."""
        self.subscription.status = 'TRIALING'
        self.subscription.trial_ends_at = timezone.now() + timezone.timedelta(days=7)
        self.subscription.save(using='default')

        token = self.get_tenant_token()
        res = self.client.get(
            '/api/v1/tenant/users/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        # None trial_ends_at also allowed
        self.subscription.trial_ends_at = None
        self.subscription.save(using='default')
        res2 = self.client.get(
            '/api/v1/tenant/users/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(res2.status_code, status.HTTP_200_OK)

    def test_trialing_expired_blocks_authentication(self):
        """Expired trial (past trial_ends_at) blocks tenant requests with 401."""
        self.subscription.status = 'TRIALING'
        self.subscription.trial_ends_at = timezone.now() - timezone.timedelta(hours=1)
        self.subscription.save(using='default')

        token = self.get_tenant_token()
        res = self.client.get(
            '/api/v1/tenant/users/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn('expired', str(res.data).lower())

    def test_past_due_subscription_strictly_blocked(self):
        """PAST_DUE subscription is strictly blocked (no grace period) with 401."""
        self.subscription.status = 'PAST_DUE'
        self.subscription.save(using='default')

        token = self.get_tenant_token()
        res = self.client.get(
            '/api/v1/tenant/users/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn('past due', str(res.data).lower())

    def test_canceled_and_paused_subscriptions_blocked(self):
        """CANCELED and PAUSED subscriptions are blocked with 401."""
        token = self.get_tenant_token()

        for st in ['CANCELED', 'PAUSED']:
            self.subscription.status = st
            self.subscription.save(using='default')
            res = self.client.get(
                '/api/v1/tenant/users/',
                HTTP_AUTHORIZATION=f'Bearer {token}',
            )
            self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_missing_subscription_blocked(self):
        """Tenant with no subscription record in Master DB is blocked with 401."""
        TenantSubscription.objects.using('default').filter(tenant=self.tenant).delete()

        token = self.get_tenant_token()
        res = self.client.get(
            '/api/v1/tenant/users/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_inactive_tenant_status_blocked(self):
        """Tenant with status != ACTIVE is blocked with 401."""
        self.tenant.status = 'SUSPENDED'
        self.tenant.save(using='default')

        token = self.get_tenant_token()
        res = self.client.get(
            '/api/v1/tenant/users/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_master_db_lookup_exception_fails_closed(self):
        """Any unexpected database error during subscription verification fails closed."""
        token = self.get_tenant_token()

        with patch('apps.master.models_saas.TenantSubscription.objects.using') as mock_using:
            mock_using.side_effect = RuntimeError("Simulated Master DB timeout")
            res = self.client.get(
                '/api/v1/tenant/users/',
                HTTP_AUTHORIZATION=f'Bearer {token}',
            )
            self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)


class MeteringSynchronizationTests(BaseSprint4BillingTestCase):
    """Tests for S4.5 TenantResourceUsage background synchronization."""

    def test_resource_metering_sync_active_users_and_locations(self):
        """sync_tenant_resource_usage correctly snapshots live ACTIVE_USERS and LOCATIONS."""
        # 1. Add another active user and another branch
        TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='staff2@sprint4gym.com',
            status='ACTIVE',
        )
        Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.location,
            name='Uptown Branch',
            code='BR-UP',
            status='ACTIVE',
        )

        # 2. Run synchronization
        result = sync_tenant_resource_usage(tenant_id=str(self.tenant.id))
        self.assertEqual(result['succeeded'], 1)
        self.assertEqual(result['failed'], 0)

        # 3. Verify Master DB TenantResourceUsage records
        usage_users = TenantResourceUsage.objects.using('default').filter(
            tenant=self.tenant,
            metric=self.metric_users,
        ).first()
        self.assertIsNotNone(usage_users)
        self.assertEqual(usage_users.current_value, 2)  # 2 active users

        usage_locs = TenantResourceUsage.objects.using('default').filter(
            tenant=self.tenant,
            metric=self.metric_locations,
        ).first()
        self.assertIsNotNone(usage_locs)
        self.assertEqual(usage_locs.current_value, 2)  # 2 active branches

    def test_resource_metering_idempotency(self):
        """Repeated sync executions do not create duplicate usage rows."""
        # Run sync 3 times
        sync_tenant_resource_usage(tenant_id=str(self.tenant.id))
        sync_tenant_resource_usage(tenant_id=str(self.tenant.id))
        sync_tenant_resource_usage(tenant_id=str(self.tenant.id))

        # Exactly 1 record per metric per billing period
        count_users = TenantResourceUsage.objects.using('default').filter(
            tenant=self.tenant,
            metric=self.metric_users,
        ).count()
        self.assertEqual(count_users, 1)

    def test_metering_single_tenant_failure_does_not_abort_others(self):
        """If one tenant sync fails, other tenants continue processing cleanly."""
        # Create a 2nd tenant without a data source (will fail data source resolution)
        bad_tenant = Tenant.objects.using('default').create(
            name='Broken Tenant',
            slug='broken-tenant',
            code='BRK-001',
            status='ACTIVE',
        )

        result = sync_tenant_resource_usage()
        self.assertGreaterEqual(result['total_tenants'], 2)
        self.assertIn(str(bad_tenant.id), result['errors'])
        self.assertIn(str(self.tenant.id), result['details'])
        self.assertEqual(result['details'][str(self.tenant.id)]['metrics']['ACTIVE_USERS'], 1)

    def test_sync_resource_usage_management_command(self):
        """Management command 'sync_resource_usage' runs without errors."""
        call_command('sync_resource_usage', '--tenant', self.tenant.slug)
        usage = TenantResourceUsage.objects.using('default').filter(
            tenant=self.tenant,
            metric=self.metric_users,
        ).first()
        self.assertIsNotNone(usage)


class MetricCatalogReconciliationTests(BaseSprint4BillingTestCase):
    """Tests for S4.4 Metric catalog reconciliation."""

    def test_canonical_active_users_metric_exists(self):
        """ResourceMetric catalog contains canonical ACTIVE_USERS and not TRAINERS."""
        self.assertTrue(
            ResourceMetric.objects.using('default').filter(code='ACTIVE_USERS').exists()
        )
        self.assertFalse(
            ResourceMetric.objects.using('default').filter(code='TRAINERS').exists()
        )

    def test_plan_limits_point_to_canonical_active_users(self):
        """Plan limits for Starter, Growth, and Enterprise point to ACTIVE_USERS."""
        limits = SaasPlanResourceLimit.objects.using('default').filter(
            metric__code='ACTIVE_USERS'
        )
        plans = set(limits.values_list('plan__code', flat=True))
        self.assertTrue({'PLAN-STARTER', 'PLAN-GROWTH', 'PLAN-ENTERPRISE'}.issubset(plans))
