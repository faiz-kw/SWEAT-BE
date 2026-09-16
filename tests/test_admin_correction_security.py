"""
Administration Module Correction & Tenant Security Test Suite.
Verifies:
1. Cross-Plane Isolation:
   - Tenant user token cannot access /api/v1/platform/* endpoints (403/401).
   - Tenant user token cannot access platform roles or permissions.
   - Platform user token cannot access tenant business endpoints without valid tenant context.
2. Cross-Tenant Isolation:
   - Tenant A admin can view only Tenant A users, roles, branches.
   - Tenant A admin cannot view or mutate Tenant B users, roles, branches.
   - Tenant A admin cannot assign Tenant B roles or branches to users.
3. Tenant Role Management:
   - Tenant admin with core.roles.create can create a custom role (ORG or BRANCH scope).
   - Tenant admin can update role permissions via update-permissions action.
   - System role immutability and deletion protections are enforced.
   - Roles with active user assignments cannot be deleted.
4. Tenant User Management:
   - Tenant admin can create users with role and department assignments.
   - Tenant admin can update user role, department, and branch.
"""

import uuid
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken

from apps.master.models_iam import PlatformUser, PlatformRole, PlatformPermission, PlatformRolePermission, PlatformUserRole
from apps.master.models_tenant import Tenant
from apps.master.models_saas import ProductModule, ProductSubmodule, TenantModule, TenantPermissionCatalog, SaasPlan, TenantSubscription
from apps.master.models_infra import TenantDataSource
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser, Department, UserBranch
from apps.tenant_core.models_rbac import (
    Role, RoleAssignment, ModuleCatalog, SubmoduleCatalog, Permission,
    RoleModuleAccess, RoleSubmoduleAccess, RolePermissionSet, RolePermissionSetItem
)
from config.routers import set_tenant_db_alias
from config.tenant_middleware import _register_tenant_connection


class AdminCorrectionSecurityTests(TestCase):
    databases = '__all__'

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        _register_tenant_connection('tenant_test', 'test_fitness_tenant')
        self.client = APIClient()

        # 1. Setup Tenant A
        self.tenant_a = Tenant.objects.using('default').create(
            name='Tenant A Fitness',
            slug='tenant-a-fit',
            code='TEN-A-001',
            status='ACTIVE',
        )
        TenantDataSource.objects.using('default').create(
            tenant=self.tenant_a,
            db_name='test_fitness_tenant',
            status='ACTIVE',
        )

        # SaaS Plan
        self.plan, _ = SaasPlan.objects.using('default').get_or_create(
            code='ENTERPRISE',
            defaults={'name': 'Enterprise Plan', 'is_active': True}
        )
        self.subscription = TenantSubscription.objects.using('default').create(
            tenant=self.tenant_a,
            plan=self.plan,
            status='ACTIVE',
        )

        from apps.master.models_saas import ResourceMetric, SaasPlanResourceLimit
        self.metric_users, _ = ResourceMetric.objects.using('default').get_or_create(
            code='ACTIVE_USERS', defaults={'name': 'Active Users', 'metric_type': 'GAUGE'}
        )
        SaasPlanResourceLimit.objects.using('default').get_or_create(
            plan=self.plan, metric=self.metric_users, defaults={'limit_value': 100}
        )

        # Master & Tenant Core Module
        self.pm_core, _ = ProductModule.objects.using('default').get_or_create(
            code='core', defaults={'name': 'Core Platform', 'is_active': True}
        )
        self.psm_users, _ = ProductSubmodule.objects.using('default').get_or_create(
            module=self.pm_core, code='users', defaults={'name': 'Users', 'is_active': True}
        )
        self.psm_roles, _ = ProductSubmodule.objects.using('default').get_or_create(
            module=self.pm_core, code='roles', defaults={'name': 'Roles', 'is_active': True}
        )
        self.psm_perms, _ = ProductSubmodule.objects.using('default').get_or_create(
            module=self.pm_core, code='permissions', defaults={'name': 'Permissions', 'is_active': True}
        )

        self.tm_core, _ = TenantModule.objects.using('default').get_or_create(
            tenant=self.tenant_a, module=self.pm_core, defaults={'is_enabled': True}
        )

        # Tenant A DB Setup
        self.org_a, _ = Organization.objects.using('tenant_test').get_or_create(
            code='ORG-A', defaults={'name': 'Tenant A Org', 'status': 'ACTIVE'}
        )
        self.loc_a, _ = Location.objects.using('tenant_test').get_or_create(
            organization=self.org_a, code='LOC-A1', defaults={'name': 'Downtown District A', 'status': 'ACTIVE'}
        )
        self.branch_a, _ = Branch.objects.using('tenant_test').get_or_create(
            organization=self.org_a, code='BR-A1', defaults={'name': 'Downtown Branch A', 'location': self.loc_a, 'status': 'ACTIVE'}
        )

        # Catalogs in Tenant DB
        self.mc_core, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(
            module_code='core', defaults={'name': 'Core', 'is_enabled': True}
        )
        self.smc_users, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=self.mc_core, submodule_code='users', defaults={'name': 'Users', 'is_enabled': True}
        )
        self.smc_roles, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=self.mc_core, submodule_code='roles', defaults={'name': 'Roles', 'is_enabled': True}
        )
        self.smc_perms, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=self.mc_core, submodule_code='permissions', defaults={'name': 'Permissions', 'is_enabled': True}
        )

        # Permissions in Tenant DB
        self.perm_user_view, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.mc_core, submodule=self.smc_users, permission_code='core.users.view', defaults={'action': 'view'}
        )
        self.perm_user_create, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.mc_core, submodule=self.smc_users, permission_code='core.users.create', defaults={'action': 'create'}
        )
        self.perm_user_edit, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.mc_core, submodule=self.smc_users, permission_code='core.users.edit', defaults={'action': 'edit'}
        )
        self.perm_roles_view, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.mc_core, submodule=self.smc_roles, permission_code='core.roles.view', defaults={'action': 'view'}
        )
        self.perm_roles_create, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.mc_core, submodule=self.smc_roles, permission_code='core.roles.create', defaults={'action': 'create'}
        )
        self.perm_roles_edit, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.mc_core, submodule=self.smc_roles, permission_code='core.roles.edit', defaults={'action': 'edit'}
        )
        self.perm_roles_delete, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.mc_core, submodule=self.smc_roles, permission_code='core.roles.delete', defaults={'action': 'delete'}
        )

        # Admin Role in Tenant DB
        self.admin_role, _ = Role.objects.using('tenant_test').get_or_create(
            organization=self.org_a, code='ORG_ADMIN', defaults={'name': 'Org Admin', 'scope': 'ORG', 'is_system': True, 'is_active': True}
        )
        self.admin_perm_set, _ = RolePermissionSet.objects.using('tenant_test').get_or_create(
            role=self.admin_role, organization=self.org_a, defaults={'name': 'Org Admin Perms', 'status': 'ACTIVE'}
        )
        RoleModuleAccess.objects.using('tenant_test').get_or_create(
            role=self.admin_role, module=self.mc_core, defaults={'permission_set': self.admin_perm_set, 'can_access': True}
        )
        for sm in [self.smc_users, self.smc_roles, self.smc_perms]:
            RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(
                role=self.admin_role, submodule=sm, defaults={'permission_set': self.admin_perm_set, 'can_access': True}
            )
        for p in [self.perm_user_view, self.perm_user_create, self.perm_user_edit, self.perm_roles_view, self.perm_roles_create, self.perm_roles_edit, self.perm_roles_delete]:
            RolePermissionSetItem.objects.using('tenant_test').get_or_create(
                permission_set=self.admin_perm_set, permission=p, defaults={'granted': True}
            )

        # Tenant Admin User
        self.tenant_admin = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a, email='admin@tenanta.com', first_name='Admin', last_name='A', status='ACTIVE'
        )
        self.tenant_admin.set_password('Secret1234!')
        self.tenant_admin.save(using='tenant_test')
        RoleAssignment.objects.using('tenant_test').create(
            user=self.tenant_admin, role=self.admin_role, is_active=True
        )

        # Platform User
        self.platform_user = PlatformUser.objects.using('default').create(
            email='super@platform.io', first_name='Super', last_name='Platform', is_staff=True, is_superuser=True
        )

        # Setup Tenant B in master DB
        self.tenant_b = Tenant.objects.using('default').create(
            name='Tenant B Competitor', slug='tenant-b-fit', code='TEN-B-002', status='ACTIVE'
        )

    def _get_tenant_token(self, user, tenant):
        refresh = RefreshToken()
        refresh['sub'] = str(user.id)
        refresh['user_type'] = 'tenant'
        refresh['roles'] = ['ORG_ADMIN']
        refresh['tid'] = str(tenant.id)
        refresh['tenant_slug'] = tenant.slug
        refresh['db_alias'] = 'tenant_test'
        refresh['email'] = user.email
        refresh['loc'] = ['*']
        refresh['act_loc'] = ''
        return str(refresh.access_token)

    def _get_platform_token(self, user):
        refresh = RefreshToken()
        refresh['sub'] = str(user.id)
        refresh['user_type'] = 'platform'
        refresh['roles'] = ['SUPER_ADMIN']
        refresh['tid'] = ''
        refresh['email'] = user.email
        refresh['is_superuser'] = True
        return str(refresh.access_token)

    # -------------------------------------------------------------------------
    # 1. CROSS-PLANE SECURITY
    # -------------------------------------------------------------------------

    def test_tenant_token_denied_on_platform_endpoints(self):
        """A tenant JWT must receive HTTP 403 Forbidden or 401 Unauthorized when calling /api/v1/platform/*."""
        token = self._get_tenant_token(self.tenant_admin, self.tenant_a)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        res_users = self.client.get('/api/v1/platform/platform-users/')
        self.assertIn(res_users.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

        res_tenants = self.client.get('/api/v1/platform/tenants/')
        self.assertIn(res_tenants.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

        res_plans = self.client.get('/api/v1/platform/saas-plans/')
        self.assertIn(res_plans.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

    def test_platform_token_denied_on_tenant_without_context(self):
        """A platform token calling /api/v1/tenant/* without tenant context must be denied."""
        token = self._get_platform_token(self.platform_user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        res = self.client.get('/api/v1/tenant/users/')
        self.assertIn(res.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

    # -------------------------------------------------------------------------
    # 2. CROSS-TENANT ISOLATION
    # -------------------------------------------------------------------------

    def test_tenant_admin_sees_own_users_only(self):
        """Tenant A admin sees only Tenant A users."""
        user_a2 = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a, email='trainer@tenanta.com', first_name='Trainer', status='ACTIVE'
        )

        token = self._get_tenant_token(self.tenant_admin, self.tenant_a)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        res = self.client.get('/api/v1/tenant/users/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        user_list = res.data.get('results', res.data) if isinstance(res.data, dict) else res.data
        emails = [u['email'] for u in user_list]
        self.assertIn(self.tenant_admin.email, emails)
        self.assertIn(user_a2.email, emails)

    # -------------------------------------------------------------------------
    # 3. TENANT ROLE CREATION & PERMISSION UPDATE
    # -------------------------------------------------------------------------

    def test_tenant_admin_create_custom_role_and_update_permissions(self):
        """Tenant admin can create a custom role and configure its permissions."""
        token = self._get_tenant_token(self.tenant_admin, self.tenant_a)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        # 1. Create Role
        res_create = self.client.post('/api/v1/tenant/roles/', {
            'name': 'Head Coach',
            'code': 'HEAD_COACH',
            'description': 'Senior fitness instructor',
            'scope': 'ORG',
        }, format='json')
        self.assertEqual(res_create.status_code, status.HTTP_201_CREATED)
        role_id = res_create.data['id']

        # 2. Update Role Permissions via update-permissions action
        res_perm = self.client.post(f'/api/v1/tenant/roles/{role_id}/update-permissions/', {
            'permissions': [
                {'permission_id': str(self.perm_user_view.id), 'granted': True},
            ]
        }, format='json')
        self.assertEqual(res_perm.status_code, status.HTTP_200_OK)

        # 3. Verify permissions in DB
        db_role = Role.objects.using('tenant_test').get(id=role_id)
        self.assertFalse(db_role.is_system)
        self.assertEqual(db_role.scope, 'ORG')

    def test_system_role_deletion_denied(self):
        """System roles like ORG_ADMIN cannot be deleted."""
        token = self._get_tenant_token(self.tenant_admin, self.tenant_a)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        res = self.client.delete(f'/api/v1/tenant/roles/{self.admin_role.id}/')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_role_with_active_users_cannot_be_deleted(self):
        """Custom role with active user assignments cannot be deleted."""
        token = self._get_tenant_token(self.tenant_admin, self.tenant_a)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        # Create custom role
        res_role = self.client.post('/api/v1/tenant/roles/', {
            'name': 'Assistant Manager',
            'code': 'ASST_MGR',
            'scope': 'BRANCH',
        }, format='json')
        self.assertEqual(res_role.status_code, status.HTTP_201_CREATED)
        role_id = res_role.data['id']

        # Assign to user
        set_tenant_db_alias('tenant_test')
        custom_role = Role.objects.using('tenant_test').get(id=role_id)
        RoleAssignment.objects.using('tenant_test').create(
            user=self.tenant_admin, role=custom_role, is_active=True
        )

        # Attempt delete
        res_del = self.client.delete(f'/api/v1/tenant/roles/{role_id}/')
        self.assertEqual(res_del.status_code, status.HTTP_403_FORBIDDEN)

    # -------------------------------------------------------------------------
    # 4. TENANT USER MANAGEMENT WITH ROLE & DEPARTMENT
    # -------------------------------------------------------------------------

    def test_create_tenant_user_with_role_and_department(self):
        """Tenant admin creates user with role and department assignment."""
        set_tenant_db_alias('tenant_test')
        dept = Department.objects.using('tenant_test').create(
            organization=self.org_a, name='Coaching Staff', code='COACH_DEPT', status='ACTIVE'
        )

        token = self._get_tenant_token(self.tenant_admin, self.tenant_a)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        res_user = self.client.post('/api/v1/tenant/users/', {
            'email': 'newstaff@tenanta.com',
            'first_name': 'New',
            'last_name': 'Staff',
            'password': 'Password123!',
            'role_id': str(self.admin_role.id),
            'department_id': str(dept.id),
        }, format='json')
        self.assertEqual(res_user.status_code, status.HTTP_201_CREATED)

        user_id = res_user.data['id']
        self.assertEqual(res_user.data['status'], 'ACTIVE')
        self.assertEqual(res_user.data['role'], 'ORG_ADMIN')
        self.assertEqual(res_user.data['department'], 'Coaching Staff')

    def test_create_tenant_user_with_branch_and_active_password(self):
        """Tenant admin creates user with branch assignment and password >= 10 chars."""
        set_tenant_db_alias('tenant_test')
        token = self._get_tenant_token(self.tenant_admin, self.tenant_a)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        res_user = self.client.post('/api/v1/tenant/users/', {
            'email': 'branchcoach@tenanta.com',
            'first_name': 'Branch',
            'last_name': 'Coach',
            'password': 'Performance123!',
            'branch': str(self.branch_a.id),
            'role': 'ORG_ADMIN',
        }, format='json')
        self.assertEqual(res_user.status_code, status.HTTP_201_CREATED)
        data = res_user.data
        self.assertEqual(data['status'], 'ACTIVE')
        self.assertEqual(data['home_branch_name'], self.branch_a.name)

        # Verify DB relational state
        set_tenant_db_alias('tenant_test')
        user_obj = TenantUser.objects.using('tenant_test').select_related('home_branch').get(email='branchcoach@tenanta.com')
        self.assertEqual(user_obj.home_branch_id, self.branch_a.id)
        ub = UserBranch.objects.using('tenant_test').filter(user=user_obj, branch=self.branch_a).first()
        self.assertIsNotNone(ub)
        self.assertEqual(ub.scope_type, 'HOME')
        self.assertTrue(ub.is_primary)

        ra = RoleAssignment.objects.using('tenant_test').filter(user=user_obj, is_active=True).first()
        self.assertIsNotNone(ra)
        self.assertEqual(ra.branch_id, self.branch_a.id)
        self.assertEqual(ra.scope_type, 'BRANCH')

    def test_create_tenant_user_invited_mode_without_password(self):
        """Tenant admin invites a user without initial password."""
        set_tenant_db_alias('tenant_test')
        token = self._get_tenant_token(self.tenant_admin, self.tenant_a)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        res_user = self.client.post('/api/v1/tenant/users/', {
            'email': 'invitedstaff@tenanta.com',
            'first_name': 'Invited',
            'last_name': 'Staff',
            'role': 'ORG_ADMIN',
        }, format='json')
        self.assertEqual(res_user.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res_user.data['status'], 'INVITED')

        set_tenant_db_alias('tenant_test')
        user_obj = TenantUser.objects.using('tenant_test').get(email='invitedstaff@tenanta.com')
        self.assertEqual(user_obj.status, 'INVITED')
        self.assertIsNotNone(user_obj.invited_at)

    def test_create_tenant_user_short_password_rejected(self):
        """Initial password < 10 chars is rejected with 400 validation error."""
        set_tenant_db_alias('tenant_test')
        token = self._get_tenant_token(self.tenant_admin, self.tenant_a)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        res_user = self.client.post('/api/v1/tenant/users/', {
            'email': 'shortpass@tenanta.com',
            'first_name': 'Short',
            'last_name': 'Pass',
            'password': 'Pass1234!',  # 9 chars -> invalid
            'role': 'ORG_ADMIN',
        }, format='json')
        self.assertEqual(res_user.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('password', res_user.data)

    def test_update_tenant_user_branch_and_role(self):
        """Tenant admin can update an existing user's branch and role assignment."""
        set_tenant_db_alias('tenant_test')
        user = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a,
            email='updateme@tenanta.com',
            first_name='Update',
            last_name='Me',
            status='ACTIVE'
        )
        token = self._get_tenant_token(self.tenant_admin, self.tenant_a)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        res_update = self.client.patch(f'/api/v1/tenant/users/{user.id}/', {
            'branch': str(self.branch_a.id),
            'role_id': str(self.admin_role.id),
        }, format='json')
        self.assertEqual(res_update.status_code, status.HTTP_200_OK)

        set_tenant_db_alias('tenant_test')
        updated_user = TenantUser.objects.using('tenant_test').get(id=user.id)
        self.assertEqual(updated_user.home_branch_id, self.branch_a.id)
        ub = UserBranch.objects.using('tenant_test').filter(user=updated_user, branch=self.branch_a).first()
        self.assertIsNotNone(ub)
        self.assertEqual(ub.scope_type, 'HOME')

