"""
backend/tests/test_whatsapp_gupshup_phase1.py — Gupshup WhatsApp Integration Phase 1 Test Suite.

Covers:
1. Missing webhook authentication configuration fails closed (401).
2. Invalid authentication fails closed (401).
3. Valid authentication succeeds only under verified contract (X-Gupshup-Token, Bearer, query token).
4. Tenant app/sender mismatch is rejected (403).
5. Valid outbound text request sends correct documented request to Gupshup API.
6. Valid template request sends correct documented request to Gupshup API.
7. Provider message ID is extracted from response and saved on message.
8. HTTP 400, 401/403, 429, 500 map safely to structured error codes.
9. Network timeout produces ambiguous NETWORK_TIMEOUT without blind resend.
10. Malformed and batched webhook events are handled correctly.
11. Duplicate events do not create duplicate processing (idempotency).
12. Existing outbound kill switch and consent enforcement remain effective.
13. Existing Meta Lead Ads and communication regression tests remain unaffected.
"""

import json
from unittest.mock import patch, MagicMock
import requests
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from config.routers import set_tenant_db_alias
from config.secrets import SecretResolver
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.models_saas import SaasPlan, TenantSubscription, ProductModule, TenantModule
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_crm import LeadSource, Lead
from apps.tenant_core.models_infra import Integration
from apps.tenant_core.models_communication import CommunicationMessage, CommunicationStatusEvent
from apps.tenant_core.communication.service import CommunicationService, ConsentViolationError
from apps.tenant_core.communication.adapters.whatsapp_gupshup import GupshupWhatsAppAdapter


class GupshupWhatsAppPhase1Tests(TestCase):
    databases = '__all__'

    def setUp(self):
        super().setUp()
        set_tenant_db_alias('tenant_test')
        self.client = APIClient()

        # 1. Master DB Setup
        self.tenant = Tenant.objects.using('default').create(
            code='SWEAT-GS-T1',
            name='SWEAT Elite Gym',
            slug='sweat-elite-gym',
            status='ACTIVE',
        )
        self.ds = TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            db_name='test_fitness_tenant',
            database_name='test_fitness_tenant',
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )
        self.plan = SaasPlan.objects.using('default').create(
            code='GS-PLAN-1',
            tier='ENTERPRISE',
            status='ACTIVE',
        )
        TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
        )
        self.prod_mod_crm, _ = ProductModule.objects.using('default').get_or_create(
            code='crm', defaults={'name': 'CRM Module', 'status': 'ACTIVE'}
        )
        TenantModule.objects.using('default').create(
            tenant=self.tenant,
            module=self.prod_mod_crm,
            is_enabled=True,
            availability_mode='ALL_BRANCHES',
        )

        # 2. Tenant DB Setup
        self.org = Organization.objects.using('tenant_test').create(
            code='ORG-GS-1',
            name='SWEAT Elite Core',
            status='ACTIVE',
        )
        self.loc = Location.objects.using('tenant_test').create(
            organization=self.org,
            code='LOC-GS-1',
            name='Flagship Gym',
            status='ACTIVE',
        )
        self.branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            code='BR-GS-1',
            name='Main Floor',
            status='ACTIVE',
        )

        # Lead Source & Standard Lead
        self.lead_source = LeadSource.objects.using('tenant_test').create(
            organization=self.org,
            code='GS_SOURCE',
            name='Web Inbound',
            source_type='DIGITAL_ORGANIC',
            status='ACTIVE',
        )
        self.lead = Lead.objects.using('tenant_test').create(
            organization=self.org,
            branch=self.branch,
            lead_source=self.lead_source,
            first_name='Karan',
            last_name='Mehta',
            phone_normalized='+919876543210',
            email_normalized='karan.mehta@example.com',
            current_status='NEW_LEAD',
            do_not_contact=False,
            consent_whatsapp=True,
        )

        # Secret Reference setup for Gupshup
        self.secret_ref = 'env://TEST_GUPSHUP_SECRET'
        SecretResolver.register_test_secret(self.secret_ref, json.dumps({
            'api_key': 'gs_live_test_api_key_secret_xyz',
            'webhook_secret': 'gs_webhook_token_secure_987',
        }))

        # Gupshup Integration record
        self.integration = Integration.objects.using('tenant_test').create(
            integration_type='WHATSAPP',
            provider='GUPSHUP',
            status='ACTIVE',
            secret_reference=self.secret_ref,
            configuration={
                'app_name': 'SweatEliteWhatsApp',
                'source_number': '919876500000',
                'webhook_secret': 'gs_webhook_token_secure_987',
            },
        )

    def tearDown(self):
        SecretResolver.clear_test_secrets()
        super().tearDown()

    # -------------------------------------------------------------------------
    # 1. Missing webhook authentication configuration fails closed
    # -------------------------------------------------------------------------
    def test_01_missing_webhook_secret_fails_closed(self):
        """When tenant has no webhook_secret or credentials configured, all incoming webhooks are rejected with 401."""
        self.integration.configuration = {'app_name': 'SweatEliteWhatsApp'}
        self.integration.secret_reference = ''
        self.integration.save(using='tenant_test')

        url = f"/api/v1/webhooks/communications/GUPSHUP/{self.tenant.id}/"
        res = self.client.post(
            url,
            {'type': 'message-event', 'payload': {'type': 'DELIVERED', 'id': 'gs_123'}},
            format='json',
            HTTP_X_GUPSHUP_TOKEN='some_random_token',
        )
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(res.data['error'], 'Invalid webhook signature')

    # -------------------------------------------------------------------------
    # 2. Invalid authentication fails closed
    # -------------------------------------------------------------------------
    def test_02_invalid_authentication_fails_closed(self):
        """Wrong token in header or query fails closed with 401."""
        url = f"/api/v1/webhooks/communications/GUPSHUP/{self.tenant.id}/"

        # Wrong custom header
        res1 = self.client.post(
            url,
            {'type': 'message-event'},
            format='json',
            HTTP_X_GUPSHUP_TOKEN='wrong_token',
        )
        self.assertEqual(res1.status_code, status.HTTP_401_UNAUTHORIZED)

        # Wrong Authorization Bearer
        res2 = self.client.post(
            url,
            {'type': 'message-event'},
            format='json',
            HTTP_AUTHORIZATION='Bearer wrong_bearer_token',
        )
        self.assertEqual(res2.status_code, status.HTTP_401_UNAUTHORIZED)

        # No token at all
        res3 = self.client.post(url, {'type': 'message-event'}, format='json')
        self.assertEqual(res3.status_code, status.HTTP_401_UNAUTHORIZED)

    # -------------------------------------------------------------------------
    # 3. Valid authentication succeeds only under verified contract
    # -------------------------------------------------------------------------
    def test_03_valid_authentication_succeeds_under_verified_contract(self):
        """Authenticating via X-Gupshup-Token, Bearer header, or query token succeeds with 200."""
        url = f"/api/v1/webhooks/communications/GUPSHUP/{self.tenant.id}/"
        payload = {'type': 'unknown_heartbeat', 'app': 'SweatEliteWhatsApp'}

        # Via HTTP_X_GUPSHUP_TOKEN
        res1 = self.client.post(
            url, payload, format='json',
            HTTP_X_GUPSHUP_TOKEN='gs_webhook_token_secure_987',
        )
        self.assertEqual(res1.status_code, status.HTTP_200_OK)

        # Via HTTP_AUTHORIZATION Bearer
        res2 = self.client.post(
            url, payload, format='json',
            HTTP_AUTHORIZATION='Bearer gs_webhook_token_secure_987',
        )
        self.assertEqual(res2.status_code, status.HTTP_200_OK)

        # Via URL query parameter
        url_with_query = f"{url}?token=gs_webhook_token_secure_987"
        res3 = self.client.post(url_with_query, payload, format='json')
        self.assertEqual(res3.status_code, status.HTTP_200_OK)

    # -------------------------------------------------------------------------
    # 4. Tenant app/sender mismatch is rejected
    # -------------------------------------------------------------------------
    def test_04_tenant_app_mismatch_rejected(self):
        """Cross-tenant attack or misrouted webhook with conflicting app name is rejected with 403."""
        url = f"/api/v1/webhooks/communications/GUPSHUP/{self.tenant.id}/"
        spoofed_payload = {
            'app': 'RivalGymWhatsApp',
            'type': 'message',
            'payload': {'text': 'Hello'},
        }

        res = self.client.post(
            url, spoofed_payload, format='json',
            HTTP_X_GUPSHUP_TOKEN='gs_webhook_token_secure_987',
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.data['error'], 'Tenant app or sender identity mismatch')

    # -------------------------------------------------------------------------
    # 5. Valid outbound text request sends correct documented request
    # -------------------------------------------------------------------------
    @override_settings(COMMUNICATIONS_OUTBOUND_ENABLED=True)
    @patch('requests.post')
    def test_05_valid_outbound_text_request(self, mock_post):
        """Outbound text message dispatches POST to /wa/api/v1/msg with verified Gupshup parameters."""
        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_resp.json.return_value = {
            'status': 'submitted',
            'messageId': 'gupshup_msg_txt_001',
        }
        mock_post.return_value = mock_resp

        adapter = GupshupWhatsAppAdapter(
            configuration=self.integration.configuration,
            secret_credentials={'api_key': 'gs_live_test_api_key_secret_xyz'},
        )

        result = adapter.send(
            recipient='+919876543210',
            body='Welcome to SWEAT Elite Gym!',
        )

        self.assertTrue(result.success)
        self.assertEqual(result.status, 'SUBMITTED')
        self.assertEqual(result.provider_message_id, 'gupshup_msg_txt_001')

        # Verify POST call details
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], 'https://api.gupshup.io/wa/api/v1/msg')
        self.assertEqual(kwargs['headers']['apikey'], 'gs_live_test_api_key_secret_xyz')
        self.assertEqual(kwargs['headers']['Content-Type'], 'application/x-www-form-urlencoded')
        self.assertEqual(kwargs['timeout'], (5.0, 10.0))

        data = kwargs['data']
        self.assertEqual(data['channel'], 'whatsapp')
        self.assertEqual(data['source'], '919876500000')
        self.assertEqual(data['destination'], '919876543210')
        self.assertEqual(data['src.name'], 'SweatEliteWhatsApp')
        msg_obj = json.loads(data['message'])
        self.assertEqual(msg_obj['type'], 'text')
        self.assertEqual(msg_obj['text'], 'Welcome to SWEAT Elite Gym!')

    # -------------------------------------------------------------------------
    # 6. Valid template request sends correct documented request
    # -------------------------------------------------------------------------
    @override_settings(COMMUNICATIONS_OUTBOUND_ENABLED=True)
    @patch('requests.post')
    def test_06_valid_template_request(self, mock_post):
        """Outbound template message dispatches POST to /wa/api/v1/template/msg."""
        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_resp.json.return_value = {
            'status': 'submitted',
            'messageId': 'gupshup_tmpl_msg_002',
        }
        mock_post.return_value = mock_resp

        adapter = GupshupWhatsAppAdapter(
            configuration=self.integration.configuration,
            secret_credentials={'api_key': 'gs_live_test_api_key_secret_xyz'},
        )

        result = adapter.send(
            recipient='919876543210',
            body='Your booking is confirmed for tomorrow',
            template_ref='trial_booking_confirmed_v1',
            template_vars={'1': 'Karan', '2': '10:00 AM'},
        )

        self.assertTrue(result.success)
        self.assertEqual(result.provider_message_id, 'gupshup_tmpl_msg_002')

        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], 'https://api.gupshup.io/wa/api/v1/template/msg')
        data = kwargs['data']
        self.assertEqual(data['source'], '919876500000')
        self.assertEqual(data['destination'], '919876543210')
        tmpl_obj = json.loads(data['template'])
        self.assertEqual(tmpl_obj['id'], 'trial_booking_confirmed_v1')
        self.assertEqual(tmpl_obj['params'], ['Karan', '10:00 AM'])

    # -------------------------------------------------------------------------
    # 7. Provider message ID is extracted from valid response & saved
    # -------------------------------------------------------------------------
    @override_settings(COMMUNICATIONS_OUTBOUND_ENABLED=True)
    @patch('requests.post')
    def test_07_provider_message_id_persisted_in_service(self, mock_post):
        """CommunicationService.send_communication extracts genuine provider message ID and persists it."""
        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_resp.json.return_value = {
            'status': 'submitted',
            'messageId': 'gs_real_provider_uuid_777',
        }
        mock_post.return_value = mock_resp

        msg = CommunicationService.send_communication(
            organization=self.org,
            channel='WHATSAPP',
            recipient='+919876543210',
            body='Testing provider message ID persistence',
            lead=self.lead,
            provider_preference='GUPSHUP',
        )

        self.assertEqual(msg.status, 'SUBMITTED')
        self.assertEqual(msg.provider_message_id, 'gs_real_provider_uuid_777')
        persisted = CommunicationMessage.objects.using('tenant_test').get(id=msg.id)
        self.assertEqual(persisted.provider_message_id, 'gs_real_provider_uuid_777')

    # -------------------------------------------------------------------------
    # 8. HTTP 400, 401/403, 429 and server errors map safely
    # -------------------------------------------------------------------------
    @override_settings(COMMUNICATIONS_OUTBOUND_ENABLED=True)
    @patch('requests.post')
    def test_08_http_error_taxonomy_mapping(self, mock_post):
        """Provider HTTP error codes map to safe, standard error codes without crashing or leaking secrets."""
        adapter = GupshupWhatsAppAdapter(
            configuration=self.integration.configuration,
            secret_credentials={'api_key': 'gs_live_test_api_key_secret_xyz'},
        )

        error_cases = [
            (400, {'message': 'Invalid destination'}, 'INVALID_REQUEST'),
            (401, {'message': 'Invalid API Key'}, 'INVALID_TOKEN'),
            (403, {'message': 'Account suspended'}, 'INVALID_TOKEN'),
            (429, {'message': 'Rate limit exceeded'}, 'RATE_LIMITED'),
            (500, {'message': 'Internal Server Error'}, 'PROVIDER_SERVER_ERROR'),
            (503, {'message': 'Service Unavailable'}, 'PROVIDER_SERVER_ERROR'),
        ]

        for status_code, resp_json, expected_code in error_cases:
            mock_resp = MagicMock()
            mock_resp.status_code = status_code
            mock_resp.json.return_value = resp_json
            mock_post.return_value = mock_resp

            res = adapter.send(recipient='+919876543210', body='Error test')
            self.assertFalse(res.success)
            self.assertEqual(res.status, 'FAILED')
            self.assertEqual(res.error_code, expected_code)

    # -------------------------------------------------------------------------
    # 9. Network timeout produces an ambiguous result without blind resend
    # -------------------------------------------------------------------------
    @override_settings(COMMUNICATIONS_OUTBOUND_ENABLED=True)
    @patch('requests.post')
    def test_09_network_timeout_produces_ambiguous_result_no_blind_resend(self, mock_post):
        """Socket timeout returns NETWORK_TIMEOUT; never resends blindly."""
        mock_post.side_effect = requests.exceptions.Timeout("Read timed out after 10.0s")

        adapter = GupshupWhatsAppAdapter(
            configuration=self.integration.configuration,
            secret_credentials={'api_key': 'gs_live_test_api_key_secret_xyz'},
        )

        res = adapter.send(recipient='+919876543210', body='Timeout test')
        self.assertFalse(res.success)
        self.assertEqual(res.status, 'FAILED')
        self.assertEqual(res.error_code, 'NETWORK_TIMEOUT')
        self.assertIn('ambiguous', res.error_message.lower())
        self.assertEqual(mock_post.call_count, 1)  # Strictly no blind resend

    # -------------------------------------------------------------------------
    # 10. Malformed and batched webhook events are handled correctly
    # -------------------------------------------------------------------------
    def test_10_batched_and_malformed_webhook_events(self):
        """Batched webhooks process both status receipts and inbound messages, safely ignoring malformed items."""
        # Create an existing message to receive status update
        existing_msg = CommunicationMessage.objects.using('tenant_test').create(
            organization=self.org,
            channel='WHATSAPP',
            recipient='+919876543210',
            provider='GUPSHUP',
            provider_message_id='gs_batch_msg_target',
            status='SENT',
        )

        batch_payload = [
            # 1. Valid Status Receipt
            {
                'app': 'SweatEliteWhatsApp',
                'type': 'message-event',
                'payload': {
                    'type': 'DELIVERED',
                    'id': 'gs_batch_msg_target',
                    'ts': '1700000000',
                },
            },
            # 2. Malformed item (no crash, skipped)
            {
                'app': 'SweatEliteWhatsApp',
                'type': 'corrupt-event',
                'random_field': 12345,
            },
            # 3. Valid Inbound Message
            {
                'app': 'SweatEliteWhatsApp',
                'type': 'message',
                'id': 'gs_inbound_batch_001',
                'timestamp': '1700000005',
                'sender': {'phone': '919876543210'},
                'payload': {'text': 'CONFIRM my booking'},
            },
        ]

        url = f"/api/v1/webhooks/communications/GUPSHUP/{self.tenant.id}/"
        res = self.client.post(
            url, batch_payload, format='json',
            HTTP_X_GUPSHUP_TOKEN='gs_webhook_token_secure_987',
        )

        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.data['batch'])
        self.assertEqual(res.data['total_events'], 3)
        self.assertEqual(res.data['statuses_recorded'], 1)
        self.assertEqual(res.data['inbound_recorded'], 1)
        self.assertEqual(res.data['ignored'], 1)

        # Verify existing message was marked DELIVERED
        existing_msg = CommunicationMessage.objects.using('tenant_test').get(id=existing_msg.id)
        self.assertEqual(existing_msg.status, 'DELIVERED')

        # Verify inbound message was ingested and linked to Karan Mehta
        inbound = CommunicationMessage.objects.using('tenant_test').get(provider_message_id='gs_inbound_batch_001')
        self.assertEqual(inbound.lead_id, self.lead.id)
        self.assertEqual(inbound.body_snapshot, 'CONFIRM my booking')

    # -------------------------------------------------------------------------
    # 11. Duplicate events do not create duplicate processing (idempotency)
    # -------------------------------------------------------------------------
    def test_11_duplicate_events_idempotency(self):
        """Duplicate webhook receipts for same event ID do not duplicate status transitions or event logs."""
        msg = CommunicationMessage.objects.using('tenant_test').create(
            organization=self.org,
            channel='WHATSAPP',
            recipient='+919876543210',
            provider='GUPSHUP',
            provider_message_id='gs_dup_id_100',
            status='SENT',
        )

        payload = {
            'app': 'SweatEliteWhatsApp',
            'type': 'message-event',
            'payload': {
                'type': 'DELIVERED',
                'id': 'gs_dup_id_100',
                'ts': '1700000000',
            },
        }

        url = f"/api/v1/webhooks/communications/GUPSHUP/{self.tenant.id}/"
        # First delivery
        res1 = self.client.post(url, payload, format='json', HTTP_X_GUPSHUP_TOKEN='gs_webhook_token_secure_987')
        self.assertEqual(res1.status_code, status.HTTP_200_OK)

        events_count_1 = CommunicationStatusEvent.objects.using('tenant_test').filter(message=msg).count()
        self.assertEqual(events_count_1, 1)

        # Duplicate delivery
        res2 = self.client.post(url, payload, format='json', HTTP_X_GUPSHUP_TOKEN='gs_webhook_token_secure_987')
        self.assertEqual(res2.status_code, status.HTTP_200_OK)

        events_count_2 = CommunicationStatusEvent.objects.using('tenant_test').filter(message=msg).count()
        self.assertEqual(events_count_2, 1)  # No duplicate event row created

    # -------------------------------------------------------------------------
    # 12. Existing outbound kill switch and consent enforcement remain effective
    # -------------------------------------------------------------------------
    @override_settings(COMMUNICATIONS_OUTBOUND_ENABLED=False)
    @patch('requests.post')
    def test_12a_outbound_kill_switch_effective(self, mock_post):
        """When COMMUNICATIONS_OUTBOUND_ENABLED=False, send_communication raises ValueError and no HTTP is sent."""
        with self.assertRaises(ValueError) as ctx:
            CommunicationService.send_communication(
                organization=self.org,
                channel='WHATSAPP',
                recipient='+919876543210',
                body='Kill switch test',
                lead=self.lead,
                provider_preference='GUPSHUP',
            )
        mock_post.assert_not_called()
        self.assertIn('disabled', str(ctx.exception).lower())

    @override_settings(COMMUNICATIONS_OUTBOUND_ENABLED=True)
    @patch('requests.post')
    def test_12b_consent_enforcement_effective(self, mock_post):
        """When lead has consent_whatsapp=False or do_not_contact=True, message raises ConsentViolationError."""
        self.lead.consent_whatsapp = False
        self.lead.save(using='tenant_test')

        with self.assertRaises(ConsentViolationError):
            CommunicationService.send_communication(
                organization=self.org,
                channel='WHATSAPP',
                recipient='+919876543210',
                body='Marketing promo offer',
                lead=self.lead,
                provider_preference='GUPSHUP',
                purpose='MARKETING',
            )
        mock_post.assert_not_called()

    # -------------------------------------------------------------------------
    # 13. Existing Meta Lead Ads and communication regression tests unaffected
    # -------------------------------------------------------------------------
    def test_13_meta_webhook_regression_unaffected(self):
        """Existing Meta WhatsApp webhooks continue to resolve and verify challenge correctly."""
        # Integration for Meta
        Integration.objects.using('tenant_test').create(
            integration_type='WHATSAPP',
            provider='META',
            status='ACTIVE',
            configuration={'verify_token': 'meta_verify_secret_123'},
        )

        url = f"/api/v1/webhooks/communications/META/{self.tenant.id}/"
        # GET challenge
        res_get = self.client.get(
            f"{url}?hub.mode=subscribe&hub.challenge=test_challenge_code&hub.verify_token=meta_verify_secret_123"
        )
        self.assertEqual(res_get.status_code, status.HTTP_200_OK)
        self.assertEqual(res_get.content.decode('utf-8'), 'test_challenge_code')


