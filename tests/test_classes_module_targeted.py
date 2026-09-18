"""
Targeted Verification Tests for Layer 2 Module E (Classes, Scheduling & Administration)
Covers all 12 exact validation scenarios specified in requirements:
1. ClassCategory create
2. ClassTemplate create
3. branch availability
4. RecurringRule create
5. invalid branch-hours rule rejected
6. occurrence generation
7. one-off occurrence
8. trainer assignment
9. trainer conflict rejected
10. specialty requirement
11. unauthorized role 403
12. tenant isolation
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
from apps.tenant_core.models_govern import BranchWorkingHours, BranchOperatingException
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_rbac import (
    Role, RoleAssignment, ModuleCatalog, SubmoduleCatalog,
    Permission, RolePermissionSet, RolePermissionSetItem,
    RoleModuleAccess, RoleSubmoduleAccess,
)
from apps.tenant_core.models_workforce import (
    UserProfile, EmployeeProfile, TrainerProfile,
    EmployeeWorkSchedule,
    TrainerSpecialty, TrainerSpecialtyAssignment,
)
from apps.tenant_core.models_classes import (
    ClassCategory, ClassTemplate, ClassPrice, ClassBranchAvailability,
    ClassScheduleRule, ClassOccurrence, ClassOccurrenceTrainer,
    ClassSpecialtyRequirement,
)
from apps.tenant_core.services_classes import ClassSchedulingService


class ClassesModuleTargetedTestCase(APITestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')

        # 1. Master Tenant Setup
        self.tenant = Tenant.objects.using('default').create(
            code='SWEAT-UAT',
            name='Sweat UAT Gym',
            slug='sweat-uat-gym',
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
            name='UAT Enterprise Plan',
            code='UAT-PLAN',
            tier='ENTERPRISE',
            status='ACTIVE',
        )
        self.sub = TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
        )
        self.prod_mod_core, _ = ProductModule.objects.using('default').get_or_create(
            code='core',
            defaults={'name': 'Core Platform', 'status': 'ACTIVE'},
        )
        self.tm_core = TenantModule.objects.using('default').create(
            tenant=self.tenant,
            module=self.prod_mod_core,
            is_enabled=True,
            availability_mode='ALL_BRANCHES',
        )
        self.prod_mod_classes, _ = ProductModule.objects.using('default').get_or_create(
            code='classes',
            defaults={'name': 'Classes', 'status': 'ACTIVE'},
        )
        self.tm_classes = TenantModule.objects.using('default').create(
            tenant=self.tenant,
            module=self.prod_mod_classes,
            is_enabled=True,
            availability_mode='ALL_BRANCHES',
        )

        # 2. Tenant Org & Branch
        self.org = Organization.objects.using('tenant_test').create(
            code='SWEAT-ORG',
            name='Sweat Organization',
            status='ACTIVE',
        )
        self.loc = Location.objects.using('tenant_test').create(
            organization=self.org,
            code='LOC-DOWNTOWN',
            name='Downtown Location',
            status='ACTIVE',
        )
        self.branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            code='BR-DOWNTOWN',
            name='Downtown Flagship',
            status='ACTIVE',
        )

        # Setup Branch Working Hours: Mon, Wed, Fri 06:00 to 22:00
        for dow in [1, 3, 5]:
            BranchWorkingHours.objects.using('tenant_test').create(
                branch=self.branch,
                day_of_week=dow,
                is_open=True,
                is_24_hours=False,
                open_time=time(6, 0),
                close_time=time(22, 0),
            )
        # Tuesday closed
        BranchWorkingHours.objects.using('tenant_test').create(
            branch=self.branch,
            day_of_week=2,
            is_open=False,
            is_24_hours=False,
        )

        # 3. Tenant Admin User
        self.admin_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='orgadmin@sweat.uat',
            first_name='Admin',
            last_name='User',
            status='ACTIVE',
        )
        self.admin_user.set_password('AdminSecret123!')
        self.admin_user.save(using='tenant_test')

        # RBAC Setup
        self.role_admin = Role.objects.using('tenant_test').create(
            name='Org Admin',
            code='ORG_ADMIN',
            is_system_role=True,
            scope='ORG',
            organization=self.org,
            is_active=True,
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.admin_user,
            role=self.role_admin,
            organization=self.org,
            is_active=True,
        )
        self.mod_cat, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(
            module_code='core',
            defaults={'name': 'Core System', 'is_enabled': True},
        )
        RoleModuleAccess.objects.using('tenant_test').get_or_create(
            role=self.role_admin, module=self.mod_cat, defaults={'can_access': True}
        )
        self.sub_settings, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=self.mod_cat,
            submodule_code='settings',
            defaults={'name': 'Settings', 'is_enabled': True},
        )
        RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(
            role=self.role_admin, submodule=self.sub_settings, defaults={'can_access': True}
        )
        self.perm_view, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.mod_cat,
            submodule=self.sub_settings,
            action='view',
            defaults={'permission_code': 'core.settings.view', 'label': 'View Settings'},
        )
        self.perm_edit, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.mod_cat,
            submodule=self.sub_settings,
            action='edit',
            defaults={'permission_code': 'core.settings.edit', 'label': 'Edit Settings'},
        )
        self.perm_set = RolePermissionSet.objects.using('tenant_test').create(
            role=self.role_admin,
            name='Admin Perm Set',
        )
        RolePermissionSetItem.objects.using('tenant_test').get_or_create(
            permission_set=self.perm_set, permission=self.perm_view, defaults={'granted': True}
        )
        RolePermissionSetItem.objects.using('tenant_test').get_or_create(
            permission_set=self.perm_set, permission=self.perm_edit, defaults={'granted': True}
        )

        # 4. Unauthorized User (Member role with view-only or no access)
        self.unauth_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='unauth@sweat.uat',
            first_name='Unauth',
            last_name='Member',
            status='ACTIVE',
        )
        self.role_member = Role.objects.using('tenant_test').create(
            name='Member',
            code='MEMBER',
            is_system_role=False,
            scope='ORG',
            organization=self.org,
            is_active=True,
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.unauth_user,
            role=self.role_member,
            organization=self.org,
            is_active=True,
        )

        # 5. Trainer Setup
        self.specialty_mobility = TrainerSpecialty.objects.using('tenant_test').create(
            organization=self.org,
            code='MOBILITY',
            name='Mobility & Recovery',
            status='ACTIVE',
        )
        self.specialty_hiit = TrainerSpecialty.objects.using('tenant_test').create(
            organization=self.org,
            code='HIIT',
            name='High Intensity',
            status='ACTIVE',
        )

        self.trainer_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='trainer.alex@sweat.uat',
            first_name='Alex',
            last_name='Coach',
            status='ACTIVE',
        )
        self.user_profile = UserProfile.objects.using('tenant_test').create(
            user=self.trainer_user,
            first_name_snapshot='Alex',
            last_name_snapshot='Coach',
            preferred_branch=self.branch,
            member_status='ACTIVE',
        )
        self.emp_profile = EmployeeProfile.objects.using('tenant_test').create(
            user_profile=self.user_profile,
            organization=self.org,
            employee_code='EMP-ALEX',
            designation='Master Trainer',
            employment_type='FULL_TIME',
            employment_status='ACTIVE',
        )
        self.trainer_profile = TrainerProfile.objects.using('tenant_test').create(
            employee_profile=self.emp_profile,
            trainer_code='TR-ALEX',
            trainer_status='ACTIVE',
            experience_years=Decimal('4.0'),
            minimum_schedule_buffer_minutes=15,
        )
        # Coach Alex has MOBILITY specialty
        TrainerSpecialtyAssignment.objects.using('tenant_test').create(
            trainer_profile=self.trainer_profile,
            trainer_specialty=self.specialty_mobility,
            status='ACTIVE',
            allow_group=True,
        )
        # Shift on Mondays 06:00 to 18:00
        EmployeeWorkSchedule.objects.using('tenant_test').create(
            employee_profile=self.emp_profile,
            branch=self.branch,
            day_of_week=1,
            start_time=time(6, 0),
            end_time=time(18, 0),
            valid_from=date(2026, 1, 1),
            status='ACTIVE',
        )

    def _auth_as_admin(self):
        refresh = _build_tenant_token(user=self.admin_user, tenant=self.tenant, db_alias='tenant_test')
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {str(refresh.access_token)}')

    def _auth_as_unauth(self):
        refresh = _build_tenant_token(user=self.unauth_user, tenant=self.tenant, db_alias='tenant_test')
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {str(refresh.access_token)}')

    # 1. ClassCategory create
    def test_01_class_category_create(self):
        self._auth_as_admin()
        res = self.client.post('/api/v1/tenant/class-categories/', {
            'code': 'QA_GROUP_FITNESS',
            'name': 'QA Group Fitness',
            'description': 'Temporary UAT class category',
            'display_order': 1,
            'status': 'ACTIVE',
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['code'], 'QA_GROUP_FITNESS')
        self.assertTrue(
            ClassCategory.objects.using('tenant_test').filter(code='QA_GROUP_FITNESS').exists()
        )

    # 2. ClassTemplate create
    def test_02_class_template_create(self):
        self._auth_as_admin()
        cat = ClassCategory.objects.using('tenant_test').create(
            organization=self.org,
            code='QA_CAT',
            name='QA Category',
            status='ACTIVE',
        )
        res = self.client.post('/api/v1/tenant/class-templates/', {
            'code': 'QA_MORNING_MOBILITY',
            'name': 'QA Morning Mobility',
            'category': str(cat.id),
            'default_duration_minutes': 60,
            'default_capacity': 20,
            'default_trial_capacity': 2,
            'default_waitlist_capacity': 5,
            'default_delivery_mode': 'OFFLINE',
            'allow_booking': True,
            'allow_trial': True,
            'allow_waitlist': True,
            'allow_reschedule': True,
            'status': 'ACTIVE',
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['code'], 'QA_MORNING_MOBILITY')
        self.assertTrue(
            ClassTemplate.objects.using('tenant_test').filter(code='QA_MORNING_MOBILITY').exists()
        )

    # 3. Branch availability
    def test_03_branch_availability(self):
        self._auth_as_admin()
        tpl = ClassTemplate.objects.using('tenant_test').create(
            organization=self.org,
            code='TPL-AVAIL',
            name='Availability Template',
            default_duration_minutes=45,
            default_capacity=25,
            status='ACTIVE',
        )
        res = self.client.post('/api/v1/tenant/class-branch-availabilities/', {
            'class_template': str(tpl.id),
            'branch': str(self.branch.id),
            'status': 'ENABLED',
            'capacity_override': 30,
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            ClassBranchAvailability.objects.using('tenant_test')
            .filter(class_template=tpl, branch=self.branch, capacity_override=30)
            .exists()
        )

    # 4. RecurringRule create
    def test_04_recurring_rule_create(self):
        self._auth_as_admin()
        tpl = ClassTemplate.objects.using('tenant_test').create(
            organization=self.org,
            code='TPL-RULE',
            name='Rule Template',
            default_duration_minutes=60,
            default_capacity=20,
            status='ACTIVE',
        )
        res = self.client.post('/api/v1/tenant/class-schedule-rules/', {
            'class_template': str(tpl.id),
            'branch': str(self.branch.id),
            'recurrence_type': 'WEEKLY',
            'days_of_week': [1, 3, 5],
            'start_time': '07:00',
            'end_time': '08:00',
            'valid_from': '2026-10-01',
            'valid_until': '2026-10-31',
            'delivery_mode': 'OFFLINE',
            'status': 'ACTIVE',
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            ClassScheduleRule.objects.using('tenant_test').filter(class_template=tpl).exists()
        )

    # 5. Invalid branch-hours rule rejected
    def test_05_invalid_branch_hours_rule_rejected(self):
        self._auth_as_admin()
        tpl = ClassTemplate.objects.using('tenant_test').create(
            organization=self.org,
            code='TPL-FAIL',
            name='Fail Template',
            default_duration_minutes=60,
            status='ACTIVE',
        )
        # Downtown Flagship opens at 06:00. Trying 05:00 class must be rejected!
        res = self.client.post('/api/v1/tenant/class-schedule-rules/', {
            'class_template': str(tpl.id),
            'branch': str(self.branch.id),
            'recurrence_type': 'WEEKLY',
            'days_of_week': [1],
            'start_time': '05:00',
            'end_time': '06:00',
            'valid_from': '2026-10-01',
            'status': 'ACTIVE',
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('before branch opening time', str(res.data).lower())

        # Also, branch is closed on Tuesday (day 2). Trying Tuesday class must be rejected!
        res_tue = self.client.post('/api/v1/tenant/class-schedule-rules/', {
            'class_template': str(tpl.id),
            'branch': str(self.branch.id),
            'recurrence_type': 'WEEKLY',
            'days_of_week': [2],
            'start_time': '09:00',
            'end_time': '10:00',
            'valid_from': '2026-10-01',
            'status': 'ACTIVE',
        }, format='json')
        self.assertEqual(res_tue.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('closed on tuesday', str(res_tue.data).lower())

    # 6. Occurrence generation
    def test_06_occurrence_generation(self):
        self._auth_as_admin()
        tpl = ClassTemplate.objects.using('tenant_test').create(
            organization=self.org,
            code='TPL-GEN',
            name='Generation Template',
            default_duration_minutes=60,
            default_capacity=15,
            status='ACTIVE',
        )
        rule = ClassScheduleRule.objects.using('tenant_test').create(
            class_template=tpl,
            branch=self.branch,
            recurrence_type='WEEKLY',
            days_of_week=[1],  # Monday
            start_time=time(7, 0),
            end_time=time(8, 0),
            valid_from=date(2026, 10, 1),
            valid_until=date(2026, 10, 31),
            status='ACTIVE',
        )
        url = f'/api/v1/tenant/class-schedule-rules/{rule.id}/generate-occurrences/'
        res = self.client.post(url, {
            'from_date': '2026-10-01',
            'to_date': '2026-10-15',
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        # Mondays in range: Oct 5 and Oct 12
        self.assertEqual(len(res.data), 2)
        self.assertEqual(
            ClassOccurrence.objects.using('tenant_test').filter(schedule_rule=rule).count(), 2
        )

    # 7. One-off occurrence
    def test_07_one_off_occurrence(self):
        self._auth_as_admin()
        tpl = ClassTemplate.objects.using('tenant_test').create(
            organization=self.org,
            code='TPL-ONEOFF',
            name='One Off Template',
            default_duration_minutes=60,
            default_capacity=12,
            status='ACTIVE',
        )
        res = self.client.post('/api/v1/tenant/class-occurrences/', {
            'class_template': str(tpl.id),
            'branch': str(self.branch.id),
            'occurrence_date': '2026-10-05',
            'start_time': '08:00',
            'end_time': '09:00',
            'delivery_mode': 'OFFLINE',
            'capacity': 18,
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        occ = ClassOccurrence.objects.using('tenant_test').get(id=res.data['id'])
        self.assertTrue(occ.is_manual)
        self.assertEqual(occ.capacity, 18)

    # 8. Trainer assignment
    def test_08_trainer_assignment(self):
        self._auth_as_admin()
        tpl = ClassTemplate.objects.using('tenant_test').create(
            organization=self.org,
            code='TPL-TR-ASSIGN',
            name='Trainer Assign Template',
            default_duration_minutes=60,
            status='ACTIVE',
        )
        start_at = timezone.make_aware(datetime(2026, 10, 5, 7, 0))
        end_at = timezone.make_aware(datetime(2026, 10, 5, 8, 0))
        occ = ClassOccurrence.objects.using('tenant_test').create(
            class_template=tpl,
            branch=self.branch,
            occurrence_date=start_at.date(),
            start_at=start_at,
            end_at=end_at,
            status='SCHEDULED',
        )
        url = f'/api/v1/tenant/class-occurrences/{occ.id}/assign-trainer/'
        res = self.client.post(url, {
            'trainer_profile_id': str(self.trainer_profile.id),
            'trainer_role': 'LEAD',
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            ClassOccurrenceTrainer.objects.using('tenant_test')
            .filter(occurrence=occ, trainer_profile=self.trainer_profile, trainer_role='LEAD')
            .exists()
        )

    # 9. Trainer conflict rejected
    def test_09_trainer_conflict_rejected(self):
        self._auth_as_admin()
        tpl = ClassTemplate.objects.using('tenant_test').create(
            organization=self.org,
            code='TPL-CONFLICT',
            name='Conflict Template',
            default_duration_minutes=60,
            status='ACTIVE',
        )
        # Occurrence 1: 07:00 to 08:00
        start1 = timezone.make_aware(datetime(2026, 10, 5, 7, 0))
        end1 = timezone.make_aware(datetime(2026, 10, 5, 8, 0))
        occ1 = ClassOccurrence.objects.using('tenant_test').create(
            class_template=tpl, branch=self.branch,
            occurrence_date=start1.date(), start_at=start1, end_at=end1, status='SCHEDULED',
        )
        # Assign trainer to occ1
        ClassSchedulingService.assign_trainer_to_occurrence(
            occurrence_id=str(occ1.id),
            trainer_profile_id=str(self.trainer_profile.id),
            trainer_role='LEAD',
            db_alias='tenant_test',
        )

        # Occurrence 2: 07:30 to 08:30 (overlaps with occ1!)
        start2 = timezone.make_aware(datetime(2026, 10, 5, 7, 30))
        end2 = timezone.make_aware(datetime(2026, 10, 5, 8, 30))
        occ2 = ClassOccurrence.objects.using('tenant_test').create(
            class_template=tpl, branch=self.branch,
            occurrence_date=start2.date(), start_at=start2, end_at=end2, status='SCHEDULED',
        )

        url = f'/api/v1/tenant/class-occurrences/{occ2.id}/assign-trainer/'
        res = self.client.post(url, {
            'trainer_profile_id': str(self.trainer_profile.id),
            'trainer_role': 'LEAD',
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue('overlapping' in str(res.data).lower() or 'conflict' in str(res.data).lower())

    # 10. Specialty requirement
    def test_10_specialty_requirement(self):
        self._auth_as_admin()
        tpl = ClassTemplate.objects.using('tenant_test').create(
            organization=self.org,
            code='TPL-SPEC-REQ',
            name='Specialty Req Template',
            default_duration_minutes=60,
            status='ACTIVE',
        )
        # Template mandates HIIT specialty
        ClassSpecialtyRequirement.objects.using('tenant_test').create(
            class_template=tpl,
            trainer_specialty=self.specialty_hiit,
            is_mandatory=True,
            status='ACTIVE',
        )
        start_at = timezone.make_aware(datetime(2026, 10, 5, 9, 0))
        end_at = timezone.make_aware(datetime(2026, 10, 5, 10, 0))
        occ = ClassOccurrence.objects.using('tenant_test').create(
            class_template=tpl, branch=self.branch,
            occurrence_date=start_at.date(), start_at=start_at, end_at=end_at, status='SCHEDULED',
        )

        # Coach Alex only has MOBILITY, lacks HIIT!
        url = f'/api/v1/tenant/class-occurrences/{occ.id}/assign-trainer/'
        res = self.client.post(url, {
            'trainer_profile_id': str(self.trainer_profile.id),
            'trainer_role': 'LEAD',
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('specialty', str(res.data).lower())

    # 11. Unauthorized role 403
    def test_11_unauthorized_role_403(self):
        self._auth_as_unauth()
        # Non-admin attempting to create a Class Category must receive 403
        res = self.client.post('/api/v1/tenant/class-categories/', {
            'code': 'UNAUTH_CAT',
            'name': 'Unauthorized Category',
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

        # Non-admin attempting to create a Class Template must receive 403
        res_tpl = self.client.post('/api/v1/tenant/class-templates/', {
            'code': 'UNAUTH_TPL',
            'name': 'Unauthorized Template',
        }, format='json')
        self.assertEqual(res_tpl.status_code, status.HTTP_403_FORBIDDEN)

    # 12. Tenant isolation
    def test_12_tenant_isolation(self):
        # Create a category in Tenant 1 (tenant_test)
        cat1 = ClassCategory.objects.using('tenant_test').create(
            organization=self.org,
            code='T1_CAT',
            name='Tenant 1 Category',
            status='ACTIVE',
        )
        # Verify it exists in tenant_test DB
        self.assertTrue(ClassCategory.objects.using('tenant_test').filter(id=cat1.id).exists())

        # Ensure it does NOT exist in master DB (zero master contamination)
        with self.assertRaises(Exception):
            ClassCategory.objects.using('default').filter(id=cat1.id).exists()
