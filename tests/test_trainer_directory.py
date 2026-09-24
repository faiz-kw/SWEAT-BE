from datetime import timedelta
from types import SimpleNamespace

from django.test import TestCase
from django.utils import timezone

from config.routers import set_tenant_db_alias
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_rbac import Role, RoleAssignment
from apps.tenant_core.models_workforce import UserProfile, EmployeeProfile, TrainerProfile
from apps.tenant_core.views_workforce import TrainerProfileViewSet


class TrainerDirectoryTests(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        self.addCleanup(set_tenant_db_alias, None)
        self.org = Organization.objects.create(code='DIRECTORY', name='Directory', status='ACTIVE')
        location = Location.objects.create(organization=self.org, code='LOC', name='Andheri', city='Mumbai')
        self.branch = Branch.objects.create(organization=self.org, location=location, code='BR', name='Andheri')

    def staff(self, code, polluted=False):
        user = TenantUser.objects.create(
            organization=self.org, email=f'{code.lower()}@example.test',
            user_type='STAFF', status='ACTIVE', home_branch=self.branch,
        )
        role = Role.objects.create(organization=self.org, code=code, name=code.replace('_', ' '))
        assignment = RoleAssignment.objects.create(organization=self.org, user=user, role=role)
        if polluted:
            profile = UserProfile.objects.create(user=user)
            employee = EmployeeProfile.objects.create(
                user_profile=profile, organization=self.org,
                employee_code=code, designation='Fitness Trainer',
            )
            TrainerProfile.objects.create(employee_profile=employee, trainer_code=code)
        return user, assignment

    def directory(self, branch=None):
        view = TrainerProfileViewSet()
        view.request = SimpleNamespace(
            organization=self.org, query_params={'branch_id': str(branch.pk)} if branch else {},
        )
        return set(view.get_queryset().values_list('employee_profile__user_profile__user_id', flat=True))

    def test_existing_nontrainer_profiles_hidden_without_deletion(self):
        trainer, _ = self.staff('TRAINER', polluted=True)
        self.staff('ORG_ADMIN', polluted=True)
        self.staff('SALES_STAFF', polluted=True)
        self.assertEqual(self.directory(), {trainer.pk})
        self.assertEqual(self.directory(self.branch), {trainer.pk})
        self.assertEqual(TrainerProfile.objects.count(), 3)

    def test_only_explicit_training_roles_auto_sync(self):
        trainers = {self.staff(code)[0].pk for code in ['TRAINER', 'HEAD_COACH', 'YOGA_INSTRUCTOR']}
        for code in ['ORG_ADMIN', 'SALES_STAFF', 'STAFF', 'RECEPTIONIST', 'NONTRAINER']:
            self.staff(code)
        self.assertEqual(self.directory(), trainers)
        self.assertEqual(TrainerProfile.objects.count(), 3)

    def test_inactive_expired_and_disabled_roles_excluded(self):
        _, inactive = self.staff('TRAINER', polluted=True)
        inactive.status = 'INACTIVE'
        inactive.save()
        _, expired = self.staff('COACH', polluted=True)
        expired.expires_at = timezone.now() - timedelta(days=1)
        expired.save()
        _, disabled = self.staff('INSTRUCTOR', polluted=True)
        disabled.role.status = 'INACTIVE'
        disabled.role.save()
        self.assertEqual(self.directory(), set())
