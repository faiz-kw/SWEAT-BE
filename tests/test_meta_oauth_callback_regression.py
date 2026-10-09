import unittest
import os
import uuid
import time
from unittest.mock import patch
from django.test import TestCase, RequestFactory
from rest_framework.exceptions import PermissionDenied, ValidationError, AuthenticationFailed

from apps.master.models_tenant import Tenant
from apps.tenant_core.context import tenant_database_context
from apps.tenant_core.models_org import Organization
from apps.tenant_core.models_users import TenantUser
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
        with tenant_database_context(str(self.tenant.id)) as alias:
            self.alias = alias
            self.org = Organization.objects.using(alias).filter(status='ACTIVE').first()
            if not self.org:
                self.org = Organization.objects.using(alias).create(
                    name='SWEAT UAT Organization', status='ACTIVE'
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

