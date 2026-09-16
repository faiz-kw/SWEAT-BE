"""
tests/test_layer2_phase1_workforce.py — Tests for Layer 2 Phase 1: Module A (Workforce & Trainers)
"""

import uuid
from datetime import datetime, date, time, timedelta
from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from rest_framework import status

from config.routers import set_tenant_db_alias, get_tenant_db_alias
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.models_saas import SaasPlan, TenantSubscription, ProductModule, TenantModule
from apps.authentication.views import _build_tenant_token
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
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
from apps.tenant_core.models_workforce import (
    UserProfile,
    EmployeeProfile,
    TrainerProfile,
    SalesProfile,
    EmployeeWorkSchedule,
    EmployeeScheduleException,
    TrainerSpecialty,
    TrainerSpecialtyAssignment,
)
from apps.tenant_core.services_workforce import TrainerAvailabilityService


class Layer2Phase1WorkforceTests(TestCase):
    databases = '__all__'

    def setUp(self):
        super().setUp()
        set_tenant_db_alias('tenant_test')

        # 1. Master DB Setup
        self.tenant = Tenant.objects.using('default').create(
            code='WORKFORCE-TENANT',
            name='Workforce Gym',
            slug='wf-gym',
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
            name='Workforce Plan',
            code='WF-PLAN',
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
            defaults={'name': 'Core Module', 'status': 'ACTIVE'},
        )
        self.tm_core = TenantModule.objects.using('default').create(
            tenant=self.tenant,
            module=self.prod_mod_core,
            is_enabled=True,
            availability_mode='ALL_BRANCHES',
        )

        # 2. Tenant DB Org Setup
        self.org = Organization.objects.using('tenant_test').create(
            code='WF-ORG',
            name='Workforce Org',
            status='ACTIVE',
        )
        self.loc = Location.objects.using('tenant_test').create(
            organization=self.org,
            code='WF-LOC',
            name='Workforce Location',
            status='ACTIVE',
        )
        self.branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            code='WF-BR-1',
            name='Workforce Branch 1',
            status='ACTIVE',
        )

        # 3. Tenant Admin User
        self.admin_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='admin@workforce.test',
            first_name='Workforce',
            last_name='Admin',
            status='ACTIVE',
        )
        self.admin_user.set_password('TestPass123!')
        self.admin_user.save(using='tenant_test')

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

        # 4. RBAC Catalog and Permissions for core.users
        self.cat_core, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(
            module_code='core',
            defaults={'name': 'Core System', 'is_enabled': True},
        )
        self.csub_users, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=self.cat_core,
            submodule_code='users',
            defaults={'name': 'Users', 'is_enabled': True},
        )
        self.perm_users_view, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.cat_core,
            submodule=self.csub_users,
            action='view',
            defaults={'permission_code': 'core.users.view', 'label': 'View Users'},
        )
        self.perm_users_create, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.cat_core,
            submodule=self.csub_users,
            action='create',
            defaults={'permission_code': 'core.users.create', 'label': 'Create Users'},
        )
        RoleModuleAccess.objects.using('tenant_test').get_or_create(
            role=self.role_admin, module=self.cat_core, defaults={'can_access': True}
        )
        RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(
            role=self.role_admin, submodule=self.csub_users, defaults={'can_access': True}
        )
        self.perm_set, _ = RolePermissionSet.objects.using('tenant_test').get_or_create(
            role=self.role_admin, defaults={'name': 'Admin Workforce Permissions', 'is_active': True}
        )
        RolePermissionSetItem.objects.using('tenant_test').get_or_create(
            permission_set=self.perm_set, permission=self.perm_users_view, defaults={'granted': True}
        )
        RolePermissionSetItem.objects.using('tenant_test').get_or_create(
            permission_set=self.perm_set, permission=self.perm_users_create, defaults={'granted': True}
        )

    def tearDown(self):
        set_tenant_db_alias(None)
        super().tearDown()

    def get_token(self, user):
        refresh = _build_tenant_token(
            user=user,
            tenant=self.tenant,
            db_alias='tenant_test',
        )
        return str(refresh.access_token)

    def _create_trainer(self, code='TR-001', email='trainer1@wf.test', first='John', last='Doe'):
        user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email=email,
            first_name=first,
            last_name=last,
            status='ACTIVE',
        )
        user_prof = UserProfile.objects.using('tenant_test').create(
            user=user,
            first_name_snapshot=first,
            last_name_snapshot=last,
            preferred_branch=self.branch,
            member_status='ACTIVE',
        )
        emp_prof = EmployeeProfile.objects.using('tenant_test').create(
            user_profile=user_prof,
            organization=self.org,
            employee_code=f"EMP-{code}",
            designation='Fitness Coach',
            employment_type='FULL_TIME',
            employment_status='ACTIVE',
        )
        trainer_prof = TrainerProfile.objects.using('tenant_test').create(
            employee_profile=emp_prof,
            trainer_code=code,
            trainer_status='ACTIVE',
            experience_years=Decimal('4.5'),
            minimum_schedule_buffer_minutes=15,
        )
        return trainer_prof

    def test_01_user_and_employee_profile_creation(self):
        """User profile and employee profile are linked with unique employee code."""
        trainer = self._create_trainer('TR-TEST-1', 'emp1@wf.test', 'Alice', 'Smith')
        self.assertEqual(trainer.employee_profile.employee_code, 'EMP-TR-TEST-1')
        self.assertEqual(trainer.employee_profile.user_profile.first_name_snapshot, 'Alice')
        self.assertEqual(trainer.trainer_code, 'TR-TEST-1')

    def test_02_trainer_specialty_and_assignment(self):
        """Specialties are data-driven and linked via assignments with delivery mode constraints."""
        trainer = self._create_trainer('TR-TEST-2', 'emp2@wf.test', 'Bob', 'Jones')
        specialty = TrainerSpecialty.objects.using('tenant_test').create(
            organization=self.org,
            code='HIIT',
            name='High Intensity Interval Training',
            category='Cardio',
            status='ACTIVE',
        )
        assignment = TrainerSpecialtyAssignment.objects.using('tenant_test').create(
            trainer_profile=trainer,
            trainer_specialty=specialty,
            branch=self.branch,
            proficiency_level='ADVANCED',
            allow_group=True,
            allow_individual=True,
            allow_online=False,
            is_primary=True,
            status='ACTIVE',
        )
        self.assertTrue(assignment.allow_group)
        self.assertFalse(assignment.allow_online)
        self.assertEqual(assignment.proficiency_level, 'ADVANCED')

    def test_03_work_schedule_creation(self):
        """Employee regular shifts are scheduled by weekday with start and end times."""
        trainer = self._create_trainer('TR-TEST-3', 'emp3@wf.test', 'Carol', 'Ray')
        shift = EmployeeWorkSchedule.objects.using('tenant_test').create(
            employee_profile=trainer.employee_profile,
            branch=self.branch,
            day_of_week=2,  # Tuesday
            start_time=time(9, 0),
            end_time=time(17, 0),
            valid_from=date(2026, 1, 1),
            schedule_type='REGULAR',
            status='ACTIVE',
        )
        self.assertEqual(shift.day_of_week, 2)
        self.assertEqual(shift.start_time, time(9, 0))

    def test_04_trainer_availability_service_shift_matching(self):
        """Availability service returns True during active shift, False outside shift hours."""
        trainer = self._create_trainer('TR-TEST-4', 'emp4@wf.test', 'David', 'Clark')
        EmployeeWorkSchedule.objects.using('tenant_test').create(
            employee_profile=trainer.employee_profile,
            branch=self.branch,
            day_of_week=2,  # Tuesday
            start_time=time(9, 0),
            end_time=time(17, 0),
            valid_from=date(2026, 1, 1),
            schedule_type='REGULAR',
            status='ACTIVE',
        )

        # Tuesday at 10:00 AM -> Inside shift
        tuesday_slot = datetime(2026, 9, 22, 10, 0)  # 2026-09-22 is Tuesday
        avail, reason, details = TrainerAvailabilityService.is_trainer_available(
            trainer=trainer,
            branch=self.branch,
            start_datetime=tuesday_slot,
            duration_minutes=60,
            db_alias='tenant_test',
        )
        self.assertTrue(avail)
        self.assertEqual(details['code'], 'AVAILABLE')

        # Tuesday at 18:00 PM -> Outside shift
        outside_slot = datetime(2026, 9, 22, 18, 0)
        avail2, reason2, details2 = TrainerAvailabilityService.is_trainer_available(
            trainer=trainer,
            branch=self.branch,
            start_datetime=outside_slot,
            duration_minutes=60,
            db_alias='tenant_test',
        )
        self.assertFalse(avail2)
        self.assertEqual(details2['code'], 'NO_MATCHING_SHIFT')

    def test_05_trainer_availability_service_leave_exception(self):
        """Approved leave exception overrides regular shift and marks trainer unavailable."""
        trainer = self._create_trainer('TR-TEST-5', 'emp5@wf.test', 'Eva', 'Green')
        EmployeeWorkSchedule.objects.using('tenant_test').create(
            employee_profile=trainer.employee_profile,
            branch=self.branch,
            day_of_week=2,  # Tuesday
            start_time=time(9, 0),
            end_time=time(17, 0),
            valid_from=date(2026, 1, 1),
            schedule_type='REGULAR',
            status='ACTIVE',
        )
        # Leave on 2026-09-22
        EmployeeScheduleException.objects.using('tenant_test').create(
            employee_profile=trainer.employee_profile,
            branch=self.branch,
            exception_date=date(2026, 9, 22),
            exception_type='LEAVE',
            is_available=False,
            reason='Annual Medical Leave',
            status='ACTIVE',
        )

        tuesday_slot = datetime(2026, 9, 22, 10, 0)
        avail, reason, details = TrainerAvailabilityService.is_trainer_available(
            trainer=trainer,
            branch=self.branch,
            start_datetime=tuesday_slot,
            duration_minutes=60,
            db_alias='tenant_test',
        )
        self.assertFalse(avail)
        self.assertEqual(details['code'], 'SCHEDULE_EXCEPTION_ABSENT')
        self.assertIn('Annual Medical Leave', reason)

    def test_06_trainer_availability_specialty_and_mode(self):
        """Trainer with GROUP Yoga assignment cannot deliver INDIVIDUAL PT or unassigned Pilates."""
        trainer = self._create_trainer('TR-TEST-6', 'emp6@wf.test', 'Frank', 'Sinatra')
        EmployeeWorkSchedule.objects.using('tenant_test').create(
            employee_profile=trainer.employee_profile,
            branch=self.branch,
            day_of_week=2,  # Tuesday
            start_time=time(9, 0),
            end_time=time(17, 0),
            valid_from=date(2026, 1, 1),
            status='ACTIVE',
        )
        yoga = TrainerSpecialty.objects.using('tenant_test').create(
            organization=self.org,
            code='YOGA',
            name='Yoga Flow',
            status='ACTIVE',
        )
        TrainerSpecialtyAssignment.objects.using('tenant_test').create(
            trainer_profile=trainer,
            trainer_specialty=yoga,
            branch=self.branch,
            proficiency_level='INTERMEDIATE',
            allow_group=True,
            allow_individual=False,  # Not qualified for 1-on-1 PT
            status='ACTIVE',
        )

        tuesday_slot = datetime(2026, 9, 22, 10, 0)

        # 1. GROUP Yoga -> Available
        avail_group, _, _ = TrainerAvailabilityService.is_trainer_available(
            trainer=trainer,
            branch=self.branch,
            start_datetime=tuesday_slot,
            duration_minutes=60,
            delivery_mode='GROUP',
            specialty_code='YOGA',
            db_alias='tenant_test',
        )
        self.assertTrue(avail_group)

        # 2. INDIVIDUAL Yoga -> Denied
        avail_ind, _, details_ind = TrainerAvailabilityService.is_trainer_available(
            trainer=trainer,
            branch=self.branch,
            start_datetime=tuesday_slot,
            duration_minutes=60,
            delivery_mode='INDIVIDUAL',
            specialty_code='YOGA',
            db_alias='tenant_test',
        )
        self.assertFalse(avail_ind)
        self.assertEqual(details_ind['code'], 'SPECIALTY_NOT_ASSIGNED')

        # 3. PILATES -> Denied (not assigned)
        avail_pilates, _, details_pilates = TrainerAvailabilityService.is_trainer_available(
            trainer=trainer,
            branch=self.branch,
            start_datetime=tuesday_slot,
            duration_minutes=60,
            delivery_mode='GROUP',
            specialty_code='PILATES',
            db_alias='tenant_test',
        )
        self.assertFalse(avail_pilates)
        self.assertEqual(details_pilates['code'], 'SPECIALTY_NOT_ASSIGNED')

    def test_07_trainer_api_and_availability_endpoints(self):
        """API list, check-availability action, and find-eligible action return real data."""
        trainer = self._create_trainer('TR-API-1', 'api1@wf.test', 'Grace', 'Hopper')
        EmployeeWorkSchedule.objects.using('tenant_test').create(
            employee_profile=trainer.employee_profile,
            branch=self.branch,
            day_of_week=2,  # Tuesday
            start_time=time(9, 0),
            end_time=time(17, 0),
            valid_from=date(2026, 1, 1),
            status='ACTIVE',
        )
        token = self.get_token(self.admin_user)

        # 1. List trainers
        res = self.client.get(
            '/api/v1/tenant/trainer-profiles/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        results = res.json().get('results', res.json())
        self.assertTrue(any(t.get('trainer_code') == 'TR-API-1' for t in results))

        # 2. Check availability endpoint
        res_check = self.client.get(
            f'/api/v1/tenant/trainer-profiles/{trainer.id}/check-availability/',
            {
                'start_datetime': '2026-09-22T10:00:00Z',
                'branch_id': str(self.branch.id),
                'duration_minutes': 60,
            },
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(res_check.status_code, status.HTTP_200_OK)
        self.assertTrue(res_check.json().get('is_available'))

        # 3. Find eligible trainers endpoint
        res_find = self.client.get(
            '/api/v1/tenant/trainer-profiles/find-eligible/',
            {
                'start_datetime': '2026-09-22T10:00:00Z',
                'branch_id': str(self.branch.id),
                'duration_minutes': 60,
            },
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(res_find.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(res_find.json().get('count', 0), 1)
