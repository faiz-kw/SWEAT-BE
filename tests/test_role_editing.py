"""Regression coverage for permission toggles and user role/branch editing."""
from types import SimpleNamespace
from unittest.mock import Mock, patch
from django.test import TestCase
from rest_framework.exceptions import ValidationError
from config.routers import set_tenant_db_alias
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_rbac import Role, RoleAssignment, ModuleCatalog, SubmoduleCatalog, Permission, RolePermissionSet, RolePermissionSetItem
from apps.tenant_core.views import TenantUserViewSet


class RoleEditingTests(TestCase):
    databases = {'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        self.addCleanup(set_tenant_db_alias, None)
        sync = patch('apps.master.services_auth_directory.sync_tenant_user_identity')
        sync.start()
        self.addCleanup(sync.stop)
        self.org = Organization.objects.using('tenant_test').create(name='Test Gym', code='GYM')
        location = Location.objects.using('tenant_test').create(organization=self.org, name='City', code='CITY')
        self.a = Branch.objects.using('tenant_test').create(organization=self.org, location=location, name='A', code='A')
        self.b = Branch.objects.using('tenant_test').create(organization=self.org, location=location, name='B', code='B')
        self.role = Role.objects.using('tenant_test').create(organization=self.org, name='Branch Staff', code='branch_staff', scope='BRANCH')
        self.user = TenantUser.objects.using('tenant_test').create(organization=self.org, email='staff@example.test', first_name='Staff', last_name='Test', home_branch=self.a)
        self.actor = TenantUser.objects.using('tenant_test').create(organization=self.org, email='admin@example.test', first_name='Admin', last_name='Test')
        admin_role = Role.objects.using('tenant_test').create(organization=self.org, name='Admin', code='ORG_ADMIN', scope='ORG')
        RoleAssignment.objects.using('tenant_test').create(organization=self.org, user=self.actor, role=admin_role, scope_type='ORGANIZATION')
        self.assignment = RoleAssignment.objects.using('tenant_test').create(organization=self.org, user=self.user, role=self.role, branch=self.a, scope_type='BRANCH')

    def update(self, data):
        view = TenantUserViewSet()
        view.request = SimpleNamespace(data=data, user=self.actor)
        serializer = Mock(instance=self.user)
        serializer.save.return_value = self.user
        with patch('apps.tenant_core.views.emit_audit_event'):
            view.perform_update(serializer)
        return serializer

    def test_permission_grant_and_revoke_survive_save_and_reload(self):
        module = ModuleCatalog.objects.using('tenant_test').create(module_code='core', name='Core')
        submodule = SubmoduleCatalog.objects.using('tenant_test').create(module=module, submodule_code='users', name='Users')
        permission = Permission.objects.using('tenant_test').create(module=module, submodule=submodule, permission_code='core.users.edit', action='edit', label='Edit users')
        permission_set = RolePermissionSet.objects.using('tenant_test').create(role=self.role, organization=self.org, name='Staff permissions')
        for granted in (False, True, False):
            item, _ = RolePermissionSetItem.objects.using('tenant_test').update_or_create(permission_set=permission_set, permission=permission, defaults={'granted': granted})
            item.refresh_from_db(using='tenant_test')
            self.assertEqual(item.granted, granted)
            self.assertEqual(item.is_allowed, granted)

    def test_role_and_branch_change_uses_selected_branch(self):
        self.update({'role_id': str(self.role.id), 'branch_id': str(self.b.id)})
        current = RoleAssignment.objects.using('tenant_test').get(user=self.user, is_active=True)
        self.assertEqual(current.branch_id, self.b.id)
        self.assertEqual(current.role_id, self.role.id)
        self.assignment.refresh_from_db(using='tenant_test')
        self.assertFalse(self.assignment.is_active)

    def test_branch_only_change_moves_existing_access(self):
        self.update({'branch_id': str(self.b.id)})
        self.assignment.refresh_from_db(using='tenant_test')
        self.assertEqual(self.assignment.branch_id, self.b.id)
        self.user.refresh_from_db(using='tenant_test')
        self.assertEqual(self.user.home_branch_id, self.b.id)
        # Repeating an edit must not create duplicate home associations.
        self.update({'branch_id': str(self.b.id)})
        self.update({'branch_id': str(self.a.id)})

    def test_invalid_role_and_branch_are_rejected(self):
        for data in ({'role_id': 'missing-role'}, {'branch_id': 'missing-branch'}):
            with self.assertRaises(ValidationError):
                self.update(data)
        self.assignment.refresh_from_db(using='tenant_test')
        self.assertTrue(self.assignment.is_active)
        self.assertEqual(self.assignment.branch_id, self.a.id)

    def test_org_role_remains_organization_scoped(self):
        role = Role.objects.using('tenant_test').create(organization=self.org, name='Org Staff', code='org_staff', scope='ORG')
        self.update({'role_id': str(role.id), 'branch_id': str(self.b.id)})
        current = RoleAssignment.objects.using('tenant_test').get(user=self.user, is_active=True)
        self.assertEqual(current.scope_type, 'ORGANIZATION')
        self.assertIsNone(current.branch_id)
