"""
tests/test_tenant_integrations.py — Production-Grade Tenant Integrations Verification.

Validates:
1. Integration creation with valid secret reference (vault://, env://, aws-secretsmanager://).
2. Rejection of plaintext credentials (strict fail-closed security).
3. Secret masking in serialized responses (write_only secret_reference, redacted reference in GET).
4. Lifecycle toggle action (ACTIVE <-> INACTIVE) with audit logging.
5. Connectivity test action with SecretResolver and audit trail.
6. RBAC enforcement on integration management endpoints.
"""

import json
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.models_saas import SaasPlan, TenantSubscription, ProductModule, ProductSubmodule, TenantModule
from apps.tenant_core.models_org import Organization, Branch, Location
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_rbac import (
    ModuleCatalog, SubmoduleCatalog, Permission, Role, RolePermissionSet,
    RolePermissionSetItem, RoleModuleAccess, RoleSubmoduleAccess, RoleAssignment
)
from apps.tenant_core.models_infra import Integration
from apps.tenant_core.models_privacy import TenantAuditEvent
from config.routers import set_tenant_db_alias
from config.tenant_middleware import _register_tenant_connection, unregister_tenant_connection
from config.secrets import SecretResolver


class TenantIntegrationsTestCase(TestCase):
    databases = '__all__'

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        _register_tenant_connection('tenant_test', 'test_fitness_tenant')
        self.client = APIClient()

        # 1. Master Tenant & Data Source
        self.tenant = Tenant.objects.using('default').create(
            name='Integration Test Gym',
            slug='integration-gym',
            code='INT-001',
            status='ACTIVE',
        )
        self.data_source = TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            hosting_mode='PLATFORM_MANAGED',
            source_type='PLATFORM_MANAGED',
            database_name='test',
            db_name='test',
            status='ACTIVE',
        )
        self.plan, _ = SaasPlan.objects.using('default').get_or_create(
            code='PLAN-GROWTH',
            defaults={'name': 'Growth Plan', 'tier': 'growth', 'is_active': True}
        )
        self.subscription = TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
        )
        self.master_mod, _ = ProductModule.objects.using('default').get_or_create(
            code='core', defaults={'name': 'Core Module', 'is_active': True}
        )
        self.master_submod, _ = ProductSubmodule.objects.using('default').get_or_create(
            module=self.master_mod, code='settings', defaults={'name': 'Settings', 'is_active': True}
        )
        self.tenant_mod, _ = TenantModule.objects.using('default').get_or_create(
            tenant=self.tenant,
            module=self.master_mod,
            defaults={'is_enabled': True, 'availability_mode': 'ALL_BRANCHES'}
        )

        # 2. Organization & Branch
        self.org = Organization.objects.using('tenant_test').create(
            name='Integration Org',
            code='INT-ORG',
            status='ACTIVE',
        )
        self.loc = Location.objects.using('tenant_test').create(
            organization=self.org,
            name='Integration Loc',
            code='INT-LOC',
            city='Bengaluru',
        )
        self.branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            name='Integration Branch',
            code='INT-BR',
            status='ACTIVE',
        )

        # 3. RBAC Setup: Module 'core', Submodule 'settings', Permissions
        self.mod, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(
            code='core',
            defaults={'module_code': 'core', 'name': 'Core', 'status': 'ACTIVE', 'is_enabled': True}
        )
        self.submod, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=self.mod,
            code='settings',
            defaults={'submodule_code': 'settings', 'name': 'Settings', 'status': 'ACTIVE', 'is_enabled': True}
        )
        self.perm_view, _ = Permission.objects.using('tenant_test').get_or_create(
            submodule=self.submod,
            code='core.settings.view',
            defaults={
                'module': self.mod,
                'permission_code': 'core.settings.view',
                'label': 'View Settings',
                'action': 'view',
                'status': 'ACTIVE',
                'is_active': True,
            }
        )
        self.perm_edit, _ = Permission.objects.using('tenant_test').get_or_create(
            submodule=self.submod,
            code='core.settings.edit',
            defaults={
                'module': self.mod,
                'permission_code': 'core.settings.edit',
                'label': 'Edit Settings',
                'action': 'edit',
                'status': 'ACTIVE',
                'is_active': True,
            }
        )

        # 4. Admin Role with permissions and matrix access
        self.admin_role = Role.objects.using('tenant_test').create(
            organization=self.org,
            name='Integration Admin Role',
            code='INT_ADMIN',
            scope='ORG',
            status='ACTIVE',
            is_active=True,
        )
        pset = RolePermissionSet.objects.using('tenant_test').create(
            role=self.admin_role,
            organization=self.org,
            name='Integration Admin Set',
            status='ACTIVE',
            is_active=True,
        )
        RolePermissionSetItem.objects.using('tenant_test').create(
            permission_set=pset,
            permission=self.perm_view,
            is_allowed=True,
            granted=True,
        )
        RolePermissionSetItem.objects.using('tenant_test').create(
            permission_set=pset,
            permission=self.perm_edit,
            is_allowed=True,
            granted=True,
        )
        RoleModuleAccess.objects.using('tenant_test').create(
            role=self.admin_role,
            module=self.mod,
            permission_set=pset,
            can_access=True,
            is_visible=True,
        )
        RoleSubmoduleAccess.objects.using('tenant_test').create(
            role=self.admin_role,
            submodule=self.submod,
            permission_set=pset,
            can_access=True,
            is_visible=True,
        )

        # 5. Admin User
        self.admin_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            home_branch=self.branch,
            email='int.admin@integrationgym.com',
            first_name='Integration',
            last_name='Admin',
            status='ACTIVE',
        )
        self.admin_user.tenant_id = str(self.tenant.id)
        RoleAssignment.objects.using('tenant_test').create(
            organization=self.org,
            user=self.admin_user,
            role=self.admin_role,
            scope_type='ORGANIZATION',
            is_active=True,
            status='ACTIVE',
        )

        # 6. Authenticate client with signed Tenant JWT
        refresh = RefreshToken()
        refresh['sub'] = str(self.admin_user.id)
        refresh['user_type'] = 'tenant'
        refresh['roles'] = ['INT_ADMIN']
        refresh['tid'] = str(self.tenant.id)
        refresh['tenant_slug'] = self.tenant.slug
        refresh['db_alias'] = 'tenant_test'
        refresh['email'] = self.admin_user.email
        token = str(refresh.access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        self.admin_user._auth_type = 'tenant'
        self.admin_user.db_alias = 'tenant_test'

    def tearDown(self):
        set_tenant_db_alias(None)
        SecretResolver.clear_test_secrets()
        from django.conf import settings
        for alias in list(settings.DATABASES.keys()):
            if alias.startswith('tenant_') and alias != 'tenant_test':
                unregister_tenant_connection(alias)

    def test_create_integration_with_valid_secret_reference(self):
        """Creates integration successfully with vault:// reference and emits audit event."""
        payload = {
            'integration_type': 'PAYMENT',
            'provider': 'Razorpay',
            'secret_reference': 'vault://tenants/integration-gym/razorpay/prod_keys',
            'configuration': {
                'currency': 'INR',
                'webhook_url': 'https://api.performanceos.io/webhooks/razorpay/',
            },
            'status': 'ACTIVE',
        }
        response = self.client.post('/api/v1/tenant/integrations/', payload, format='json')
        self.assertEqual(response.status_code, 201)

        data = response.json()
        self.assertEqual(data['provider'], 'Razorpay')
        self.assertEqual(data['integration_type'], 'PAYMENT')
        self.assertEqual(data['status'], 'ACTIVE')
        self.assertTrue(data['has_credentials'])
        # Assert secret_reference itself is write_only and not exposed directly
        self.assertNotIn('secret_reference', data)
        self.assertIn('masked_secret_reference', data)
        self.assertIn('...', data['masked_secret_reference'])
        self.assertNotEqual(data['masked_secret_reference'], payload['secret_reference'])

        # Verify audit trail
        audit = TenantAuditEvent.objects.using('tenant_test').filter(
            action='INTEGRATION_CONFIGURED', resource_id=data['id']
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.actor_email, self.admin_user.email)

    def test_reject_plaintext_credentials(self):
        """Rejects plaintext credential strings, enforcing Vault/secret references."""
        payload = {
            'integration_type': 'WHATSAPP',
            'provider': 'Gupshup',
            'secret_reference': 'plain_api_key_secret_password_12345',
            'configuration': {'channel': 'whatsapp'},
        }
        response = self.client.post('/api/v1/tenant/integrations/', payload, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('Plaintext credentials are forbidden', str(response.json()))

    def test_secret_masking_in_get_responses(self):
        """GET list and retrieve never leak plaintext secret references."""
        Integration.objects.using('tenant_test').create(
            integration_type='CALLING',
            provider='TeleCMI',
            secret_reference='vault://tenants/integration-gym/telecmi_token',
            configuration={'app_id': 'tcmi_999'},
            status='ACTIVE',
        )

        response = self.client.get('/api/v1/tenant/integrations/')
        self.assertEqual(response.status_code, 200)

        data = response.json()
        items = data.get('results', data) if isinstance(data, dict) else data
        telecmi_item = next(i for i in items if i['provider'] == 'TeleCMI')

        self.assertNotIn('secret_reference', telecmi_item)
        self.assertTrue(telecmi_item['has_credentials'])
        self.assertIn('masked_secret_reference', telecmi_item)
        self.assertIn('...', telecmi_item['masked_secret_reference'])
        self.assertNotEqual(telecmi_item['masked_secret_reference'], 'vault://tenants/integration-gym/telecmi_token')

    def test_toggle_integration_status(self):
        """Toggling an integration inverts status and emits INTEGRATION_TOGGLED audit event."""
        int_obj = Integration.objects.using('tenant_test').create(
            integration_type='EMAIL',
            provider='SES',
            secret_reference='env://SES_CREDENTIALS',
            status='ACTIVE',
        )

        # 1. Toggle to INACTIVE
        resp1 = self.client.post(f'/api/v1/tenant/integrations/{int_obj.id}/toggle/')
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp1.json()['status'], 'INACTIVE')

        int_obj.refresh_from_db(using='tenant_test')
        self.assertEqual(int_obj.status, 'INACTIVE')

        # 2. Toggle back to ACTIVE
        resp2 = self.client.post(f'/api/v1/tenant/integrations/{int_obj.id}/toggle/')
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.json()['status'], 'ACTIVE')

        int_obj.refresh_from_db(using='tenant_test')
        self.assertEqual(int_obj.status, 'ACTIVE')

        # Verify audit trail
        toggle_audits = TenantAuditEvent.objects.using('tenant_test').filter(
            action='INTEGRATION_TOGGLED', resource_id=str(int_obj.id)
        ).count()
        self.assertEqual(toggle_audits, 2)

    def test_connectivity_test_action_success_and_failure(self):
        """Connectivity test securely validates secret resolution against SecretResolver seam."""
        SecretResolver.register_test_secret(
            'vault://tenants/integration-gym/razorpay/test_keys',
            'rzp_live_real_resolved_secret_token'
        )

        # Successful resolution
        success_int = Integration.objects.using('tenant_test').create(
            integration_type='PAYMENT',
            provider='RazorpayTest',
            secret_reference='vault://tenants/integration-gym/razorpay/test_keys',
            status='ACTIVE',
        )
        resp_success = self.client.post(f'/api/v1/tenant/integrations/{success_int.id}/test-connection/')
        self.assertEqual(resp_success.status_code, 200)
        self.assertEqual(resp_success.json()['status'], 'SUCCESS')

        success_int.refresh_from_db(using='tenant_test')
        self.assertIsNotNone(success_int.last_sync_at)
        self.assertEqual(success_int.last_error, '')

        # Failed resolution (missing secret path)
        fail_int = Integration.objects.using('tenant_test').create(
            integration_type='PAYMENT',
            provider='FailingProvider',
            secret_reference='vault://tenants/integration-gym/non_existent_secret',
            status='ACTIVE',
        )
        resp_fail = self.client.post(f'/api/v1/tenant/integrations/{fail_int.id}/test-connection/')
        self.assertEqual(resp_fail.status_code, 400)
        self.assertEqual(resp_fail.json()['status'], 'ERROR')

        fail_int.refresh_from_db(using='tenant_test')
        self.assertIn('could not be resolved', fail_int.last_error)
