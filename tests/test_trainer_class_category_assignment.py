"""
backend/tests/test_trainer_class_category_assignment.py

Unit and integration tests for Class Category Assignment on Trainer Profiles:
- Relational persistence in TrainerClassCategoryAssignment
- Serialization and deserialization in TrainerProfileSerializer
- Category multi-select and update/removal synchronization
- Integration with TrainerAvailabilityService
- Integration with ClassSchedulingService.assign_trainer_to_occurrence
"""

from types import SimpleNamespace
from django.test import TestCase
from django.utils import timezone
from django.core.exceptions import ValidationError as DjangoValidationError

from config.routers import set_tenant_db_alias
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_rbac import Role, RoleAssignment
from apps.tenant_core.models_workforce import (
    UserProfile,
    EmployeeProfile,
    TrainerProfile,
    EmployeeWorkSchedule,
    TrainerClassCategoryAssignment,
)
from apps.tenant_core.models_classes import (
    ClassCategory,
    ClassTemplate,
    ClassOccurrence,
)
from apps.tenant_core.serializers_workforce import TrainerProfileSerializer
from apps.tenant_core.services_workforce import TrainerAvailabilityService
from apps.tenant_core.services_classes import ClassSchedulingService


class TrainerClassCategoryAssignmentTests(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        self.addCleanup(set_tenant_db_alias, None)

        self.org = Organization.objects.create(code='ORG_CAT_TEST', name='Category Test Org', status='ACTIVE')
        location = Location.objects.create(organization=self.org, code='LOC1', name='Andheri', city='Mumbai')
        self.branch = Branch.objects.create(organization=self.org, location=location, code='BR1', name='Andheri West', status='ACTIVE')

        # Create user & trainer
        self.user = TenantUser.objects.create(
            organization=self.org,
            email='trainer_cat@example.test',
            user_type='TRAINER',
            status='ACTIVE',
            first_name='Praveen',
            last_name='Nair',
            home_branch=self.branch,
        )
        role = Role.objects.create(organization=self.org, code='TRAINER', name='Fitness Trainer')
        RoleAssignment.objects.create(organization=self.org, user=self.user, role=role)

        self.user_profile = UserProfile.objects.create(user=self.user, first_name_snapshot='Praveen', last_name_snapshot='Nair')
        self.emp = EmployeeProfile.objects.create(
            user_profile=self.user_profile,
            organization=self.org,
            employee_code='EMP-CAT-01',
            employment_status='ACTIVE',
        )
        self.trainer = TrainerProfile.objects.create(
            employee_profile=self.emp,
            trainer_code='TRN-CAT-01',
            trainer_status='ACTIVE',
            can_teach_all_specialties=False,
            minimum_schedule_buffer_minutes=15,
        )

        from datetime import time, date
        for day in range(1, 8):
            EmployeeWorkSchedule.objects.create(
                employee_profile=self.emp,
                branch=self.branch,
                day_of_week=day,
                start_time=time(0, 0),
                end_time=time(23, 59),
                valid_from=date(2020, 1, 1),
                schedule_type='REGULAR',
                status='ACTIVE',
            )

        # Create Class Categories
        self.cat_pilates = ClassCategory.objects.create(
            organization=self.org,
            code='PILATES',
            name='Sweat Pilates',
            status='ACTIVE',
            display_order=1,
        )
        self.cat_bootcamp = ClassCategory.objects.create(
            organization=self.org,
            code='BOOTCAMP',
            name='Sweat Bootcamp',
            status='ACTIVE',
            display_order=2,
        )
        self.cat_strength = ClassCategory.objects.create(
            organization=self.org,
            code='STRENGTH',
            name='Strength & Conditioning',
            status='ACTIVE',
            display_order=3,
        )

    def test_assign_multiple_categories_via_serializer(self):
        """Verify multiple class categories can be assigned to a trainer profile."""
        cat_ids = [str(self.cat_pilates.id), str(self.cat_bootcamp.id)]
        serializer = TrainerProfileSerializer(
            instance=self.trainer,
            data={'category_ids': cat_ids},
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()

        # Verify DB records
        assignments = TrainerClassCategoryAssignment.objects.filter(trainer_profile=self.trainer)
        self.assertEqual(assignments.count(), 2)
        assigned_cids = set(assignments.values_list('class_category_id', flat=True))
        self.assertEqual(assigned_cids, {self.cat_pilates.id, self.cat_bootcamp.id})

        # Verify representation output
        data = TrainerProfileSerializer(instance=self.trainer).data
        self.assertEqual(set(data['category_ids']), set(cat_ids))
        self.assertEqual(set(data['category_names']), {'Sweat Pilates', 'Sweat Bootcamp'})
        self.assertEqual(len(data['categories']), 2)

    def test_update_and_remove_category_assignments(self):
        """Verify categories can be added or removed dynamically."""
        # Initial assignment: Pilates + Bootcamp
        TrainerClassCategoryAssignment.objects.create(
            trainer_profile=self.trainer,
            class_category=self.cat_pilates,
        )
        TrainerClassCategoryAssignment.objects.create(
            trainer_profile=self.trainer,
            class_category=self.cat_bootcamp,
        )

        # Update to Bootcamp + Strength (removes Pilates, adds Strength)
        new_cat_ids = [str(self.cat_bootcamp.id), str(self.cat_strength.id)]
        serializer = TrainerProfileSerializer(
            instance=self.trainer,
            data={'category_ids': new_cat_ids},
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()

        assignments = TrainerClassCategoryAssignment.objects.filter(trainer_profile=self.trainer)
        assigned_cids = set(assignments.values_list('class_category_id', flat=True))
        self.assertEqual(assigned_cids, {self.cat_bootcamp.id, self.cat_strength.id})

    def test_clear_all_categories(self):
        """Verify setting category_ids to empty list removes all assignments."""
        TrainerClassCategoryAssignment.objects.create(
            trainer_profile=self.trainer,
            class_category=self.cat_pilates,
        )
        serializer = TrainerProfileSerializer(
            instance=self.trainer,
            data={'category_ids': []},
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()

        self.assertEqual(TrainerClassCategoryAssignment.objects.filter(trainer_profile=self.trainer).count(), 0)

    def test_trainer_availability_service_category_check(self):
        """Verify TrainerAvailabilityService enforces class category eligibility."""
        TrainerClassCategoryAssignment.objects.create(
            trainer_profile=self.trainer,
            class_category=self.cat_pilates,
        )

        start = timezone.now() + timezone.timedelta(days=1)

        # Check for assigned category (Pilates) -> should pass
        avail, reason, details = TrainerAvailabilityService.is_trainer_available(
            trainer=self.trainer,
            branch=self.branch,
            start_datetime=start,
            duration_minutes=50,
            category_id=str(self.cat_pilates.id),
            db_alias='tenant_test',
        )
        self.assertTrue(avail, f"Expected available but got: {reason}")

        # Check for unassigned category (Bootcamp) -> should fail
        avail_bc, reason_bc, details_bc = TrainerAvailabilityService.is_trainer_available(
            trainer=self.trainer,
            branch=self.branch,
            start_datetime=start,
            duration_minutes=50,
            category_id=str(self.cat_bootcamp.id),
            db_alias='tenant_test',
        )
        self.assertFalse(avail_bc)
        self.assertEqual(details_bc.get('code'), 'CATEGORY_NOT_ASSIGNED')

    def test_class_occurrence_assignment_enforces_category(self):
        """Verify ClassSchedulingService.assign_trainer_to_occurrence validates category suitability."""
        TrainerClassCategoryAssignment.objects.create(
            trainer_profile=self.trainer,
            class_category=self.cat_pilates,
        )

        # Create templates
        tpl_pilates = ClassTemplate.objects.create(
            organization=self.org,
            category=self.cat_pilates,
            code='TPL_PILATES',
            name='Pilates Matwork',
            default_duration_minutes=50,
        )
        tpl_bootcamp = ClassTemplate.objects.create(
            organization=self.org,
            category=self.cat_bootcamp,
            code='TPL_BOOTCAMP',
            name='Sweat Bootcamp Total',
            default_duration_minutes=60,
        )

        start_time = timezone.now() + timezone.timedelta(days=2)
        end_time = start_time + timezone.timedelta(minutes=50)

        occ_pilates = ClassOccurrence.objects.create(
            class_template=tpl_pilates,
            branch=self.branch,
            occurrence_date=start_time.date(),
            start_at=start_time,
            end_at=end_time,
            status='SCHEDULED',
        )
        occ_bootcamp = ClassOccurrence.objects.create(
            class_template=tpl_bootcamp,
            branch=self.branch,
            occurrence_date=start_time.date(),
            start_at=start_time,
            end_at=end_time,
            status='SCHEDULED',
        )

        # Assigning to Pilates occurrence (trainer has Pilates category) -> succeeds
        actor = self.user
        assignment = ClassSchedulingService.assign_trainer_to_occurrence(
            occurrence_id=str(occ_pilates.id),
            trainer_profile_id=str(self.trainer.id),
            actor=actor,
            db_alias='tenant_test',
        )
        self.assertIsNotNone(assignment)
        self.assertEqual(assignment.trainer_profile_id, self.trainer.id)

        # Assigning to Bootcamp occurrence (trainer lacks Bootcamp category) -> raises ValidationError
        with self.assertRaises(DjangoValidationError) as ctx:
            ClassSchedulingService.assign_trainer_to_occurrence(
                occurrence_id=str(occ_bootcamp.id),
                trainer_profile_id=str(self.trainer.id),
                actor=actor,
                db_alias='tenant_test',
            )
        self.assertIn("not qualified or assigned to conduct 'Sweat Bootcamp' classes", str(ctx.exception))
