"""
Targeted RBAC Verification Tests:
1. ORG_ADMIN class configuration allowed
2. Branch Manager branch-limited class operations
3. Front Desk class occurrence view
4. Front Desk create booking
5. Front Desk other branch denied
6. Trainer assigned class view
7. Trainer inappropriate admin action denied
8. Member eligible occurrence view
9. Member own booking create
10. Member ClassTemplate create denied (403)
11. Member BookingPolicy modify denied (403)
12. Custom role with same permission works regardless of role name
13. Dynamic permission revocation immediately returns 403
14. Tenant isolation
"""

import uuid
from datetime import date, time, timedelta, datetime
from decimal import Decimal
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework import status

from apps.authentication.views import _build_tenant_token
from apps.master.models import Tenant, TenantDataSource, ProductModule, TenantModule, SaasPlan, TenantSubscription
from apps.tenant_core.context import set_tenant_db_alias
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_govern import BranchWorkingHours
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_rbac import (
    Role, RoleAssignment, ModuleCatalog, SubmoduleCatalog,
    Permission, RolePermissionSet, RolePermissionSetItem,
    RoleModuleAccess, RoleSubmoduleAccess,
)
from apps.tenant_core.models_workforce import (
    UserProfile, EmployeeProfile, TrainerProfile,
)
from apps.tenant_core.models_classes import (
    ClassCategory, ClassTemplate, ClassOccurrence,
)
from apps.tenant_core.models_bookings import (
    BookingPolicySet, Booking,
)
from apps.tenant_core.rbac_defaults import sync_default_role_permissions


class RolePermissionsTargetedTestCase(APITestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')

        # 1. Master Tenant Setup
        self.tenant = Tenant.objects.using('default').create(
            code='SWEAT-RBAC',
            name='Sweat RBAC Test Gym',
            slug='sweat-rbac-gym',
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
            name='RBAC Enterprise Plan',
            code='RBAC-PLAN',
            tier='ENTERPRISE',
            status='ACTIVE',
        )
        self.sub = TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
        )
        for mod_code, mod_name in [
            ('core', 'Core'),
            ('ops', 'Operations'),
            ('members', 'Members'),
            ('crm', 'CRM'),
            ('finance', 'Finance'),
            ('reports', 'Reports'),
            ('classes', 'Classes'),
            ('bookings', 'Bookings'),
        ]:
            pm, _ = ProductModule.objects.using('default').get_or_create(
                code=mod_code,
                defaults={'name': mod_name, 'status': 'ACTIVE'},
            )
            TenantModule.objects.using('default').create(
                tenant=self.tenant,
                module=pm,
                is_enabled=True,
                availability_mode='ALL_BRANCHES',
            )

        # 2. Tenant Org & Branches
        self.org = Organization.objects.using('tenant_test').create(
            code='SWEAT-ORG',
            name='Sweat Organization',
            status='ACTIVE',
        )
        self.loc = Location.objects.using('tenant_test').create(
            organization=self.org,
            code='LOC-MAIN',
            name='Main Location',
            status='ACTIVE',
        )
        self.branch_a = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            code='BR-ALPHA',
            name='Branch Alpha',
            status='ACTIVE',
        )
        self.branch_b = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            code='BR-BETA',
            name='Branch Beta',
            status='ACTIVE',
        )

        # 3. Seed Module, Submodule, and Permission Catalogs in Tenant DB
        catalog_defs = [
            ('core', 'Core System', [
                ('settings', 'Settings', ['view', 'edit']),
                ('users', 'Users', ['view', 'create', 'edit', 'delete']),
            ]),
            ('ops', 'Operations', [
                ('classes', 'Classes & Scheduling', ['view', 'create', 'edit', 'delete']),
                ('bookings', 'Bookings & Waitlist', ['view', 'create', 'edit', 'delete', 'cancel', 'reschedule', 'checkin']),
                ('calendar', 'Calendar', ['view', 'create', 'edit']),
                ('trainers', 'Trainers', ['view', 'create', 'edit']),
            ]),
            ('members', 'Members', [
                ('attendance', 'Attendance & Access', ['view', 'create', 'edit', 'record']),
                ('client-360', 'Client 360', ['view', 'create', 'edit']),
                ('memberships', 'Memberships', ['view', 'create', 'edit']),
            ]),
            ('crm', 'CRM', [
                ('leads', 'Leads', ['view', 'create', 'edit', 'delete']),
                ('pipeline', 'Pipeline', ['view', 'create', 'edit']),
                ('trials', 'Trials', ['view', 'create', 'edit']),
                ('follow-ups', 'Follow-ups', ['view', 'create', 'edit']),
            ]),
            ('reports', 'Reports', [
                ('business', 'Business Reports', ['view']),
                ('sales', 'Sales Reports', ['view']),
                ('members', 'Member Reports', ['view']),
                ('trainers', 'Trainer Reports', ['view']),
            ]),
            ('finance', 'Finance', [
                ('invoices', 'Invoices', ['view', 'create', 'edit', 'delete']),
                ('payments', 'Payments', ['view', 'create', 'edit', 'delete']),
                ('refunds', 'Refunds', ['view', 'create', 'edit']),
                ('outstanding', 'Outstanding Dues', ['view', 'create', 'edit']),
                ('expenses', 'Expenses', ['view', 'create', 'edit']),
                ('revenue', 'Revenue', ['view']),
            ]),
        ]

        for mod_code, mod_name, submods in catalog_defs:
            mc, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(
                module_code=mod_code,
                defaults={'name': mod_name, 'is_enabled': True},
            )
            for sub_code, sub_name, actions in submods:
                sc, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
                    module=mc,
                    submodule_code=sub_code,
                    defaults={'name': sub_name, 'is_enabled': True},
                )
                for act in actions:
                    pcode = f"{mod_code}.{sub_code}.{act}"
                    Permission.objects.using('tenant_test').get_or_create(
                        permission_code=pcode,
                        defaults={
                            'module': mc,
                            'submodule': sc,
                            'action': act,
                            'label': f"{act.capitalize()} {sub_name}",
                        },
                    )

        # 4. Provision Canonical Role Defaults
        sync_default_role_permissions(db_alias='tenant_test', org=self.org, overwrite_custom=True)

        # 5. Fetch Synced Canonical Roles
        self.role_org_admin = Role.objects.using('tenant_test').get(organization=self.org, code='ORG_ADMIN')
        self.role_branch_mgr = Role.objects.using('tenant_test').get(organization=self.org, code='BRANCH_MANAGER')
        self.role_front_desk = Role.objects.using('tenant_test').get(organization=self.org, code='FRONT_DESK')
        self.role_trainer = Role.objects.using('tenant_test').get(organization=self.org, code='TRAINER')
        self.role_member = Role.objects.using('tenant_test').get(organization=self.org, code='MEMBER')

        # 5. Create Test Users
        # Org Admin
        self.admin_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='admin@sweat.test',
            first_name='Admin',
            last_name='User',
            status='ACTIVE',
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.admin_user,
            role=self.role_org_admin,
            organization=self.org,
            scope_type='ORGANIZATION',
            is_active=True,
        )

        # Branch Manager (Branch A)
        self.bm_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='bm@sweat.test',
            first_name='Branch',
            last_name='Manager',
            home_branch=self.branch_a,
            status='ACTIVE',
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.bm_user,
            role=self.role_branch_mgr,
            organization=self.org,
            branch=self.branch_a,
            scope_type='BRANCH',
            is_active=True,
        )

        # Front Desk (Branch A)
        self.fd_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='fd@sweat.test',
            first_name='Front',
            last_name='Desk',
            home_branch=self.branch_a,
            status='ACTIVE',
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.fd_user,
            role=self.role_front_desk,
            organization=self.org,
            branch=self.branch_a,
            scope_type='BRANCH',
            is_active=True,
        )

        # Trainer User
        self.trainer_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='trainer@sweat.test',
            first_name='Coach',
            last_name='Alex',
            home_branch=self.branch_a,
            status='ACTIVE',
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.trainer_user,
            role=self.role_trainer,
            organization=self.org,
            branch=self.branch_a,
            scope_type='BRANCH',
            is_active=True,
        )
        self.trainer_up = UserProfile.objects.using('tenant_test').create(
            user=self.trainer_user,
            first_name_snapshot='Coach',
            last_name_snapshot='Alex',
            member_status='ACTIVE',
        )
        self.trainer_ep = EmployeeProfile.objects.using('tenant_test').create(
            user_profile=self.trainer_up,
            organization=self.org,
            employee_code='EMP-001',
            employment_status='ACTIVE',
        )
        self.trainer_tp = TrainerProfile.objects.using('tenant_test').create(
            employee_profile=self.trainer_ep,
            trainer_code='TR-001',
            trainer_status='ACTIVE',
        )

        # Member User
        self.member_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='member@sweat.test',
            first_name='Jane',
            last_name='Member',
            home_branch=self.branch_a,
            status='ACTIVE',
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.member_user,
            role=self.role_member,
            organization=self.org,
            branch=self.branch_a,
            scope_type='BRANCH',
            is_active=True,
        )
        self.member_up = UserProfile.objects.using('tenant_test').create(
            user=self.member_user,
            first_name_snapshot='Jane',
            last_name_snapshot='Member',
            member_status='ACTIVE',
        )

        # 6. Seed Operational Data
        self.cat = ClassCategory.objects.using('tenant_test').create(
            organization=self.org,
            code='CAT-YOGA',
            name='Yoga & Flow',
            status='ACTIVE',
        )
        self.tpl = ClassTemplate.objects.using('tenant_test').create(
            organization=self.org,
            category=self.cat,
            code='TPL-VINYASA',
            name='Vinyasa Morning',
            default_duration_minutes=60,
            default_capacity=20,
            allow_booking=True,
            status='ACTIVE',
        )
        now = timezone.now()
        start_a = now + timedelta(days=1)
        end_a = start_a + timedelta(hours=1)
        self.occ_a = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.tpl,
            branch=self.branch_a,
            occurrence_date=start_a.date(),
            start_at=start_a,
            end_at=end_a,
            capacity=20,
            status='SCHEDULED',
        )
        self.occ_b = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.tpl,
            branch=self.branch_b,
            occurrence_date=start_a.date(),
            start_at=start_a,
            end_at=end_a,
            capacity=20,
            status='SCHEDULED',
        )
        self.policy = BookingPolicySet.objects.using('tenant_test').create(
            organization=self.org,
            version_number=1,
            rule_behavior='OPERATIONAL',
            status='ACTIVE',
        )

    def _auth(self, user):
        token = _build_tenant_token(user=user, tenant=self.tenant, db_alias='tenant_test')
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {str(token.access_token)}')

    # 1. ORG_ADMIN class configuration allowed
    def test_01_org_admin_class_config_allowed(self):
        self._auth(self.admin_user)
        res = self.client.post('/api/v1/tenant/class-categories/', {
            'code': 'CAT-PILATES',
            'name': 'Pilates Core',
            'status': 'ACTIVE',
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

    # 2. Branch Manager branch-limited class operations
    def test_02_branch_manager_branch_scoped_occurrences(self):
        self._auth(self.bm_user)
        res = self.client.get('/api/v1/tenant/class-occurrences/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        # Should only see Branch A occurrence
        results = res.data.get('results', res.data)
        ids = [item['id'] for item in results]
        self.assertIn(str(self.occ_a.id), ids)
        self.assertNotIn(str(self.occ_b.id), ids)

    # 3. Front Desk class occurrence view
    def test_03_front_desk_occurrence_view(self):
        self._auth(self.fd_user)
        res = self.client.get('/api/v1/tenant/class-occurrences/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        results = res.data.get('results', res.data)
        ids = [item['id'] for item in results]
        self.assertIn(str(self.occ_a.id), ids)
        self.assertNotIn(str(self.occ_b.id), ids)

    # 4. Front Desk create booking at assigned branch
    def test_04_front_desk_create_booking(self):
        self._auth(self.fd_user)
        res = self.client.post('/api/v1/tenant/bookings/', {
            'occurrence': str(self.occ_a.id),
            'user_profile': str(self.member_up.id),
            'booking_type': 'CLASS',
            'status': 'CONFIRMED',
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

    # 5. Front Desk other branch denied
    def test_05_front_desk_other_branch_denied(self):
        self._auth(self.fd_user)
        res = self.client.post('/api/v1/tenant/bookings/', {
            'occurrence': str(self.occ_b.id),
            'user_profile': str(self.member_up.id),
            'booking_type': 'CLASS',
            'status': 'CONFIRMED',
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    # 6. Trainer occurrence view
    def test_06_trainer_assigned_class_view(self):
        self._auth(self.trainer_user)
        res = self.client.get('/api/v1/tenant/class-occurrences/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    # 7. Trainer inappropriate admin action denied
    def test_07_trainer_admin_action_denied(self):
        self._auth(self.trainer_user)
        res = self.client.post('/api/v1/tenant/class-categories/', {
            'code': 'CAT-HIIT',
            'name': 'HIIT Cardio',
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    # 8. Member eligible occurrence view
    def test_08_member_eligible_occurrence_view(self):
        self._auth(self.member_user)
        res = self.client.get('/api/v1/tenant/class-occurrences/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        results = res.data.get('results', res.data)
        ids = [item['id'] for item in results]
        self.assertIn(str(self.occ_a.id), ids)

    # 9. Member own booking create
    def test_09_member_own_booking_create(self):
        self._auth(self.member_user)
        res = self.client.post('/api/v1/tenant/bookings/', {
            'occurrence': str(self.occ_a.id),
            'user_profile': str(self.member_up.id),
            'booking_type': 'CLASS',
            'status': 'CONFIRMED',
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

    # 10. Member ClassTemplate create denied
    def test_10_member_class_template_create_denied(self):
        self._auth(self.member_user)
        res = self.client.post('/api/v1/tenant/class-templates/', {
            'code': 'TPL-HACK',
            'name': 'Hacked Class',
            'category': str(self.cat.id),
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    # 11. Member BookingPolicy modify denied
    def test_11_member_booking_policy_modify_denied(self):
        self._auth(self.member_user)
        res = self.client.post('/api/v1/tenant/booking-policy-sets/', {
            'rule_behavior': 'OPERATIONAL',
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    # 12. Custom role with same permission works regardless of role name
    def test_12_custom_role_with_same_permission_works(self):
        custom_role = Role.objects.using('tenant_test').create(
            organization=self.org,
            name='VIP Concierge Assistant',
            code='VIP_CONCIERGE',
            is_system_role=False,
            scope='BRANCH',
            is_active=True,
        )
        custom_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='concierge@sweat.test',
            first_name='VIP',
            last_name='Staff',
            home_branch=self.branch_a,
            status='ACTIVE',
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=custom_user,
            role=custom_role,
            organization=self.org,
            branch=self.branch_a,
            scope_type='BRANCH',
            is_active=True,
        )
        # Grant ops.bookings submodule and ops.bookings.view / create
        mod = ModuleCatalog.objects.using('tenant_test').get(module_code='ops')
        sub = SubmoduleCatalog.objects.using('tenant_test').get(module=mod, submodule_code='bookings')
        RoleModuleAccess.objects.using('tenant_test').create(role=custom_role, module=mod, can_access=True)
        RoleSubmoduleAccess.objects.using('tenant_test').create(role=custom_role, submodule=sub, can_access=True)
        pset = RolePermissionSet.objects.using('tenant_test').create(role=custom_role, organization=self.org, name='VIP Perms')
        p_view = Permission.objects.using('tenant_test').get(permission_code='ops.bookings.view')
        p_create = Permission.objects.using('tenant_test').get(permission_code='ops.bookings.create')
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=pset, permission=p_view, granted=True)
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=pset, permission=p_create, granted=True)

        self._auth(custom_user)
        # Should be able to view bookings
        res = self.client.get('/api/v1/tenant/bookings/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        # Should be able to create booking
        res = self.client.post('/api/v1/tenant/bookings/', {
            'occurrence': str(self.occ_a.id),
            'user_profile': str(self.member_up.id),
            'booking_type': 'CLASS',
            'status': 'CONFIRMED',
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

    # 13. Dynamic permission revocation immediately returns 403
    def test_13_dynamic_permission_revocation_returns_403(self):
        self._auth(self.fd_user)
        # 1st attempt: FD can create booking
        res1 = self.client.post('/api/v1/tenant/bookings/', {
            'occurrence': str(self.occ_a.id),
            'user_profile': str(self.member_up.id),
            'booking_type': 'CLASS',
            'status': 'CONFIRMED',
        }, format='json')
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)

        # Admin disables ops.bookings.create for FRONT_DESK
        pset = RolePermissionSet.objects.using('tenant_test').get(role=self.role_front_desk)
        p_create = Permission.objects.using('tenant_test').get(permission_code='ops.bookings.create')
        item = RolePermissionSetItem.objects.using('tenant_test').get(permission_set=pset, permission=p_create)
        item.granted = False
        item.save(using='tenant_test')

        # 2nd attempt with same user / token: immediately 403
        res2 = self.client.post('/api/v1/tenant/bookings/', {
            'occurrence': str(self.occ_a.id),
            'user_profile': str(self.member_up.id),
            'booking_type': 'CLASS',
            'status': 'CONFIRMED',
        }, format='json')
        self.assertEqual(res2.status_code, status.HTTP_403_FORBIDDEN)

    # 14. Tenant Isolation
    def test_14_tenant_isolation(self):
        # Create second tenant
        other_tenant = Tenant.objects.using('default').create(
            code='OTHER-TENANT',
            name='Other Gym',
            slug='other-gym',
            status='ACTIVE',
        )
        other_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='intruder@other.test',
            first_name='Intruder',
            last_name='User',
            status='ACTIVE',
        )
        token = _build_tenant_token(user=other_user, tenant=other_tenant, db_alias='tenant_test')
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {str(token.access_token)}')
        res = self.client.get('/api/v1/tenant/class-templates/')
        self.assertIn(res.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN, status.HTTP_503_SERVICE_UNAVAILABLE])
