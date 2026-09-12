"""
Sprint 14 Test Suite — User Lifecycle & Tenant DataSource Implementation

Authoritative Test Coverage:
A. Existing users remain valid after migration (is_login_allowed=True, other fields NULL).
B. Existing ACTIVE user: is_login_allowed TRUE -> login succeeds.
C. ACTIVE user: is_login_allowed FALSE -> login denied (HTTP 403) and token rejected.
D. INVITED -> login denied (HTTP 403).
E. INACTIVE -> login denied (HTTP 403).
F. SUSPENDED -> login denied (HTTP 403).
G. BLOCKED -> login denied (HTTP 403).
H. DEACTIVATED -> login denied (HTTP 403).
I. SUSPENDED with suspended_until in the past: STILL denied (no automatic reactivation).
J. Explicit SUSPENDED -> ACTIVE: requires core.users.edit, invokes existing ACTIVE_USERS quota check.
K. deactivated_by FK: points to users.id, ON DELETE SET NULL, NOT DEFERRABLE.
L. Lifecycle mutation creates the correct immutable audit event.
M. Unauthorized user without core.users.edit cannot change lifecycle state.
N. Existing db_host / db_port remain the sole datasource authority (database_host/port properties work).
O. No database_host/database_port physical columns are created in the database.
"""

import uuid
from datetime import timedelta
from django.test import TestCase
from django.utils import timezone
from django.db import connection, connections
from rest_framework.test import APIClient
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken

from apps.master.models_tenant import Tenant
from apps.master.models_saas import (
    SaasPlan, TenantSubscription, SaasPlanResourceLimit, ResourceMetric,
    ProductModule, ProductSubmodule, TenantModule
)
from apps.master.models_infra import TenantDataSource
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_rbac import (
    Organization, Branch, Location, Role, RoleAssignment,
    ModuleCatalog, SubmoduleCatalog, Permission,
    RoleModuleAccess, RoleSubmoduleAccess, RolePermissionSet, RolePermissionSetItem
)
from apps.tenant_core.models_privacy import TenantAuditEvent
from config.routers import set_tenant_db_alias
from config.tenant_middleware import _register_tenant_connection


class Sprint14UserLifecycleTestCase(TestCase):
    databases = '__all__'

    def setUp(self):
        # 1. Tenant routing and connection setup
        set_tenant_db_alias('tenant_test')
        _register_tenant_connection('tenant_test', 'test_fitness_tenant')
        self.client = APIClient()

        # 2. Master Tenant & Subscription
        self.tenant = Tenant.objects.using('default').create(
            name='Sprint14 Fitness Corp',
            slug='sprint14-fitness',
            code='S14-FIT-001',
            status='ACTIVE',
        )

        self.plan = SaasPlan.objects.using('default').create(
            name='Growth Tier',
            code='PLAN-S14-GROWTH',
            tier='growth',
            is_active=True,
        )

        self.subscription = TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
        )

        self.data_source = TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            db_name='test',
            db_host='localhost',
            db_port=5432,
            status='ACTIVE',
        )

        # Active users metric and limit
        self.metric_active_users, _ = ResourceMetric.objects.using('default').get_or_create(
            code='ACTIVE_USERS',
            defaults={'name': 'Active Staff Users', 'unit': 'users', 'aggregation_method': 'MAX'}
        )
        self.plan_limit, _ = SaasPlanResourceLimit.objects.using('default').get_or_create(
            plan=self.plan,
            metric=self.metric_active_users,
            defaults={'limit_value': 10}
        )

        # 3. Master Modules
        self.mod_core, _ = ProductModule.objects.using('default').get_or_create(
            code='core',
            defaults={'name': 'Core Operations', 'is_core': True, 'is_active': True}
        )
        self.sub_users, _ = ProductSubmodule.objects.using('default').get_or_create(
            module=self.mod_core,
            code='users',
            defaults={'name': 'Users Management', 'is_active': True}
        )

        TenantModule.objects.using('default').get_or_create(
            tenant=self.tenant,
            module=self.mod_core,
            defaults={'availability_mode': 'ALL_BRANCHES', 'is_enabled': True}
        )

        # 4. Tenant Organization & Hierarchy
        self.org = Organization.objects.using('tenant_test').create(
            name='S14 Org Hub',
            code='S14-ORG',
            status='ACTIVE',
        )
        self.loc = Location.objects.using('tenant_test').create(
            organization=self.org,
            name='S14 Main Location',
            code='S14-LOC-01',
            status='ACTIVE',
        )
        self.branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            name='S14 Alpha Branch',
            code='S14-BR-01',
            status='ACTIVE',
        )

        # 5. Local Module & Permission Catalogs
        self.local_mod, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(
            module_code='core',
            defaults={'name': 'Core Operations', 'is_enabled': True}
        )
        self.local_sub, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=self.local_mod,
            submodule_code='users',
            defaults={'name': 'Users Management', 'is_enabled': True}
        )

        self.perm_view, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.local_mod,
            submodule=self.local_sub,
            permission_code='core.users.view',
            defaults={'action': 'view', 'label': 'View Users'}
        )
        self.perm_edit, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.local_mod,
            submodule=self.local_sub,
            permission_code='core.users.edit',
            defaults={'action': 'edit', 'label': 'Edit Users'}
        )

        # 6. Roles: Admin Role (with edit) & Viewer Role (only view)
        self.role_admin = Role.objects.using('tenant_test').create(
            organization=self.org,
            name='User Manager Admin',
            code='USER_MGR_ADMIN',
            scope='ORG',
            is_system=False,
            is_active=True,
        )
        self.pset_admin = RolePermissionSet.objects.using('tenant_test').create(
            role=self.role_admin,
            scope_type='ORG',
            is_override=False,
            status='ACTIVE',
        )
        RoleModuleAccess.objects.using('tenant_test').create(role=self.role_admin, module=self.local_mod, can_access=True)
        RoleSubmoduleAccess.objects.using('tenant_test').create(role=self.role_admin, submodule=self.local_sub, can_access=True)
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=self.pset_admin, permission=self.perm_view, granted=True)
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=self.pset_admin, permission=self.perm_edit, granted=True)

        self.role_viewer = Role.objects.using('tenant_test').create(
            organization=self.org,
            name='User Viewer',
            code='USER_VIEWER',
            scope='ORG',
            is_system=False,
            is_active=True,
        )
        self.pset_viewer = RolePermissionSet.objects.using('tenant_test').create(
            role=self.role_viewer,
            scope_type='ORG',
            is_override=False,
            status='ACTIVE',
        )
        RoleModuleAccess.objects.using('tenant_test').create(role=self.role_viewer, module=self.local_mod, can_access=True)
        RoleSubmoduleAccess.objects.using('tenant_test').create(role=self.role_viewer, submodule=self.local_sub, can_access=True)
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=self.pset_viewer, permission=self.perm_view, granted=True)

        # 7. Users
        self.admin_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='admin.s14@testfitness.com',
            first_name='Admin',
            last_name='S14',
            status='ACTIVE',
            is_login_allowed=True,
            home_branch=self.branch,
        )
        self.admin_user.set_password('AdminPass123!')
        self.admin_user.save()
        RoleAssignment.objects.using('tenant_test').create(
            user=self.admin_user,
            role=self.role_admin,
            branch=self.branch,
            is_active=True,
        )

        self.viewer_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='viewer.s14@testfitness.com',
            first_name='Viewer',
            last_name='S14',
            status='ACTIVE',
            is_login_allowed=True,
            home_branch=self.branch,
        )
        self.viewer_user.set_password('ViewerPass123!')
        self.viewer_user.save()
        RoleAssignment.objects.using('tenant_test').create(
            user=self.viewer_user,
            role=self.role_viewer,
            branch=self.branch,
            is_active=True,
        )

        # Target test user for lifecycle state changes
        self.target_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='target.user@testfitness.com',
            first_name='Target',
            last_name='User',
            status='ACTIVE',
            is_login_allowed=True,
            home_branch=self.branch,
        )
        self.target_user.set_password('TargetPass123!')
        self.target_user.save()

    def tearDown(self):
        set_tenant_db_alias(None)

    def _get_token(self, user):
        refresh = RefreshToken()
        refresh['sub'] = str(user.id)
        refresh['user_type'] = 'tenant'
        refresh['roles'] = [ra.role.code for ra in user.role_assignments.filter(is_active=True)]
        refresh['tid'] = str(self.tenant.id)
        refresh['tenant_slug'] = self.tenant.slug
        refresh['db_alias'] = 'tenant_test'
        refresh['email'] = user.email
        return str(refresh.access_token)

    # -------------------------------------------------------------------------
    # A. Existing users remain valid after migration
    # -------------------------------------------------------------------------
    def test_a_existing_user_default_values(self):
        """Existing users have is_login_allowed=True and null deactivation metadata."""
        user = TenantUser.objects.using('tenant_test').get(id=self.target_user.id)
        self.assertTrue(user.is_login_allowed)
        self.assertIsNone(user.deactivated_at)
        self.assertIsNone(user.deactivated_by)
        self.assertIsNone(user.deactivation_reason)
        self.assertIsNone(user.suspended_until)
        self.assertTrue(user.is_accessible)

    # -------------------------------------------------------------------------
    # B. Existing ACTIVE user: is_login_allowed TRUE -> login succeeds
    # -------------------------------------------------------------------------
    def test_b_active_user_login_succeeds(self):
        """Active user with is_login_allowed=True succeeds in both Universal and Tenant login."""
        # Universal Login
        res_univ = self.client.post('/api/v1/auth/login/', {
            'email': self.target_user.email,
            'password': 'TargetPass123!',
            'tenant_slug': self.tenant.slug,
        })
        self.assertEqual(res_univ.status_code, status.HTTP_200_OK)
        self.assertIn('access', res_univ.data)

        # Tenant Explicit Login
        res_tenant = self.client.post('/api/v1/auth/tenant/login/', {
            'email': self.target_user.email,
            'password': 'TargetPass123!',
            'tenant_slug': self.tenant.slug,
        })
        self.assertEqual(res_tenant.status_code, status.HTTP_200_OK)
        self.assertIn('access', res_tenant.data)

    # -------------------------------------------------------------------------
    # C. ACTIVE user: is_login_allowed FALSE -> login denied & token rejected
    # -------------------------------------------------------------------------
    def test_c_active_user_is_login_allowed_false_denied(self):
        """Active user with is_login_allowed=False is denied at login and existing token fails."""
        # Get token while allowed
        token = self._get_token(self.target_user)

        # Disable login
        self.target_user.is_login_allowed = False
        self.target_user.save()
        self.assertFalse(self.target_user.is_accessible)

        # Login attempts fail with 403
        res_univ = self.client.post('/api/v1/auth/login/', {
            'email': self.target_user.email,
            'password': 'TargetPass123!',
            'tenant_slug': self.tenant.slug,
        })
        self.assertEqual(res_univ.status_code, status.HTTP_403_FORBIDDEN)

        res_tenant = self.client.post('/api/v1/auth/tenant/login/', {
            'email': self.target_user.email,
            'password': 'TargetPass123!',
            'tenant_slug': self.tenant.slug,
        })
        self.assertEqual(res_tenant.status_code, status.HTTP_403_FORBIDDEN)

        # Existing token fails live DB check
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        res_me = self.client.get('/api/v1/auth/me/')
        self.assertEqual(res_me.status_code, status.HTTP_401_UNAUTHORIZED)

    # -------------------------------------------------------------------------
    # D - H. Lifecycle Status Login Denials (INVITED, INACTIVE, SUSPENDED, BLOCKED, DEACTIVATED)
    # -------------------------------------------------------------------------
    def _assert_login_denied_for_status(self, user_status):
        self.target_user.status = user_status
        self.target_user.is_login_allowed = True
        self.target_user.save()

        # Universal login
        res_univ = self.client.post('/api/v1/auth/login/', {
            'email': self.target_user.email,
            'password': 'TargetPass123!',
            'tenant_slug': self.tenant.slug,
        })
        self.assertEqual(res_univ.status_code, status.HTTP_403_FORBIDDEN)

        # Tenant login
        res_tenant = self.client.post('/api/v1/auth/tenant/login/', {
            'email': self.target_user.email,
            'password': 'TargetPass123!',
            'tenant_slug': self.tenant.slug,
        })
        self.assertEqual(res_tenant.status_code, status.HTTP_403_FORBIDDEN)

    def test_d_invited_user_denied(self):
        self._assert_login_denied_for_status('INVITED')

    def test_e_inactive_user_denied(self):
        self._assert_login_denied_for_status('INACTIVE')

    def test_f_suspended_user_denied(self):
        self._assert_login_denied_for_status('SUSPENDED')

    def test_g_blocked_user_denied(self):
        self._assert_login_denied_for_status('BLOCKED')

    def test_h_deactivated_user_denied(self):
        self._assert_login_denied_for_status('DEACTIVATED')

    # -------------------------------------------------------------------------
    # I. SUSPENDED with suspended_until in past: STILL denied (no automatic reactivation)
    # -------------------------------------------------------------------------
    def test_i_suspended_in_past_still_denied(self):
        """Expired suspended_until does NOT automatically allow login."""
        self.target_user.status = 'SUSPENDED'
        self.target_user.is_login_allowed = True
        self.target_user.suspended_until = timezone.now() - timedelta(days=2)
        self.target_user.save()

        res = self.client.post('/api/v1/auth/login/', {
            'email': self.target_user.email,
            'password': 'TargetPass123!',
            'tenant_slug': self.tenant.slug,
        })
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        # Verify status remains SUSPENDED
        refreshed = TenantUser.objects.using('tenant_test').get(id=self.target_user.id)
        self.assertEqual(refreshed.status, 'SUSPENDED')

    # -------------------------------------------------------------------------
    # J. Explicit SUSPENDED -> ACTIVE: requires core.users.edit & enforces quota
    # -------------------------------------------------------------------------
    def test_j_explicit_suspended_to_active_enforces_quota(self):
        """Admin with core.users.edit transitions user to ACTIVE; quota is enforced."""
        self.target_user.status = 'SUSPENDED'
        self.target_user.suspended_until = timezone.now() + timedelta(days=5)
        self.target_user.save()

        admin_token = self._get_token(self.admin_user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {admin_token}')

        # 1. Normal reactivation within quota succeeds
        res = self.client.patch(
            f'/api/v1/tenant/users/{self.target_user.id}/',
            {'status': 'ACTIVE'},
            format='json'
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        user_after = TenantUser.objects.using('tenant_test').get(id=self.target_user.id)
        self.assertEqual(user_after.status, 'ACTIVE')
        self.assertIsNone(user_after.suspended_until)

        # 2. When quota is exceeded, reactivation is blocked
        # Set limit to 2 (admin_user + viewer_user already take 2)
        self.plan_limit.limit_value = 2
        self.plan_limit.save(using='default')

        user_after.status = 'SUSPENDED'
        user_after.save(using='tenant_test')

        res_blocked = self.client.patch(
            f'/api/v1/tenant/users/{self.target_user.id}/',
            {'status': 'ACTIVE'},
            format='json'
        )
        self.assertEqual(res_blocked.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('quota exceeded', str(res_blocked.data).lower())

    # -------------------------------------------------------------------------
    # K. deactivated_by FK points to users.id, ON DELETE SET NULL, NOT DEFERRABLE
    # -------------------------------------------------------------------------
    def test_k_deactivated_by_fk_contract(self):
        """Verify deactivated_by references users(id) with ON DELETE SET NULL."""
        # Deactivate target user by admin_user
        self.target_user.status = 'DEACTIVATED'
        self.target_user.deactivated_at = timezone.now()
        self.target_user.deactivated_by = self.admin_user
        self.target_user.deactivation_reason = 'Terminated contract'
        self.target_user.save(using='tenant_test')

        refreshed = TenantUser.objects.using('tenant_test').get(id=self.target_user.id)
        self.assertEqual(refreshed.deactivated_by_id, self.admin_user.id)

        # Create temporary user and delete it to verify ON DELETE SET NULL
        temp_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='temp.actor@testfitness.com',
            first_name='Temp',
            last_name='Actor',
            status='ACTIVE',
            home_branch=self.branch,
        )
        self.target_user.deactivated_by = temp_user
        self.target_user.save(using='tenant_test')

        # Delete temp_user
        temp_user.delete(using='tenant_test')
        self.target_user.refresh_from_db()
        self.assertIsNone(self.target_user.deactivated_by)

    # -------------------------------------------------------------------------
    # L. Lifecycle mutation creates the correct immutable audit event
    # -------------------------------------------------------------------------
    def test_l_lifecycle_mutation_creates_audit_event(self):
        """Transitioning to DEACTIVATED produces an immutable audit event."""
        admin_token = self._get_token(self.admin_user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {admin_token}')

        before_count = TenantAuditEvent.objects.using('tenant_test').filter(
            resource_type='TenantUser',
            resource_id=str(self.target_user.id)
        ).count()

        res = self.client.patch(
            f'/api/v1/tenant/users/{self.target_user.id}/',
            {
                'status': 'DEACTIVATED',
                'deactivation_reason': 'Voluntary resignation',
            },
            format='json'
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        after_count = TenantAuditEvent.objects.using('tenant_test').filter(
            resource_type='TenantUser',
            resource_id=str(self.target_user.id)
        ).count()
        self.assertGreater(after_count, before_count)

        latest_event = TenantAuditEvent.objects.using('tenant_test').filter(
            resource_type='TenantUser',
            resource_id=str(self.target_user.id)
        ).order_by('-created_at').first()

        self.assertEqual(latest_event.action, 'UPDATE')
        self.assertEqual(str(latest_event.actor_id), str(self.admin_user.id))

    # -------------------------------------------------------------------------
    # M. Unauthorized user cannot change lifecycle state
    # -------------------------------------------------------------------------
    def test_m_unauthorized_user_denied_lifecycle_transition(self):
        """User with only core.users.view (no edit) receives HTTP 403."""
        viewer_token = self._get_token(self.viewer_user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {viewer_token}')

        res = self.client.patch(
            f'/api/v1/tenant/users/{self.target_user.id}/',
            {'status': 'SUSPENDED'},
            format='json'
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    # -------------------------------------------------------------------------
    # N. Existing db_host / db_port remain the sole datasource authority
    # -------------------------------------------------------------------------
    def test_n_datasource_property_aliases(self):
        """database_host and database_port expose db_host and db_port as read-only properties."""
        ds = TenantDataSource.objects.using('default').get(id=self.data_source.id)
        self.assertEqual(ds.database_host, 'localhost')
        self.assertEqual(ds.database_port, 5432)
        self.assertEqual(ds.database_host, ds.db_host)
        self.assertEqual(ds.database_port, ds.db_port)

    # -------------------------------------------------------------------------
    # O. No database_host / database_port physical columns are created
    # -------------------------------------------------------------------------
    def test_o_no_duplicate_datasource_physical_columns(self):
        """Verify master database tenant_data_sources table has NO database_host/port columns."""
        with connections['default'].cursor() as cursor:
            cursor.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'tenant_data_sources'
                  AND column_name IN ('database_host', 'database_port');
            """)
            duplicate_cols = cursor.fetchall()
            self.assertEqual(len(duplicate_cols), 0, f"Duplicate physical columns found: {duplicate_cols}")
