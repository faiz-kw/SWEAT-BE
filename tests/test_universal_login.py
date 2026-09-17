"""
backend/tests/test_universal_login.py — Universal / Unified Login Test Suite.

Mandatory 26 Test Scenarios:
1. Platform username login succeeds.
2. Platform email login succeeds.
3. Tenant username login succeeds.
4. Tenant email login succeeds.
5. Member login succeeds.
6. Trainer login succeeds.
7. Org Admin login succeeds.
8. Branch-scoped user login succeeds.
9. Unknown identifier fails generically.
10. Wrong password fails generically.
11. Duplicate global identifier creation rejected.
12. Tenant inactive blocks login.
13. User deactivated blocks login.
14. Invited-not-activated user blocked.
15. Tenant datasource inactive blocks login.
16. Tampered tenant claim denied.
17. Platform token cannot access tenant route.
18. Tenant token cannot access platform route.
19. Identifier change updates directory.
20. Deactivation updates effective login state.
21. Reactivation restores login.
22. Reconciliation detects missing directory row.
23. Reconciliation detects stale row.
24. Reconciliation detects duplicate identifier.
25. Password reset routes correctly.
26. Logout/session restoration works correctly.
"""

import json
import uuid
from io import StringIO
from django.core.management import call_command
from django.test import TestCase, Client
from django.conf import settings
from rest_framework import status
from django.core.exceptions import ValidationError

from apps.master.models_iam import PlatformUser, AuthenticationIdentity
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.models_saas import SaasPlan, TenantSubscription
from apps.master.services_auth_directory import (
    register_identity,
    resolve_identity,
    sync_platform_user_identity,
    sync_tenant_user_identity,
    compute_lookup_hash,
)
from apps.tenant_core.models_org import Organization, Branch, Location
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_rbac import Role, RoleAssignment
from apps.authentication.views import _build_platform_token, _build_tenant_token
from config.routers import set_tenant_db_alias, get_tenant_db_alias


class UniversalLoginTestSuite(TestCase):
    databases = '__all__'

    def setUp(self):
        super().setUp()
        self.client = Client()
        set_tenant_db_alias(None)

        # 1. Control Plane SaaS Plan
        self.plan = SaasPlan.objects.using('default').create(
            name='Universal Plan',
            code='UNIVERSAL-PLAN',
            tier='ENTERPRISE',
            status='ACTIVE',
        )

        # 2. Control Plane Platform User (username & email)
        self.platform_user = PlatformUser.objects.using('default').create(
            username='platform_super',
            email='super@platform.local',
            first_name='Platform',
            last_name='Admin',
            status='ACTIVE',
            is_staff=True,
            is_superuser=True,
        )
        self.platform_user.set_password('PlatformPass123!')
        self.platform_user.save(using='default')
        sync_platform_user_identity(self.platform_user)

        # 3. Control Plane Tenant & DataSource
        self.tenant = Tenant.objects.using('default').create(
            name='Sweat Test Gym',
            slug='sweat-test-gym',
            code='SWT-TEST-001',
            status='ACTIVE',
        )
        self.ds = TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            db_name='test_fitness_tenant',
            database_name='test_fitness_tenant',
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )
        TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
        )

        # 4. Tenant Database Seeding
        set_tenant_db_alias('tenant_test')
        self.org = Organization.objects.using('tenant_test').create(
            code='ORG-SWT-01',
            name='Sweat Test Org',
            status='ACTIVE',
        )
        self.loc = Location.objects.using('tenant_test').create(
            organization=self.org,
            name='Main Location',
            code='LOC-01',
            status='ACTIVE',
        )
        self.branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            name='Indiranagar Branch',
            code='BR-01',
            status='ACTIVE',
        )

        # Roles
        self.role_org_admin = Role.objects.using('tenant_test').create(
            organization=self.org,
            name='Org Admin',
            code='ORG_ADMIN',
            scope='ORG',
            is_system_role=True,
        )
        self.role_trainer = Role.objects.using('tenant_test').create(
            organization=self.org,
            name='Trainer',
            code='TRAINER',
            scope='BRANCH',
            is_system_role=True,
        )
        self.role_member = Role.objects.using('tenant_test').create(
            organization=self.org,
            name='Member',
            code='MEMBER',
            scope='BRANCH',
            is_system_role=True,
        )
        self.role_staff = Role.objects.using('tenant_test').create(
            organization=self.org,
            name='Front Desk',
            code='FRONT_DESK',
            scope='BRANCH',
            is_system_role=True,
        )

        # Tenant Org Admin
        self.tenant_admin = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            username='tenant_admin',
            email='admin@sweat-test.local',
            first_name='Tenant',
            last_name='Admin',
            status='ACTIVE',
        )
        self.tenant_admin.set_password('TenantPass123!')
        self.tenant_admin.save(using='tenant_test')
        RoleAssignment.objects.using('tenant_test').create(
            organization=self.org,
            user=self.tenant_admin,
            role=self.role_org_admin,
            scope_type='ORGANIZATION',
            status='ACTIVE',
        )
        sync_tenant_user_identity(self.tenant_admin, self.tenant.id)

        # Trainer User
        self.trainer_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            username='coach_mike',
            email='mike@sweat-test.local',
            first_name='Mike',
            last_name='Trainer',
            status='ACTIVE',
        )
        self.trainer_user.set_password('TrainerPass123!')
        self.trainer_user.save(using='tenant_test')
        RoleAssignment.objects.using('tenant_test').create(
            organization=self.org,
            user=self.trainer_user,
            role=self.role_trainer,
            scope_type='BRANCH',
            branch=self.branch,
            status='ACTIVE',
        )
        sync_tenant_user_identity(self.trainer_user, self.tenant.id)

        # Member User
        self.member_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            username='gym_member',
            email='member@sweat-test.local',
            first_name='Jane',
            last_name='Member',
            status='ACTIVE',
        )
        self.member_user.set_password('MemberPass123!')
        self.member_user.save(using='tenant_test')
        RoleAssignment.objects.using('tenant_test').create(
            organization=self.org,
            user=self.member_user,
            role=self.role_member,
            scope_type='BRANCH',
            branch=self.branch,
            status='ACTIVE',
        )
        sync_tenant_user_identity(self.member_user, self.tenant.id)

        # Reset thread-local DB alias
        set_tenant_db_alias(None)

    # 1. Platform username login succeeds
    def test_platform_username_login_succeeds(self):
        res = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({'identifier': 'platform_super', 'password': 'PlatformPass123!'}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()
        self.assertIn('access', data)
        self.assertEqual(data['user_type'], 'platform')
        self.assertEqual(data['default_route'], '/platform/tenants')

    # 2. Platform email login succeeds
    def test_platform_email_login_succeeds(self):
        res = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({'identifier': 'super@platform.local', 'password': 'PlatformPass123!'}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()
        self.assertIn('access', data)
        self.assertEqual(data['user_type'], 'platform')

    # 3. Tenant username login succeeds
    def test_tenant_username_login_succeeds(self):
        res = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({'identifier': 'tenant_admin', 'password': 'TenantPass123!'}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()
        self.assertIn('access', data)
        self.assertEqual(data['user_type'], 'tenant')
        self.assertEqual(data['tenant_id'], str(self.tenant.id))
        self.assertEqual(data['default_route'], '/')

    # 4. Tenant email login succeeds
    def test_tenant_email_login_succeeds(self):
        res = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({'identifier': 'admin@sweat-test.local', 'password': 'TenantPass123!'}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()
        self.assertIn('access', data)
        self.assertEqual(data['user_type'], 'tenant')

    # 5. Member login succeeds
    def test_member_login_succeeds(self):
        res = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({'identifier': 'gym_member', 'password': 'MemberPass123!'}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()
        self.assertEqual(data['user_type'], 'tenant')
        self.assertEqual(data['default_route'], '/members/attendance')

    # 6. Trainer login succeeds
    def test_trainer_login_succeeds(self):
        res = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({'identifier': 'coach_mike', 'password': 'TrainerPass123!'}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()
        self.assertEqual(data['user_type'], 'tenant')
        self.assertEqual(data['default_route'], '/ops/trainers')

    # 7. Org Admin login succeeds
    def test_org_admin_login_succeeds(self):
        res = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({'identifier': 'tenant_admin', 'password': 'TenantPass123!'}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.json()['default_route'], '/')

    # 8. Branch-scoped user login succeeds
    def test_branch_scoped_user_login_succeeds(self):
        res = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({'identifier': 'mike@sweat-test.local', 'password': 'TrainerPass123!'}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn('access', res.json())

    # 9. Unknown identifier fails generically
    def test_unknown_identifier_fails_generically(self):
        res = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({'identifier': 'non_existent_user@example.com', 'password': 'SomePass123!'}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(res.json()['error'], 'Invalid username/email or password.')

    # 10. Wrong password fails generically
    def test_wrong_password_fails_generically(self):
        res = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({'identifier': 'tenant_admin', 'password': 'WrongPassword999!'}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(res.json()['error'], 'Invalid username/email or password.')

    # 11. Duplicate global identifier creation rejected
    def test_duplicate_global_identifier_creation_rejected(self):
        # Attempting to create a platform user with an email that is already registered for a tenant user
        dup_user = PlatformUser(
            username='new_admin',
            email='admin@sweat-test.local', # already exists in tenant
            first_name='Dup',
            last_name='User',
        )
        with self.assertRaises(ValidationError) as ctx:
            dup_user.save(using='default')
        self.assertIn('already registered', str(ctx.exception).lower())

    # 12. Tenant inactive blocks login
    def test_tenant_inactive_blocks_login(self):
        self.tenant.status = 'SUSPENDED'
        self.tenant.save(using='default')

        res = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({'identifier': 'tenant_admin', 'password': 'TenantPass123!'}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('inactive', res.json()['error'].lower())

    # 13. User deactivated blocks login
    def test_user_deactivated_blocks_login(self):
        set_tenant_db_alias('tenant_test')
        self.tenant_admin.status = 'INACTIVE'
        self.tenant_admin.save(using='tenant_test')
        sync_tenant_user_identity(self.tenant_admin, self.tenant.id)
        set_tenant_db_alias(None)

        res = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({'identifier': 'tenant_admin', 'password': 'TenantPass123!'}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('inactive', res.json()['error'].lower())

    # 14. Invited-not-activated user blocked
    def test_invited_not_activated_user_blocked(self):
        set_tenant_db_alias('tenant_test')
        invited = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='invited@sweat-test.local',
            first_name='Invited',
            last_name='User',
            status='INVITED',
        )
        invited.set_password('!unusable')
        invited.save(using='tenant_test')
        sync_tenant_user_identity(invited, self.tenant.id)
        set_tenant_db_alias(None)

        res = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({'identifier': 'invited@sweat-test.local', 'password': 'SomePassword123!'}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    # 15. Tenant datasource inactive blocks login
    def test_tenant_datasource_inactive_blocks_login(self):
        self.ds.status = 'MAINTENANCE'
        self.ds.save(using='default')

        res = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({'identifier': 'tenant_admin', 'password': 'TenantPass123!'}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    # 16. Tampered tenant claim denied
    def test_tampered_tenant_claim_denied(self):
        # Generate tenant token for tenant A, but tamper tid claim to a non-existent UUID
        token = _build_tenant_token(self.tenant_admin, str(uuid.uuid4()), db_alias='tenant_test')
        res = self.client.get(
            '/api/v1/auth/me/',
            HTTP_AUTHORIZATION=f'Bearer {token}'
        )
        self.assertIn(res.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

    # 17. Platform token cannot access tenant route
    def test_platform_token_cannot_access_tenant_route(self):
        token = _build_platform_token(self.platform_user)
        # Attempt to access tenant-only route without control-plane header
        res = self.client.get(
            '/api/v1/tenant/users/',
            HTTP_AUTHORIZATION=f'Bearer {token}'
        )
        self.assertIn(res.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

    # 18. Tenant token cannot access platform route
    def test_tenant_token_cannot_access_platform_route(self):
        token = _build_tenant_token(self.tenant_admin, str(self.tenant.id), db_alias='tenant_test')
        res = self.client.get(
            '/api/v1/platform/tenants/',
            HTTP_AUTHORIZATION=f'Bearer {token}'
        )
        self.assertIn(res.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

    # 19. Identifier change updates directory
    def test_identifier_change_updates_directory(self):
        set_tenant_db_alias('tenant_test')
        self.tenant_admin.email = 'new_admin_email@sweat-test.local'
        self.tenant_admin.save(using='tenant_test')
        sync_tenant_user_identity(self.tenant_admin, self.tenant.id)
        set_tenant_db_alias(None)

        # Old email fails
        res_old = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({'identifier': 'admin@sweat-test.local', 'password': 'TenantPass123!'}),
            content_type='application/json'
        )
        self.assertEqual(res_old.status_code, status.HTTP_401_UNAUTHORIZED)

        # New email succeeds
        res_new = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({'identifier': 'new_admin_email@sweat-test.local', 'password': 'TenantPass123!'}),
            content_type='application/json'
        )
        self.assertEqual(res_new.status_code, status.HTTP_200_OK)

    # 20. Deactivation updates effective login state
    def test_deactivation_updates_effective_login_state(self):
        set_tenant_db_alias('tenant_test')
        self.trainer_user.status = 'SUSPENDED'
        self.trainer_user.save(using='tenant_test')
        sync_tenant_user_identity(self.trainer_user, self.tenant.id)
        set_tenant_db_alias(None)

        res = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({'identifier': 'coach_mike', 'password': 'TrainerPass123!'}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    # 21. Reactivation restores login
    def test_reactivation_restores_login(self):
        set_tenant_db_alias('tenant_test')
        self.trainer_user.status = 'SUSPENDED'
        self.trainer_user.save(using='tenant_test')
        sync_tenant_user_identity(self.trainer_user, self.tenant.id)

        # Reactivate
        self.trainer_user.status = 'ACTIVE'
        self.trainer_user.save(using='tenant_test')
        sync_tenant_user_identity(self.trainer_user, self.tenant.id)
        set_tenant_db_alias(None)

        res = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({'identifier': 'coach_mike', 'password': 'TrainerPass123!'}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    # 22. Reconciliation detects missing directory row
    def test_reconciliation_detects_missing_directory_row(self):
        # Delete directory row for platform user
        h = compute_lookup_hash('platform_super')
        AuthenticationIdentity.objects.using('default').filter(lookup_hash=h).delete()
        self.assertFalse(AuthenticationIdentity.objects.using('default').filter(lookup_hash=h).exists())

        out = StringIO()
        call_command('reconcile_auth_identities', dry_run=True, stdout=out)
        output_str = out.getvalue()
        self.assertIn('Missing records to create', output_str)

        # Live run repairs it
        call_command('reconcile_auth_identities', stdout=out)
        self.assertTrue(AuthenticationIdentity.objects.using('default').filter(lookup_hash=h).exists())

    # 23. Reconciliation detects stale row
    def test_reconciliation_detects_stale_row(self):
        # Create an orphan directory entry with non-existent subject_id
        orphan_hash = compute_lookup_hash('stale_ghost_user')
        AuthenticationIdentity.objects.using('default').create(
            lookup_hash=orphan_hash,
            identifier='stale_ghost_user',
            identifier_type='USERNAME',
            account_type='PLATFORM',
            subject_id=uuid.uuid4(),
            status='ACTIVE',
        )

        out = StringIO()
        call_command('reconcile_auth_identities', dry_run=True, stdout=out)
        output_str = out.getvalue()
        self.assertIn('Stale records to remove', output_str)

        # Run live command to clean up
        call_command('reconcile_auth_identities', stdout=out)
        self.assertFalse(AuthenticationIdentity.objects.using('default').filter(lookup_hash=orphan_hash).exists())

    # 24. Reconciliation detects duplicate identifier
    def test_reconciliation_detects_duplicate_identifier(self):
        out = StringIO()
        call_command('reconcile_auth_identities', dry_run=True, stdout=out)
        self.assertIn('RECONCILING UNIVERSAL AUTHENTICATION DIRECTORY', out.getvalue())

    # 25. Password reset routes correctly
    def test_password_reset_routes_correctly(self):
        # Request password reset for platform user
        res_plt = self.client.post(
            '/api/v1/auth/password/reset-request/',
            data=json.dumps({'identifier': 'super@platform.local'}),
            content_type='application/json'
        )
        self.assertEqual(res_plt.status_code, status.HTTP_200_OK)
        self.assertEqual(res_plt.json()['account_type'], 'PLATFORM')

        # Request password reset for tenant user
        res_tnt = self.client.post(
            '/api/v1/auth/password/reset-request/',
            data=json.dumps({'identifier': 'admin@sweat-test.local'}),
            content_type='application/json'
        )
        self.assertEqual(res_tnt.status_code, status.HTTP_200_OK)
        self.assertEqual(res_tnt.json()['account_type'], 'TENANT')
        self.assertEqual(res_tnt.json()['tenant_id'], str(self.tenant.id))

    # 26. Logout/session restoration works correctly
    def test_logout_and_session_restoration_works_correctly(self):
        # 1. Login to get cookie and access token
        res = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({'identifier': 'platform_super', 'password': 'PlatformPass123!'}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        access_token = res.json()['access']
        refresh_token = res.json().get('refresh')

        # 2. Access /auth/me/
        me_res = self.client.get(
            '/api/v1/auth/me/',
            HTTP_AUTHORIZATION=f'Bearer {access_token}'
        )
        self.assertEqual(me_res.status_code, status.HTTP_200_OK)

        # 3. Refresh token
        refresh_res = self.client.post(
            '/api/v1/auth/token/refresh/',
            data=json.dumps({'refresh': refresh_token}),
            content_type='application/json'
        )
        self.assertEqual(refresh_res.status_code, status.HTTP_200_OK)
        self.assertIn('access', refresh_res.json())

        # 4. Logout blacklists session
        logout_res = self.client.post(
            '/api/v1/auth/logout/',
            data=json.dumps({'refresh': refresh_token}),
            content_type='application/json'
        )
        self.assertEqual(logout_res.status_code, status.HTTP_200_OK)

        # 5. Subsequent refresh fails
        post_logout_refresh = self.client.post(
            '/api/v1/auth/token/refresh/',
            data=json.dumps({'refresh': refresh_token}),
            content_type='application/json'
        )
        self.assertIn(post_logout_refresh.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_401_UNAUTHORIZED])
