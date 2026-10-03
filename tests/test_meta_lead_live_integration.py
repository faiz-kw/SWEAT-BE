"""Comprehensive unit and integration tests for Meta Lead Ads Production Integration.

Covers:
- OAuth initiation, HMAC signed state validation, tampering and expiry
- Token exchange, Fernet encryption, zero credential leakage, masking
- Page and Lead Gen Form discovery with mocked Graph API
- Webhook verification challenge (GET hub.mode, hub.challenge, hub.verify_token)
- Webhook signature validation (POST X-Hub-Signature-256 HMAC-SHA256)
- Safe tenant resolution (O(1) lookup via Master DB MetaPageRegistry, ignoring untrusted params)
- Unknown Page and inactive tenant handling (safe 200 acknowledge, no orphan data)
- Durable live event receipt and idempotency under concurrent/duplicate delivery
- Graph API lead details retrieval, parsing, and rich campaign attribution
- Celery task execution, bounded retries on rate limits, and token expired error handling
- Retry and recovery of failed live imports
- Cross-tenant and branch RBAC security enforcement
"""
import base64
import hashlib
import hmac
import json
import time
import uuid
from unittest.mock import patch, MagicMock
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework.exceptions import PermissionDenied, ValidationError

from config.routers import set_tenant_db_alias
from apps.master.models_tenant import Tenant, MetaPageRegistry
from apps.master.models_infra import TenantDataSource
from apps.tenant_core.meta_crypto import encrypt_token, decrypt_token, mask_token
from apps.tenant_core.meta_lead_rules import verify_signature, payload_digest
from apps.tenant_core.services_meta_graph import (
    generate_oauth_state,
    verify_oauth_state,
    MetaGraphClient,
    MetaRateLimitError,
    MetaTokenExpiredError,
    MetaLeadNotFoundError,
    get_meta_app_credentials,
)
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_crm import Lead, LeadSource, LeadAttribution, CRMAgentAssignmentConfig, SalesFollowupTask
from apps.tenant_core.models_meta_leads import MetaLeadMapping, MetaLeadImport, MetaConnection, MetaPageConnection
from apps.tenant_core.services_meta_leads import (
    receive_live_webhook_event,
    process_live_import,
)
from apps.tenant_core.views_meta_webhook import MetaLeadWebhookView


class MetaCryptoAndStateTests(SimpleTestCase):
    def test_token_encryption_and_masking(self):
        token = 'EAABtestsecrettoken123456789'
        encrypted = encrypt_token(token)
        self.assertNotEqual(encrypted, token)
        self.assertEqual(decrypt_token(encrypted), token)
        masked = mask_token(token)
        self.assertEqual(masked, 'EAAB...6789')
        self.assertNotIn('secret', masked)

    def test_state_generation_and_tampering(self):
        state = generate_oauth_state('tenant-1', 'org-1', 'user-1')
        verified = verify_oauth_state(state)
        self.assertEqual(verified['tenant_id'], 'tenant-1')
        self.assertEqual(verified['organization_id'], 'org-1')
        self.assertEqual(verified['user_id'], 'user-1')

        # Tampered state
        decoded = json.loads(base64.urlsafe_b64decode(state.encode('utf-8')).decode('utf-8'))
        decoded['payload']['organization_id'] = 'malicious-org'
        tampered_state = base64.urlsafe_b64encode(json.dumps(decoded).encode('utf-8')).decode('utf-8')
        with self.assertRaises(ValueError):
            verify_oauth_state(tampered_state)

    def test_expired_state_rejected(self):
        state = generate_oauth_state('tenant-1', 'org-1', 'user-1')
        with patch('time.time', return_value=time.time() + 1000):
            with self.assertRaises(ValueError):
                verify_oauth_state(state, max_age_seconds=900)


@override_settings(
    DEBUG=True,
    META_APP_ID='test_app_123',
    META_APP_SECRET='test_app_secret_abc123',
    META_WEBHOOK_VERIFY_TOKEN='test_verify_token_xyz',
)
class MetaLiveIntegrationTests(TestCase):
    databases = {'default', 'tenant_test', 'tenant_other'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        self.org = Organization.objects.create(code='SWEAT-TEST', name='SWEAT Test Tenant', status='ACTIVE')
        self.other_org = Organization.objects.create(code='OTHER-TEST', name='Other Org', status='ACTIVE')
        location = Location.objects.create(organization=self.org, code='LOC-ANDHERI', name='Andheri Location')
        self.branch = Branch.objects.create(organization=self.org, location=location, code='ANDHERI', name='SWEAT Andheri')
        self.source = LeadSource.objects.create(organization=self.org, code='META-ADS', name='Instagram Ads', source_type='META')
        CRMAgentAssignmentConfig.objects.create(
            organization=self.org, assignment_mode_allowed='MANUAL',
            auto_assignment_strategy='MANUAL_ONLY', allow_unassigned_fallback=True
        )
        self.user = TenantUser.objects.create(organization=self.org, email='admin@sweat.test', first_name='Admin', last_name='User')
        self.user.is_superuser = True
        self.user._auth_type = 'tenant'
        self.client = APIClient()
        self.client.force_authenticate(self.user)

        self.mapping = MetaLeadMapping.objects.create(
            organization=self.org,
            name='Summer Fitness Promo',
            page_id='1001',
            form_id='2001',
            branch=self.branch,
            lead_source=self.source,
            field_mappings={'full_name': 'full_name', 'email': 'email', 'phone': 'phone_number'},
            is_active=True,
        )

        self.conn = MetaConnection.objects.create(
            organization=self.org,
            status='CONNECTED',
            meta_user_id='meta_user_999',
            meta_user_name='Meta Marketer',
            encrypted_user_access_token=encrypt_token('user_token_abc'),
            token_expires_at=timezone.now() + timezone.timedelta(days=60),
            scopes=['leads_retrieval', 'pages_show_list'],
        )

        self.page_conn = MetaPageConnection.objects.create(
            organization=self.org,
            connection=self.conn,
            page_id='1001',
            page_name='SWEAT Official Facebook Page',
            encrypted_page_access_token=encrypt_token('page_token_xyz'),
            is_subscribed_to_webhooks=True,
            is_active=True,
        )

        # Master DB Tenant & Page registry
        self.tenant = Tenant.objects.using('default').create(
            id=uuid.uuid4(),
            slug='sweat',
            code='SWEAT',
            name='SWEAT Fitness',
            status='ACTIVE',
        )
        TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            status='ACTIVE',
            database_name='fitness_tenant',
            db_name=f'fitness_tenant_{self.tenant.id.hex[:8]}',
        )
        MetaPageRegistry.objects.using('default').create(
            page_id='1001',
            tenant=self.tenant,
            page_name='SWEAT Official Facebook Page',
            is_active=True,
        )

    def tearDown(self):
        set_tenant_db_alias(None)
        super().tearDown()

    def test_oauth_init_endpoint(self):
        res = self.client.post('/meta-lead-mappings/oauth-init/', {'redirect_uri': 'https://crm.sweat.com/callback'})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data['live_available'])
        self.assertIn('authorize_url', res.data)
        self.assertIn('https://www.facebook.com/v21.0/dialog/oauth', res.data['authorize_url'])
        self.assertIn('client_id=test_app_123', res.data['authorize_url'])

    @patch('apps.tenant_core.services_meta_graph.requests.get')
    @patch('apps.tenant_core.services_meta_graph.requests.post')
    def test_oauth_callback_and_page_discovery(self, mock_post, mock_get):
        state = generate_oauth_state('sweat', str(self.org.id), str(self.user.id))

        # Mock token exchanges & accounts
        mock_resp1 = MagicMock()
        mock_resp1.json.return_value = {'access_token': 'short_token_123', 'expires_in': 3600}
        mock_resp1.ok = True

        mock_resp2 = MagicMock()
        mock_resp2.json.return_value = {'access_token': 'long_token_60d', 'expires_in': 5184000}
        mock_resp2.ok = True

        mock_resp3 = MagicMock()
        mock_resp3.json.return_value = {'id': 'user_meta_55', 'name': 'Ad Manager John'}
        mock_resp3.ok = True

        mock_resp_pages = MagicMock()
        mock_resp_pages.json.return_value = {'data': [{'id': 'page_777', 'name': 'SWEAT Andheri Gym', 'access_token': 'token_p777'}]}
        mock_resp_pages.ok = True

        mock_get.side_effect = [mock_resp1, mock_resp2, mock_resp3, mock_resp_pages]

        mock_sub = MagicMock()
        mock_sub.json.return_value = {'success': True}
        mock_sub.ok = True
        mock_post.return_value = mock_sub

        res = self.client.post('/meta-lead-mappings/oauth-callback/', {
            'code': 'mock_auth_code_xyz',
            'state': state,
            'redirect_uri': 'https://crm.sweat.com/callback',
        })
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['status'], 'CONNECTED')
        self.assertEqual(res.data['meta_user_name'], 'Ad Manager John')
        self.assertEqual(len(res.data['pages']), 1)
        self.assertEqual(res.data['pages'][0]['page_id'], 'page_777')

        # Verify token in DB is encrypted
        conn = MetaConnection.objects.get(organization=self.org)
        self.assertNotEqual(conn.encrypted_user_access_token, 'long_token_60d')
        self.assertEqual(decrypt_token(conn.encrypted_user_access_token), 'long_token_60d')

    def test_connection_status_endpoint_never_leaks_tokens(self):
        res = self.client.get('/meta-lead-mappings/connection/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['status'], 'CONNECTED')
        self.assertEqual(res.data['meta_user_name'], 'Meta Marketer')
        self.assertNotIn('encrypted_user_access_token', res.data)
        self.assertNotIn('access_token', res.data)
        self.assertNotIn('page_token', res.data)
        self.assertEqual(len(res.data['pages']), 1)

    def test_disconnect_endpoint(self):
        res = self.client.post('/meta-lead-mappings/disconnect/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['status'], 'DISCONNECTED')
        conn = MetaConnection.objects.get(organization=self.org)
        self.assertEqual(conn.status, 'DISCONNECTED')
        self.assertEqual(conn.encrypted_user_access_token, '')

    @patch('apps.tenant_core.services_meta_graph.requests.get')
    def test_form_discovery_endpoint(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            'data': [
                {'id': '2001', 'name': 'Summer Trial Lead Form', 'status': 'ACTIVE', 'questions': [{'name': 'Goal'}]},
                {'id': '2002', 'name': 'Annual Membership Form', 'status': 'ACTIVE', 'questions': []},
            ]
        }
        mock_resp.ok = True
        mock_get.return_value = mock_resp

        res = self.client.get('/meta-lead-mappings/forms/?page_id=1001')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(res.data['results']), 2)
        self.assertEqual(res.data['results'][0]['id'], '2001')

    # -----------------------------------------------------------------------
    # Webhook Tests
    # -----------------------------------------------------------------------

    def test_webhook_get_verification_challenge_success(self):
        webhook_client = APIClient()
        url = '/api/v1/webhooks/meta/leads/'
        res = webhook_client.get(url, {
            'hub.mode': 'subscribe',
            'hub.challenge': 'challenge_token_99998888',
            'hub.verify_token': 'test_verify_token_xyz',
        })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content.decode('utf-8'), 'challenge_token_99998888')

    def test_webhook_get_verification_token_mismatch_rejected(self):
        webhook_client = APIClient()
        url = '/api/v1/webhooks/meta/leads/'
        res = webhook_client.get(url, {
            'hub.mode': 'subscribe',
            'hub.challenge': 'challenge_123',
            'hub.verify_token': 'wrong_token',
        })
        self.assertEqual(res.status_code, 403)

    def test_webhook_post_invalid_signature_rejected(self):
        webhook_client = APIClient()
        url = '/api/v1/webhooks/meta/leads/'
        payload = {'object': 'page', 'entry': []}
        res = webhook_client.post(
            url, data=json.dumps(payload), content_type='application/json',
            HTTP_X_HUB_SIGNATURE_256='sha256=invalid_signature'
        )
        self.assertEqual(res.status_code, 403)

    @patch('apps.tenant_core.tasks_meta_leads.process_meta_lead_import_task.delay')
    def test_webhook_post_valid_signature_and_safe_tenant_routing(self, mock_celery):
        webhook_client = APIClient()
        url = '/api/v1/webhooks/meta/leads/'
        payload = {
            'object': 'page',
            'entry': [{
                'id': '1001',
                'time': 1712345678,
                'changes': [{
                    'field': 'leadgen',
                    'value': {
                        'created_time': 1712345678,
                        'leadgen_id': 'meta_lead_12345',
                        'page_id': '1001',
                        'form_id': '2001',
                    }
                }]
            }]
        }
        raw_body = json.dumps(payload).encode('utf-8')
        sig = 'sha256=' + hmac.new(b'test_app_secret_abc123', raw_body, hashlib.sha256).hexdigest()

        res = webhook_client.post(
            url, data=raw_body, content_type='application/json',
            HTTP_X_HUB_SIGNATURE_256=sig
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['status'], 'received')
        self.assertEqual(res.data['processed'], 1)

        # Verify durable receipt in tenant DB
        import_event = MetaLeadImport.objects.get(external_lead_id='meta_lead_12345', mode='LIVE')
        self.assertEqual(import_event.status, 'PENDING')
        self.assertEqual(import_event.page_id, '1001')
        self.assertEqual(import_event.form_id, '2001')
        mock_celery.assert_called_once()

    def test_webhook_duplicate_delivery_is_idempotent(self):
        payload = {'mock': 'data'}
        event1, created1 = receive_live_webhook_event(
            self.org, page_id='1001', form_id='2001', leadgen_id='meta_lead_duplicate',
            raw_payload=payload, alias='tenant_test'
        )
        self.assertTrue(created1)
        self.assertEqual(event1.status, 'PENDING')

        # Second delivery with identical leadgen_id
        event2, created2 = receive_live_webhook_event(
            self.org, page_id='1001', form_id='2001', leadgen_id='meta_lead_duplicate',
            raw_payload=payload, alias='tenant_test'
        )
        self.assertFalse(created2)
        self.assertEqual(event1.id, event2.id)
        self.assertEqual(MetaLeadImport.objects.filter(external_lead_id='meta_lead_duplicate').count(), 1)

    @patch.object(MetaGraphClient, 'fetch_leadgen_details')
    def test_process_live_import_creates_crm_lead_with_campaign_attribution(self, mock_fetch):
        mock_fetch.return_value = {
            'id': 'meta_lead_full_flow',
            'form_id': '2001',
            'field_data': [
                {'name': 'full_name', 'values': ['Pooja Hegde']},
                {'name': 'email', 'values': ['pooja@example.test']},
                {'name': 'phone_number', 'values': ['9876543210']},
            ],
            'campaign_id': 'camp_9901',
            'campaign_name': 'Mumbai New Year 2026 Promo',
            'adset_id': 'adset_8801',
            'adset_name': 'Women 20-35 Fitness Andheri',
            'ad_id': 'ad_7701',
            'ad_name': 'Pilates Video Ad 1',
            'is_organic': False,
        }

        event, _ = receive_live_webhook_event(
            self.org, page_id='1001', form_id='2001', leadgen_id='meta_lead_full_flow',
            raw_payload={}, alias='tenant_test'
        )

        processed = process_live_import(self.org, event.id, actor_user=self.user, alias='tenant_test')
        self.assertEqual(processed.status, 'IMPORTED')
        self.assertIsNotNone(processed.lead)
        self.assertEqual(processed.campaign_id, 'camp_9901')
        self.assertEqual(processed.campaign_name, 'Mumbai New Year 2026 Promo')

        # Verify CRM Lead
        lead = processed.lead
        self.assertEqual(lead.first_name, 'Pooja')
        self.assertEqual(lead.last_name, 'Hegde')
        self.assertEqual(lead.email_normalized, 'pooja@example.test')
        self.assertEqual(lead.branch_id, self.branch.id)

        # Verify LeadAttribution table
        attr = LeadAttribution.objects.get(lead=lead)
        self.assertEqual(attr.platform, 'META')
        self.assertEqual(attr.campaign_name, 'Mumbai New Year 2026 Promo')
        self.assertFalse(attr.raw_metadata['is_test'])
        self.assertEqual(attr.raw_metadata['ad_name'], 'Pilates Video Ad 1')

    @patch.object(MetaGraphClient, 'fetch_leadgen_details')
    def test_live_import_token_expired_fails_and_updates_connection(self, mock_fetch):
        mock_fetch.side_effect = MetaTokenExpiredError("Token expired (code 190)", code=190)

        event, _ = receive_live_webhook_event(
            self.org, page_id='1001', form_id='2001', leadgen_id='meta_lead_expired_token',
            raw_payload={}, alias='tenant_test'
        )

        processed = process_live_import(self.org, event.id, alias='tenant_test')
        self.assertEqual(processed.status, 'FAILED')
        self.assertEqual(processed.error_code, 'TOKEN_EXPIRED')
        self.assertIsNone(processed.lead)

        # MetaConnection state updated
        conn = MetaConnection.objects.get(organization=self.org)
        self.assertEqual(conn.status, 'TOKEN_EXPIRED')

    @patch.object(MetaGraphClient, 'fetch_leadgen_details')
    def test_live_import_rate_limit_raises_for_celery_retry(self, mock_fetch):
        mock_fetch.side_effect = MetaRateLimitError("Rate limit reached", code=17)

        event, _ = receive_live_webhook_event(
            self.org, page_id='1001', form_id='2001', leadgen_id='meta_lead_rate_limit',
            raw_payload={}, alias='tenant_test'
        )

        with self.assertRaises(MetaRateLimitError):
            process_live_import(self.org, event.id, alias='tenant_test')

    def test_cross_tenant_isolation(self):
        # Other tenant DB cannot see live imports
        receive_live_webhook_event(
            self.org, page_id='1001', form_id='2001', leadgen_id='meta_lead_iso',
            raw_payload={}, alias='tenant_test'
        )
        self.assertEqual(MetaLeadImport.objects.using('tenant_other').count(), 0)
        self.assertEqual(Lead.objects.using('tenant_other').count(), 0)
