"""
Sprint 2 RBAC & Authorization Enforcement Test Suite.
Verifies the 10-check schema-defined authorization chain and security controls:

 1. Inactive tenant rejected (Check 1)
 2. Inactive organization rejected (Check 2)
 3. Inactive branch rejected on branch-scoped resource (Check 3)
 4. Disabled tenant module rejected (Check 4)
 5. Unavailable branch module rejected in SELECTED_BRANCHES mode (Check 5)
 6. Inactive user rejected (Check 6)
 7. Inactive / revoked role assignment rejected (Check 7)
 8. Role module access denied (Check 8)
 9. Role submodule access denied (Check 9)
10. Action permission denied (Check 10)
11. Cross-branch isolation: Branch A user cannot access or mutate Branch B resources
12. Database-driven revocation: JWT role claim does not grant access if DB assignment is revoked
13. Platform RBAC: Platform user without required permission receives 403 Forbidden
14. System role protection: Cannot delete or mutate code/scope on system roles
"""

import json
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken

from apps.master.models_iam import PlatformUser, PlatformRole, PlatformPermission, PlatformRolePermission, PlatformUserRole
from apps.master.models_tenant import Tenant
from apps.master.models_saas import ProductModule, ProductSubmodule, TenantModule, TenantPermissionCatalog
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_rbac import (
    Role, RoleAssignment, ModuleCatalog, SubmoduleCatalog, Permission,
    RoleModuleAccess, RoleSubmoduleAccess, RolePermissionSet, RolePermissionSetItem, BranchModule
)
from apps.tenant_core.rbac_engine import RBACAuthorizationEngine
from config.routers import set_tenant_db_alias


class BaseRBACTestCase(TestCase):
    """Base setup for RBAC tests using the default database in test mode."""
    databases = '__all__'

    def setUp(self):
        from apps.master.models_infra import TenantDataSource
        from apps.master.models_saas import TenantSubscription
        from config.tenant_middleware import _register_tenant_connection

        # Set tenant context alias for test environment
        set_tenant_db_alias('tenant_test')
        _register_tenant_connection('tenant_test', 'test_fitness_tenant')
        self.client = APIClient()

        # 1. Master Tenant
        self.tenant = Tenant.objects.create(
            name='Test Fitness Club',
            slug='test-fitness',
            code='TEST-FIT-001',
            status='ACTIVE',
        )

        # 1b. SaaS Plan, Subscription & DataSource (required by TenantJWTAuthentication)
        from apps.master.models_saas import SaasPlan
        self.plan = SaasPlan.objects.create(
            name='Growth Plan',
            code='PLAN-GROWTH',
            tier='growth',
            is_active=True,
        )
        self.subscription = TenantSubscription.objects.create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
        )
        self.data_source = TenantDataSource.objects.create(
            tenant=self.tenant,
            db_name='test_fitness_tenant',
            status='ACTIVE',
        )

        # 2. Master Product Module & Submodule
        self.module_crm = ProductModule.objects.create(
            code='crm',
            name='CRM & Sales Automation',
            is_active=True,
        )
        self.submodule_leads = ProductSubmodule.objects.create(
            module=self.module_crm,
            code='leads',
            name='Leads Management',
            is_active=True,
        )

        # 3. Tenant Module Entitlement (ALL_BRANCHES by default)
        self.tenant_module = TenantModule.objects.create(
            tenant=self.tenant,
            module=self.module_crm,
            availability_mode='ALL_BRANCHES',
            is_enabled=True,
        )

        # 4. Tenant Organization
        self.org = Organization.objects.create(
            name='Test Fitness Org',
            code='TFO-01',
            status='ACTIVE',
        )

        # 5. Branches (Branch A and Branch B)
        self.loc = Location.objects.create(
            organization=self.org,
            name='North District',
            code='NORTH-01',
            status='ACTIVE',
        )
        self.branch_a = Branch.objects.create(
            organization=self.org,
            location=self.loc,
            name='Branch Downtown (A)',
            code='BR-A',
            status='ACTIVE',
        )
        self.branch_b = Branch.objects.create(
            organization=self.org,
            location=self.loc,
            name='Branch Uptown (B)',
            code='BR-B',
            status='ACTIVE',
        )

        # 6. Tenant-local Module, Submodule, and Permission Catalog
        self.local_module = ModuleCatalog.objects.create(
            module_code='crm',
            name='CRM & Sales Automation',
            is_enabled=True,
        )
        self.local_submodule = SubmoduleCatalog.objects.create(
            module=self.local_module,
            submodule_code='leads',
            name='Leads Management',
            is_enabled=True,
        )
        self.perm_view_leads = Permission.objects.create(
            module=self.local_module,
            submodule=self.local_submodule,
            permission_code='crm.leads.view',
            action='view',
            label='View Leads',
        )
        self.perm_create_leads = Permission.objects.create(
            module=self.local_module,
            submodule=self.local_submodule,
            permission_code='crm.leads.create',
            action='create',
            label='Create Leads',
        )

        # 7. Roles:
        # a) System ORG_ADMIN (ORG-scoped)
        self.role_admin = Role.objects.create(
            organization=self.org,
            name='Organization Administrator',
            code='ORG_ADMIN',
            scope='ORG',
            is_system=True,
            is_active=True,
        )
        # b) Branch Staff (BRANCH-scoped)
        self.role_staff = Role.objects.create(
            organization=self.org,
            name='Front Desk Staff',
            code='FRONT_DESK',
            scope='BRANCH',
            is_system=False,
            is_active=True,
        )

        # 8. RBAC Matrix for staff role:
        # Grants module 'crm', submodule 'leads', and permission 'crm.leads.view'
        self.staff_rma = RoleModuleAccess.objects.create(
            role=self.role_staff,
            module=self.local_module,
            can_access=True,
        )
        self.staff_rsa = RoleSubmoduleAccess.objects.create(
            role=self.role_staff,
            submodule=self.local_submodule,
            can_access=True,
        )
        self.staff_pset = RolePermissionSet.objects.create(
            role=self.role_staff,
            name='Front Desk Permissions',
            is_active=True,
        )
        self.staff_item_view = RolePermissionSetItem.objects.create(
            permission_set=self.staff_pset,
            permission=self.perm_view_leads,
            granted=True,
        )

        # 9. Tenant Users:
        # a) Org Admin User
        self.admin_user = TenantUser.objects.create(
            organization=self.org,
            email='admin@testfitness.com',
            first_name='Admin',
            last_name='User',
            status='ACTIVE',
            home_branch=self.branch_a,
        )
        self.admin_user.set_password('AdminPassword123!')
        self.admin_user.save()

        self.admin_assignment = RoleAssignment.objects.create(
            user=self.admin_user,
            role=self.role_admin,
            branch=None,
            is_active=True,
        )

        # b) Staff User assigned to Branch A
        self.staff_user_a = TenantUser.objects.create(
            organization=self.org,
            email='staff.a@testfitness.com',
            first_name='Staff',
            last_name='BranchA',
            status='ACTIVE',
            home_branch=self.branch_a,
        )
        self.staff_user_a.set_password('StaffPassword123!')
        self.staff_user_a.save()

        self.staff_assignment_a = RoleAssignment.objects.create(
            user=self.staff_user_a,
            role=self.role_staff,
            branch=self.branch_a,
            is_active=True,
        )

        # c) Staff User assigned to Branch B
        self.staff_user_b = TenantUser.objects.create(
            organization=self.org,
            email='staff.b@testfitness.com',
            first_name='Staff',
            last_name='BranchB',
            status='ACTIVE',
            home_branch=self.branch_b,
        )
        self.staff_user_b.set_password('StaffPassword123!')
        self.staff_user_b.save()

        self.staff_assignment_b = RoleAssignment.objects.create(
            user=self.staff_user_b,
            role=self.role_staff,
            branch=self.branch_b,
            is_active=True,
        )

        for u in [self.admin_user, self.staff_user_a, self.staff_user_b]:
            u._auth_type = 'tenant'
            u._tenant_id = str(self.tenant.id)
            u._db_alias = 'tenant_test'

        # Ensure DB alias threadlocal matches in test runner
        set_tenant_db_alias('tenant_test')

        # Ensure core product module exists and is entitled
        self.module_core = ProductModule.objects.filter(code='core').first()
        if not self.module_core:
            self.module_core = ProductModule.objects.create(
                code='core',
                name='Core System & Administration',
                is_core=True,
                is_active=True,
            )
        self.tenant_module_core, _ = TenantModule.objects.get_or_create(
            tenant=self.tenant,
            module=self.module_core,
            defaults={'availability_mode': 'ALL_BRANCHES', 'is_enabled': True},
        )

        # Synchronize core catalog to tenant test DB and seed ORG_ADMIN
        from apps.master.provisioning import sync_tenant_catalog_and_rbac
        sync_tenant_catalog_and_rbac('tenant_test', self.org, self.admin_user)

        # Staff role also gets core.users.view so it can view users at assigned branch
        mod_core_local = ModuleCatalog.objects.get(module_code='core')
        sub_users_local = SubmoduleCatalog.objects.get(module=mod_core_local, submodule_code='users')
        perm_users_view = Permission.objects.get(permission_code='core.users.view')

        RoleModuleAccess.objects.get_or_create(role=self.role_staff, module=mod_core_local, defaults={'can_access': True})
        RoleSubmoduleAccess.objects.get_or_create(role=self.role_staff, submodule=sub_users_local, defaults={'can_access': True})
        RolePermissionSetItem.objects.get_or_create(permission_set=self.staff_pset, permission=perm_users_view, defaults={'granted': True})

    def tearDown(self):
        set_tenant_db_alias(None)
        from django.db import connections
        dynamic_aliases = [alias for alias in list(connections) if alias not in ('default', 'tenant_test')]
        for alias in dynamic_aliases:
            try:
                connections[alias].close()
            except Exception:
                pass
            connections.databases.pop(alias, None)
            try:
                delattr(connections._connections, alias)
            except Exception:
                pass

    def _get_tenant_token(self, user):
        """Build a valid JWT with tenant claims pointing to test db."""
        refresh = RefreshToken()
        refresh['sub'] = str(user.id)
        refresh['user_type'] = 'tenant'
        refresh['roles'] = [ra.role.code for ra in user.role_assignments.filter(is_active=True)]
        refresh['tid'] = str(self.tenant.id)
        refresh['tenant_slug'] = self.tenant.slug
        refresh['db_alias'] = 'tenant_test'
        refresh['email'] = user.email
        return str(refresh.access_token)

    def _get_platform_token(self, user):
        refresh = RefreshToken()
        refresh['sub'] = str(user.id)
        refresh['user_type'] = 'platform'
        refresh['roles'] = [ur.role.code for ur in user.role_assignments.filter(is_active=True)]
        refresh['tid'] = ''
        refresh['email'] = user.email
        return str(refresh.access_token)


class Authorization10ChecksTests(BaseRBACTestCase):
    """Tests evaluating each of the 10 checks directly on RBACAuthorizationEngine."""

    def test_check_1_inactive_tenant_rejected(self):
        """Check 1: Tenant status != ACTIVE must fail closed."""
        self.tenant.status = 'SUSPENDED'
        self.tenant.save()

        allowed, reason, check_code = RBACAuthorizationEngine.evaluate(
            user=self.staff_user_a,
            required_module='crm',
            required_permission='crm.leads.view',
            branch_id=str(self.branch_a.id),
        )
        self.assertFalse(allowed)
        self.assertEqual(check_code, 'CHECK_1_TENANT_INACTIVE')
        self.assertIn('SUSPENDED', reason)

    def test_check_2_inactive_organization_rejected(self):
        """Check 2: Organization status != ACTIVE must fail closed."""
        self.org.status = 'INACTIVE'
        self.org.save()

        allowed, reason, check_code = RBACAuthorizationEngine.evaluate(
            user=self.staff_user_a,
            required_module='crm',
            required_permission='crm.leads.view',
            branch_id=str(self.branch_a.id),
        )
        self.assertFalse(allowed)
        self.assertEqual(check_code, 'CHECK_2_ORG_INACTIVE')
        self.assertIn('INACTIVE', reason)

    def test_check_3_inactive_branch_rejected(self):
        """Check 3: Branch status != ACTIVE must reject operations targeting that branch."""
        self.branch_a.status = 'INACTIVE'
        self.branch_a.save()

        allowed, reason, check_code = RBACAuthorizationEngine.evaluate(
            user=self.staff_user_a,
            required_module='crm',
            required_permission='crm.leads.view',
            branch_id=str(self.branch_a.id),
        )
        self.assertFalse(allowed)
        self.assertEqual(check_code, 'CHECK_3_BRANCH_INACTIVE')
        self.assertIn('INACTIVE', reason)

    def test_check_4_disabled_tenant_module_rejected(self):
        """Check 4: TenantModule disabled on Master DB must reject access."""
        self.tenant_module.is_enabled = False
        self.tenant_module.save()

        allowed, reason, check_code = RBACAuthorizationEngine.evaluate(
            user=self.staff_user_a,
            required_module='crm',
            required_permission='crm.leads.view',
            branch_id=str(self.branch_a.id),
        )
        self.assertFalse(allowed)
        self.assertEqual(check_code, 'CHECK_4_MODULE_DISABLED')
        self.assertIn('disabled', reason)

    def test_check_5_unavailable_branch_module_rejected(self):
        """Check 5: In SELECTED_BRANCHES mode, branch without BranchModule is rejected."""
        self.tenant_module.availability_mode = 'SELECTED_BRANCHES'
        self.tenant_module.save()

        # No BranchModule row exists for branch_a
        allowed, reason, check_code = RBACAuthorizationEngine.evaluate(
            user=self.staff_user_a,
            required_module='crm',
            required_permission='crm.leads.view',
            branch_id=str(self.branch_a.id),
        )
        self.assertFalse(allowed)
        self.assertEqual(check_code, 'CHECK_5_BRANCH_MODULE_UNAVAILABLE')

        # Now enable for branch_a
        BranchModule.objects.create(
            branch=self.branch_a,
            module_code='crm',
            is_enabled=True,
        )
        allowed_now, _, _ = RBACAuthorizationEngine.evaluate(
            user=self.staff_user_a,
            required_module='crm',
            required_permission='crm.leads.view',
            branch_id=str(self.branch_a.id),
        )
        self.assertTrue(allowed_now)

    def test_check_6_inactive_user_rejected(self):
        """Check 6: Inactive or suspended user must be rejected."""
        self.staff_user_a.status = 'SUSPENDED'
        self.staff_user_a.save()

        allowed, reason, check_code = RBACAuthorizationEngine.evaluate(
            user=self.staff_user_a,
            required_module='crm',
            required_permission='crm.leads.view',
            branch_id=str(self.branch_a.id),
        )
        self.assertFalse(allowed)
        self.assertEqual(check_code, 'CHECK_6_USER_INACTIVE')
        self.assertIn('SUSPENDED', reason)

    def test_check_7_inactive_or_revoked_role_assignment_rejected(self):
        """Check 7: User with revoked or inactive RoleAssignment must be rejected."""
        self.staff_assignment_a.is_active = False
        self.staff_assignment_a.save()

        allowed, reason, check_code = RBACAuthorizationEngine.evaluate(
            user=self.staff_user_a,
            required_module='crm',
            required_permission='crm.leads.view',
            branch_id=str(self.branch_a.id),
        )
        self.assertFalse(allowed)
        self.assertEqual(check_code, 'CHECK_7_NO_ACTIVE_ROLE')

    def test_check_7_branch_scope_mismatch_rejected(self):
        """Check 7: Staff assigned to Branch A cannot access Branch B."""
        allowed, reason, check_code = RBACAuthorizationEngine.evaluate(
            user=self.staff_user_a,
            required_module='crm',
            required_permission='crm.leads.view',
            branch_id=str(self.branch_b.id),  # Cross-branch request
        )
        self.assertFalse(allowed)
        self.assertEqual(check_code, 'CHECK_7_SCOPE_MISMATCH')
        self.assertIn('Cross-branch access denied', reason)

    def test_check_8_role_module_access_denied(self):
        """Check 8: RoleModuleAccess can_access=False must deny module access."""
        self.staff_rma.can_access = False
        self.staff_rma.save()

        allowed, reason, check_code = RBACAuthorizationEngine.evaluate(
            user=self.staff_user_a,
            required_module='crm',
            required_permission='crm.leads.view',
            branch_id=str(self.branch_a.id),
        )
        self.assertFalse(allowed)
        self.assertEqual(check_code, 'CHECK_8_MODULE_ACCESS_DENIED')

    def test_check_9_role_submodule_access_denied(self):
        """Check 9: RoleSubmoduleAccess can_access=False must deny submodule access."""
        self.staff_rsa.can_access = False
        self.staff_rsa.save()

        allowed, reason, check_code = RBACAuthorizationEngine.evaluate(
            user=self.staff_user_a,
            required_module='crm',
            required_submodule='leads',
            required_permission='crm.leads.view',
            branch_id=str(self.branch_a.id),
        )
        self.assertFalse(allowed)
        self.assertEqual(check_code, 'CHECK_9_SUBMODULE_ACCESS_DENIED')

    def test_check_10_action_permission_denied(self):
        """Check 10: RolePermissionSetItem without granted=True must deny action."""
        # Staff role only has 'crm.leads.view', does NOT have 'crm.leads.create'
        allowed, reason, check_code = RBACAuthorizationEngine.evaluate(
            user=self.staff_user_a,
            required_module='crm',
            required_submodule='leads',
            required_permission='crm.leads.create',
            branch_id=str(self.branch_a.id),
        )
        self.assertFalse(allowed)
        self.assertEqual(check_code, 'CHECK_10_PERMISSION_DENIED')
        self.assertIn('crm.leads.create', reason)


class APIEndpointAuthorizationTests(BaseRBACTestCase):
    """Integration tests on live API endpoints verifying DRF permission classes and scoping."""

    def test_branch_scoping_limits_user_queryset(self):
        """Branch-scoped staff query on /api/v1/tenant/users/ returns only their branch users."""
        token = self._get_tenant_token(self.staff_user_a)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        response = self.client.get('/api/v1/tenant/users/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        results = data.get('results', data) if isinstance(data, dict) else data
        returned_ids = [u['id'] for u in results]

        # Staff A must see Branch A users (admin_user, staff_user_a)
        self.assertIn(str(self.staff_user_a.id), returned_ids)
        # Staff A must NOT see Branch B user
        self.assertNotIn(str(self.staff_user_b.id), returned_ids)

    def test_branch_staff_cannot_modify_user_in_another_branch(self):
        """Branch A staff attempting to update Branch B user is blocked (404/403)."""
        token = self._get_tenant_token(self.staff_user_a)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        # Direct PATCH on Branch B user
        response = self.client.patch(
            f'/api/v1/tenant/users/{self.staff_user_b.id}/',
            data=json.dumps({'first_name': 'Hacked'}),
            content_type='application/json',
        )
        # Filtered out from queryset -> 404 Not Found (or 403 PermissionDenied)
        self.assertIn(response.status_code, [status.HTTP_404_NOT_FOUND, status.HTTP_403_FORBIDDEN])
        set_tenant_db_alias('tenant_test')
        self.staff_user_b.refresh_from_db()
        self.assertEqual(self.staff_user_b.first_name, 'Staff')

    def test_database_driven_revocation_invalidates_active_jwt(self):
        """A user with a valid JWT whose role is revoked in DB immediately receives 403."""
        # 1. User logs in, has valid token with FRONT_DESK role
        token = self._get_tenant_token(self.staff_user_a)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        # First request succeeds
        resp1 = self.client.get('/api/v1/tenant/users/')
        self.assertEqual(resp1.status_code, status.HTTP_200_OK)

        # 2. Administrator revokes role assignment in database
        set_tenant_db_alias('tenant_test')
        self.staff_assignment_a.is_active = False
        self.staff_assignment_a.save()

        # 3. User re-uses the exact same unexpired JWT
        resp2 = self.client.get('/api/v1/tenant/users/')
        self.assertEqual(resp2.status_code, status.HTTP_403_FORBIDDEN)

    def test_system_role_cannot_be_deleted(self):
        """Deleting a system role (ORG_ADMIN) via API must be rejected with 403."""
        token = self._get_tenant_token(self.admin_user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        response = self.client.delete(f'/api/v1/tenant/roles/{self.role_admin.id}/')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('System roles cannot be deleted', response.json().get('detail', ''))
        set_tenant_db_alias('tenant_test')
        self.assertTrue(Role.objects.filter(id=self.role_admin.id).exists())

    def test_system_role_code_and_scope_are_immutable(self):
        """Updating code or scope on a system role must be rejected with 403."""
        token = self._get_tenant_token(self.admin_user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        # Attempt to rename code
        response = self.client.patch(
            f'/api/v1/tenant/roles/{self.role_admin.id}/',
            data=json.dumps({'code': 'COMPROMISED'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        set_tenant_db_alias('tenant_test')
        self.role_admin.refresh_from_db()
        self.assertEqual(self.role_admin.code, 'ORG_ADMIN')

    def test_branch_staff_cannot_manage_roles(self):
        """Branch-scoped staff cannot create or modify roles."""
        token = self._get_tenant_token(self.staff_user_a)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        response = self.client.post(
            '/api/v1/tenant/roles/',
            data=json.dumps({
                'organization': str(self.org.id),
                'name': 'Unauthorized Role',
                'code': 'UNAUTH_ROLE',
                'scope': 'BRANCH',
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_verify_access_endpoint(self):
        """POST /api/v1/tenant/verify-access/ tests the full RBAC chain."""
        token = self._get_tenant_token(self.staff_user_a)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        # Staff user has view permission for leads in branch A
        resp_allowed = self.client.post(
            '/api/v1/tenant/verify-access/',
            data=json.dumps({
                'module': 'crm',
                'submodule': 'leads',
                'permission': 'crm.leads.view',
                'branch_id': str(self.branch_a.id),
            }),
            content_type='application/json',
        )
        self.assertEqual(resp_allowed.status_code, status.HTTP_200_OK)
        self.assertTrue(resp_allowed.json()['allowed'])

        # Staff user does NOT have create permission
        resp_denied = self.client.post(
            '/api/v1/tenant/verify-access/',
            data=json.dumps({
                'module': 'crm',
                'submodule': 'leads',
                'permission': 'crm.leads.create',
                'branch_id': str(self.branch_a.id),
            }),
            content_type='application/json',
        )
        self.assertEqual(resp_denied.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(resp_denied.json()['allowed'])


class PlatformRBACTests(BaseRBACTestCase):
    """Tests evaluating Platform RBAC enforcement on Master DB endpoints."""

    def setUp(self):
        super().setUp()
        from apps.master.models_iam import PlatformModule
        self.platform_module_billing, _ = PlatformModule.objects.get_or_create(
            code='billing',
            defaults={'name': 'Billing Module', 'is_active': True},
        )
        self.platform_role_billing, _ = PlatformRole.objects.get_or_create(
            code='BILLING_SPECIALIST',
            defaults={'name': 'Billing Specialist', 'is_active': True},
        )
        self.perm_billing_view, _ = PlatformPermission.objects.get_or_create(
            code='billing.view',
            defaults={
                'module': self.platform_module_billing,
                'action': 'view',
                'label': 'View Billing',
            },
        )
        # Role has billing.view permission
        PlatformRolePermission.objects.get_or_create(
            role=self.platform_role_billing,
            permission=self.perm_billing_view,
            defaults={'granted': True},
        )

        # Platform user with Billing Specialist role
        self.billing_user = PlatformUser.objects.create(
            email='billing@performanceos.io',
            first_name='Billing',
            last_name='User',
            status='ACTIVE',
            is_staff=False,
            is_superuser=False,
        )
        self.billing_user.set_password('BillingPass123!')
        self.billing_user.save()

        PlatformUserRole.objects.create(
            user=self.billing_user,
            role=self.platform_role_billing,
            is_active=True,
        )

        # Platform Superuser
        self.platform_super = PlatformUser.objects.create(
            email='super@performanceos.io',
            first_name='Super',
            last_name='Admin',
            status='ACTIVE',
            is_staff=True,
            is_superuser=True,
        )
        self.platform_super.set_password('SuperPass123!')
        self.platform_super.save()

    def test_platform_user_without_required_permission_rejected(self):
        """Billing specialist cannot call tenant provisioning or modify tenants."""
        token = self._get_platform_token(self.billing_user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        # Attempt to access tenants CRUD (requires 'tenants.view' / 'tenants.create')
        response = self.client.get('/api/v1/platform/tenants/')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('tenants.view', response.json().get('detail', ''))

    def test_platform_user_with_permission_allowed(self):
        """Billing specialist can view SaaS plans (requires billing.view)."""
        token = self._get_platform_token(self.billing_user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        response = self.client.get('/api/v1/platform/saas-plans/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_platform_superuser_allowed_all_platform_endpoints(self):
        """Platform superuser can access all platform endpoints."""
        token = self._get_platform_token(self.platform_super)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        response = self.client.get('/api/v1/platform/tenants/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class CoreCatalogAndHardeningTests(BaseRBACTestCase):
    """
    Validates the canonical Core system administration module, Master->Tenant sync,
    ORG_ADMIN RBAC records, tenant admin CRUD, fail-closed enforcement, and read-only structural views.
    """

    def test_master_core_catalog_structure(self):
        """Master DB defines canonical core module, submodules, and action permissions without duplicates."""
        core_mods = ProductModule.objects.filter(code='core')
        self.assertEqual(core_mods.count(), 1)
        core_mod = core_mods.first()
        self.assertTrue(core_mod.is_core)

        submodules = set(ProductSubmodule.objects.filter(module=core_mod).values_list('code', flat=True))
        self.assertTrue({'users', 'departments', 'roles', 'permissions'}.issubset(submodules))

        perms = TenantPermissionCatalog.objects.filter(module=core_mod)
        self.assertGreaterEqual(perms.count(), 15)

    def test_tenant_db_synchronized_catalog(self):
        """Tenant DB has synchronized core module, submodules, and action permissions."""
        self.assertTrue(ModuleCatalog.objects.filter(module_code='core', is_enabled=True).exists())
        self.assertGreaterEqual(SubmoduleCatalog.objects.filter(module__module_code='core').count(), 4)
        self.assertGreaterEqual(Permission.objects.filter(module__module_code='core').count(), 15)

    def test_org_admin_has_all_core_permissions(self):
        """ORG_ADMIN receives complete DB RBAC grants for core module and permissions."""
        admin_ps = RolePermissionSet.objects.get(role=self.role_admin)
        granted_core_perms = RolePermissionSetItem.objects.filter(
            permission_set=admin_ps,
            permission__module__module_code='core',
            granted=True,
        ).count()
        self.assertGreaterEqual(granted_core_perms, 15)

    def test_tenant_admin_crud_succeeds_with_core_permissions(self):
        """ORG_ADMIN can create roles, departments, and role assignments via RBAC endpoints."""
        token = self._get_tenant_token(self.admin_user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        # 1. Create a custom role (core.roles.create)
        resp_role = self.client.post(
            '/api/v1/tenant/roles/',
            data=json.dumps({
                'organization': str(self.org.id),
                'name': 'Assistant Trainer',
                'code': 'ASST_TRAINER',
                'scope': 'BRANCH',
            }),
            content_type='application/json',
        )
        self.assertEqual(resp_role.status_code, status.HTTP_201_CREATED)
        role_id = resp_role.json()['id']

        # 2. Create a department (core.departments.create)
        resp_dept = self.client.post(
            '/api/v1/tenant/departments/',
            data=json.dumps({
                'organization': str(self.org.id),
                'name': 'Fitness Training Department',
                'code': 'FIT_TRAIN',
            }),
            content_type='application/json',
        )
        self.assertEqual(resp_dept.status_code, status.HTTP_201_CREATED)

        # 3. Create a role assignment (core.roles.assign)
        resp_ra = self.client.post(
            '/api/v1/tenant/role-assignments/',
            data=json.dumps({
                'user': str(self.staff_user_b.id),
                'role': role_id,
                'branch': str(self.branch_b.id),
                'is_active': True,
            }),
            content_type='application/json',
        )
        self.assertEqual(resp_ra.status_code, status.HTTP_201_CREATED)

    def test_revoked_core_permission_causes_immediate_403(self):
        """Revoking core.roles.create from ORG_ADMIN immediately causes 403 Forbidden."""
        token = self._get_tenant_token(self.admin_user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        # Revoke core.roles.create
        admin_ps = RolePermissionSet.objects.get(role=self.role_admin)
        perm_role_create = Permission.objects.get(permission_code='core.roles.create')
        RolePermissionSetItem.objects.filter(permission_set=admin_ps, permission=perm_role_create).update(granted=False)

        resp = self.client.post(
            '/api/v1/tenant/roles/',
            data=json.dumps({
                'organization': str(self.org.id),
                'name': 'Should Fail Role',
                'code': 'FAIL_ROLE',
                'scope': 'BRANCH',
            }),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_unauthorized_role_denied_admin_endpoints(self):
        """Staff user without core.roles.create or core.departments.create receives 403."""
        token = self._get_tenant_token(self.staff_user_a)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        resp1 = self.client.post(
            '/api/v1/tenant/roles/',
            data=json.dumps({
                'organization': str(self.org.id),
                'name': 'Unauthorized Role',
                'code': 'UNAUTH_ROLE_2',
                'scope': 'BRANCH',
            }),
            content_type='application/json',
        )
        self.assertEqual(resp1.status_code, status.HTTP_403_FORBIDDEN)

        resp2 = self.client.post(
            '/api/v1/tenant/departments/',
            data=json.dumps({
                'organization': str(self.org.id),
                'name': 'Unauthorized Dept',
                'code': 'UNAUTH_DEPT',
            }),
            content_type='application/json',
        )
        self.assertEqual(resp2.status_code, status.HTTP_403_FORBIDDEN)

    def test_structural_endpoints_read_only_and_cannot_mutate(self):
        """Tenant users cannot mutate platform-owned structural entities (all mutations return 405)."""
        token = self._get_tenant_token(self.admin_user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        for endpoint in ['organizations', 'locations', 'branches', 'module-catalog', 'branch-modules']:
            resp = self.client.post(
                f'/api/v1/tenant/{endpoint}/',
                data=json.dumps({'dummy': 'value'}),
                content_type='application/json',
            )
            self.assertEqual(
                resp.status_code, status.HTTP_405_METHOD_NOT_ALLOWED,
                f"Endpoint /api/v1/tenant/{endpoint}/ must reject POST with 405 Method Not Allowed"
            )

    def test_fail_closed_on_missing_view_metadata(self):
        """TenantRBACPermission raises PermissionDenied (403) when view metadata is missing."""
        from apps.tenant_core.permissions import TenantRBACPermission
        from rest_framework.exceptions import PermissionDenied
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        drf_request = Request(factory.get('/dummy/'))
        drf_request.user = self.admin_user

        perm = TenantRBACPermission()

        # 1. View missing required_module
        class ViewMissingModule:
            action = 'list'
            permission_prefix = 'core.users'
        with self.assertRaises(PermissionDenied) as ctx1:
            perm.has_permission(drf_request, ViewMissingModule())
        self.assertIn('missing required module', str(ctx1.exception))

        # 2. View missing required_permission
        class ViewMissingPerm:
            required_module = 'core'
            action = 'unknown_custom_action'
        with self.assertRaises(PermissionDenied) as ctx2:
            perm.has_permission(drf_request, ViewMissingPerm())
        self.assertIn('missing required permission mapping', str(ctx2.exception))

        # 3. View missing required_submodule when module has submodules
        class ViewMissingSubmodule:
            required_module = 'core'
            action = 'list'
            required_permission = 'core.users.view'
        with self.assertRaises(PermissionDenied) as ctx3:
            perm.has_permission(drf_request, ViewMissingSubmodule())
        self.assertIn('missing required submodule', str(ctx3.exception))
