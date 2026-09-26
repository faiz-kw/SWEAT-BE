"""
tests/test_staff_vs_member_separation.py — Comprehensive Test Suite for Staff vs Member Domain Separation

Covers all 15 verification scenarios:
 1. Staff-only Admin does NOT appear in Member Directory
 2. Staff-only Sales user does NOT appear in Member Directory
 3. Staff-only Front Desk user does NOT appear in Member Directory
 4. Staff-only Trainer does NOT appear in Member Directory
 5. Genuine converted Member appears in Member Directory
 6. Manually-created genuine Member appears in Member Directory
 7. Member with no active Membership but valid historical Member/customer identity still appears
 8. Dual-role Staff + Member appears in Member Directory
 9. Staff-only profile cannot open Member 360 (HTTP 404)
10. Staff-only profile cannot call membership lifecycle actions (HTTP 404)
11. Member Directory count excludes staff (truthful member count)
12. Search for staff-only email returns 0 Member results
13. Administration Users (/api/v1/tenant/users/) still contains staff
14. Lead conversion creates/establishes correct member state (member_number & acquisition_source)
15. No duplicate user/customer identity created when existing TenantUser legitimately becomes Member
"""

import uuid
from decimal import Decimal
from datetime import date, timedelta
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from config.routers import set_tenant_db_alias
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.models_saas import SaasPlan, TenantSubscription, ProductModule, TenantModule
from apps.authentication.views import _build_tenant_token
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser, UserBranch
from apps.tenant_core.models_workforce import UserProfile, EmployeeProfile, TrainerProfile, SalesProfile
from apps.tenant_core.models_catalog import ProgramCategory, Program, Package, PackageVersion, PackagePrice
from apps.tenant_core.models_memberships import Membership
from apps.tenant_core.models_crm import Lead, LeadConversion
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
from apps.tenant_core.services_crm import CRMLeadService, LeadConversionService
from apps.tenant_core.views_members import MemberViewSet


class StaffVsMemberSeparationTests(TestCase):
    databases = '__all__'

    def setUp(self):
        super().setUp()
        set_tenant_db_alias('tenant_test')
        self.client = APIClient()

        # 1. Master DB Setup
        self.tenant = Tenant.objects.using('default').create(
            code='MEM-SEP-TENANT',
            name='Member Separation Tenant',
            slug='member-sep-tenant',
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
            name='Enterprise Plan',
            code='ENT-PLAN',
            tier='ENTERPRISE',
            status='ACTIVE',
        )
        self.sub = TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
        )
        for m_code in ['core', 'crm', 'finance', 'memberships']:
            pm, _ = ProductModule.objects.using('default').get_or_create(
                code=m_code, defaults={'name': f'{m_code.capitalize()} Module', 'status': 'ACTIVE'}
            )
            TenantModule.objects.using('default').create(
                tenant=self.tenant, module=pm, is_enabled=True, availability_mode='ALL_BRANCHES'
            )

        # 2. Tenant DB Setup
        self.org = Organization.objects.using('tenant_test').create(
            code='SEP-ORG',
            name='Separation Test Org',
            status='ACTIVE',
        )
        self.loc = Location.objects.using('tenant_test').create(
            organization=self.org,
            code='LOC-SEP',
            name='Separation Location',
            status='ACTIVE',
        )
        self.branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            code='BR-SEP',
            name='Separation Branch',
            status='ACTIVE',
        )

        # 3. Superuser Staff for Auth
        self.admin_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='admin@sweat.test',
            phone='+919876543210',
            first_name='Admin',
            last_name='User',
            display_name='Admin User',
            user_type='STAFF',
            status='ACTIVE',
            home_branch=self.branch,
        )
        self.admin_profile = UserProfile.objects.using('tenant_test').create(
            user=self.admin_user,
            preferred_branch=self.branch,
            joining_date=timezone.now().date(),
            member_type=None,
            member_status=None,
            member_number=None,
            acquisition_source=None,
        )

        # 4. RBAC Setup for Admin User
        self.mod_core, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(
            module_code='core', defaults={'name': 'Core', 'status': 'ACTIVE', 'is_enabled': True, 'source_module_id': uuid.uuid4()}
        )
        self.sub_users, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=self.mod_core, submodule_code='users', defaults={'name': 'Users', 'status': 'ACTIVE', 'is_enabled': True, 'source_submodule_id': uuid.uuid4()}
        )
        self.role_admin = Role.objects.using('tenant_test').create(
            organization=self.org,
            code='ORG_ADMIN',
            name='Org Admin',
            scope='ORG',
            is_active=True,
            status='ACTIVE',
        )
        RoleModuleAccess.objects.using('tenant_test').get_or_create(role=self.role_admin, module=self.mod_core, defaults={'can_access': True})
        RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(role=self.role_admin, submodule=self.sub_users, defaults={'can_access': True})

        pset = RolePermissionSet.objects.using('tenant_test').create(
            role=self.role_admin, name='Admin Permissions', is_active=True
        )
        for perm_code in ['core.users.view', 'core.users.create', 'core.users.edit', 'core.users.delete', 'finance.payments.create']:
            p, _ = Permission.objects.using('tenant_test').get_or_create(
                permission_code=perm_code,
                defaults={'module': self.mod_core, 'submodule': self.sub_users, 'action': perm_code.split('.')[-1], 'label': perm_code}
            )
            RolePermissionSetItem.objects.using('tenant_test').create(
                permission_set=pset, permission=p, granted=True
            )

        RoleAssignment.objects.using('tenant_test').create(
            user=self.admin_user, role=self.role_admin, organization=self.org, status='ACTIVE', is_active=True
        )

        # 5. Auth token
        refresh = _build_tenant_token(user=self.admin_user, tenant=self.tenant, db_alias='tenant_test')
        self.token = str(refresh.access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token}')

        # 5. Catalog setup for memberships
        self.prog_cat = ProgramCategory.objects.using('tenant_test').create(
            organization=self.org, code='CAT-PIL', name='Pilates Category', status='ACTIVE'
        )
        self.prog = Program.objects.using('tenant_test').create(
            organization=self.org, category=self.prog_cat, code='PIL-GEN', name='Pilates', status='ACTIVE'
        )
        self.pkg = Package.objects.using('tenant_test').create(
            organization=self.org, program=self.prog, name='Pilates Unlimited', code='PKG-PIL', status='ACTIVE'
        )
        self.pkg_v = PackageVersion.objects.using('tenant_test').create(
            package=self.pkg, version_number=1, duration_unit='DAYS', duration_value=30, effective_from=timezone.now(), created_by_user=self.admin_user, status='ACTIVE'
        )
        self.pkg_price = PackagePrice.objects.using('tenant_test').create(
            package_version=self.pkg_v, branch=self.branch, base_price=Decimal('5000.00'), currency='INR', effective_from=timezone.now(), created_by_user=self.admin_user, status='ACTIVE'
        )

    # -------------------------------------------------------------------------
    # TEST 1: Staff-only Admin does NOT appear in Member Directory
    # -------------------------------------------------------------------------
    def test_01_staff_only_admin_excluded_from_member_directory(self):
        res = self.client.get('/api/v1/tenant/members/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        ids = [m['id'] for m in res.data['results']]
        self.assertNotIn(str(self.admin_profile.id), ids)

    # -------------------------------------------------------------------------
    # TEST 2: Staff-only Sales user does NOT appear
    # -------------------------------------------------------------------------
    def test_02_staff_only_sales_user_excluded(self):
        sales_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='sales@sweat.test',
            first_name='Sam',
            last_name='Sales',
            display_name='Sam Sales',
            user_type='STAFF',
            status='ACTIVE',
            home_branch=self.branch,
        )
        sales_profile = UserProfile.objects.using('tenant_test').create(
            user=sales_user,
            preferred_branch=self.branch,
            member_number=None,
            acquisition_source=None,
        )
        sales_emp = EmployeeProfile.objects.using('tenant_test').create(
            user_profile=sales_profile,
            organization=self.org,
            employee_code='EMP-SALES-1',
            designation='Sales Representative',
            employment_status='ACTIVE',
        )
        SalesProfile.objects.using('tenant_test').create(
            employee_profile=sales_emp,
            sales_code='SALES01',
            sales_status='ACTIVE',
        )

        res = self.client.get('/api/v1/tenant/members/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        ids = [m['id'] for m in res.data['results']]
        self.assertNotIn(str(sales_profile.id), ids)

    # -------------------------------------------------------------------------
    # TEST 3: Staff-only Front Desk user does NOT appear
    # -------------------------------------------------------------------------
    def test_03_staff_only_frontdesk_user_excluded(self):
        fd_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='frontdesk@sweat.test',
            first_name='Fiona',
            last_name='FrontDesk',
            display_name='Fiona FrontDesk',
            user_type='STAFF',
            status='ACTIVE',
            home_branch=self.branch,
        )
        fd_profile = UserProfile.objects.using('tenant_test').create(
            user=fd_user,
            preferred_branch=self.branch,
            member_number=None,
            acquisition_source=None,
        )
        EmployeeProfile.objects.using('tenant_test').create(
            user_profile=fd_profile,
            organization=self.org,
            employee_code='EMP-FD-1',
            designation='Front Desk Executive',
            employment_status='ACTIVE',
        )

        res = self.client.get('/api/v1/tenant/members/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        ids = [m['id'] for m in res.data['results']]
        self.assertNotIn(str(fd_profile.id), ids)

    # -------------------------------------------------------------------------
    # TEST 4: Staff-only Trainer does NOT appear
    # -------------------------------------------------------------------------
    def test_04_staff_only_trainer_excluded(self):
        trainer_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='trainer@sweat.test',
            first_name='Tom',
            last_name='Trainer',
            display_name='Tom Trainer',
            user_type='STAFF',
            status='ACTIVE',
            home_branch=self.branch,
        )
        trainer_profile = UserProfile.objects.using('tenant_test').create(
            user=trainer_user,
            preferred_branch=self.branch,
            member_number=None,
            acquisition_source=None,
        )
        trainer_emp = EmployeeProfile.objects.using('tenant_test').create(
            user_profile=trainer_profile,
            organization=self.org,
            employee_code='EMP-TRAIN-1',
            designation='Fitness Trainer',
            employment_status='ACTIVE',
        )
        TrainerProfile.objects.using('tenant_test').create(
            employee_profile=trainer_emp,
            trainer_status='ACTIVE',
        )

        res = self.client.get('/api/v1/tenant/members/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        ids = [m['id'] for m in res.data['results']]
        self.assertNotIn(str(trainer_profile.id), ids)

    # -------------------------------------------------------------------------
    # TEST 5: Genuine converted Member appears
    # -------------------------------------------------------------------------
    def test_05_genuine_converted_member_appears(self):
        member_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='sarah.connor@sweat.test',
            first_name='Sarah',
            last_name='Connor',
            display_name='Sarah Connor',
            user_type='MEMBER',
            status='ACTIVE',
            home_branch=self.branch,
        )
        member_profile = UserProfile.objects.using('tenant_test').create(
            user=member_user,
            member_number='MEM-00101',
            preferred_branch=self.branch,
            acquisition_source='WEBSITE_FORM',
            member_status='ACTIVE',
        )

        res = self.client.get('/api/v1/tenant/members/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        ids = [m['id'] for m in res.data['results']]
        self.assertIn(str(member_profile.id), ids)
        member_data = next(m for m in res.data['results'] if m['id'] == str(member_profile.id))
        self.assertEqual(member_data['name'], 'Sarah Connor')
        self.assertEqual(member_data['membership_number'], 'MEM-00101')

    # -------------------------------------------------------------------------
    # TEST 6: Manually-created genuine Member appears
    # -------------------------------------------------------------------------
    def test_06_manually_created_genuine_member_appears(self):
        payload = {
            'name': 'John Matrix',
            'email': 'john.matrix@sweat.test',
            'phone': '+919999988888',
            'gender': 'M',
            'status': 'Active',
            'location': str(self.branch.id),
        }
        res = self.client.post('/api/v1/tenant/members/', data=payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        new_id = res.data['id']
        self.assertTrue(res.data['membership_number'].startswith('MEM-'))

        # Verify appears in directory list
        list_res = self.client.get('/api/v1/tenant/members/')
        self.assertEqual(list_res.status_code, status.HTTP_200_OK)
        ids = [m['id'] for m in list_res.data['results']]
        self.assertIn(str(new_id), ids)

    # -------------------------------------------------------------------------
    # TEST 7: Historical Member without active plan still appears
    # -------------------------------------------------------------------------
    def test_07_historical_member_without_active_plan_still_appears(self):
        hist_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='kyle.reese@sweat.test',
            first_name='Kyle',
            last_name='Reese',
            display_name='Kyle Reese',
            user_type='MEMBER',
            status='ACTIVE',
            home_branch=self.branch,
        )
        hist_profile = UserProfile.objects.using('tenant_test').create(
            user=hist_user,
            member_number='MEM-00099',
            preferred_branch=self.branch,
            acquisition_source='MANUAL_CREATE',
            member_status='INACTIVE',
        )

        res = self.client.get('/api/v1/tenant/members/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        ids = [m['id'] for m in res.data['results']]
        self.assertIn(str(hist_profile.id), ids)

    # -------------------------------------------------------------------------
    # TEST 8: Dual-role Staff + Member appears
    # -------------------------------------------------------------------------
    def test_08_dual_role_staff_plus_member_appears(self):
        trainer_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='rahul.trainer@sweat.test',
            first_name='Rahul',
            last_name='Sharma',
            display_name='Rahul Sharma',
            user_type='STAFF',
            status='ACTIVE',
            home_branch=self.branch,
        )
        # Rahul has a staff TrainerProfile AND buys a customer membership
        rahul_profile = UserProfile.objects.using('tenant_test').create(
            user=trainer_user,
            member_number='MEM-RAHUL01',
            preferred_branch=self.branch,
            acquisition_source='STAFF_MEMBERSHIP_PURCHASE',
            member_status='ACTIVE',
        )
        rahul_emp = EmployeeProfile.objects.using('tenant_test').create(
            user_profile=rahul_profile,
            organization=self.org,
            employee_code='EMP-RAHUL-1',
            designation='Pilates Trainer',
            employment_status='ACTIVE',
        )
        TrainerProfile.objects.using('tenant_test').create(
            employee_profile=rahul_emp,
            trainer_status='ACTIVE',
        )
        Membership.objects.using('tenant_test').create(
            user_profile=rahul_profile,
            home_branch=self.branch,
            purchase_branch=self.branch,
            package=self.pkg,
            package_version=self.pkg_v,
            program=self.prog,
            membership_number='MEM-RAHUL01',
            status='ACTIVE',
            start_date=timezone.now().date(),
            end_date=timezone.now().date() + timedelta(days=30),
        )

        res = self.client.get('/api/v1/tenant/members/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        ids = [m['id'] for m in res.data['results']]
        self.assertIn(str(rahul_profile.id), ids)

    # -------------------------------------------------------------------------
    # TEST 9: Staff-only profile cannot open Member 360 (returns HTTP 404)
    # -------------------------------------------------------------------------
    def test_09_staff_only_profile_cannot_open_member_360(self):
        res = self.client.get(f'/api/v1/tenant/members/{self.admin_profile.id}/360/')
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    # -------------------------------------------------------------------------
    # TEST 10: Staff-only profile cannot call membership lifecycle actions
    # -------------------------------------------------------------------------
    def test_10_staff_only_profile_cannot_call_lifecycle_actions(self):
        # Renew
        res = self.client.post(f'/api/v1/tenant/members/{self.admin_profile.id}/renew/', data={'months': 1}, format='json')
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

        # Freeze
        res = self.client.post(f'/api/v1/tenant/members/{self.admin_profile.id}/freeze/', data={'days': 7}, format='json')
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

        # Cancel
        res = self.client.post(f'/api/v1/tenant/members/{self.admin_profile.id}/cancel/', data={'reason': 'test'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

        # Check-in
        res = self.client.post(f'/api/v1/tenant/members/{self.admin_profile.id}/check-in/', data={'location': str(self.branch.id)}, format='json')
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    # -------------------------------------------------------------------------
    # TEST 11: Member Directory count excludes staff
    # -------------------------------------------------------------------------
    def test_11_member_directory_count_excludes_staff(self):
        # Create 2 genuine members
        for i in range(2):
            u = TenantUser.objects.using('tenant_test').create(
                organization=self.org,
                email=f'customer{i}@sweat.test',
                first_name=f'Cust{i}',
                last_name='Test',
                user_type='MEMBER',
            )
            UserProfile.objects.using('tenant_test').create(
                user=u,
                member_number=f'MEM-TEST-{i}',
                acquisition_source='MANUAL_CREATE',
            )

        # Create 3 staff users
        for i in range(3):
            u = TenantUser.objects.using('tenant_test').create(
                organization=self.org,
                email=f'employee{i}@sweat.test',
                first_name=f'Staff{i}',
                last_name='Emp',
                user_type='STAFF',
            )
            UserProfile.objects.using('tenant_test').create(
                user=u,
                member_number=None,
                acquisition_source=None,
            )

        res = self.client.get('/api/v1/tenant/members/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        # Total count must be 2, strictly excluding the 3 staff users and the setUp admin_user
        self.assertEqual(res.data['count'], 2)

    # -------------------------------------------------------------------------
    # TEST 12: Search for staff-only email returns 0 Member results
    # -------------------------------------------------------------------------
    def test_12_search_for_staff_only_email_returns_zero(self):
        res = self.client.get(f'/api/v1/tenant/members/?search={self.admin_user.email}')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['count'], 0)
        self.assertEqual(len(res.data['results']), 0)

    # -------------------------------------------------------------------------
    # TEST 13: Administration Users (/api/v1/tenant/users/) still contains staff
    # -------------------------------------------------------------------------
    def test_13_admin_users_still_contains_staff(self):
        res = self.client.get('/api/v1/tenant/users/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        emails = [u['email'] for u in (res.data if isinstance(res.data, list) else res.data.get('results', []))]
        self.assertIn(self.admin_user.email, emails)

    # -------------------------------------------------------------------------
    # TEST 14: Lead conversion creates/establishes correct member state
    # -------------------------------------------------------------------------
    def test_14_lead_conversion_establishes_canonical_member_state(self):
        lead = Lead.objects.using('tenant_test').create(
            organization=self.org,
            branch=self.branch,
            first_name='Prospect',
            last_name='Lead',
            email_normalized='prospect.lead@sweat.test',
            phone_normalized='+919888877777',
            current_status='NEW_LEAD',
        )

        profile, created = LeadConversionService.resolve_or_create_member_identity(
            lead=lead,
            branch=self.branch,
            actor_user=self.admin_user,
            db_alias='tenant_test',
        )

        self.assertTrue(created)
        self.assertIsNotNone(profile.member_number)
        self.assertTrue(profile.member_number.startswith('MEM-'))
        self.assertEqual(profile.acquisition_source, 'CRM_CONVERSION')
        self.assertTrue(profile.is_customer_member)

        # Ensure appears in Member Directory
        res = self.client.get('/api/v1/tenant/members/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        ids = [m['id'] for m in res.data['results']]
        self.assertIn(str(profile.id), ids)

    # -------------------------------------------------------------------------
    # TEST 15: No duplicate user identity created when existing TenantUser becomes Member
    # -------------------------------------------------------------------------
    def test_15_no_duplicate_identity_when_existing_tenant_user_becomes_member(self):
        existing_staff = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='dual.person@sweat.test',
            phone='+919111122222',
            first_name='Dual',
            last_name='Person',
            display_name='Dual Person',
            user_type='STAFF',
            status='ACTIVE',
        )
        existing_profile = UserProfile.objects.using('tenant_test').create(
            user=existing_staff,
            preferred_branch=self.branch,
            member_number=None,
            acquisition_source=None,
        )

        # Before becoming member: not in directory
        self.assertFalse(existing_profile.is_customer_member)
        pre_res = self.client.get('/api/v1/tenant/members/')
        self.assertNotIn(str(existing_profile.id), [m['id'] for m in pre_res.data['results']])

        # Now add them as a customer member via Add Member flow
        payload = {
            'name': 'Dual Person',
            'email': 'dual.person@sweat.test',
            'phone': '+919111122222',
            'location': str(self.branch.id),
        }
        res = self.client.post('/api/v1/tenant/members/', data=payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        # Verify no duplicate TenantUser was created
        set_tenant_db_alias('tenant_test')
        user_count = TenantUser.objects.using('tenant_test').filter(email='dual.person@sweat.test').count()
        self.assertEqual(user_count, 1)

        # Verify same profile was updated with member markers
        updated_profile = UserProfile.objects.using('tenant_test').get(id=existing_profile.id)
        self.assertIsNotNone(updated_profile.member_number)
        self.assertTrue(updated_profile.member_number.startswith('MEM-'))
        self.assertEqual(updated_profile.acquisition_source, 'MANUAL_CREATE')
        self.assertTrue(updated_profile.is_customer_member)

        # Verify now appears in Member Directory
        post_res = self.client.get('/api/v1/tenant/members/')
        self.assertIn(str(existing_profile.id), [m['id'] for m in post_res.data['results']])
