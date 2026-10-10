"""
backend/tests/test_sarvam_voice_calling.py — Comprehensive Unit & Integration Tests for Sarvam AI Voice Calling.

Covers:
1. Successful outbound submission using mocked HTTP response
2. Missing/invalid configuration error handling
3. Provider HTTP errors and timeouts handling (zero secret leakage)
4. Duplicate call prevention for active sessions
5. Deterministic client idempotency deduplication
6. Lead consent and DNC suppression enforcement
7. Invalid webhook authentication rejection
8. Valid post-call webhook processing (duration, transcript, variables)
9. Duplicate and out-of-order callback regression protection
10. Strict multi-tenant isolation
11. Lead timeline integration without duplicate activities
"""

import json
import uuid
import io
import urllib.error
from unittest.mock import patch, MagicMock
from datetime import timedelta
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from config.routers import set_tenant_db_alias
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.models_saas import SaasPlan, TenantSubscription, ProductModule, TenantModule
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_crm import Lead, LeadSource, CallSession
from apps.tenant_core.models_infra import Integration
from apps.tenant_core.communication.registry import CommunicationProviderRegistry
from apps.tenant_core.communication.adapters.voice_sarvam import SarvamVoiceAdapter
from apps.tenant_core.services_voice_calling import VoiceCallingService
from apps.tenant_core.services_crm import CRMLeadService


class SarvamVoiceCallingTests(TestCase):
    databases = '__all__'

    def setUp(self):
        super().setUp()
        set_tenant_db_alias('tenant_test')
        self.client = APIClient()

        # 1. Master DB Setup (Tenant A)
        self.tenant_a = Tenant.objects.using('default').create(
            code='CRM-SARVAM-A',
            name='Tenant Sarvam Gym',
            slug='tenant-sarvam-gym',
            status='ACTIVE',
        )
        self.ds_a = TenantDataSource.objects.using('default').create(
            tenant=self.tenant_a,
            db_name='test_fitness_tenant',
            database_name='test_fitness_tenant',
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )
        self.plan = SaasPlan.objects.using('default').create(
            name='Enterprise Voice Plan',
            code='ENT-VOICE-1',
            tier='ENTERPRISE',
            status='ACTIVE',
        )
        TenantSubscription.objects.using('default').create(
            tenant=self.tenant_a,
            plan=self.plan,
            status='ACTIVE',
        )

        # 2. Tenant DB Setup (Tenant A)
        self.org_a = Organization.objects.using('tenant_test').create(
            code='ORG-SARVAM-A',
            name='Sarvam Fitness Club',
            status='ACTIVE',
        )
        self.loc_a = Location.objects.using('tenant_test').create(
            organization=self.org_a,
            code='LOC-SARVAM-A',
            name='Bangalore Central',
            status='ACTIVE',
        )
        self.branch_a = Branch.objects.using('tenant_test').create(
            organization=self.org_a,
            location=self.loc_a,
            code='BR-BLR-1',
            name='Koramangala Club',
            status='ACTIVE',
        )

        self.lead_source = LeadSource.objects.using('tenant_test').create(
            organization=self.org_a,
            name='Meta Ads',
            code='META_ADS',
            source_type='META',
            status='ACTIVE',
        )

        self.lead_a = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            first_name='Rahul',
            last_name='Sharma',
            phone_normalized='+919876543210',
            email_normalized='rahul@example.com',
            lead_source=self.lead_source,
            current_status='NEW_LEAD',
            do_not_contact=False,
        )

        # Integration configuration for Sarvam
        self.integration_a = Integration.objects.using('tenant_test').create(
            integration_type='CALLING',
            provider='SARVAM',
            secret_reference='env://SARVAM_API_KEY',
            configuration={
                'org_id': 'org_test_123',
                'workspace_id': 'ws_test_456',
                'app_id': 'app_voice_agent_789',
                'app_version': 1,
                'connection_id': 'conn_rented_001',
                'agent_phone_number': '+918044620704',
                'webhook_token': 'secret_webhook_token_abc',
                'public_integration_id': str(self.tenant_a.id),
                'webhook_base_url': 'https://api.fitness.vibecopilot.ai',
            },
            status='ACTIVE',
        )

    # -------------------------------------------------------------------------
    # 1. Successful Outbound Call Submission (Mocked)
    # -------------------------------------------------------------------------
    @patch('urllib.request.urlopen')
    def test_successful_outbound_submission_mocked(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.getcode.return_value = 200
        mock_response.read.return_value = json.dumps({'attempt_id': 'outbound_attempt_test_999'}).encode('utf-8')
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        with patch.dict('os.environ', {'SARVAM_API_KEY': 'test_sarvam_key_secret'}):
            session = VoiceCallingService.initiate_outbound_call(
                lead=self.lead_a,
                idempotency_key='test-key-001',
                db_alias='tenant_test',
            )

        self.assertIsNotNone(session)
        self.assertEqual(session.status, 'INITIATED')
        self.assertEqual(session.provider_attempt_id, 'outbound_attempt_test_999')
        self.assertEqual(session.user_phone_number, '+919876543210')
        self.assertEqual(session.agent_phone_number, '+918044620704')
        self.assertEqual(session.direction, 'OUTBOUND')
        self.assertIsNotNone(session.started_at)
        self.assertEqual(session.transcript, [])

        # Verify correct URL and headers submitted
        called_req = mock_urlopen.call_args[0][0]
        self.assertIn('/orgs/org_test_123/workspaces/ws_test_456/outbounds', called_req.full_url)
        self.assertEqual(called_req.headers.get('X-api-key'), 'test_sarvam_key_secret')

    # -------------------------------------------------------------------------
    # 2. Missing or Invalid Configuration
    # -------------------------------------------------------------------------
    def test_missing_or_invalid_configuration(self):
        # Corrupt integration configuration
        self.integration_a.configuration = {}
        self.integration_a.save(using='tenant_test')

        with patch.dict('os.environ', {}, clear=True):
            session = VoiceCallingService.initiate_outbound_call(
                lead=self.lead_a,
                idempotency_key='test-key-unconf',
                db_alias='tenant_test',
            )

        self.assertEqual(session.status, 'FAILED')
        self.assertEqual(session.error_code, 'UNCONFIGURED')
        self.assertIn('not configured', session.error_message)

    # -------------------------------------------------------------------------
    # 3. Provider Errors & Timeouts (No Secret Exposure)
    # -------------------------------------------------------------------------
    @patch('urllib.request.urlopen')
    def test_provider_http_error_and_timeout(self, mock_urlopen):
        # 3a. HTTP 500 error from Sarvam
        err_fp = io.BytesIO(b'{"detail": "Internal gateway failure"}')
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url='https://apps.sarvam.ai/...',
            code=500,
            msg='Internal Server Error',
            hdrs={},
            fp=err_fp,
        )

        with patch.dict('os.environ', {'SARVAM_API_KEY': 'super_secret_key'}):
            session = VoiceCallingService.initiate_outbound_call(
                lead=self.lead_a,
                idempotency_key='test-key-500',
                db_alias='tenant_test',
            )
            self.assertEqual(session.status, 'FAILED')
            self.assertEqual(session.error_code, 'HTTP_500')
            self.assertNotIn('super_secret_key', session.error_message)

            # 3b. Timeout error
            mock_urlopen.side_effect = TimeoutError('Request timed out')
            session_timeout = VoiceCallingService.initiate_outbound_call(
                lead=self.lead_a,
                idempotency_key='test-key-timeout',
                db_alias='tenant_test',
            )
            self.assertEqual(session_timeout.status, 'FAILED')
            self.assertEqual(session_timeout.error_code, 'TIMEOUT')

    # -------------------------------------------------------------------------
    # 4. Duplicate Active Call Prevention
    # -------------------------------------------------------------------------
    def test_duplicate_active_call_prevention(self):
        # Create an existing active session
        CallSession.objects.using('tenant_test').create(
            organization=self.org_a,
            lead=self.lead_a,
            provider='SARVAM',
            status='RINGING',
            direction='OUTBOUND',
            user_phone_number=self.lead_a.phone_normalized,
            provider_attempt_id='attempt_active_1',
            started_at=timezone.now(),
        )

        # Second attempt should return the active session without placing new call
        session = VoiceCallingService.initiate_outbound_call(
            lead=self.lead_a,
            db_alias='tenant_test',
        )
        self.assertEqual(session.provider_attempt_id, 'attempt_active_1')
        self.assertEqual(session.status, 'RINGING')

    # -------------------------------------------------------------------------
    # 5. Idempotency Key Deduplication
    # -------------------------------------------------------------------------
    def test_idempotency_key_deduplication(self):
        session1 = CallSession.objects.using('tenant_test').create(
            organization=self.org_a,
            lead=self.lead_a,
            provider='SARVAM',
            status='COMPLETED',
            idempotency_key='idemp-unique-123',
        )

        session2 = VoiceCallingService.initiate_outbound_call(
            lead=self.lead_a,
            idempotency_key='idemp-unique-123',
            db_alias='tenant_test',
        )
        self.assertEqual(session1.id, session2.id)

    # -------------------------------------------------------------------------
    # 6. Lead Consent and DNC Suppression
    # -------------------------------------------------------------------------
    def test_lead_consent_suppression(self):
        self.lead_a.do_not_contact = True
        self.lead_a.save(using='tenant_test')

        with self.assertRaises(ValueError) as ctx:
            VoiceCallingService.initiate_outbound_call(lead=self.lead_a, db_alias='tenant_test')
        self.assertIn('Do Not Contact', str(ctx.exception))

    # -------------------------------------------------------------------------
    # 7. Invalid Webhook Authentication
    # -------------------------------------------------------------------------
    def test_invalid_webhook_authentication(self):
        url = f"/api/v1/webhooks/communications/sarvam/{self.tenant_a.id}/"
        payload = {
            'attempt_id': 'attempt_fake_1',
            'status': 'connected',
        }

        # Request with wrong token
        response = self.client.post(
            url,
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_X_WEBHOOK_TOKEN='invalid_token_999',
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_valid_webhook_query_token_authentication(self):
        url = f"/api/v1/webhooks/communications/sarvam/{self.tenant_a.id}/?token=secret_webhook_token_abc"
        payload = {
            'attempt_id': 'attempt_fake_token_test',
            'status': 'connected',
        }
        response = self.client.post(
            url,
            data=json.dumps(payload),
            content_type='application/json',
        )
        # Should authenticate successfully (200 OK)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    # -------------------------------------------------------------------------
    # 8. Valid Webhook Callback Processing & Lead Stage Update
    # -------------------------------------------------------------------------
    def test_valid_webhook_records_transcript_and_duration(self):
        session = CallSession.objects.using('tenant_test').create(
            organization=self.org_a,
            lead=self.lead_a,
            provider='SARVAM',
            provider_attempt_id='attempt_sarvam_abc123',
            status='INITIATED',
            direction='OUTBOUND',
            user_phone_number=self.lead_a.phone_normalized,
            started_at=timezone.now(),
        )

        url = f"/api/v1/webhooks/communications/sarvam/{self.tenant_a.id}/"
        webhook_payload = {
            'attempt_id': 'attempt_sarvam_abc123',
            'status': 'connected',
            'duration': 54.2,
            'interaction_transcript': [
                {'role': 'agent', 'en_text': 'Hello, calling from SWEAT Fitness.'},
                {'role': 'user', 'en_text': 'Hi, I would like to book a trial.'},
            ],
            'final_agent_variables': {
                'intent': 'BOOK_TRIAL',
                'preferred_time': '10:00 AM',
            },
            'metadata': {
                'call_session_id': str(session.id),
                'lead_id': str(self.lead_a.id),
            },
        }

        response = self.client.post(
            url,
            data=json.dumps(webhook_payload),
            content_type='application/json',
            HTTP_X_WEBHOOK_TOKEN='secret_webhook_token_abc',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()['status'], 'recorded')

        session = CallSession.objects.using('tenant_test').get(id=session.id)
        self.assertEqual(session.status, 'COMPLETED')
        self.assertEqual(session.duration_seconds, 54.2)
        self.assertEqual(len(session.transcript), 2)
        self.assertEqual(session.final_agent_variables['intent'], 'BOOK_TRIAL')

        # Verify Lead status automatically updated from NEW_LEAD to FOLLOW_UP_PENDING
        lead_a = Lead.objects.using('tenant_test').get(id=self.lead_a.id)
        self.assertEqual(lead_a.current_status, 'FOLLOW_UP_PENDING')

    # -------------------------------------------------------------------------
    # 9. Out-of-Order Webhook Regression Protection
    # -------------------------------------------------------------------------
    def test_out_of_order_webhook_protection(self):
        session = CallSession.objects.using('tenant_test').create(
            organization=self.org_a,
            lead=self.lead_a,
            provider='SARVAM',
            provider_attempt_id='attempt_terminal_1',
            status='COMPLETED',
            duration_seconds=30.0,
        )

        url = f"/api/v1/webhooks/communications/sarvam/{self.tenant_a.id}/"
        # Out-of-order callback arriving late with status 'ringing'
        delayed_payload = {
            'attempt_id': 'attempt_terminal_1',
            'status': 'ringing',
            'metadata': {'call_session_id': str(session.id)},
        }

        response = self.client.post(
            url,
            data=json.dumps(delayed_payload),
            content_type='application/json',
            HTTP_X_WEBHOOK_TOKEN='secret_webhook_token_abc',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        session = CallSession.objects.using('tenant_test').get(id=session.id)
        # Must still be COMPLETED, not regressed to RINGING
        self.assertEqual(session.status, 'COMPLETED')

    # -------------------------------------------------------------------------
    # 10. Multi-Tenant Isolation
    # -------------------------------------------------------------------------
    def test_tenant_isolation(self):
        # Create second tenant
        tenant_b = Tenant.objects.using('default').create(
            code='CRM-SARVAM-B',
            name='Tenant B Competitor',
            slug='tenant-b-competitor',
            status='ACTIVE',
        )

        session_a = CallSession.objects.using('tenant_test').create(
            organization=self.org_a,
            lead=self.lead_a,
            provider='SARVAM',
            provider_attempt_id='attempt_iso_a',
            status='INITIATED',
        )

        # Attempt to access Tenant A's session using Tenant B's webhook URL
        url_b = f"/api/v1/webhooks/communications/sarvam/{tenant_b.id}/"
        payload = {
            'attempt_id': 'attempt_iso_a',
            'status': 'connected',
            'metadata': {'call_session_id': str(session_a.id)},
        }

        # Tenant B has no Sarvam integration configured in its DB
        response = self.client.post(
            url_b,
            data=json.dumps(payload),
            content_type='application/json',
        )
        # Should not mutate Tenant A's session
        session_a = CallSession.objects.using('tenant_test').get(id=session_a.id)
        self.assertEqual(session_a.status, 'INITIATED')

    # -------------------------------------------------------------------------
    # 11. Lead Timeline Integration
    # -------------------------------------------------------------------------
    def test_lead_timeline_integration(self):
        CallSession.objects.using('tenant_test').create(
            organization=self.org_a,
            lead=self.lead_a,
            provider='SARVAM',
            provider_attempt_id='attempt_timeline_1',
            status='COMPLETED',
            duration_seconds=42.0,
            transcript=[{'role': 'agent', 'en_text': 'Hello'}],
            final_agent_variables={'interest': 'HIGH'},
            started_at=timezone.now(),
        )

        timeline = CRMLeadService.get_lead_timeline(self.lead_a, db_alias='tenant_test')
        voice_events = [e for e in timeline if e.get('event_type') == 'VOICE_CALL']

        self.assertEqual(len(voice_events), 1)
        ev = voice_events[0]
        self.assertEqual(ev['title'], 'AI Call: Completed')
        self.assertIn('42s', ev['description'])
        self.assertEqual(ev['channel'], 'VOICE')
        self.assertEqual(ev['metadata']['duration_seconds'], 42.0)
