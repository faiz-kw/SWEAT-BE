import unittest
import os
import uuid
import time
from unittest.mock import patch
from django.test import TestCase, RequestFactory
from rest_framework.exceptions import PermissionDenied, ValidationError, AuthenticationFailed

from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.models_saas import SaasPlan, TenantSubscription, ProductModule, TenantModule
from apps.tenant_core.context import tenant_database_context
from apps.tenant_core.models_org import Organization
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
from apps.tenant_core.views_meta_leads import MetaLeadMappingViewSet
from apps.tenant_core.services_meta_graph import generate_oauth_state, verify_oauth_state


class MetaOAuthCallbackRegressionTests(unittest.TestCase):
    # unittest.TestCase allows all tenant db connections

    def setUp(self):
        self.factory = RequestFactory()
        self.tenant = Tenant.objects.using('default').filter(slug='sweat').first()
        if not self.tenant:
            self.tenant = Tenant.objects.using('default').create(
                slug='sweat', name='SWEAT', status='ACTIVE'
            )
        TenantDataSource.objects.using('default').get_or_create(
            tenant=self.tenant,
            defaults={
                'db_name': 'test_fitness_tenant',
                'database_name': 'test_fitness_tenant',
                'status': 'ACTIVE',
                'database_engine': 'POSTGRESQL',
            }
        )
        plan, _ = SaasPlan.objects.using('default').get_or_create(
            code='SWEAT-PRO-TEST',
            defaults={'name': 'SWEAT Pro Test Plan', 'tier': 'ENTERPRISE', 'status': 'ACTIVE'}
        )
        TenantSubscription.objects.using('default').get_or_create(
            tenant=self.tenant,
            defaults={'plan': plan, 'status': 'ACTIVE'}
        )
        prod_mod_crm, _ = ProductModule.objects.using('default').get_or_create(
            code='crm', defaults={'name': 'CRM Module', 'status': 'ACTIVE'}
        )
        TenantModule.objects.using('default').get_or_create(
            tenant=self.tenant,
            module=prod_mod_crm,
            defaults={'is_enabled': True, 'availability_mode': 'ALL_BRANCHES'},
        )
        with tenant_database_context(str(self.tenant.id)) as alias:
            self.alias = alias
            self.org = Organization.objects.using(alias).filter(status='ACTIVE').first()
            if not self.org:
                self.org = Organization.objects.using(alias).create(
                    name='SWEAT UAT Organization', status='ACTIVE'
                )

            mod_crm, _ = ModuleCatalog.objects.using(alias).get_or_create(
                module_code='crm', defaults={'name': 'CRM', 'is_enabled': True}
            )
            submod_leads, _ = SubmoduleCatalog.objects.using(alias).get_or_create(
                module=mod_crm, submodule_code='leads', defaults={'name': 'Leads', 'is_enabled': True}
            )
            submod_settings, _ = SubmoduleCatalog.objects.using(alias).get_or_create(
                module=mod_crm, submodule_code='settings', defaults={'name': 'Settings', 'is_enabled': True}
            )
            perm_edit, _ = Permission.objects.using(alias).get_or_create(
                permission_code='crm.settings.edit',
                defaults={
                    'code': 'crm.settings.edit',
                    'label': 'crm.settings.edit',
                    'action': 'edit',
                    'module': mod_crm,
                    'submodule': submod_settings,
                    'source_permission_id': uuid.uuid4(),
                    'is_active': True,
                },
            )
            perm_view, _ = Permission.objects.using(alias).get_or_create(
                permission_code='crm.settings.view',
                defaults={
                    'code': 'crm.settings.view',
                    'label': 'crm.settings.view',
                    'action': 'view',
                    'module': mod_crm,
                    'submodule': submod_settings,
                    'source_permission_id': uuid.uuid4(),
                    'is_active': True,
                },
            )
            role_admin, _ = Role.objects.using(alias).get_or_create(
                organization=self.org,
                code='ORG_ADMIN',
                defaults={'name': 'Organization Admin', 'scope': 'ORG', 'is_active': True},
            )
            RoleModuleAccess.objects.using(alias).get_or_create(role=role_admin, module=mod_crm, defaults={'can_access': True})
            RoleSubmoduleAccess.objects.using(alias).get_or_create(role=role_admin, submodule=submod_leads, defaults={'can_access': True})
            RoleSubmoduleAccess.objects.using(alias).get_or_create(role=role_admin, submodule=submod_settings, defaults={'can_access': True})
            ps, _ = RolePermissionSet.objects.using(alias).get_or_create(role=role_admin, name='admin_perms')
            RolePermissionSetItem.objects.using(alias).get_or_create(permission_set=ps, permission=perm_edit, defaults={'granted': True})
            RolePermissionSetItem.objects.using(alias).get_or_create(permission_set=ps, permission=perm_view, defaults={'granted': True})

            self.user, _ = TenantUser.objects.using(alias).get_or_create(
                organization=self.org,
                email='admin@sweat.com',
                defaults={'user_type': 'STAFF', 'status': 'ACTIVE', 'is_login_allowed': True, 'first_name': 'Admin', 'last_name': 'User'},
            )
            RoleAssignment.objects.using(alias).get_or_create(
                user=self.user, role=role_admin, defaults={'is_active': True, 'status': 'ACTIVE'}
            )

    def test_1_oauth_callback_does_not_require_jwt(self):
        """1. oauth_callback does NOT require tenant JWT (AllowAny and no authenticators)."""
        view = MetaLeadMappingViewSet.as_view({'get': 'oauth_callback'})
        req = self.factory.get('/api/v1/tenant/meta-lead-mappings/oauth-callback/?code=test&state=invalid')
        res = view(req)
        self.assertNotEqual(res.status_code, 401)
        self.assertEqual(res.status_code, 302)
        self.assertIn('meta_error=invalid_state', res.url)

    def test_2_and_3_valid_signed_state_resolves_tenant_and_org_no_attribute_error(self):
        """2 and 3. Valid signed state resolves tenant + organization and NO self.organization AttributeError occurs."""
        state = generate_oauth_state(
            tenant_id=str(self.tenant.id),
            organization_id=str(self.org.id),
            user_id='00000000-0000-0000-0000-000000000001',
            redirect_uri='https://catalyst-unbundle-hungry.ngrok-free.dev/api/v1/tenant/meta-lead-mappings/oauth-callback/'
        )

        view = MetaLeadMappingViewSet.as_view({'get': 'oauth_callback'})
        req = self.factory.get(f'/api/v1/tenant/meta-lead-mappings/oauth-callback/?code=fb_auth_code_live&state={state}')

        with patch('apps.tenant_core.services_meta_graph.MetaGraphClient.exchange_code_for_tokens') as mock_exchange,              patch('apps.tenant_core.services_meta_graph.MetaGraphClient.fetch_user_pages') as mock_pages,              patch('apps.tenant_core.services_meta_graph.MetaGraphClient.subscribe_page_to_webhooks') as mock_sub:
            mock_exchange.return_value = {
                'user_access_token': 'EAAB_test_token_live_12345678901234567890',
                'meta_user_id': '1092837465',
                'meta_user_name': 'Kunal Singh',
                'expires_in': 5184000
            }
            mock_pages.return_value = [
                {'id': '1370696339461040', 'name': 'SWEAT CRM Test', 'access_token': 'EAA_Page_Token_Real_12345'}
            ]
            mock_sub.return_value = True

            # This must NOT raise AttributeError: 'MetaLeadMappingViewSet' object has no attribute 'organization'
            res = view(req)
            self.assertEqual(res.status_code, 302)
            self.assertIn('meta_connected=true', res.url)

    def test_4_invalid_state_rejected(self):
        """4. Invalid state is rejected with validation error / safe redirect."""
        view = MetaLeadMappingViewSet.as_view({'get': 'oauth_callback'})
        req = self.factory.get('/api/v1/tenant/meta-lead-mappings/oauth-callback/?code=test_code&state=tampered_corrupt_state')
        res = view(req)
        self.assertEqual(res.status_code, 302)
        self.assertIn('meta_error=invalid_state', res.url)

    def test_5_expired_state_rejected(self):
        """5. Expired state (> 15 min TTL) is rejected."""
        from django.conf import settings
        import json, base64, hmac, hashlib
        past_time = int(time.time()) - 1200
        payload = {
            'tenant_id': str(self.tenant.id),
            'organization_id': str(self.org.id),
            'user_id': '00000000-0000-0000-0000-000000000001',
            'redirect_uri': 'https://example.com',
            'nonce': 'expirednonce123456',
            'timestamp': past_time,
        }
        payload_bytes = json.dumps(payload, sort_keys=True).encode('utf-8')
        sig = hmac.new(settings.SECRET_KEY.encode('utf-8'), payload_bytes, hashlib.sha256).hexdigest()
        envelope = json.dumps({'payload': payload, 'sig': sig})
        expired_state = base64.urlsafe_b64encode(envelope.encode('utf-8')).decode('utf-8')

        view = MetaLeadMappingViewSet.as_view({'get': 'oauth_callback'})
        req = self.factory.get(f'/api/v1/tenant/meta-lead-mappings/oauth-callback/?code=test_code&state={expired_state}')
        res = view(req)
        self.assertEqual(res.status_code, 302)
        self.assertIn('meta_error=invalid_state', res.url)

    def test_6_replayed_consumed_state_rejected(self):
        """6. Replayed / already-consumed state is rejected."""
        state = generate_oauth_state(
            tenant_id=str(self.tenant.id),
            organization_id=str(self.org.id),
            user_id='00000000-0000-0000-0000-000000000001',
            redirect_uri='https://example.com'
        )
        # Consume once
        verify_oauth_state(state, consume=True)

        # Attempt to consume second time (replay)
        view = MetaLeadMappingViewSet.as_view({'get': 'oauth_callback'})
        req = self.factory.get(f'/api/v1/tenant/meta-lead-mappings/oauth-callback/?code=test_code&state={state}')
        res = view(req)
        self.assertEqual(res.status_code, 302)
        self.assertIn('meta_error=invalid_state', res.url)

    def test_7_organization_from_another_tenant_rejected(self):
        """7. Organization not in the tenant database is rejected."""
        random_org_id = str(uuid.uuid4())
        state = generate_oauth_state(
            tenant_id=str(self.tenant.id),
            organization_id=random_org_id,
            user_id='00000000-0000-0000-0000-000000000001',
            redirect_uri='https://example.com'
        )

        view = MetaLeadMappingViewSet.as_view({'get': 'oauth_callback'})
        req = self.factory.get(f'/api/v1/tenant/meta-lead-mappings/oauth-callback/?code=test_code&state={state}')
        res = view(req)
        self.assertEqual(res.status_code, 302)
        self.assertIn('meta_error=organization_not_found', res.url)

    def test_8_normal_meta_mapping_apis_still_require_authenticated_jwt(self):
        """8. Normal Meta mapping APIs (list, create, connection, etc.) still require tenant authentication."""
        view_list = MetaLeadMappingViewSet.as_view({'get': 'list'})
        req_unauth = self.factory.get('/api/v1/tenant/meta-lead-mappings/')
        res = view_list(req_unauth)
        self.assertEqual(res.status_code, 401)

    def test_9_real_oauth_init_to_callback_tenant_context_resolution(self):
        """9. Integration: Real authenticated oauth_init generates signed state with Master Tenant ID,
        verifies organization_id != tenant_id, and completes oauth_callback context resolution cleanly."""
        from rest_framework.test import APIClient
        from apps.authentication.views import _build_tenant_token

        # Obtain a real authenticated user and token for this tenant
        with tenant_database_context(str(self.tenant.id)) as alias:
            user = TenantUser.objects.using(alias).filter(email='admin@sweat.com', status='ACTIVE').first() or TenantUser.objects.using(alias).filter(organization=self.org, status='ACTIVE', is_login_allowed=True).first()
            if not user:
                user = TenantUser.objects.using(alias).create(
                    organization=self.org, email='admin@sweat.com', user_type='STAFF', status='ACTIVE', is_login_allowed=True
                )
            token = _build_tenant_token(user, self.tenant, alias)
            access_token = str(token.access_token)
            self.org = user.organization

        # 1. Call oauth_init via real authenticated API client
        auth_client = APIClient()
        auth_client.credentials(HTTP_AUTHORIZATION=f'Bearer {access_token}')
        redirect_target = 'https://catalyst-unbundle-hungry.ngrok-free.dev/api/v1/tenant/meta-lead-mappings/oauth-callback/'
        init_res = auth_client.post(
            '/api/v1/tenant/meta-lead-mappings/oauth-init/',
            {'redirect_uri': redirect_target},
            format='json'
        )
        self.assertEqual(init_res.status_code, 200, f'oauth_init failed: {init_res.data}')
        state_token = init_res.data.get('state')
        self.assertTrue(state_token, 'oauth_init did not return state token')

        # 2. Decode state payload without consuming nonce
        decoded_state = verify_oauth_state(state_token, consume=False)
        state_tenant_id = decoded_state.get('tenant_id')
        state_org_id = decoded_state.get('organization_id')

        # 3. Assert state tenant_id is MASTER Tenant ID, not Organization ID
        self.assertEqual(state_tenant_id, str(self.tenant.id), 'State tenant_id must be Master Tenant ID!')
        self.assertEqual(state_org_id, str(self.org.id), 'State organization_id must be Tenant DB Organization ID!')
        self.assertNotEqual(state_tenant_id, state_org_id, 'Master Tenant ID and Organization ID must not be equal!')

        # 4. Assert both resolve from their proper databases
        master_t = Tenant.objects.using('default').filter(id=state_tenant_id, status='ACTIVE').first()
        self.assertIsNotNone(master_t, 'Master tenant must exist in default DB with status ACTIVE')

        with tenant_database_context(state_tenant_id) as resolved_alias:
            org_in_tenant_db = Organization.objects.using(resolved_alias).filter(id=state_org_id, status='ACTIVE').first()
            self.assertIsNotNone(org_in_tenant_db, 'Organization must exist in resolved tenant DB with status ACTIVE')

        # 5. Call unauthenticated oauth_callback using the generated real state
        public_client = APIClient()
        with patch('apps.tenant_core.services_meta_graph.MetaGraphClient.exchange_code_for_tokens') as mock_exchange, \
             patch('apps.tenant_core.services_meta_graph.MetaGraphClient.fetch_user_pages') as mock_pages, \
             patch('apps.tenant_core.services_meta_graph.MetaGraphClient.subscribe_page_to_webhooks') as mock_sub:
            mock_exchange.return_value = {
                'user_access_token': 'EAAB_real_test_token_regression_1234567890',
                'meta_user_id': '1092837465',
                'meta_user_name': 'Kunal Singh',
                'expires_in': 5184000
            }
            mock_pages.return_value = [
                {'id': '1370696339461040', 'name': 'SWEAT CRM Test', 'access_token': 'EAA_Page_Token_Real_12345'}
            ]
            mock_sub.return_value = True

            cb_res = public_client.get(f'/api/v1/tenant/meta-lead-mappings/oauth-callback/?code=live_test_auth_code&state={state_token}')
            self.assertEqual(cb_res.status_code, 302)
            self.assertNotIn('meta_error=tenant_not_found', cb_res.url)
            self.assertNotIn('meta_error=organization_not_found', cb_res.url)
            self.assertIn('meta_connected=true', cb_res.url)


    def test_10_oauth_init_uses_configured_production_meta_app_id(self):
        """10. Regression: oauth_init dynamically uses configured META_APP_ID (e.g. 1039087885155974)
        in authorize_url, matches production callback URI, and never hardcodes app IDs."""
        from django.test import override_settings
        from rest_framework.test import APIClient
        from apps.authentication.views import _build_tenant_token
        from urllib.parse import urlparse, parse_qs

        with tenant_database_context(str(self.tenant.id)) as alias:
            user = TenantUser.objects.using(alias).filter(email='admin@sweat.com', status='ACTIVE').first() or \
                   TenantUser.objects.using(alias).filter(organization=self.org, status='ACTIVE', is_login_allowed=True).first()
            if not user:
                user = TenantUser.objects.using(alias).create(
                    organization=self.org, email='admin@sweat.com', user_type='STAFF', status='ACTIVE', is_login_allowed=True
                )
            token = _build_tenant_token(user, self.tenant, alias)
            access_token = str(token.access_token)

        auth_client = APIClient()
        auth_client.credentials(HTTP_AUTHORIZATION=f'Bearer {access_token}')

        prod_app_id = '1039087885155974'
        prod_redirect_uri = 'https://api.fitness.vibecopilot.ai/api/v1/tenant/meta-lead-mappings/oauth-callback/'

        # Test A: With published production app ID configured in settings
        with override_settings(
            META_APP_ID=prod_app_id,
            META_REDIRECT_URI=prod_redirect_uri,
            BACKEND_PUBLIC_URL='https://api.fitness.vibecopilot.ai',
        ):
            res = auth_client.get('/api/v1/tenant/meta-lead-mappings/oauth-init/')
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.data.get('live_available'))
            auth_url = res.data.get('authorize_url') or res.data.get('auth_url')
            self.assertIsNotNone(auth_url)

            parsed_url = urlparse(auth_url)
            query_params = parse_qs(parsed_url.query)

            # Ensure client_id in URL is strictly the configured production app ID
            self.assertEqual(query_params.get('client_id'), [prod_app_id])
            self.assertNotEqual(query_params.get('client_id'), ['1578645693537376'])

            # Ensure redirect URI matches expected production callback exactly
            self.assertEqual(query_params.get('redirect_uri'), [prod_redirect_uri])

            # Ensure required permissions / scopes are present
            scopes_param = query_params.get('scope', [''])[0].split(',')
            for expected_scope in ['leads_retrieval', 'pages_show_list', 'pages_read_engagement', 'pages_manage_ads', 'pages_manage_metadata']:
                self.assertIn(expected_scope, scopes_param)

        # Test B: Confirm no hardcoding - dynamic app ID update works
        dynamic_app_id = '987654321098765'
        with override_settings(
            META_APP_ID=dynamic_app_id,
            META_REDIRECT_URI=prod_redirect_uri,
        ):
            res_dyn = auth_client.get('/api/v1/tenant/meta-lead-mappings/oauth-init/')
            self.assertEqual(res_dyn.status_code, 200)
            auth_url_dyn = res_dyn.data.get('authorize_url')
            query_dyn = parse_qs(urlparse(auth_url_dyn).query)
            self.assertEqual(query_dyn.get('client_id'), [dynamic_app_id])
            self.assertNotEqual(query_dyn.get('client_id'), [prod_app_id])

        # Test C: Missing app ID gracefully returns 400 Bad Request with setup guidance
        with override_settings(META_APP_ID=''), patch.dict(os.environ, {'META_APP_ID': ''}):
            res_missing = auth_client.get('/api/v1/tenant/meta-lead-mappings/oauth-init/')
            self.assertEqual(res_missing.status_code, 400)
            self.assertFalse(res_missing.data.get('live_available'))
            self.assertIn('META_APP_ID is not configured', res_missing.data.get('error', ''))

    def test_11_metadata_and_pages_endpoints_expose_distinct_page_id_and_uuid_and_labels(self):
        """11. Regression: metadata and pages endpoints return Page objects exposing real Facebook Page ID,
        proper Page Name labels, and keep the internal database UUID distinct."""
        from rest_framework.test import APIClient
        from apps.authentication.views import _build_tenant_token
        from apps.tenant_core.models_meta_leads import MetaConnection, MetaPageConnection
        from apps.tenant_core.meta_crypto import encrypt_token

        fb_page_id = '1265728509951597'
        fb_page_name = 'Sweat pilates'
        live_token = 'EAAB_live_production_real_token_12345678901234567890'

        with tenant_database_context(str(self.tenant.id)) as alias:
            user = TenantUser.objects.using(alias).filter(email='admin@sweat.com', status='ACTIVE').first()
            if not user:
                user = TenantUser.objects.using(alias).create(
                    organization=self.org, email='admin@sweat.com', user_type='STAFF', status='ACTIVE', is_login_allowed=True
                )
            target_org = user.organization
            token = _build_tenant_token(user, self.tenant, alias)
            access_token = str(token.access_token)

            conn, _ = MetaConnection.objects.using(alias).update_or_create(
                organization=target_org,
                defaults={
                    'status': 'CONNECTED',
                    'meta_user_name': 'Maahek Nair',
                    'meta_user_id': '1092837465',
                    'encrypted_user_access_token': encrypt_token(live_token),
                    'scopes': ['leads_retrieval', 'pages_show_list'],
                }
            )

            page_conn, _ = MetaPageConnection.objects.using(alias).update_or_create(
                organization=target_org,
                page_id=fb_page_id,
                defaults={
                    'connection': conn,
                    'page_name': fb_page_name,
                    'encrypted_page_access_token': encrypt_token('EAA_page_token_live_12345678901234567890'),
                    'is_subscribed_to_webhooks': True,
                    'is_active': True,
                }
            )
            page_conn_uuid = str(page_conn.id)

        auth_client = APIClient()
        auth_client.credentials(HTTP_AUTHORIZATION=f'Bearer {access_token}')

        # Test A: metadata endpoint
        res_meta = auth_client.get('/api/v1/tenant/meta-lead-mappings/metadata/')
        self.assertEqual(res_meta.status_code, 200)
        pages_list = res_meta.data.get('pages', [])
        matching_page = next((p for p in pages_list if p.get('page_id') == fb_page_id or p.get('id') == fb_page_id), None)
        self.assertIsNotNone(matching_page, f"Page {fb_page_id} not found in metadata.pages: {pages_list}")

        # Assert Facebook Page ID vs Database UUID separation
        self.assertEqual(matching_page.get('id'), fb_page_id, "matching_page['id'] must equal the Facebook Page ID for dropdown value")
        self.assertEqual(matching_page.get('page_id'), fb_page_id, "matching_page['page_id'] must equal the Facebook Page ID")
        self.assertEqual(matching_page.get('name'), fb_page_name, "matching_page['name'] must equal the Facebook Page Name for dropdown label")
        self.assertEqual(matching_page.get('page_name'), fb_page_name, "matching_page['page_name'] must equal the Facebook Page Name")
        self.assertEqual(matching_page.get('connection_id'), page_conn_uuid, "matching_page['connection_id'] must preserve DB row UUID")
        self.assertNotEqual(matching_page.get('connection_id'), matching_page.get('page_id'), "Internal UUID and Facebook Page ID must be distinct")

        # Test B: pages endpoint dual envelope
        res_pages = auth_client.get('/api/v1/tenant/meta-lead-mappings/pages/')
        self.assertEqual(res_pages.status_code, 200)
        self.assertIn('results', res_pages.data)
        self.assertIn('pages', res_pages.data)
        page_in_pages = next((p for p in res_pages.data['pages'] if p.get('page_id') == fb_page_id), None)
        self.assertIsNotNone(page_in_pages)
        self.assertEqual(page_in_pages.get('name'), fb_page_name)

    def test_12_forms_endpoint_dual_envelope_and_page_id_and_uuid_resolution(self):
        """12. Regression: forms endpoint returns both 'forms' and 'results' keys, and resolves
        using either numeric Facebook Page ID or internal row UUID."""
        from rest_framework.test import APIClient
        from apps.authentication.views import _build_tenant_token
        from apps.tenant_core.models_meta_leads import MetaConnection, MetaPageConnection
        from apps.tenant_core.meta_crypto import encrypt_token

        fb_page_id = '1265728509951597'
        fb_page_name = 'Sweat pilates'
        live_token = 'EAAB_live_production_real_token_12345678901234567890'

        with tenant_database_context(str(self.tenant.id)) as alias:
            user = TenantUser.objects.using(alias).filter(email='admin@sweat.com', status='ACTIVE').first()
            target_org = user.organization
            token = _build_tenant_token(user, self.tenant, alias)
            access_token = str(token.access_token)

            conn, _ = MetaConnection.objects.using(alias).update_or_create(
                organization=target_org,
                defaults={
                    'status': 'CONNECTED',
                    'meta_user_name': 'Maahek Nair',
                    'meta_user_id': '1092837465',
                    'encrypted_user_access_token': encrypt_token(live_token),
                }
            )
            page_conn, _ = MetaPageConnection.objects.using(alias).update_or_create(
                organization=target_org,
                page_id=fb_page_id,
                defaults={
                    'connection': conn,
                    'page_name': fb_page_name,
                    'encrypted_page_access_token': encrypt_token('EAA_page_token_live_12345678901234567890'),
                    'is_subscribed_to_webhooks': True,
                    'is_active': True,
                }
            )
            page_uuid = str(page_conn.id)

        auth_client = APIClient()
        auth_client.credentials(HTTP_AUTHORIZATION=f'Bearer {access_token}')

        mock_forms = [
            {'id': '1755557852149138', 'name': 'SWEAT LEADS ADS', 'status': 'ACTIVE', 'questions': [{'key': 'full_name'}]}
        ]

        with patch('apps.tenant_core.services_meta_graph.MetaGraphClient.fetch_page_forms') as mock_fetch:
            mock_fetch.return_value = mock_forms

            # Query via Facebook Page ID
            res_by_page_id = auth_client.get(f'/api/v1/tenant/meta-lead-mappings/forms/?page_id={fb_page_id}')
            self.assertEqual(res_by_page_id.status_code, 200)
            self.assertIn('forms', res_by_page_id.data)
            self.assertIn('results', res_by_page_id.data)
            self.assertEqual(len(res_by_page_id.data['forms']), 1)
            self.assertEqual(res_by_page_id.data['forms'][0]['id'], '1755557852149138')
            self.assertEqual(mock_fetch.call_args[0][0], fb_page_id)
            self.assertNotEqual(mock_fetch.call_args[0][0], page_uuid)

            # Query via Database UUID fallback: Must still call Meta Graph API with Facebook Page ID
            mock_fetch.reset_mock()
            res_by_uuid = auth_client.get(f'/api/v1/tenant/meta-lead-mappings/forms/?page_id={page_uuid}')
            self.assertEqual(res_by_uuid.status_code, 200)
            self.assertIn('forms', res_by_uuid.data)
            self.assertIn('results', res_by_uuid.data)
            self.assertEqual(len(res_by_uuid.data['forms']), 1)
            self.assertEqual(res_by_uuid.data['forms'][0]['id'], '1755557852149138')
            self.assertEqual(mock_fetch.call_args[0][0], fb_page_id, "Backend must resolve DB UUID to actual Facebook Page ID before calling Meta Graph API")
            self.assertNotEqual(mock_fetch.call_args[0][0], page_uuid, "Backend must NEVER send internal DB UUID to Meta Graph API")

    def test_13_form_fields_endpoint_dual_envelope_and_alias(self):
        """13. Regression: form_fields endpoint returns 'form' object with questions, and supports
        both kebab-case and snake_case routes without requiring page_id."""
        from rest_framework.test import APIClient
        from apps.authentication.views import _build_tenant_token
        from apps.tenant_core.models_meta_leads import MetaConnection, MetaPageConnection
        from apps.tenant_core.meta_crypto import encrypt_token

        fb_page_id = '1265728509951597'
        live_token = 'EAAB_live_production_real_token_12345678901234567890'

        with tenant_database_context(str(self.tenant.id)) as alias:
            user = TenantUser.objects.using(alias).filter(email='admin@sweat.com', status='ACTIVE').first()
            target_org = user.organization
            token = _build_tenant_token(user, self.tenant, alias)
            access_token = str(token.access_token)

            conn, _ = MetaConnection.objects.using(alias).update_or_create(
                organization=target_org,
                defaults={
                    'status': 'CONNECTED',
                    'meta_user_name': 'Maahek Nair',
                    'meta_user_id': '1092837465',
                    'encrypted_user_access_token': encrypt_token(live_token),
                }
            )
            MetaPageConnection.objects.using(alias).update_or_create(
                organization=target_org,
                page_id=fb_page_id,
                defaults={
                    'connection': conn,
                    'page_name': 'Sweat pilates',
                    'encrypted_page_access_token': encrypt_token('EAA_page_token_live_12345678901234567890'),
                    'is_subscribed_to_webhooks': True,
                    'is_active': True,
                }
            )

        auth_client = APIClient()
        auth_client.credentials(HTTP_AUTHORIZATION=f'Bearer {access_token}')

        mock_details = {
            'id': '1755557852149138',
            'name': 'SWEAT LEADS ADS',
            'questions': [
                {'key': 'full_name', 'label': 'Full Name', 'type': 'TEXT'},
                {'key': 'email', 'label': 'Email Address', 'type': 'EMAIL'},
            ]
        }

        with patch('apps.tenant_core.services_meta_graph.MetaGraphClient.fetch_form_details') as mock_fetch:
            mock_fetch.return_value = mock_details

            # Kebab-case route
            res_kebab = auth_client.get('/api/v1/tenant/meta-lead-mappings/form-fields/?form_id=1755557852149138')
            self.assertEqual(res_kebab.status_code, 200)
            self.assertIn('form', res_kebab.data)
            self.assertIn('questions', res_kebab.data)
            self.assertEqual(len(res_kebab.data['form']['questions']), 2)

            # Snake-case route
            res_snake = auth_client.get('/api/v1/tenant/meta-lead-mappings/form_fields/?form_id=1755557852149138')
            self.assertEqual(res_snake.status_code, 200)
            self.assertIn('form', res_snake.data)
