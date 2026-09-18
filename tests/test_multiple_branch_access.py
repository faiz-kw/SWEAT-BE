"""Multiple branch assignments and per-branch enable/disable regression tests."""
from types import SimpleNamespace
from django.test import TestCase
from rest_framework.exceptions import ValidationError, PermissionDenied
from tests.test_role_editing import RoleEditingTests
from apps.tenant_core.models_rbac import Role, RoleAssignment
from apps.tenant_core.serializers import TenantUserSerializer
from apps.tenant_core.views import TenantUserViewSet

class MultipleBranchAccessTests(TestCase):
    databases = {'tenant_test'}
    setUp = RoleEditingTests.setUp
    update = RoleEditingTests.update

    def save_access(self, a=True, b=True):
        self.update({'role_id': str(self.role.id), 'branch_access': [
            {'branch_id': str(self.a.id), 'enabled': a},
            {'branch_id': str(self.b.id), 'enabled': b},
        ]})

    def test_enable_disable_and_reenable_persists(self):
        self.save_access()
        self.assertEqual(RoleAssignment.objects.using('tenant_test').filter(user=self.user,is_active=True).count(),2)
        self.save_access(b=False)
        rows=TenantUserSerializer(self.user).data['branch_access']
        self.assertEqual({r['branch_id']:r['enabled'] for r in rows},{str(self.a.id):True,str(self.b.id):False})
        view=TenantUserViewSet(); view.request=SimpleNamespace(user=self.user)
        self.assertEqual(set(view.get_user_branch_scope()[1]),{self.a.id})
        self.save_access()
        self.assertEqual(RoleAssignment.objects.using('tenant_test').filter(user=self.user).count(),2)
        self.assertEqual(set(view.get_user_branch_scope()[1]),{self.a.id,self.b.id})

    def test_disable_all_removes_branch_scope(self):
        self.save_access(False,False)
        view=TenantUserViewSet();view.request=SimpleNamespace(user=self.user)
        self.assertEqual(view.get_user_branch_scope(),(False,[]))
        self.assertEqual(len(TenantUserSerializer(self.user).data['branch_access']),2)

    def test_saving_same_role_preserves_multiple_assignments(self):
        self.save_access()
        self.update({'role_id':str(self.role.id)})
        self.assertEqual(RoleAssignment.objects.using('tenant_test').filter(user=self.user,is_active=True).count(),2)

    def test_other_roles_are_preserved(self):
        other=Role.objects.using('tenant_test').create(organization=self.org,name='Other',code='OTHER',scope='BRANCH')
        ra=RoleAssignment.objects.using('tenant_test').create(organization=self.org,user=self.user,role=other,branch=self.b)
        self.save_access(False,False)
        ra.refresh_from_db(using='tenant_test');self.assertTrue(ra.is_active)

    def test_invalid_payload_is_atomic(self):
        with self.assertRaises(ValidationError):
            self.update({'role_id':str(self.role.id),'branch_access':[
                {'branch_id':str(self.a.id),'enabled':False},
                {'branch_id':'invalid','enabled':True},
            ]})
        self.assignment.refresh_from_db(using='tenant_test');self.assertTrue(self.assignment.is_active)
        with self.assertRaises(ValidationError):
            self.update({'role_id':str(self.role.id),'branch_access':[{'branch_id':str(self.a.id),'enabled':'false'}]})

    def test_branch_manager_cannot_grant_outside_their_scope(self):
        RoleAssignment.objects.using('tenant_test').filter(user=self.actor).update(is_active=False)
        RoleAssignment.objects.using('tenant_test').create(organization=self.org,user=self.actor,role=self.role,branch=self.a)
        with self.assertRaises(PermissionDenied):
            self.save_access()
