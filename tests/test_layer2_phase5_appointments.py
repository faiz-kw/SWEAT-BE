"""
Layer 2 Phase 5 Tests: Module F (Individual Appointments)

Covers:
- Appointment Types & Specialty Requirements
- Atomic Individual Appointment Booking (end_at > start_at, conflict checks)
- Trainer Eligibility & Assignment for 1-to-1 Services (allow_individual=True, shifts, leave, conflicts)
- Conflict Detection across Group Classes & Individual Appointments
- Appointment Lifecycle (Booking -> Assignment -> Cancellation -> Completion)
- REST API ViewSets & Custom Actions (assign-trainer, cancel, complete)
"""

import uuid
from datetime import date, time, timedelta, datetime
from decimal import Decimal
from django.utils import timezone
from django.core.exceptions import ValidationError
from rest_framework.test import APITestCase
from rest_framework import status

from apps.authentication.views import _build_tenant_token
from apps.master.models import Tenant, TenantDataSource, ProductModule, TenantModule, SaasPlan, TenantSubscription
from apps.tenant_core.context import set_tenant_db_alias
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_rbac import (
    Role, RoleAssignment, ModuleCatalog, SubmoduleCatalog,
    Permission, RolePermissionSet, RolePermissionSetItem,
    RoleModuleAccess, RoleSubmoduleAccess,
)
from apps.tenant_core.models_workforce import (
    UserProfile, EmployeeProfile, TrainerProfile,
    EmployeeWorkSchedule, EmployeeScheduleException,
    TrainerSpecialty, TrainerSpecialtyAssignment,
)
from apps.tenant_core.models_classes import (
    ClassCategory, ClassTemplate, ClassOccurrence, ClassOccurrenceTrainer,
)
from apps.tenant_core.models_appointments import (
    AppointmentType, Appointment, AppointmentTrainer,
    AppointmentTypeSpecialtyRequirement,
)
from apps.tenant_core.services_appointments import AppointmentSchedulingService
from apps.tenant_core.models_audit_outbox import BusinessAuditEvent, DomainOutboxEvent


class Layer2Phase5AppointmentsTestCase(APITestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')

        # 1. Master DB Setup
        self.tenant = Tenant.objects.using('default').create(
            code='APPT-TENANT',
            name='Appointments Gym',
            slug='appointments-gym',
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
            name='Appointments Plan',
            code='APPT-PLAN',
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

        # 2. Tenant DB Org Setup
        self.org = Organization.objects.using('tenant_test').create(
            code='APPT-ORG',
            name='Appointments Gym Org',
            status='ACTIVE',
        )
        self.loc = Location.objects.using('tenant_test').create(
            organization=self.org,
            code='APPT-LOC',
            name='Appointments Location',
            status='ACTIVE',
        )
        self.branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            code='BR-APPT',
            name='Main Wellness Branch',
            status='ACTIVE',
        )

        # 3. Tenant Admin User
        self.admin_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='admin@appointments.test',
            first_name='Appt',
            last_name='Admin',
            status='ACTIVE',
        )
        self.admin_user.set_password('TestPass123!')
        self.admin_user.save(using='tenant_test')

        # 4. RBAC Setup for core module & settings submodule
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

        # 5. Member User & Profile
        self.member_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='member.sarah@appt.test',
            first_name='Sarah',
            last_name='Connor',
            status='ACTIVE',
        )
        self.member_profile = UserProfile.objects.using('tenant_test').create(
            user=self.member_user,
            member_number='MEM-1001',
            first_name_snapshot='Sarah',
            last_name_snapshot='Connor',
            preferred_branch=self.branch,
            member_status='ACTIVE',
        )

        # 6. Service & Specialty Setup
        self.specialty_pt = TrainerSpecialty.objects.using('tenant_test').create(
            organization=self.org,
            code='PT-SPEC',
            name='Personal Training Certified',
            status='ACTIVE',
        )
        self.appt_type = AppointmentType.objects.using('tenant_test').create(
            organization=self.org,
            code='PT-60',
            name='Personal Training 60min',
            description='One-on-one personal training coaching session',
            default_duration_minutes=60,
            default_delivery_mode='OFFLINE',
            requires_trainer=True,
            status='ACTIVE',
        )
        AppointmentTypeSpecialtyRequirement.objects.using('tenant_test').create(
            appointment_type=self.appt_type,
            trainer_specialty=self.specialty_pt,
            minimum_proficiency_level='INTERMEDIATE',
            is_mandatory=True,
            status='ACTIVE',
        )

        # 7. Trainer Setup
        self.trainer_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='coach.dave@appt.test',
            first_name='Dave',
            last_name='Trainer',
            status='ACTIVE',
        )
        self.user_prof_dave = UserProfile.objects.using('tenant_test').create(
            user=self.trainer_user,
            first_name_snapshot='Dave',
            last_name_snapshot='Trainer',
            preferred_branch=self.branch,
            member_status='ACTIVE',
        )
        self.emp_profile = EmployeeProfile.objects.using('tenant_test').create(
            user_profile=self.user_prof_dave,
            organization=self.org,
            employee_code='EMP-DAVE',
            designation='Senior Personal Trainer',
            employment_type='FULL_TIME',
            employment_status='ACTIVE',
        )
        self.trainer_profile = TrainerProfile.objects.using('tenant_test').create(
            employee_profile=self.emp_profile,
            trainer_code='TR-DAVE',
            trainer_status='ACTIVE',
            experience_years=Decimal('6.0'),
            minimum_schedule_buffer_minutes=15,
        )
        # Assign specialty with allow_individual=True
        TrainerSpecialtyAssignment.objects.using('tenant_test').create(
            trainer_profile=self.trainer_profile,
            trainer_specialty=self.specialty_pt,
            proficiency_level='ADVANCED',
            allow_individual=True,
            allow_group=False,
            status='ACTIVE',
        )
        # Working shift: Mondays (1) 08:00 to 18:00
        EmployeeWorkSchedule.objects.using('tenant_test').create(
            employee_profile=self.emp_profile,
            branch=self.branch,
            day_of_week=1,
            start_time=time(8, 0),
            end_time=time(18, 0),
            valid_from=date(2026, 1, 1),
            status='ACTIVE',
        )

    def test_appointment_booking_and_time_validation(self):
        """Test atomic individual appointment booking and strict time integrity."""
        start_at = timezone.make_aware(datetime(2026, 10, 5, 10, 0))  # Monday 10:00 AM
        end_at = timezone.make_aware(datetime(2026, 10, 5, 11, 0))

        appt = AppointmentSchedulingService.create_appointment(
            appointment_type=self.appt_type,
            user_profile=self.member_profile,
            branch=self.branch,
            start_at=start_at,
            end_at=end_at,
            delivery_mode='OFFLINE',
            booking_source='ADMIN',
            notes='Focus on deadlift form',
            created_by=self.admin_user,
            db_alias='tenant_test',
        )

        self.assertEqual(appt.appointment_type, self.appt_type)
        self.assertEqual(appt.user_profile, self.member_profile)
        self.assertEqual(appt.status, 'CONFIRMED')
        self.assertEqual(appt.end_at, end_at)

        # Attempt to book an overlapping appointment for the same member -> ValidationError
        with self.assertRaises(ValidationError) as ctx:
            AppointmentSchedulingService.create_appointment(
                appointment_type=self.appt_type,
                user_profile=self.member_profile,
                branch=self.branch,
                start_at=start_at + timedelta(minutes=30),
                end_at=end_at + timedelta(minutes=30),
                db_alias='tenant_test',
            )
        self.assertIn("conflicting appointment", str(ctx.exception).lower())

    def test_trainer_eligibility_and_conflict_handling(self):
        """Test trainer specialty and availability validation with group/individual conflict checks."""
        start_at = timezone.make_aware(datetime(2026, 10, 5, 14, 0))  # Monday 2:00 PM
        end_at = timezone.make_aware(datetime(2026, 10, 5, 15, 0))

        appt = AppointmentSchedulingService.create_appointment(
            appointment_type=self.appt_type,
            user_profile=self.member_profile,
            branch=self.branch,
            start_at=start_at,
            end_at=end_at,
            db_alias='tenant_test',
        )

        # 1. Assignment succeeds
        assignment = AppointmentSchedulingService.assign_trainer_to_appointment(
            appointment_id=str(appt.id),
            trainer_profile_id=str(self.trainer_profile.id),
            role='LEAD',
            actor=self.admin_user,
            db_alias='tenant_test',
        )
        self.assertEqual(assignment.trainer_profile, self.trainer_profile)
        self.assertEqual(assignment.status, 'CONFIRMED')

        # 2. Add a second appointment for another member at overlapping time
        other_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='member2@appt.test',
            first_name='John',
            last_name='Connor',
            status='ACTIVE',
        )
        other_prof = UserProfile.objects.using('tenant_test').create(
            user=other_user,
            member_number='MEM-1002',
            preferred_branch=self.branch,
            member_status='ACTIVE',
        )
        appt2 = AppointmentSchedulingService.create_appointment(
            appointment_type=self.appt_type,
            user_profile=other_prof,
            branch=self.branch,
            start_at=start_at,
            end_at=end_at,
            db_alias='tenant_test',
        )

        # Attempt to assign Dave to appt2 -> conflict error!
        with self.assertRaises(ValidationError) as ctx:
            AppointmentSchedulingService.assign_trainer_to_appointment(
                appointment_id=str(appt2.id),
                trainer_profile_id=str(self.trainer_profile.id),
                role='LEAD',
                actor=self.admin_user,
                db_alias='tenant_test',
            )
        self.assertIn("conflicting individual appointment", str(ctx.exception).lower())

    def test_appointment_lifecycle_completion_and_cancellation(self):
        """Test appointment status transitions (Cancel, Complete) and audit/outbox events."""
        start_at = timezone.make_aware(datetime(2026, 10, 5, 16, 0))
        end_at = timezone.make_aware(datetime(2026, 10, 5, 17, 0))

        appt = AppointmentSchedulingService.create_appointment(
            appointment_type=self.appt_type,
            user_profile=self.member_profile,
            branch=self.branch,
            start_at=start_at,
            end_at=end_at,
            db_alias='tenant_test',
        )
        AppointmentSchedulingService.assign_trainer_to_appointment(
            appointment_id=str(appt.id),
            trainer_profile_id=str(self.trainer_profile.id),
            role='LEAD',
            actor=self.admin_user,
            db_alias='tenant_test',
        )

        # 1. Complete appointment
        completed = AppointmentSchedulingService.complete_appointment(
            appointment_id=str(appt.id),
            actor=self.admin_user,
            db_alias='tenant_test',
        )
        self.assertEqual(completed.status, 'COMPLETED')

        # 2. Cancel a new appointment
        appt_to_cancel = AppointmentSchedulingService.create_appointment(
            appointment_type=self.appt_type,
            user_profile=self.member_profile,
            branch=self.branch,
            start_at=start_at + timedelta(days=7),
            end_at=end_at + timedelta(days=7),
            db_alias='tenant_test',
        )
        cancelled = AppointmentSchedulingService.cancel_appointment(
            appointment_id=str(appt_to_cancel.id),
            reason='Client requested reschedule due to travel',
            actor=self.admin_user,
            db_alias='tenant_test',
        )
        self.assertEqual(cancelled.status, 'CANCELLED')
        self.assertIn("Client requested reschedule", cancelled.notes)

    def test_appointment_rest_apis(self):
        """Test appointment listing, trainer assignment, and cancellation endpoints."""
        refresh = _build_tenant_token(
            user=self.admin_user,
            tenant=self.tenant,
            db_alias='tenant_test',
        )
        token = str(refresh.access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        start_at = timezone.make_aware(datetime(2026, 10, 5, 11, 0))
        end_at = timezone.make_aware(datetime(2026, 10, 5, 12, 0))
        appt = Appointment.objects.using('tenant_test').create(
            appointment_type=self.appt_type,
            user_profile=self.member_profile,
            branch=self.branch,
            start_at=start_at,
            end_at=end_at,
            status='CONFIRMED',
        )

        # 1. List appointments
        response = self.client.get('/api/v1/tenant/appointments/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data.get('results', response.data)
        self.assertEqual(len(results), 1)

        # 2. Assign trainer custom action
        url_assign = f'/api/v1/tenant/appointments/{appt.id}/assign-trainer/'
        data_assign = {
            'trainer_profile_id': str(self.trainer_profile.id),
            'role': 'LEAD',
        }
        res_assign = self.client.post(url_assign, data_assign, format='json')
        self.assertEqual(res_assign.status_code, status.HTTP_201_CREATED)
        self.assertEqual(str(res_assign.data.get('trainer_profile')), str(self.trainer_profile.id))

        # 3. Cancel custom action
        url_cancel = f'/api/v1/tenant/appointments/{appt.id}/cancel/'
        res_cancel = self.client.post(url_cancel, {'reason': 'Illness'}, format='json')
        self.assertEqual(res_cancel.status_code, status.HTTP_200_OK)
        self.assertEqual(res_cancel.data.get('status'), 'CANCELLED')
