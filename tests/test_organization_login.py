"""Organization-scoped login regression tests; no live accounts are used."""
import uuid
from unittest.mock import patch
from django.test import TestCase
from django.core.exceptions import ValidationError
from rest_framework.test import APIRequestFactory
from apps.authentication.views import UniversalLoginView
from config.routers import set_tenant_db_alias
from apps.master.models_iam import PlatformUser, AuthenticationIdentity
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.services_auth_directory import register_identity, resolve_identity, sync_tenant_user_identity
from apps.tenant_core.models_org import Organization
from apps.tenant_core.models_users import TenantUser


class OrganizationLoginTests(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias(None)
        self.addCleanup(set_tenant_db_alias, None)
        for name, result in [('check_login_lockout', (False, 0)), ('record_login_failure', (False, 0)), ('reset_login_lockout', None)]:
            mock = patch('apps.authentication.views.' + name, return_value=result)
            mock.start()
            self.addCleanup(mock.stop)
        throttle = patch.object(UniversalLoginView, 'throttle_classes', [])
        throttle.start()
        self.addCleanup(throttle.stop)
        self.platform = PlatformUser.objects.create(email='shared@example.test', first_name='Super', last_name='Admin', status='ACTIVE', is_superuser=True)
        self.platform.set_password('MasterPass123!')
        self.platform.save()
        self.tenant = Tenant.objects.create(name='Gym A', slug='gym-a', code='GYM-A', status='ACTIVE')
        TenantDataSource.objects.create(tenant=self.tenant, db_name='tenant_test', status='ACTIVE')
        self.org = Organization.objects.using('tenant_test').create(name='Gym A', code='GYM-A', status='ACTIVE')
        self.user = TenantUser.objects.using('tenant_test').create(organization_id=self.org.id, email=self.platform.email, username='member', first_name='Gym', last_name='Member', status='ACTIVE')
        self.user.set_password('TenantPass123!')
        self.user.save(using='tenant_test')
        sync_tenant_user_identity(self.user, self.tenant.id)
        def select_tenant(tenant):
            set_tenant_db_alias('tenant_test')
            return 'tenant_test'
        resolver = patch('apps.authentication.views._register_and_resolve_tenant', side_effect=select_tenant)
        self.resolver = resolver.start()
        self.addCleanup(resolver.stop)

    def login(self, password, code=None, identifier='shared@example.test'):
        payload = {'identifier': identifier, 'password': password}
        if code is not None:
            payload['tenant_slug'] = code
        return UniversalLoginView.as_view()(APIRequestFactory().post('/api/v1/auth/login/', payload, format='json'))

    def test_blank_code_authenticates_master_superadmin_only(self):
        AuthenticationIdentity.objects.filter(account_type='PLATFORM').delete()
        response = self.login('MasterPass123!')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['user_type'], 'platform')
        self.resolver.assert_not_called()

    def test_tenant_code_uses_tenant_password_and_database(self):
        response = self.login('TenantPass123!', ' GYM-A ')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['user_type'], 'tenant')
        self.assertEqual(response.data['tenant']['id'], str(self.tenant.id))
        self.resolver.assert_called_with(self.tenant)
        self.assertEqual(self.login('MasterPass123!', 'gym-a').status_code, 401)

    def test_missing_code_prompts_without_searching_tenants(self):
        response = self.login('TenantPass123!')
        self.assertEqual(response.data['code'], 'organization_required')
        self.assertNotIn('access', response.data)
        self.resolver.assert_not_called()

    def test_non_superuser_and_inactive_superadmin_cannot_use_blank_code(self):
        for fields in ({'is_superuser': False}, {'is_superuser': True, 'is_active': False}):
            PlatformUser.objects.filter(pk=self.platform.pk).update(**fields)
            response = self.login('MasterPass123!')
            self.assertEqual(response.data['code'], 'organization_required')
            self.assertNotIn('access', response.data)

    def test_invalid_organization_never_falls_back_to_master(self):
        response = self.login('MasterPass123!', 'unknown-gym')
        self.assertEqual(response.status_code, 401)
        self.resolver.assert_not_called()

    def test_same_email_can_be_registered_in_multiple_gyms(self):
        other = uuid.uuid4()
        subject = uuid.uuid4()
        register_identity(self.user.email, 'TENANT', subject, tenant_id=other)
        self.assertEqual(resolve_identity(self.user.email, 'TENANT', other).subject_id, subject)
        self.assertEqual(resolve_identity(self.user.email, 'TENANT', self.tenant.id).subject_id, self.user.id)
        self.assertIsNone(resolve_identity(self.user.email))
        with self.assertRaises(ValidationError):
            register_identity(self.user.email, 'TENANT', uuid.uuid4(), tenant_id=other)

    def test_missing_directory_entry_backfills_only_selected_gym(self):
        AuthenticationIdentity.objects.filter(account_type='TENANT').delete()
        self.assertEqual(self.login('TenantPass123!', 'gym-a').status_code, 200)
        self.assertEqual(resolve_identity(self.user.email, 'TENANT', self.tenant.id).subject_id, self.user.id)

    def test_disabled_tenant_user_is_blocked(self):
        TenantUser.objects.using('tenant_test').filter(pk=self.user.pk).update(is_login_allowed=False)
        self.assertEqual(self.login('TenantPass123!', 'gym-a').status_code, 403)

    def test_gym_code_selects_the_matching_identity_for_shared_email(self):
        from rest_framework.response import Response
        other = Tenant.objects.create(name='Gym B', slug='gym-b', code='GYM-B', status='ACTIVE')
        subject = uuid.uuid4()
        register_identity(self.user.email, 'TENANT', subject, tenant_id=other.id)
        with patch.object(UniversalLoginView, '_tenant_login', return_value=Response({})) as authenticate:
            self.login('GymBPassword!', 'gym-b')
            selected = authenticate.call_args.args[1]
            self.assertEqual(selected.tenant_id, other.id)
            self.assertEqual(selected.subject_id, subject)
        with self.assertRaises(ValidationError):
            TenantUser.objects.using('tenant_test').create(
                organization_id=self.org.id, email=self.user.email.upper(),
                first_name='Duplicate', last_name='Member', status='ACTIVE',
            )
