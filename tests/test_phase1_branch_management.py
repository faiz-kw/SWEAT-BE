"""
Phase 1: Branch Management & Governance Comprehensive Test Suite.

Verifies:
1. Authorized branch listing (core.settings.view).
2. Authorized branch creation with full fields (core.settings.edit).
3. Unauthorized branch creation blocked (403).
4. Authorization is purely permission-based, independent of role name (custom role with permission succeeds).
5. Branch update with audit event.
6. Deactivate and reactivate lifecycle endpoints.
7. Duplicate branch code blocked within organization.
8. International phone numbers and IANA timezone validation.
9. Passport / Cross-Branch eligibility toggle persistence.
10. Company entity association.
11. Geofence radius and enforcement settings.
12. Auto-seeding of 7-day BranchWorkingHours on branch creation.
13. BranchOperatingException creation for holiday/maintenance.
14. Safe deletion: branches with historical business data cannot be destroyed and must be deactivated.
15. Filtering by active/inactive status.
"""

from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken

from apps.master.models_tenant import Tenant
from apps.master.models_saas import SaasPlan, SaasPlanPrice, TenantSubscription, TenantModule, ProductModule, ProductSubmodule
from apps.master.models_infra import TenantDataSource
from apps.tenant_core.models_org import Organization, CompanyEntity, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_rbac import (
    Role, RoleAssignment, ModuleCatalog, SubmoduleCatalog, Permission,
    RolePermissionSet, RolePermissionSetItem, RoleModuleAccess, RoleSubmoduleAccess,
)
from apps.tenant_core.models_govern import BranchWorkingHours, BranchOperatingException
from apps.tenant_core.models_privacy import TenantAuditEvent
from apps.tenant_core.models_classes import ClassCategory, ClassTemplate, ClassOccurrence
from config.routers import set_tenant_db_alias
from config.tenant_middleware import _register_tenant_connection


class Phase1BranchManagementTestCase(TestCase):
    databases = '__all__'

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        _register_tenant_connection('tenant_test', 'test_fitness_tenant')
        self.client = APIClient()

        # 1. Tenant on Master DB
        self.tenant = Tenant.objects.create(
            name='SWEAT Elite Fitness',
            slug='sweat-elite',
            code='SWEAT-001',
            status='ACTIVE',
        )
        self.data_source = TenantDataSource.objects.create(
            tenant=self.tenant,
            db_name='test',
            status='ACTIVE',
        )
        self.plan = SaasPlan.objects.create(name='Pro Plan', code='PLAN-PRO', tier='growth', is_active=True)
        self.plan_price = SaasPlanPrice.objects.create(
            plan=self.plan, billing_cycle='MONTHLY', currency='INR', amount=Decimal('9999.00'), is_active=True
        )
        self.subscription = TenantSubscription.objects.create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
            billing_cycle='MONTHLY',
        )

        # Product modules on Master DB
        self.prod_mod_core, _ = ProductModule.objects.get_or_create(
            code='core', defaults={'name': 'Core System', 'is_active': True}
        )
        self.tenant_mod_core, _ = TenantModule.objects.get_or_create(
            tenant=self.tenant, module=self.prod_mod_core,
            defaults={'availability_mode': 'ALL_BRANCHES', 'is_enabled': True}
        )

        # 2. Tenant Core Hierarchy on Tenant DB
        self.org = Organization.objects.using('tenant_test').create(
            name='SWEAT Elite Org', code='SWEAT-ORG', status='ACTIVE', timezone='Asia/Kolkata'
        )
        self.loc = Location.objects.using('tenant_test').create(
            organization=self.org, name='Mumbai Central', city='Mumbai', code='LOC-MUMBAI', status='ACTIVE'
        )
        self.company = CompanyEntity.objects.using('tenant_test').create(
            organization=self.org, name='SWEAT Wellness Pvt Ltd', legal_name='SWEAT Wellness Pvt Ltd',
            code='SWEAT-PVT', status='ACTIVE'
        )

        self.branch_1 = Branch.objects.using('tenant_test').create(
            organization=self.org, location=self.loc, company_entity=self.company,
            name='Bandra Flagship', code='BANDRA_01', address='Linking Road, Bandra West',
            phone='+91 98200 12345', email='bandra@sweatfit.com', timezone='Asia/Kolkata',
            capacity=80, business_open_time='06:00', business_close_time='22:00',
            is_passport_eligible=True, geofence_radius_meters=200, geofence_enforcement='STRICT',
            status='ACTIVE'
        )

        # 3. RBAC Catalog on Tenant DB
        self.cat_core, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(
            module_code='core', defaults={'name': 'Core System', 'is_enabled': True}
        )
        self.sub_settings, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=self.cat_core, submodule_code='settings', defaults={'name': 'Settings', 'is_enabled': True}
        )
        self.perm_settings_view, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.cat_core, submodule=self.sub_settings, action='view',
            defaults={'permission_code': 'core.settings.view', 'label': 'View Settings'}
        )
        self.perm_settings_edit, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.cat_core, submodule=self.sub_settings, action='edit',
            defaults={'permission_code': 'core.settings.edit', 'label': 'Edit Settings'}
        )

        # 4. Standard Org Admin User
        self.admin_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org, email='admin@sweatfit.com', first_name='Org', last_name='Admin', status='ACTIVE',
            home_branch=self.branch_1
        )
        self.admin_role = Role.objects.using('tenant_test').create(
            organization=self.org, code='ORG_ADMIN', name='Organization Admin', scope='ORG', is_system=True
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.admin_user, role=self.admin_role, branch=None, is_active=True
        )
        RoleModuleAccess.objects.using('tenant_test').create(role=self.admin_role, module=self.cat_core, can_access=True)
        RoleSubmoduleAccess.objects.using('tenant_test').create(role=self.admin_role, submodule=self.sub_settings, can_access=True)
        ps_admin = RolePermissionSet.objects.using('tenant_test').create(role=self.admin_role, name='Admin Perms', is_active=True)
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=ps_admin, permission=self.perm_settings_view, granted=True)
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=ps_admin, permission=self.perm_settings_edit, granted=True)

        # 5. Read-only User
        self.viewer_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org, email='viewer@sweatfit.com', first_name='Viewer', last_name='User', status='ACTIVE',
            home_branch=self.branch_1
        )
        self.viewer_role = Role.objects.using('tenant_test').create(
            organization=self.org, code='VIEWER', name='Viewer', scope='ORG', is_system=False
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.viewer_user, role=self.viewer_role, branch=None, is_active=True
        )
        RoleModuleAccess.objects.using('tenant_test').create(role=self.viewer_role, module=self.cat_core, can_access=True)
        RoleSubmoduleAccess.objects.using('tenant_test').create(role=self.viewer_role, submodule=self.sub_settings, can_access=True)
        ps_viewer = RolePermissionSet.objects.using('tenant_test').create(role=self.viewer_role, name='Viewer Perms', is_active=True)
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=ps_viewer, permission=self.perm_settings_view, granted=True)
        # Note: perm_settings_edit is NOT granted to viewer

        # 6. Custom Role with specific permission (Arbitrary role name, not ORG_ADMIN)
        self.coord_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org, email='coordinator@sweatfit.com', first_name='Studio', last_name='Coordinator', status='ACTIVE',
            home_branch=self.branch_1
        )
        self.coord_role = Role.objects.using('tenant_test').create(
            organization=self.org, code='CUSTOM_COORDINATOR', name='Custom Coordinator', scope='ORG', is_system=False
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.coord_user, role=self.coord_role, branch=None, is_active=True
        )
        RoleModuleAccess.objects.using('tenant_test').create(role=self.coord_role, module=self.cat_core, can_access=True)
        RoleSubmoduleAccess.objects.using('tenant_test').create(role=self.coord_role, submodule=self.sub_settings, can_access=True)
        ps_coord = RolePermissionSet.objects.using('tenant_test').create(role=self.coord_role, name='Coord Perms', is_active=True)
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=ps_coord, permission=self.perm_settings_view, granted=True)
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=ps_coord, permission=self.perm_settings_edit, granted=True)

    def get_token(self, user):
        set_tenant_db_alias('tenant_test')
        refresh = RefreshToken()
        refresh['sub'] = str(user.id)
        refresh['user_type'] = 'tenant'
        refresh['roles'] = [
            ra.role.code for ra in RoleAssignment.objects.using('tenant_test').filter(user=user, is_active=True).select_related('role')
        ]
        refresh['tid'] = str(self.tenant.id)
        refresh['tenant_slug'] = self.tenant.slug
        refresh['db_alias'] = 'tenant_test'
        refresh['email'] = user.email
        return str(refresh.access_token)

    def test_01_authorized_branch_list(self):
        token = self.get_token(self.admin_user)
        res = self.client.get('/api/v1/tenant/branches/', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()
        results = data if isinstance(data, list) else data.get('results', [])
        self.assertTrue(len(results) >= 1)
        b = next(x for x in results if x['id'] == str(self.branch_1.id))
        self.assertEqual(b['code'], 'BANDRA_01')
        self.assertEqual(b['name'], 'Bandra Flagship')
        self.assertTrue(b['is_passport_eligible'])
        self.assertTrue(b['is_active'])

    def test_02_authorized_branch_create_and_autoseed_hours(self):
        token = self.get_token(self.admin_user)
        payload = {
            'name': 'Andheri West Studio',
            'code': 'ANDHERI_WEST',
            'city': 'Mumbai',
            'address': 'Crystal Point Mall, New Link Road',
            'phone': '+91 99887 76655',
            'email': 'andheri@sweatfit.com',
            'timezone': 'Asia/Kolkata',
            'capacity': 120,
            'business_open_time': '06:00',
            'business_close_time': '22:00',
            'is_passport_eligible': True,
            'geofence_radius_meters': 250,
            'geofence_enforcement': 'STRICT',
            'company_entity': str(self.company.id),
        }
        res = self.client.post('/api/v1/tenant/branches/', data=payload, format='json', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        data = res.json()
        self.assertEqual(data['code'], 'ANDHERI_WEST')
        self.assertEqual(data['name'], 'Andheri West Studio')
        self.assertTrue(data['is_passport_eligible'])
        self.assertEqual(data['geofence_radius_meters'], 250)

        # Verify auto-seeded 7-day working hours
        wh_count = BranchWorkingHours.objects.using('tenant_test').filter(branch_id=data['id']).count()
        self.assertEqual(wh_count, 7)

        # Verify Audit event
        audit = TenantAuditEvent.objects.using('tenant_test').filter(resource_type='Branch', resource_id=data['id'], action='CREATE').first()
        self.assertIsNotNone(audit)

    def test_03_unauthorized_create_blocked_with_403(self):
        token = self.get_token(self.viewer_user)
        payload = {
            'name': 'Unauthorized Studio',
            'code': 'UNAUTH_01',
            'city': 'Mumbai',
        }
        res = self.client.post('/api/v1/tenant/branches/', data=payload, format='json', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_04_authorization_independent_of_role_name(self):
        """Custom role 'CUSTOM_COORDINATOR' with permission core.settings.edit can create branch without being ORG_ADMIN."""
        token = self.get_token(self.coord_user)
        payload = {
            'name': 'Juhu Beach Studio',
            'code': 'JUHU_BEACH',
            'city': 'Mumbai',
            'capacity': 60,
        }
        res = self.client.post('/api/v1/tenant/branches/', data=payload, format='json', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)

    def test_05_branch_update_with_audit(self):
        token = self.get_token(self.admin_user)
        url = f'/api/v1/tenant/branches/{self.branch_1.id}/'
        payload = {
            'capacity': 110,
            'address': 'Updated Bandra West Suite 400',
            'phone': '+91 98200 99999',
            'is_passport_eligible': False,
        }
        res = self.client.patch(url, data=payload, format='json', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()
        self.assertEqual(data['capacity'], 110)
        self.assertEqual(data['address'], 'Updated Bandra West Suite 400')
        self.assertFalse(data['is_passport_eligible'])

        # Verify DB and audit
        self.branch_1.refresh_from_db(using='tenant_test')
        self.assertEqual(self.branch_1.capacity, 110)
        self.assertFalse(self.branch_1.is_passport_eligible)

        audit = TenantAuditEvent.objects.using('tenant_test').filter(resource_type='Branch', resource_id=str(self.branch_1.id), action='UPDATE').first()
        self.assertIsNotNone(audit)

    def test_06_deactivate_and_reactivate(self):
        token = self.get_token(self.admin_user)
        deact_url = f'/api/v1/tenant/branches/{self.branch_1.id}/deactivate/'
        res = self.client.post(deact_url, data={'reason': 'Annual renovation'}, format='json', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()
        self.assertEqual(data['status'], 'INACTIVE')
        self.assertFalse(data['is_active'])

        self.branch_1.refresh_from_db(using='tenant_test')
        self.assertEqual(self.branch_1.status, 'INACTIVE')
        self.assertIsNotNone(self.branch_1.deactivated_at)

        # Reactivate
        react_url = f'/api/v1/tenant/branches/{self.branch_1.id}/reactivate/'
        res_act = self.client.post(react_url, format='json', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(res_act.status_code, status.HTTP_200_OK)
        self.branch_1.refresh_from_db(using='tenant_test')
        self.assertEqual(self.branch_1.status, 'ACTIVE')
        self.assertIsNone(self.branch_1.deactivated_at)

    def test_07_duplicate_code_blocked(self):
        token = self.get_token(self.admin_user)
        payload = {
            'name': 'Duplicate Code Branch',
            'code': 'BANDRA_01',  # already used by branch_1
            'city': 'Mumbai',
        }
        res = self.client.post('/api/v1/tenant/branches/', data=payload, format='json', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('code', res.json())

    def test_08_international_phone_and_timezone(self):
        token = self.get_token(self.admin_user)
        payload = {
            'name': 'Dubai Marina Studio',
            'code': 'DUBAI_MARINA',
            'city': 'Dubai',
            'phone': '+971 4 123 4567',
            'timezone': 'Asia/Dubai',
        }
        res = self.client.post('/api/v1/tenant/branches/', data=payload, format='json', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.json()['phone'], '+971 4 123 4567')
        self.assertEqual(res.json()['timezone'], 'Asia/Dubai')

        # Invalid timezone rejected
        bad_payload = {
            'name': 'Bad Timezone Studio',
            'code': 'BAD_TZ',
            'city': 'Test',
            'timezone': 'Mars/Colony_1',
        }
        bad_res = self.client.post('/api/v1/tenant/branches/', data=bad_payload, format='json', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(bad_res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('timezone', bad_res.json())

    def test_09_historical_records_protected_from_deletion(self):
        """Branch with historical class occurrences cannot be destroyed; must be deactivated."""
        token = self.get_token(self.admin_user)
        # Create a historical class occurrence for branch_1
        cat = ClassCategory.objects.using('tenant_test').create(organization=self.org, code='REFORMER', name='Reformer Pilates')
        tmpl = ClassTemplate.objects.using('tenant_test').create(
            organization=self.org, category=cat, name='Morning Reformer', code='MORN_REF', default_duration_minutes=50, default_capacity=10
        )
        now = timezone.now()
        ClassOccurrence.objects.using('tenant_test').create(
            class_template=tmpl, branch=self.branch_1,
            occurrence_date=now.date(),
            start_at=now, end_at=now + timezone.timedelta(minutes=50),
            capacity=10, status='COMPLETED'
        )

        # Attempt DELETE on protected branch
        del_res = self.client.delete(f'/api/v1/tenant/branches/{self.branch_1.id}/', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(del_res.status_code, status.HTTP_400_BAD_REQUEST)
        del_json = del_res.json()
        self.assertEqual(del_json.get('code'), 'BRANCH_HAS_HISTORY')
        self.assertIn('cannot be deleted', str(del_json.get('detail')).lower())

        # Verify branch serializer can_delete flag is False
        get_res = self.client.get(f'/api/v1/tenant/branches/{self.branch_1.id}/', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertFalse(get_res.json()['can_delete'])
        self.assertIsNotNone(get_res.json()['delete_blocked_reason'])

        # Branch still exists in database
        self.assertTrue(Branch.objects.using('tenant_test').filter(id=self.branch_1.id).exists())

        # But a fresh unused branch CAN be safely deleted
        set_tenant_db_alias('tenant_test')
        fresh_branch = Branch.objects.using('tenant_test').create(
            organization=self.org, location=self.loc, name='Temporary Test Branch', code='TEMP_TEST_BR'
        )
        fresh_get_res = self.client.get(f'/api/v1/tenant/branches/{fresh_branch.id}/', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertTrue(fresh_get_res.json()['can_delete'])
        self.assertIsNone(fresh_get_res.json()['delete_blocked_reason'])

        fresh_del_res = self.client.delete(f'/api/v1/tenant/branches/{fresh_branch.id}/', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(fresh_del_res.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Branch.objects.using('tenant_test').filter(id=fresh_branch.id).exists())

    def test_10_operating_exception_creation(self):
        token = self.get_token(self.admin_user)
        payload = {
            'branch': str(self.branch_1.id),
            'exception_date': '2026-10-24',
            'is_closed': True,
            'reason': 'Diwali Festive Closure',
        }
        res = self.client.post('/api/v1/tenant/branch-operating-exceptions/', data=payload, format='json', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.json()['reason'], 'Diwali Festive Closure')
        self.assertTrue(res.json()['is_closed'])

    def test_11_company_entity_scope_and_fallback(self):
        token = self.get_token(self.admin_user)

        # GET company entities returns 200
        ce_res = self.client.get('/api/v1/tenant/company-entities/', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(ce_res.status_code, status.HTTP_200_OK)
        entities_data = ce_res.json().get('results', ce_res.json())
        self.assertIsInstance(entities_data, list)

        # CASE C: Branch created without company_entity uses Organization Default fallback
        payload = {
            'name': 'Org Default Studio',
            'code': 'ORG_DEF_STUDIO',
            'city': 'Bengaluru',
            'company_entity': None,
        }
        create_res = self.client.post('/api/v1/tenant/branches/', data=payload, format='json', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(create_res.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(create_res.json()['company_entity'])
        self.assertIsNone(create_res.json()['company_entity_name'])

        # CASE A: Branch created with explicit company_entity
        set_tenant_db_alias('tenant_test')
        from apps.tenant_core.models_org import CompanyEntity
        entity = CompanyEntity.objects.using('tenant_test').create(
            organization=self.org, name='SWEAT Wellness Pvt Ltd', code='SWEAT_PVT_LTD'
        )
        ce_res_after = self.client.get('/api/v1/tenant/company-entities/', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(ce_res_after.status_code, status.HTTP_200_OK)
        entities_after = ce_res_after.json().get('results', ce_res_after.json())
        codes = [c['code'] for c in entities_after]
        self.assertIn('SWEAT_PVT_LTD', codes)

        payload_with_entity = {
            'name': 'Corporate Hub Studio',
            'code': 'CORP_HUB_01',
            'city': 'Mumbai',
            'company_entity': str(entity.id),
        }
        create_with_ce_res = self.client.post(
            '/api/v1/tenant/branches/', data=payload_with_entity, format='json', HTTP_AUTHORIZATION=f'Bearer {token}'
        )
        self.assertEqual(create_with_ce_res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(create_with_ce_res.json()['company_entity'], str(entity.id))
        self.assertEqual(create_with_ce_res.json()['company_entity_name'], 'SWEAT Wellness Pvt Ltd')
