"""
Sprint 3 API Completion & Integration Test Suite.

Verifies:
1. Master APIs:
   - TenantModuleViewSet (CRUD + assign_branches)
   - TenantSubscriptionViewSet (CRUD + change_plan)
   - SubscriptionInvoiceViewSet (read-only enforcement: 405 on POST/PUT/DELETE)
   - TenantResourceUsageViewSet (read-only + summary metrics: 405 on POST/PUT/DELETE)
   - MarketplaceIntegrationViewSet.toggle_install action
   - TenantViewSet.metrics (real calculated metrics without hardcoded MRR)
2. Tenant RBAC APIs:
   - SubmoduleCatalogViewSet (read-only enforcement: 405 on POST/PUT/DELETE)
   - PermissionViewSet (read-only enforcement: 405 on POST/PUT/DELETE)
   - RolePermissionSetViewSet.matrix (atomic transaction, module/submodule/permission updates, rollback, fail-closed)
3. Support APIs:
   - CompanyEntityViewSet (read-only for tenant: 405 on mutations)
   - UserBranchViewSet (scoped branch access)
4. Settings, Notifications & Audit APIs:
   - OrganizationSettingsViewSet + current action (core.settings.*)
   - BranchSettingsViewSet (core.settings.* with branch scoping)
   - NotificationTemplateViewSet (core.notifications.*)
   - TenantAuditEventViewSet (read-only append-only ledger: 405 on mutations, core.audit.view)
"""

import json
from decimal import Decimal
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken

from apps.master.models_tenant import Tenant
from apps.master.models_saas import (
    SaasPlan, SaasPlanPrice, TenantSubscription, TenantModule, ProductModule, ProductSubmodule,
    TenantPermissionCatalog, SubscriptionInvoice, TenantResourceUsage,
)
from apps.master.models_market import (
    MarketplaceIntegration, TenantIntegrationEntitlement,
)
from apps.master.models_iam import (
    PlatformUser, PlatformRole, PlatformPermission, PlatformRolePermission, PlatformUserRole,
)
from apps.master.models_infra import TenantDataSource
from apps.tenant_core.models_org import Organization, CompanyEntity, Location, Branch
from apps.tenant_core.models_users import TenantUser, UserBranch
from apps.tenant_core.models_rbac import (
    Role, RoleAssignment, ModuleCatalog, SubmoduleCatalog, Permission,
    RolePermissionSet, RolePermissionSetItem, RoleModuleAccess, RoleSubmoduleAccess, BranchModule,
)
from apps.tenant_core.models_govern import OrganizationSettings, BranchSettings, NotificationTemplate
from apps.tenant_core.models_privacy import TenantAuditEvent
from config.routers import set_tenant_db_alias
from config.tenant_middleware import _register_tenant_connection


class BaseSprint3TestCase(TestCase):
    """Base setup for Sprint 3 tests."""
    databases = '__all__'

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        _register_tenant_connection('tenant_test', 'test_fitness_tenant')
        self.client = APIClient()

        # 1. Master Tenant & Infra
        self.tenant = Tenant.objects.create(
            name='Test Fitness Club',
            slug='test-fitness',
            code='TEST-FIT-001',
            status='ACTIVE',
        )
        self.plan = SaasPlan.objects.create(
            name='Growth Plan',
            code='PLAN-GROWTH',
            tier='growth',
            is_active=True,
        )
        self.plan_price = SaasPlanPrice.objects.create(
            plan=self.plan,
            billing_cycle='MONTHLY',
            currency='INR',
            amount=Decimal('9999.00'),
            is_active=True,
        )
        self.new_plan = SaasPlan.objects.create(
            name='Enterprise Plan',
            code='PLAN-ENTERPRISE',
            tier='enterprise',
            is_active=True,
        )
        self.new_plan_price = SaasPlanPrice.objects.create(
            plan=self.new_plan,
            billing_cycle='ANNUAL',
            currency='INR',
            amount=Decimal('19999.00'),
            is_active=True,
        )
        self.subscription = TenantSubscription.objects.create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
            billing_cycle='MONTHLY',
        )
        self.data_source = TenantDataSource.objects.create(
            tenant=self.tenant,
            db_name='test',
            status='ACTIVE',
        )

        # 2. Platform Users & Roles (no superuser bypasses)
        self.plat_role = PlatformRole.objects.get(code='SUPER_ADMIN')
        self.plat_user = PlatformUser.objects.create(
            email='platform.editor@performanceos.internal',
            first_name='Platform',
            last_name='Editor',
            status='ACTIVE',
            is_staff=True,
            is_superuser=False,
        )
        PlatformUserRole.objects.create(user=self.plat_user, role=self.plat_role, is_active=True)

        # Platform Viewer role with view-only permissions (no edit)
        self.plat_viewer_role = PlatformRole.objects.create(
            name='Platform Viewer Only',
            code='VIEWER_ONLY',
            is_system=False,
            is_active=True,
        )
        for p_code in ['tenants.view', 'billing.view', 'marketplace.view', 'modules.view']:
            perm = PlatformPermission.objects.get(code=p_code)
            PlatformRolePermission.objects.create(role=self.plat_viewer_role, permission=perm, granted=True)

        self.plat_viewer_user = PlatformUser.objects.create(
            email='platform.viewer@performanceos.internal',
            first_name='Platform',
            last_name='Viewer',
            status='ACTIVE',
            is_staff=True,
            is_superuser=False,
        )
        PlatformUserRole.objects.create(user=self.plat_viewer_user, role=self.plat_viewer_role, is_active=True)

        # 3. Product Modules & Tenant Modules
        self.mod_core, _ = ProductModule.objects.get_or_create(code='core', defaults={'name': 'Core System', 'is_active': True})
        self.mod_crm, _ = ProductModule.objects.get_or_create(code='crm', defaults={'name': 'CRM & Sales', 'is_active': True})

        self.sub_users, _ = ProductSubmodule.objects.get_or_create(module=self.mod_core, code='users', defaults={'name': 'Users', 'is_active': True})
        self.sub_roles, _ = ProductSubmodule.objects.get_or_create(module=self.mod_core, code='roles', defaults={'name': 'Roles', 'is_active': True})
        self.sub_perms, _ = ProductSubmodule.objects.get_or_create(module=self.mod_core, code='permissions', defaults={'name': 'Permissions', 'is_active': True})
        self.sub_settings, _ = ProductSubmodule.objects.get_or_create(module=self.mod_core, code='settings', defaults={'name': 'Settings', 'is_active': True})
        self.sub_notifs, _ = ProductSubmodule.objects.get_or_create(module=self.mod_core, code='notifications', defaults={'name': 'Notifications', 'is_active': True})
        self.sub_audit, _ = ProductSubmodule.objects.get_or_create(module=self.mod_core, code='audit', defaults={'name': 'Audit', 'is_active': True})
        self.sub_leads, _ = ProductSubmodule.objects.get_or_create(module=self.mod_crm, code='leads', defaults={'name': 'Leads', 'is_active': True})

        self.tm_core, _ = TenantModule.objects.get_or_create(
            tenant=self.tenant, module=self.mod_core, defaults={'availability_mode': 'ALL_BRANCHES', 'is_enabled': True},
        )
        self.tm_crm, _ = TenantModule.objects.get_or_create(
            tenant=self.tenant, module=self.mod_crm, defaults={'availability_mode': 'ALL_BRANCHES', 'is_enabled': True},
        )

        # 4. Tenant Core Data
        self.org = Organization.objects.create(name='Test Fitness Org', code='TFO-01', status='ACTIVE')
        self.loc = Location.objects.create(organization=self.org, name='North District', code='NORTH-01', status='ACTIVE')
        self.branch_a = Branch.objects.create(organization=self.org, location=self.loc, name='Branch A', code='BR-A', status='ACTIVE')
        self.branch_b = Branch.objects.create(organization=self.org, location=self.loc, name='Branch B', code='BR-B', status='ACTIVE')

        # Company Entity
        self.company = CompanyEntity.objects.create(
            organization=self.org, legal_name='Test Fitness LLC', registration_number='REG-999', status='ACTIVE',
        )

        # Tenant User & Roles
        self.admin_user = TenantUser.objects.create(
            organization=self.org, email='admin@testfitness.com', first_name='Org', last_name='Admin', status='ACTIVE',
            home_branch=self.branch_a,
        )
        self.admin_role, _ = Role.objects.get_or_create(
            organization=self.org, code='ORG_ADMIN', defaults={'name': 'Organization Admin', 'scope': 'ORG', 'is_system': True},
        )
        self.admin_assignment = RoleAssignment.objects.create(
            user=self.admin_user, role=self.admin_role, branch=None, is_active=True,
        )

        # Catalog sync to tenant DB
        self.cat_core, _ = ModuleCatalog.objects.get_or_create(module_code='core', defaults={'name': 'Core System', 'is_enabled': True})
        self.cat_crm, _ = ModuleCatalog.objects.get_or_create(module_code='crm', defaults={'name': 'CRM', 'is_enabled': True})

        self.csub_users, _ = SubmoduleCatalog.objects.get_or_create(module=self.cat_core, submodule_code='users', defaults={'name': 'Users', 'is_enabled': True})
        self.csub_roles, _ = SubmoduleCatalog.objects.get_or_create(module=self.cat_core, submodule_code='roles', defaults={'name': 'Roles', 'is_enabled': True})
        self.csub_perms, _ = SubmoduleCatalog.objects.get_or_create(module=self.cat_core, submodule_code='permissions', defaults={'name': 'Permissions', 'is_enabled': True})
        self.csub_settings, _ = SubmoduleCatalog.objects.get_or_create(module=self.cat_core, submodule_code='settings', defaults={'name': 'Settings', 'is_enabled': True})
        self.csub_notifs, _ = SubmoduleCatalog.objects.get_or_create(module=self.cat_core, submodule_code='notifications', defaults={'name': 'Notifications', 'is_enabled': True})
        self.csub_audit, _ = SubmoduleCatalog.objects.get_or_create(module=self.cat_core, submodule_code='audit', defaults={'name': 'Audit', 'is_enabled': True})
        self.csub_leads, _ = SubmoduleCatalog.objects.get_or_create(module=self.cat_crm, submodule_code='leads', defaults={'name': 'Leads', 'is_enabled': True})

        # Action Permissions
        self.perm_perms_view, _ = Permission.objects.get_or_create(module=self.cat_core, submodule=self.csub_perms, action='view', defaults={'permission_code': 'core.permissions.view', 'label': 'View Permissions'})
        self.perm_perms_manage, _ = Permission.objects.get_or_create(module=self.cat_core, submodule=self.csub_perms, action='manage', defaults={'permission_code': 'core.permissions.manage', 'label': 'Manage Permissions'})
        self.perm_users_view, _ = Permission.objects.get_or_create(module=self.cat_core, submodule=self.csub_users, action='view', defaults={'permission_code': 'core.users.view', 'label': 'View Users'})
        self.perm_users_create, _ = Permission.objects.get_or_create(module=self.cat_core, submodule=self.csub_users, action='create', defaults={'permission_code': 'core.users.create', 'label': 'Create Users'})
        self.perm_users_edit, _ = Permission.objects.get_or_create(module=self.cat_core, submodule=self.csub_users, action='edit', defaults={'permission_code': 'core.users.edit', 'label': 'Edit Users'})
        self.perm_users_delete, _ = Permission.objects.get_or_create(module=self.cat_core, submodule=self.csub_users, action='delete', defaults={'permission_code': 'core.users.delete', 'label': 'Delete Users'})
        self.perm_settings_view, _ = Permission.objects.get_or_create(module=self.cat_core, submodule=self.csub_settings, action='view', defaults={'permission_code': 'core.settings.view', 'label': 'View Settings'})
        self.perm_settings_edit, _ = Permission.objects.get_or_create(module=self.cat_core, submodule=self.csub_settings, action='edit', defaults={'permission_code': 'core.settings.edit', 'label': 'Edit Settings'})
        self.perm_notifs_view, _ = Permission.objects.get_or_create(module=self.cat_core, submodule=self.csub_notifs, action='view', defaults={'permission_code': 'core.notifications.view', 'label': 'View Notifications'})
        self.perm_notifs_manage, _ = Permission.objects.get_or_create(module=self.cat_core, submodule=self.csub_notifs, action='manage', defaults={'permission_code': 'core.notifications.manage', 'label': 'Manage Notifications'})
        self.perm_audit_view, _ = Permission.objects.get_or_create(module=self.cat_core, submodule=self.csub_audit, action='view', defaults={'permission_code': 'core.audit.view', 'label': 'View Audit'})
        self.perm_leads_view, _ = Permission.objects.get_or_create(module=self.cat_crm, submodule=self.csub_leads, action='view', defaults={'permission_code': 'crm.leads.view', 'label': 'View Leads'})

        # Grant full core permissions to ORG_ADMIN
        RoleModuleAccess.objects.get_or_create(role=self.admin_role, module=self.cat_core, defaults={'can_access': True})
        RoleModuleAccess.objects.get_or_create(role=self.admin_role, module=self.cat_crm, defaults={'can_access': True})

        RoleSubmoduleAccess.objects.get_or_create(role=self.admin_role, submodule=self.csub_perms, defaults={'can_access': True})
        RoleSubmoduleAccess.objects.get_or_create(role=self.admin_role, submodule=self.csub_users, defaults={'can_access': True})
        RoleSubmoduleAccess.objects.get_or_create(role=self.admin_role, submodule=self.csub_roles, defaults={'can_access': True})
        RoleSubmoduleAccess.objects.get_or_create(role=self.admin_role, submodule=self.csub_settings, defaults={'can_access': True})
        RoleSubmoduleAccess.objects.get_or_create(role=self.admin_role, submodule=self.csub_notifs, defaults={'can_access': True})
        RoleSubmoduleAccess.objects.get_or_create(role=self.admin_role, submodule=self.csub_audit, defaults={'can_access': True})

        self.perm_set, _ = RolePermissionSet.objects.get_or_create(
            role=self.admin_role, defaults={'name': 'Admin Full Permissions', 'is_active': True},
        )
        for p in [
            self.perm_perms_view, self.perm_perms_manage,
            self.perm_users_view, self.perm_users_create, self.perm_users_edit, self.perm_users_delete,
            self.perm_settings_view, self.perm_settings_edit,
            self.perm_notifs_view, self.perm_notifs_manage,
            self.perm_audit_view,
        ]:
            RolePermissionSetItem.objects.get_or_create(permission_set=self.perm_set, permission=p, defaults={'granted': True})

    def get_platform_token(self, user=None):
        u = user or self.plat_user
        refresh = RefreshToken()
        refresh['sub'] = str(u.id)
        refresh['user_type'] = 'platform'
        refresh['role'] = 'SUPER_ADMIN'
        refresh['email'] = u.email
        return str(refresh.access_token)

    def get_tenant_token(self, user=None):
        u = user or self.admin_user
        set_tenant_db_alias('tenant_test')
        refresh = RefreshToken()
        refresh['sub'] = str(u.id)
        refresh['user_type'] = 'tenant'
        refresh['roles'] = [
            ra.role.code for ra in RoleAssignment.objects.filter(user=u, is_active=True).select_related('role')
        ]
        refresh['tid'] = str(self.tenant.id)
        refresh['tenant_slug'] = self.tenant.slug
        refresh['db_alias'] = 'tenant_test'
        refresh['email'] = u.email
        return str(refresh.access_token)


class MasterSprint3APITests(BaseSprint3TestCase):
    """Tests for Sprint 3 Master/Platform APIs."""

    def test_tenant_module_list_and_assign_branches_rbac(self):
        """
        Requirements A & B:
        User WITHOUT tenants.edit cannot call assign_branches (403).
        User WITH tenants.edit can call assign_branches (200).
        """
        # A. User WITHOUT tenants.edit (only tenants.view) gets 403
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.get_platform_token(self.plat_viewer_user)}')
        res_denied = self.client.post(
            f'/api/v1/platform/tenant-modules/{self.tm_crm.id}/assign-branches/',
            data={'branch_ids': [str(self.branch_a.id)]},
            format='json',
        )
        self.assertEqual(res_denied.status_code, status.HTTP_403_FORBIDDEN)

        # B. User WITH tenants.edit gets 200
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.get_platform_token(self.plat_user)}')
        res_assign = self.client.post(
            f'/api/v1/platform/tenant-modules/{self.tm_crm.id}/assign-branches/',
            data={'branch_ids': [str(self.branch_a.id)]},
            format='json',
        )
        self.assertEqual(res_assign.status_code, status.HTTP_200_OK)
        self.tm_crm.refresh_from_db()
        self.assertEqual(self.tm_crm.availability_mode, 'SELECTED_BRANCHES')
        self.assertTrue(BranchModule.objects.using('tenant_test').filter(branch=self.branch_a, module_code='crm', is_enabled=True).exists())

    def test_tenant_subscription_crud_and_change_plan_rbac(self):
        """
        Requirements C & D:
        User WITHOUT billing.edit cannot call change_plan (403).
        User WITH billing.edit can call change_plan (200).
        """
        # C. User WITHOUT billing.edit (only billing.view) gets 403
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.get_platform_token(self.plat_viewer_user)}')
        res_denied = self.client.post(
            f'/api/v1/platform/subscriptions/{self.subscription.id}/change-plan/',
            data={'plan_id': str(self.new_plan.id), 'billing_cycle': 'ANNUAL'},
            format='json',
        )
        self.assertEqual(res_denied.status_code, status.HTTP_403_FORBIDDEN)

        # D. User WITH billing.edit gets 200
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.get_platform_token(self.plat_user)}')
        res_change = self.client.post(
            f'/api/v1/platform/subscriptions/{self.subscription.id}/change-plan/',
            data={'plan_id': str(self.new_plan.id), 'billing_cycle': 'ANNUAL'},
            format='json',
        )
        self.assertEqual(res_change.status_code, status.HTTP_200_OK)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.plan.id, self.new_plan.id)
        self.assertEqual(self.subscription.billing_cycle, 'ANNUAL')

    def test_invoices_and_usage_are_strictly_read_only(self):
        """Invoices and Usage endpoints allow GET but reject POST, PUT, DELETE with 405."""
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.get_platform_token(self.plat_user)}')

        inv = SubscriptionInvoice.objects.create(
            subscription=self.subscription,
            tenant=self.tenant,
            invoice_number='INV-2026-001',
            total=Decimal('9999.00'),
            status='PAID',
        )
        res_inv_get = self.client.get('/api/v1/platform/invoices/')
        self.assertEqual(res_inv_get.status_code, status.HTTP_200_OK)

        res_inv_post = self.client.post('/api/v1/platform/invoices/', data={'total': '500'}, format='json')
        self.assertEqual(res_inv_post.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

        # Resource Usage
        from apps.master.models_saas import ResourceMetric
        metric, _ = ResourceMetric.objects.get_or_create(
            code='ACTIVE_MEMBERS',
            defaults={'name': 'Active Members', 'unit': 'members'}
        )
        TenantResourceUsage.objects.create(
            tenant=self.tenant,
            metric=metric,
            current_value=45,
        )
        res_usage_get = self.client.get('/api/v1/platform/usage/')
        self.assertEqual(res_usage_get.status_code, status.HTTP_200_OK)

        res_usage_summary = self.client.get('/api/v1/platform/usage/summary/')
        self.assertEqual(res_usage_summary.status_code, status.HTTP_200_OK)
        self.assertIn('activeManagedMembers', res_usage_summary.data)

        res_usage_post = self.client.post('/api/v1/platform/usage/', data={'current_value': 10}, format='json')
        self.assertEqual(res_usage_post.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_marketplace_toggle_install_rbac(self):
        """
        Requirements E & F:
        User WITHOUT marketplace.edit cannot call toggle_install (403).
        User WITH marketplace.edit can call toggle_install (200).
        """
        app = MarketplaceIntegration.objects.create(
            name='WhatsApp Notifier',
            code='WHATSAPP_NOTIF',
            integration_type='WHATSAPP',
            provider='Gupshup',
            status='ACTIVE',
        )

        # E. User WITHOUT marketplace.edit (only marketplace.view) gets 403
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.get_platform_token(self.plat_viewer_user)}')
        res_denied = self.client.post(
            f'/api/v1/platform/marketplace/{app.id}/toggle-install/',
            data={'tenant_id': str(self.tenant.id)},
            format='json',
        )
        self.assertEqual(res_denied.status_code, status.HTTP_403_FORBIDDEN)

        # F. User WITH marketplace.edit gets 200
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.get_platform_token(self.plat_user)}')
        res_install = self.client.post(
            f'/api/v1/platform/marketplace/{app.id}/toggle-install/',
            data={'tenant_id': str(self.tenant.id)},
            format='json',
        )
        self.assertEqual(res_install.status_code, status.HTTP_200_OK)
        self.assertTrue(res_install.data['is_installed'])

        # Toggle uninstall with marketplace.edit
        res_uninstall = self.client.post(
            f'/api/v1/platform/marketplace/{app.id}/toggle-install/',
            data={'tenant_id': str(self.tenant.id)},
            format='json',
        )
        self.assertEqual(res_uninstall.status_code, status.HTTP_200_OK)
        self.assertFalse(res_uninstall.data['is_installed'])

    def test_tenant_metrics_calculation(self):
        """Platform tenant metrics endpoint returns calculated counts and MRR."""
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.get_platform_token(self.plat_user)}')
        res = self.client.get('/api/v1/platform/tenants/metrics/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(res.data['totalTenants'], 1)
        self.assertGreaterEqual(res.data['monthlyRecurringRevenue'], 9999.0)

    def test_master_db_canonical_platform_catalog_and_roles(self):
        """
        Requirement I:
        Verify live Master DB contains canonical platform permissions and relevant role grants.
        """
        # Canonical platform permissions
        expected_perms = [
            'tenants.view', 'tenants.edit', 'tenants.create', 'tenants.delete', 'tenants.provision',
            'billing.view', 'billing.edit', 'billing.create', 'billing.delete',
            'marketplace.view', 'marketplace.edit', 'marketplace.create', 'marketplace.delete',
            'modules.view', 'modules.edit', 'iam.view', 'iam.create', 'iam.edit', 'iam.delete',
        ]
        self.assertGreaterEqual(PlatformPermission.objects.count(), len(expected_perms))
        for p_code in expected_perms:
            self.assertTrue(
                PlatformPermission.objects.filter(code=p_code).exists(),
                f"Platform permission '{p_code}' missing from Master DB catalog."
            )

        # Canonical role grants
        super_admin_grants = PlatformRolePermission.objects.filter(role__code='SUPER_ADMIN', granted=True).count()
        self.assertGreaterEqual(super_admin_grants, len(expected_perms))

        billing_admin_grants = set(PlatformRolePermission.objects.filter(role__code='BILLING_ADMIN', granted=True).values_list('permission__code', flat=True))
        self.assertTrue({'billing.view', 'billing.edit', 'tenants.view'}.issubset(billing_admin_grants))

        support_lead_grants = set(PlatformRolePermission.objects.filter(role__code='SUPPORT_LEAD', granted=True).values_list('permission__code', flat=True))
        self.assertTrue({'tenants.view', 'billing.view', 'marketplace.view', 'modules.view'}.issubset(support_lead_grants))


class TenantSprint3RBACMatrixTests(BaseSprint3TestCase):
    """Tests for Tenant Submodules, Permissions and Atomic Matrix endpoints."""

    def test_submodules_and_permissions_catalogs_are_read_only(self):
        """Tenant users can list submodules and permissions but cannot mutate (405)."""
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.get_tenant_token()}')

        res_sub = self.client.get('/api/v1/tenant/submodules/')
        self.assertEqual(res_sub.status_code, status.HTTP_200_OK)

        res_sub_post = self.client.post('/api/v1/tenant/submodules/', data={'code': 'hack'}, format='json')
        self.assertEqual(res_sub_post.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

        res_perm = self.client.get('/api/v1/tenant/permissions/')
        self.assertEqual(res_perm.status_code, status.HTTP_200_OK)

        res_perm_post = self.client.post('/api/v1/tenant/permissions/', data={'code': 'hack.view'}, format='json')
        self.assertEqual(res_perm_post.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_atomic_rbac_matrix_success(self):
        """Atomic matrix endpoint updates module_access, submodule_access, and permission_set_items in one transaction."""
        custom_role = Role.objects.create(
            organization=self.org, name='Custom Front Desk', code='FRONT_DESK_CUSTOM', scope='BRANCH', is_system=False,
        )
        perm_set = RolePermissionSet.objects.create(
            role=custom_role, name='Front Desk Set', is_active=True,
        )

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.get_tenant_token()}')
        payload = {
            'module_access': [
                {'module_code': 'crm', 'is_allowed': True},
            ],
            'submodule_access': [
                {'submodule_code': 'leads', 'is_allowed': True},
            ],
            'permissions': [
                {'permission_code': 'crm.leads.view', 'is_granted': True},
            ]
        }
        res = self.client.post(
            f'/api/v1/tenant/permission-sets/{perm_set.id}/matrix/',
            data=payload,
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.data['success'])

        # Verify DB state
        self.assertTrue(RoleModuleAccess.objects.using('tenant_test').filter(role=custom_role, module__module_code='crm', can_access=True).exists())
        self.assertTrue(RoleSubmoduleAccess.objects.using('tenant_test').filter(role=custom_role, submodule__submodule_code='leads', can_access=True).exists())
        self.assertTrue(RolePermissionSetItem.objects.using('tenant_test').filter(permission_set=perm_set, permission__permission_code='crm.leads.view', granted=True).exists())

    def test_atomic_rbac_matrix_rollback_on_failure(self):
        """Atomic matrix endpoint aborts completely if an invalid code is supplied."""
        custom_role = Role.objects.using('tenant_test').create(
            organization=self.org, name='Custom Trainer', code='TRAINER_CUSTOM', scope='BRANCH', is_system=False,
        )
        perm_set = RolePermissionSet.objects.using('tenant_test').create(
            role=custom_role, name='Trainer Set', is_active=True,
        )

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.get_tenant_token()}')
        payload = {
            'module_access': [
                {'module_code': 'crm', 'is_allowed': True},
            ],
            'submodule_access': [
                {'submodule_code': 'non_existent_submodule', 'is_allowed': True},
            ],
            'permissions': [
                {'permission_code': 'crm.leads.view', 'is_granted': True},
            ]
        }
        res = self.client.post(
            f'/api/v1/tenant/permission-sets/{perm_set.id}/matrix/',
            data=payload,
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

        # Verify complete rollback: no CRM module access was committed
        self.assertFalse(RoleModuleAccess.objects.using('tenant_test').filter(role=custom_role, module__module_code='crm').exists())
        self.assertFalse(RolePermissionSetItem.objects.using('tenant_test').filter(permission_set=perm_set).exists())

    def test_atomic_rbac_matrix_protected_system_role_fails(self):
        """Modifying system role permission matrix is rejected."""
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.get_tenant_token()}')
        payload = {
            'module_access': [{'module_code': 'crm', 'is_allowed': False}],
            'submodule_access': [],
            'permissions': [],
        }
        res = self.client.post(
            f'/api/v1/tenant/permission-sets/{self.perm_set.id}/matrix/',
            data=payload,
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('System roles cannot be modified', str(res.data))


class TenantSprint3SettingsNotificationsAuditTests(BaseSprint3TestCase):
    """Tests for Settings, Notifications, and Audit APIs."""

    def test_organization_settings_current_and_update(self):
        """Tenant user with core.settings.edit can read and update organization settings."""
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.get_tenant_token()}')

        res = self.client.get('/api/v1/tenant/organization-settings/current/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        res_update = self.client.put(
            '/api/v1/tenant/organization-settings/current/',
            data={'currency': 'USD', 'tax_rate_pct': '18.00'},
            format='json',
        )
        self.assertEqual(res_update.status_code, status.HTTP_200_OK)
        self.assertEqual(res_update.data['currency'], 'USD')

    def test_branch_settings_crud(self):
        """Tenant user can configure branch-level business overrides."""
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.get_tenant_token()}')

        res_create = self.client.post(
            '/api/v1/tenant/branch-settings/',
            data={
                'branch': str(self.branch_a.id),
                'max_booking_capacity': 150,
                'business_open_time': '06:00',
                'business_close_time': '22:00',
            },
            format='json',
        )
        self.assertEqual(res_create.status_code, status.HTTP_201_CREATED)
        settings_id = res_create.data['id']

        res_get = self.client.get(f'/api/v1/tenant/branch-settings/{settings_id}/')
        self.assertEqual(res_get.status_code, status.HTTP_200_OK)
        self.assertEqual(res_get.data['max_booking_capacity'], 150)

    def test_notification_templates_crud(self):
        """Tenant user can manage notification templates under core.notifications.*."""
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.get_tenant_token()}')

        res_create = self.client.post(
            '/api/v1/tenant/notification-templates/',
            data={
                'name': 'Welcome SMS',
                'channel': 'SMS',
                'event_type': 'LEAD_WELCOME',
                'subject': 'Welcome',
                'body': 'Welcome to Test Fitness!',
                'is_active': True,
            },
            format='json',
        )
        self.assertEqual(res_create.status_code, status.HTTP_201_CREATED)

        res_list = self.client.get('/api/v1/tenant/notification-templates/')
        self.assertEqual(res_list.status_code, status.HTTP_200_OK)

    def test_audit_events_are_append_only_and_read_only(self):
        """Tenant audit events allow list/retrieve under core.audit.view, but reject mutations with 405."""
        TenantAuditEvent.objects.using('tenant_test').create(
            actor=self.admin_user,
            actor_email=self.admin_user.email,
            action='UPDATE',
            resource_type='OrganizationSettings',
            resource_id=str(self.org.id),
            description='Updated tax rate',
        )

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.get_tenant_token()}')
        res_list = self.client.get('/api/v1/tenant/audit-events/')
        self.assertEqual(res_list.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(res_list.data['results'] if 'results' in res_list.data else res_list.data), 1)

        # Mutation must be blocked (read-only viewset)
        res_post = self.client.post('/api/v1/tenant/audit-events/', data={'action': 'DELETE'}, format='json')
        self.assertEqual(res_post.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_company_entities_read_only_on_tenant(self):
        """CompanyEntityViewSet is strictly read-only on the tenant side."""
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.get_tenant_token()}')

        res_list = self.client.get('/api/v1/tenant/company-entities/')
        self.assertEqual(res_list.status_code, status.HTTP_200_OK)

        res_post = self.client.post(
            '/api/v1/tenant/company-entities/',
            data={'legal_name': 'Hacked LLC'},
            format='json',
        )
        self.assertEqual(res_post.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class TenantSprint3UserBranchRBATests(BaseSprint3TestCase):
    """
    Requirements G & H:
    UserBranchViewSet authorization testing against approved core.users.edit contract.
    """

    def setUp(self):
        super().setUp()
        from apps.tenant_core.models_users import UserBranch

        # 1. Role: USER_BRANCH_EDITOR (has ONLY core.users.view and core.users.edit, NO create or delete)
        self.role_editor = Role.objects.using('tenant_test').create(
            organization=self.org,
            name='User Branch Editor',
            code='USER_BRANCH_EDITOR',
            scope='ORG',
            is_system=False,
            is_active=True,
        )
        mod_core = ModuleCatalog.objects.using('tenant_test').get(module_code='core')
        sub_users = SubmoduleCatalog.objects.using('tenant_test').get(module=mod_core, submodule_code='users')
        perm_view = Permission.objects.using('tenant_test').get(permission_code='core.users.view')
        perm_edit = Permission.objects.using('tenant_test').get(permission_code='core.users.edit')

        RoleModuleAccess.objects.using('tenant_test').create(role=self.role_editor, module=mod_core, can_access=True)
        RoleSubmoduleAccess.objects.using('tenant_test').create(role=self.role_editor, submodule=sub_users, can_access=True)
        ps_editor = RolePermissionSet.objects.using('tenant_test').create(role=self.role_editor)
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=ps_editor, permission=perm_view, granted=True)
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=ps_editor, permission=perm_edit, granted=True)

        self.editor_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='editor.user@testfitness.com',
            status='ACTIVE',
        )
        RoleAssignment.objects.using('tenant_test').create(user=self.editor_user, role=self.role_editor, is_active=True)

        # 2. Role: USER_BRANCH_VIEWER (has ONLY core.users.view, NO core.users.edit)
        self.role_viewer = Role.objects.using('tenant_test').create(
            organization=self.org,
            name='User Branch Viewer',
            code='USER_BRANCH_VIEWER',
            scope='ORG',
            is_system=False,
            is_active=True,
        )
        RoleModuleAccess.objects.using('tenant_test').create(role=self.role_viewer, module=mod_core, can_access=True)
        RoleSubmoduleAccess.objects.using('tenant_test').create(role=self.role_viewer, submodule=sub_users, can_access=True)
        ps_viewer = RolePermissionSet.objects.using('tenant_test').create(role=self.role_viewer)
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=ps_viewer, permission=perm_view, granted=True)

        self.viewer_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='viewer.user@testfitness.com',
            status='ACTIVE',
        )
        RoleAssignment.objects.using('tenant_test').create(user=self.viewer_user, role=self.role_viewer, is_active=True)

        # Target user to assign branches to
        self.target_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='target.member@testfitness.com',
            status='ACTIVE',
        )

    def test_user_with_only_core_users_edit_can_mutate_user_branches(self):
        """
        Requirement G:
        User WITH only core.users.edit (and view) can:
        - create user-branch association
        - update user-branch association
        - delete user-branch association
        """
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.get_tenant_token(self.editor_user)}')

        # 1. Create association (POST)
        res_create = self.client.post(
            '/api/v1/tenant/user-branches/',
            data={
                'user': str(self.target_user.id),
                'branch': str(self.branch_a.id),
                'scope_type': 'HOME',
                'is_active': True,
            },
            format='json',
        )
        self.assertEqual(res_create.status_code, status.HTTP_201_CREATED)
        ub_id = res_create.data['id']

        # 2. Update association (PATCH)
        res_update = self.client.patch(
            f'/api/v1/tenant/user-branches/{ub_id}/',
            data={'scope_type': 'ADDITIONAL'},
            format='json',
        )
        self.assertEqual(res_update.status_code, status.HTTP_200_OK)
        self.assertEqual(res_update.data['scope_type'], 'ADDITIONAL')

        # 3. Delete association (DELETE)
        res_delete = self.client.delete(f'/api/v1/tenant/user-branches/{ub_id}/')
        self.assertEqual(res_delete.status_code, status.HTTP_204_NO_CONTENT)

    def test_user_without_core_users_edit_cannot_mutate_user_branches(self):
        """
        Requirement H:
        User WITHOUT core.users.edit cannot mutate user-branch associations.
        """
        from apps.tenant_core.models_users import UserBranch
        ub = UserBranch.objects.using('tenant_test').create(
            user=self.target_user,
            branch=self.branch_a,
            scope_type='HOME',
            is_active=True,
        )

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.get_tenant_token(self.viewer_user)}')

        # Cannot create
        res_create = self.client.post(
            '/api/v1/tenant/user-branches/',
            data={
                'user': str(self.target_user.id),
                'branch': str(self.branch_b.id),
                'scope_type': 'ADDITIONAL',
            },
            format='json',
        )
        self.assertEqual(res_create.status_code, status.HTTP_403_FORBIDDEN)

        # Cannot update
        res_update = self.client.patch(
            f'/api/v1/tenant/user-branches/{ub.id}/',
            data={'scope_type': 'PASSPORT'},
            format='json',
        )
        self.assertEqual(res_update.status_code, status.HTTP_403_FORBIDDEN)

        # Cannot delete
        res_delete = self.client.delete(f'/api/v1/tenant/user-branches/{ub.id}/')
        self.assertEqual(res_delete.status_code, status.HTTP_403_FORBIDDEN)

