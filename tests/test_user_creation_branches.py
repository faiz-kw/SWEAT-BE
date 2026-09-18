"""User creation accepts the branch aliases sent by the admin form."""
import uuid
from unittest.mock import patch
from django.test import TestCase
from tests.test_role_editing import RoleEditingTests
from apps.tenant_core.serializers import TenantUserCreateSerializer
from apps.tenant_core.models_users import UserBranch
from apps.tenant_core.models_rbac import RoleAssignment

class UserCreationBranchTests(TestCase):
    databases = {'tenant_test'}
    setUp = RoleEditingTests.setUp

    def create_user(self, aliases, password='Staff!7kWm9xQ42'):
        payload = {
            'email': f'{uuid.uuid4().hex}@example.test',
            'first_name': 'New', 'last_name': 'Staff',
            'role_id': str(self.role.id), 'password': password,
            **{name: str(self.b.id) for name in aliases},
        }
        serializer = TenantUserCreateSerializer(data=payload, context={
            'db_alias': 'tenant_test', 'tenant_id': str(uuid.uuid4()),
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        with patch('apps.master.quota.QuotaChecker.assert_quota_available') as quota:
            user = serializer.save(organization=self.org)
            quota.assert_called_once()
        user.refresh_from_db(using='tenant_test')
        self.assertEqual(user.home_branch_id, self.b.id)
        assignment = RoleAssignment.objects.using('tenant_test').get(user=user, is_active=True)
        self.assertEqual(assignment.branch_id, self.b.id)
        self.assertEqual(assignment.role_id, self.role.id)
        self.assertTrue(UserBranch.objects.using('tenant_test').filter(user=user, branch=self.b, scope_type='HOME').exists())
        self.assertEqual(serializer.data['role_name'], self.role.name)
        return user

    def test_create_with_all_frontend_branch_aliases(self):
        user = self.create_user(['branch', 'branch_id', 'home_branch'])
        self.assertTrue(user.check_password('Staff!7kWm9xQ42'))
        self.assertEqual(user.status, 'ACTIVE')

    def test_create_with_each_individual_alias(self):
        for alias in ['branch', 'branch_id', 'home_branch']:
            with self.subTest(alias=alias):
                self.create_user([alias])

    def test_invitation_with_all_aliases(self):
        user = self.create_user(['branch', 'branch_id', 'home_branch'], password='')
        self.assertEqual(user.status, 'INVITED')
