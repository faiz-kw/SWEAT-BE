"""
Layer 2 Phase 4 Tests: Module E (Group Classes, Scheduling, Content Studio & Demand)

Covers:
- Class Categories & Class Templates
- Recurring Schedule Rules & Batch Occurrence Generation
- Trainer Eligibility Validation (schedule shift, specialty requirement, conflict detection)
- Content Studio NO_REPEAT_UNTIL_EXHAUSTED rotation engine
- REST API ViewSets & Custom Action Endpoints
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
from apps.tenant_core.models_catalog import ProgramCategory, Program
from apps.tenant_core.models_classes import (
    ClassCategory, ClassTemplate, ClassPrice, ClassBranchAvailability,
    ClassScheduleRule, ClassOccurrence, ClassOccurrenceTrainer,
    ClassSpecialtyRequirement, ClassContentItem, ClassContentMapping,
    ClassContentAssignment,
)
from apps.tenant_core.services_classes import ClassSchedulingService, ContentStudioService
from apps.tenant_core.models_audit_outbox import BusinessAuditEvent, DomainOutboxEvent


class Layer2Phase4ClassesTestCase(APITestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')

        # 1. Master DB Setup
        self.tenant = Tenant.objects.using('default').create(
            code='CLASSES-TENANT',
            name='Classes Gym',
            slug='classes-gym',
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
            name='Classes Plan',
            code='CLASSES-PLAN',
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

        # 2. Tenant DB Org Setup
        self.org = Organization.objects.using('tenant_test').create(
            code='CLASSES-ORG',
            name='Classes Gym Org',
            status='ACTIVE',
        )
        self.loc = Location.objects.using('tenant_test').create(
            organization=self.org,
            code='CLS-LOC',
            name='Classes Location',
            status='ACTIVE',
        )
        self.branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            code='BR-CLS',
            name='Main Studio Branch',
            status='ACTIVE',
        )

        # 3. Tenant Admin User
        self.admin_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='admin@classes.test',
            first_name='Classes',
            last_name='Admin',
            status='ACTIVE',
        )
        self.admin_user.set_password('TestPass123!')
        self.admin_user.save(using='tenant_test')

        # 4. RBAC Setup for classes module
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

        # 5. Domain Foundations
        self.prog_cat = ProgramCategory.objects.using('tenant_test').create(
            organization=self.org,
            code='FITNESS',
            name='Fitness Programs',
            status='ACTIVE',
        )
        self.program = Program.objects.using('tenant_test').create(
            organization=self.org,
            category=self.prog_cat,
            code='SWEAT-HIIT',
            name='Sweat HIIT',
            status='ACTIVE',
        )
        self.class_cat = ClassCategory.objects.using('tenant_test').create(
            organization=self.org,
            code='STRENGTH',
            name='Strength & Conditioning',
            status='ACTIVE',
        )
        self.template = ClassTemplate.objects.using('tenant_test').create(
            organization=self.org,
            program=self.program,
            category=self.class_cat,
            code='HIIT-45',
            name='High Intensity Interval 45',
            default_duration_minutes=45,
            default_capacity=20,
            default_waitlist_capacity=5,
            status='ACTIVE',
        )

        # 6. Trainer & Specialty Setup
        self.specialty_hiit = TrainerSpecialty.objects.using('tenant_test').create(
            organization=self.org,
            code='HIIT',
            name='HIIT Certified',
            status='ACTIVE',
        )
        # Template requires HIIT specialty
        ClassSpecialtyRequirement.objects.using('tenant_test').create(
            class_template=self.template,
            trainer_specialty=self.specialty_hiit,
            is_mandatory=True,
            status='ACTIVE',
        )

        # Trainer user & profile
        self.trainer_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='trainer.mike@classes.test',
            first_name='Mike',
            last_name='Trainer',
            status='ACTIVE',
        )
        self.user_profile = UserProfile.objects.using('tenant_test').create(
            user=self.trainer_user,
            first_name_snapshot='Mike',
            last_name_snapshot='Trainer',
            preferred_branch=self.branch,
            member_status='ACTIVE',
        )
        self.emp_profile = EmployeeProfile.objects.using('tenant_test').create(
            user_profile=self.user_profile,
            organization=self.org,
            employee_code='EMP-MIKE',
            designation='Fitness Coach',
            employment_type='FULL_TIME',
            employment_status='ACTIVE',
        )
        self.trainer_profile = TrainerProfile.objects.using('tenant_test').create(
            employee_profile=self.emp_profile,
            trainer_code='TR-MIKE',
            trainer_status='ACTIVE',
            experience_years=Decimal('5.0'),
            minimum_schedule_buffer_minutes=15,
        )
        # Assign specialty to trainer (with allow_group=True)
        TrainerSpecialtyAssignment.objects.using('tenant_test').create(
            trainer_profile=self.trainer_profile,
            trainer_specialty=self.specialty_hiit,
            status='ACTIVE',
            allow_group=True,
        )
        # Schedule: Mondays (1) & Wednesdays (3) 08:00 to 14:00
        EmployeeWorkSchedule.objects.using('tenant_test').create(
            employee_profile=self.emp_profile,
            branch=self.branch,
            day_of_week=1,  # Monday
            start_time=time(8, 0),
            end_time=time(14, 0),
            valid_from=date(2026, 1, 1),
            status='ACTIVE',
        )
        EmployeeWorkSchedule.objects.using('tenant_test').create(
            employee_profile=self.emp_profile,
            branch=self.branch,
            day_of_week=3,  # Wednesday
            start_time=time(8, 0),
            end_time=time(14, 0),
            valid_from=date(2026, 1, 1),
            status='ACTIVE',
        )

    def test_class_schedule_rule_and_occurrence_generation(self):
        """Test recurring weekly rule generates proper occurrences with capacity & duration."""
        # Create rule for Monday (1) 09:00 - 09:45
        rule = ClassScheduleRule.objects.using('tenant_test').create(
            class_template=self.template,
            branch=self.branch,
            days_of_week=[1],
            start_time=time(9, 0),
            end_time=time(9, 45),
            capacity_override=20,
            waitlist_capacity_override=5,
            valid_from=date(2026, 10, 1),
            valid_until=date(2026, 10, 31),
            status='ACTIVE',
        )

        occurrences = ClassSchedulingService.generate_occurrences_from_rule(
            rule=rule,
            from_date=date(2026, 10, 5),  # First Monday is 2026-10-05
            to_date=date(2026, 10, 12),    # Next Monday is 2026-10-12
            actor=self.admin_user,
            db_alias='tenant_test',
        )

        # 2 Mondays in this date range: Oct 5 and Oct 12
        self.assertEqual(len(occurrences), 2)
        for occ in occurrences:
            self.assertEqual(occ.class_template, self.template)
            self.assertEqual(occ.branch, self.branch)
            self.assertEqual(occ.capacity, 20)
            self.assertIn(occ.status, ['OPEN', 'SCHEDULED'])
            self.assertEqual(occ.start_at.time(), time(9, 0))
            self.assertEqual(occ.end_at.time(), time(9, 45))

    def test_trainer_eligibility_and_assignment(self):
        """Test trainer eligibility validation and occurrence assignment."""
        start_at = timezone.make_aware(datetime(2026, 10, 5, 9, 0))  # Monday 9:00 AM
        end_at = timezone.make_aware(datetime(2026, 10, 5, 9, 45))

        occurrence = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.template,
            branch=self.branch,
            occurrence_date=start_at.date(),
            start_at=start_at,
            end_at=end_at,
            capacity=20,
            status='SCHEDULED',
        )

        # 1. Assignment should succeed (Coach Mike has specialty & working schedule)
        assigned = ClassSchedulingService.assign_trainer_to_occurrence(
            occurrence_id=str(occurrence.id),
            trainer_profile_id=str(self.trainer_profile.id),
            trainer_role='LEAD',
            actor=self.admin_user,
            db_alias='tenant_test',
        )
        self.assertEqual(assigned.trainer_profile, self.trainer_profile)
        self.assertEqual(assigned.trainer_role, 'LEAD')

        # 2. Add an exception / leave for that day and test eligibility failure
        EmployeeScheduleException.objects.using('tenant_test').create(
            employee_profile=self.emp_profile,
            branch=self.branch,
            exception_type='LEAVE',
            exception_date=date(2026, 10, 5),
            is_available=False,
            start_time=time(8, 0),
            end_time=time(12, 0),
            reason='Medical Appointment',
            status='ACTIVE',
        )

        # Remove existing assignment to re-test eligibility check
        assigned.delete(using='tenant_test')

        with self.assertRaises(ValidationError) as ctx:
            ClassSchedulingService.assign_trainer_to_occurrence(
                occurrence_id=str(occurrence.id),
                trainer_profile_id=str(self.trainer_profile.id),
                trainer_role='LEAD',
                actor=self.admin_user,
                db_alias='tenant_test',
            )
        self.assertTrue("unavailable" in str(ctx.exception).lower() or "leave" in str(ctx.exception).lower())

    def test_content_studio_deterministic_rotation(self):
        """Test NO_REPEAT_UNTIL_EXHAUSTED rotation engine across occurrences."""
        # Create 3 content items for the template with external source URLs
        item1 = ClassContentItem.objects.using('tenant_test').create(
            organization=self.org,
            title='HIIT Workout 1: Tabata Inferno',
            display_order=1,
            external_url='https://cdn.sweat.test/hiit/1.mp4',
            status='ACTIVE',
        )
        item2 = ClassContentItem.objects.using('tenant_test').create(
            organization=self.org,
            title='HIIT Workout 2: AMRAP Gauntlet',
            display_order=2,
            external_url='https://cdn.sweat.test/hiit/2.mp4',
            status='ACTIVE',
        )
        item3 = ClassContentItem.objects.using('tenant_test').create(
            organization=self.org,
            title='HIIT Workout 3: EMOM Blast',
            display_order=3,
            external_url='https://cdn.sweat.test/hiit/3.mp4',
            status='ACTIVE',
        )
        for itm in [item1, item2, item3]:
            ClassContentMapping.objects.using('tenant_test').create(
                content_item=itm,
                class_template=self.template,
                status='ACTIVE',
            )

        # Create 4 consecutive occurrences
        occurrences = []
        for i in range(4):
            dt = timezone.make_aware(datetime(2026, 10, 5 + i, 9, 0))
            occ = ClassOccurrence.objects.using('tenant_test').create(
                class_template=self.template,
                branch=self.branch,
                occurrence_date=dt.date(),
                start_at=dt,
                end_at=dt + timedelta(minutes=45),
                capacity=20,
                status='SCHEDULED',
            )
            occurrences.append(occ)

        # Run rotation for 4 occurrences
        assigned_items = []
        for occ in occurrences:
            assignment = ContentStudioService.rotate_and_assign_content_to_occurrence(
                occurrence_id=str(occ.id),
                actor=self.admin_user,
                db_alias='tenant_test',
            )
            assigned_items.append(assignment.content_item)

        # Verify items: 1, then 2, then 3 (Cycle 1 completed), then 1 (Cycle 2 begins)
        self.assertEqual(assigned_items[0], item1)
        self.assertEqual(assigned_items[1], item2)
        self.assertEqual(assigned_items[2], item3)
        self.assertEqual(assigned_items[3], item1)

    def test_class_occurrence_rest_apis(self):
        """Test class occurrences endpoints and custom action to assign trainer."""
        refresh = _build_tenant_token(
            user=self.admin_user,
            tenant=self.tenant,
            db_alias='tenant_test',
        )
        token = str(refresh.access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        start_at = timezone.make_aware(datetime(2026, 10, 5, 9, 0))
        end_at = timezone.make_aware(datetime(2026, 10, 5, 9, 45))
        occurrence = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.template,
            branch=self.branch,
            occurrence_date=start_at.date(),
            start_at=start_at,
            end_at=end_at,
            capacity=20,
            status='SCHEDULED',
        )

        # 1. List occurrences
        response = self.client.get('/api/v1/tenant/class-occurrences/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data.get('results', response.data)
        self.assertEqual(len(results), 1)

        # 2. Assign trainer custom action
        url = f'/api/v1/tenant/class-occurrences/{occurrence.id}/assign-trainer/'
        data = {
            'trainer_profile_id': str(self.trainer_profile.id),
            'trainer_role': 'LEAD',
        }
        res_assign = self.client.post(url, data, format='json')
        self.assertEqual(res_assign.status_code, status.HTTP_201_CREATED)
        self.assertEqual(str(res_assign.data.get('trainer_profile')), str(self.trainer_profile.id))

        # Check occurrence trainer record was created
        self.assertTrue(
            ClassOccurrenceTrainer.objects.using('tenant_test')
            .filter(occurrence=occurrence, trainer_profile=self.trainer_profile)
            .exists()
        )
