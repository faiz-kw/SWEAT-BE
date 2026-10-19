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
from django.test import TestCase, TransactionTestCase, override_settings
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
from apps.authentication.views import _build_tenant_token
from apps.tenant_core.models_users import TenantUser, UserBranch
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
from apps.tenant_core.services_voice_calling import VoiceCallingService
from apps.tenant_core.services_crm import CRMLeadService


@override_settings(COMMUNICATIONS_OUTBOUND_ENABLED=True, VOICE_CALLING_ENABLED=True, VOICE_CALLING_ENFORCE_HOURS=False)
class SarvamVoiceCallingTests(TestCase):
    databases = {'default', 'tenant_test'}

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

        self._setup_rbac_and_users()

        # Voice Calling Purpose and Explicit ConsentRecord for lead_a
        from apps.tenant_core.models_privacy import ProcessingPurpose, ConsentRecord
        self.voice_purpose, _ = ProcessingPurpose.objects.using('tenant_test').get_or_create(
            code='VOICE_CALL',
            defaults={
                'name': 'AI Voice Calling',
                'description': 'Explicit consent for automated and AI voice calls',
            },
        )
        self.consent_record_a = ConsentRecord.objects.using('tenant_test').create(
            user=self.user_editor,
            purpose=self.voice_purpose,
            subject_type='lead',
            subject_id=self.lead_a.id,
            status='GRANTED',
            capture_source='WEB_FORM',
            notice_version='1.0',
        )

    def _setup_rbac_and_users(self):
        # 1. Master DB SaaS Modules
        self.prod_mod_crm, _ = ProductModule.objects.using('default').get_or_create(
            code='crm', defaults={'name': 'CRM Module', 'status': 'ACTIVE'}
        )
        TenantModule.objects.using('default').get_or_create(
            tenant=self.tenant_a,
            module=self.prod_mod_crm,
            defaults={'is_enabled': True, 'availability_mode': 'ALL_BRANCHES'},
        )

        # 2. Catalogs in Tenant DB
        self.mod_crm, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(
            module_code='crm', defaults={'name': 'CRM', 'is_enabled': True}
        )
        self.submod_leads, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=self.mod_crm, submodule_code='leads', defaults={'name': 'Leads', 'is_enabled': True}
        )

        # 3. Permissions: crm.leads.view and crm.leads.edit
        self.perm_edit, _ = Permission.objects.using('tenant_test').get_or_create(
            permission_code='crm.leads.edit',
            defaults={
                'code': 'crm.leads.edit',
                'label': 'crm.leads.edit',
                'action': 'edit',
                'module': self.mod_crm,
                'submodule': self.submod_leads,
                'source_permission_id': uuid.uuid4(),
                'is_active': True,
            },
        )
        self.perm_view, _ = Permission.objects.using('tenant_test').get_or_create(
            permission_code='crm.leads.view',
            defaults={
                'code': 'crm.leads.view',
                'label': 'crm.leads.view',
                'action': 'view',
                'module': self.mod_crm,
                'submodule': self.submod_leads,
                'source_permission_id': uuid.uuid4(),
                'is_active': True,
            },
        )

        # 4. Roles
        self.role_editor = Role.objects.using('tenant_test').create(
            organization=self.org_a,
            code='ROLE_EDITOR_SARVAM',
            name='Editor Role',
            scope='ORG',
            is_active=True,
        )
        self.role_readonly = Role.objects.using('tenant_test').create(
            organization=self.org_a,
            code='ROLE_READONLY_SARVAM',
            name='Readonly Role',
            scope='ORG',
            is_active=True,
        )

        for r in (self.role_editor, self.role_readonly):
            RoleModuleAccess.objects.using('tenant_test').get_or_create(role=r, module=self.mod_crm, defaults={'can_access': True})
            RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(role=r, submodule=self.submod_leads, defaults={'can_access': True})

        ps_ed = RolePermissionSet.objects.using('tenant_test').create(role=self.role_editor, name='editor_perms')
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=ps_ed, permission=self.perm_edit)
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=ps_ed, permission=self.perm_view)

        self.perm_comm_view, _ = Permission.objects.using('tenant_test').get_or_create(
            permission_code='crm.communications.view',
            defaults={
                'code': 'crm.communications.view',
                'label': 'crm.communications.view',
                'action': 'view',
                'module': self.mod_crm,
                'submodule': self.submod_leads,
                'source_permission_id': uuid.uuid4(),
                'is_active': True,
            },
        )
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=ps_ed, permission=self.perm_comm_view)

        ps_ro = RolePermissionSet.objects.using('tenant_test').create(role=self.role_readonly, name='readonly_perms')
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=ps_ro, permission=self.perm_view)

        # 5. Users
        self.user_editor = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a,
            email='editor_staff@sarvam.test',
            first_name='Editor',
            last_name='Staff',
            status='ACTIVE',
        )
        UserBranch.objects.using('tenant_test').create(
            user=self.user_editor, branch=self.branch_a, scope_type='HOME', status='ACTIVE', is_active=True
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.user_editor, role=self.role_editor, branch=None, is_active=True, status='ACTIVE'
        )

        self.user_readonly = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a,
            email='readonly_staff@sarvam.test',
            first_name='Readonly',
            last_name='Staff',
            status='ACTIVE',
        )
        UserBranch.objects.using('tenant_test').create(
            user=self.user_readonly, branch=self.branch_a, scope_type='HOME', status='ACTIVE', is_active=True
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.user_readonly, role=self.role_readonly, branch=None, is_active=True, status='ACTIVE'
        )

        # 6. Tokens
        self.token_editor = str(_build_tenant_token(self.user_editor, self.tenant_a, 'tenant_test').access_token)
        self.token_readonly = str(_build_tenant_token(self.user_readonly, self.tenant_a, 'tenant_test').access_token)

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
            self.assertEqual(session_timeout.status, 'INITIATED')
            self.assertEqual(session_timeout.error_code, 'TIMEOUT_UNCONFIRMED')

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
    # 7. Webhook Security Hardening & Fail-Closed Authentication
    # -------------------------------------------------------------------------
    def test_webhook_missing_configured_secret_fails_closed(self):
        """
        If tenant integration has no webhook_token configured (or empty/whitespace),
        incoming webhooks MUST be rejected (401 Unauthorized), even if request sends a token.
        """
        self.integration_a.configuration['webhook_token'] = ''
        self.integration_a.save(using='tenant_test')

        url = f"/api/v1/webhooks/communications/sarvam/{self.tenant_a.id}/"
        payload = {'attempt_id': 'attempt_fake_1', 'status': 'connected'}

        response = self.client.post(
            url,
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_X_WEBHOOK_TOKEN='some_token_123',
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.json(), {'error': 'Invalid webhook signature'})

    def test_webhook_inactive_integration_rejects_unauthorized(self):
        """
        If tenant integration is INACTIVE, incoming webhooks must be rejected (401).
        """
        self.integration_a.status = 'INACTIVE'
        self.integration_a.save(using='tenant_test')

        url = f"/api/v1/webhooks/communications/sarvam/{self.tenant_a.id}/"
        payload = {'attempt_id': 'attempt_fake_1', 'status': 'connected'}
        response = self.client.post(
            url,
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_X_WEBHOOK_TOKEN='secret_webhook_token_abc',
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_webhook_missing_request_token_rejects(self):
        """
        If request contains no X-Webhook-Token, Authorization, or query token,
        it must be rejected (401 Unauthorized).
        """
        url = f"/api/v1/webhooks/communications/sarvam/{self.tenant_a.id}/"
        payload = {'attempt_id': 'attempt_fake_1', 'status': 'connected'}
        response = self.client.post(
            url,
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_webhook_incorrect_token_rejects(self):
        """
        Request with wrong token must be rejected (401 Unauthorized).
        """
        url = f"/api/v1/webhooks/communications/sarvam/{self.tenant_a.id}/"
        payload = {'attempt_id': 'attempt_fake_1', 'status': 'connected'}
        response = self.client.post(
            url,
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_X_WEBHOOK_TOKEN='invalid_token_999',
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_webhook_valid_token_bearer_header_authorizes(self):
        """
        Token provided via Authorization: Bearer <token> must authenticate cleanly.
        """
        url = f"/api/v1/webhooks/communications/sarvam/{self.tenant_a.id}/"
        payload = {'attempt_id': 'attempt_fake_bearer_test', 'status': 'connected'}
        response = self.client.post(
            url,
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_AUTHORIZATION='Bearer secret_webhook_token_abc',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_webhook_valid_query_token_authorizes(self):
        """
        Token provided via ?token=<token> query param must authenticate cleanly.
        """
        url = f"/api/v1/webhooks/communications/sarvam/{self.tenant_a.id}/?token=secret_webhook_token_abc"
        payload = {'attempt_id': 'attempt_fake_token_test', 'status': 'connected'}
        response = self.client.post(
            url,
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_webhook_unknown_tenant_rejects_404(self):
        """
        Request for nonexistent tenant must return 404 Not Found before tenant DB lookup.
        """
        unknown_id = str(uuid.uuid4())
        url = f"/api/v1/webhooks/communications/sarvam/{unknown_id}/"
        response = self.client.post(
            url,
            data=json.dumps({'attempt_id': 'attempt_foo'}),
            content_type='application/json',
            HTTP_X_WEBHOOK_TOKEN='secret_webhook_token_abc',
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_webhook_malformed_json_authenticated_rejects_400(self):
        """
        Authenticated request containing unparseable JSON must return 400 Bad Request.
        """
        url = f"/api/v1/webhooks/communications/sarvam/{self.tenant_a.id}/"
        response = self.client.post(
            url,
            data=b'{invalid json syntax',
            content_type='application/json',
            HTTP_X_WEBHOOK_TOKEN='secret_webhook_token_abc',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json(), {'error': 'Malformed JSON payload'})

    def test_webhook_non_dict_json_authenticated_rejects_400(self):
        """
        Authenticated request with valid JSON that is not a JSON object (e.g. list) must return 400 Bad Request.
        """
        url = f"/api/v1/webhooks/communications/sarvam/{self.tenant_a.id}/"
        response = self.client.post(
            url,
            data=json.dumps(["item1", "item2"]),
            content_type='application/json',
            HTTP_X_WEBHOOK_TOKEN='secret_webhook_token_abc',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json(), {'error': 'Webhook payload must be a JSON object'})

    def test_webhook_no_database_mutation_on_failed_authentication(self):
        """
        Verify that an unauthenticated or invalidly authenticated webhook NEVER mutates
        the CallSession or advances the Lead stage.
        """
        session = CallSession.objects.using('tenant_test').create(
            organization=self.org_a,
            lead=self.lead_a,
            provider='SARVAM',
            provider_attempt_id='attempt_unauth_mutate_test',
            status='INITIATED',
            direction='OUTBOUND',
            user_phone_number=self.lead_a.phone_normalized,
            started_at=timezone.now(),
        )

        url = f"/api/v1/webhooks/communications/sarvam/{self.tenant_a.id}/"
        fraudulent_payload = {
            'attempt_id': 'attempt_unauth_mutate_test',
            'status': 'connected',
            'duration': 180.0,
            'interaction_transcript': [{'role': 'agent', 'en_text': 'Spoofed call'}],
            # Correct Sarvam schema: metadata is echoed inside webhook_config.metadata
            'webhook_config': {
                'url': 'https://test.example.com/webhook',
                'metadata': {'call_session_id': str(session.id)},
            },
        }

        # Submit with incorrect token
        response = self.client.post(
            url,
            data=json.dumps(fraudulent_payload),
            content_type='application/json',
            HTTP_X_WEBHOOK_TOKEN='attacker_forged_token',
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        # Confirm database records remain strictly unchanged
        session_reload = CallSession.objects.using('tenant_test').get(id=session.id)
        self.assertEqual(session_reload.status, 'INITIATED')
        self.assertEqual(session_reload.duration_seconds, 0.0)
        self.assertEqual(session_reload.transcript, [])

        lead_reload = Lead.objects.using('tenant_test').get(id=self.lead_a.id)
        self.assertEqual(lead_reload.current_status, 'NEW_LEAD')

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
            # Official Sarvam schema: metadata echoed inside webhook_config.metadata
            # See: https://docs.sarvam.ai/voice-agents/api/instant-outbound/webhook-payload.md
            'webhook_config': {
                'url': 'https://test.example.com/webhook',
                'metadata': {
                    'call_session_id': str(session.id),
                    'lead_id': str(self.lead_a.id),
                },
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
        # Uses correct Sarvam schema: metadata inside webhook_config.metadata
        delayed_payload = {
            'attempt_id': 'attempt_terminal_1',
            'status': 'ringing',
            'webhook_config': {
                'url': 'https://test.example.com/webhook',
                'metadata': {'call_session_id': str(session.id)},
            },
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
            'webhook_config': {
                'url': 'https://test.example.com/webhook',
                'metadata': {'call_session_id': str(session_a.id)},
            },
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

    # -------------------------------------------------------------------------
    # 12. Lead Timeline Missing Table Graceful Degradation (Regression Test)
    # -------------------------------------------------------------------------
    def test_lead_timeline_graceful_degradation_when_table_missing(self):
        """
        Verify that if migration 0055 (crm_call_sessions table) is pending/unapplied
        and querying CallSession raises ProgrammingError,
        CRMLeadService.get_lead_timeline degrades gracefully without crashing,
        returning the non-voice timeline events (Lead Created, etc.).
        """
        from django.db.utils import ProgrammingError
        with patch.object(CallSession.objects, 'using') as mock_using:
            mock_using.side_effect = ProgrammingError('relation "crm_call_sessions" does not exist')
            timeline = CRMLeadService.get_lead_timeline(self.lead_a, db_alias='tenant_test')

            self.assertIsInstance(timeline, list)
            self.assertGreater(len(timeline), 0)
            voice_events = [e for e in timeline if e.get('event_type') == 'VOICE_CALL']
            self.assertEqual(len(voice_events), 0)
            event_types = [e.get('event_type') for e in timeline]
            self.assertIn('LEAD_CREATED', event_types)

    # -------------------------------------------------------------------------
    # 13. Phase 3B: Authenticated Outbound Call REST API Tests
    # -------------------------------------------------------------------------
    def test_call_api_unauthenticated_rejects_401(self):
        """Unauthenticated requests without Authorization header must be rejected with 401."""
        self.client.credentials()  # Clear auth headers
        response = self.client.post(
            f'/api/v1/tenant/leads/{self.lead_a.id}/call/',
            data={},
            format='json',
            HTTP_X_IDEMPOTENCY_KEY='idem-unauth',
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_call_api_insufficient_permissions_rejects_403(self):
        """Staff with read-only access (crm.leads.view) must be rejected with 403 Forbidden."""
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_readonly}')
        response = self.client.post(
            f'/api/v1/tenant/leads/{self.lead_a.id}/call/',
            data={},
            format='json',
            HTTP_X_IDEMPOTENCY_KEY='idem-noperm',
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_call_api_cross_tenant_lead_access_rejects_404(self):
        """Attempting to initiate a call for a lead in another tenant/org returns 404 Not Found."""
        org_b = Organization.objects.using('tenant_test').create(
            code='ORG-SARVAM-OTHER',
            name='Other Gym Org',
            status='ACTIVE',
        )
        lead_other = Lead.objects.using('tenant_test').create(
            organization=org_b,
            first_name='Ananya',
            last_name='Verma',
            phone_normalized='+919876543299',
            current_status='NEW_LEAD',
        )

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_editor}')
        response = self.client.post(
            f'/api/v1/tenant/leads/{lead_other.id}/call/',
            data={},
            format='json',
            HTTP_X_IDEMPOTENCY_KEY='idem-crosstenant',
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_call_api_missing_idempotency_key_rejects_400(self):
        """Requests without an idempotency key (header or body) must be rejected with 400."""
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_editor}')
        response = self.client.post(
            f'/api/v1/tenant/leads/{self.lead_a.id}/call/',
            data={},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data.get('code'), 'MISSING_IDEMPOTENCY_KEY')

    def test_call_api_opted_out_and_dnc_and_invalid_phone_rejects_400(self):
        """Leads marked Do Not Contact, opted out, or with invalid phone numbers must be rejected."""
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_editor}')

        # 1. Do Not Contact lead
        self.lead_a.do_not_contact = True
        self.lead_a.save(using='tenant_test')
        res_dnc = self.client.post(
            f'/api/v1/tenant/leads/{self.lead_a.id}/call/',
            data={},
            format='json',
            HTTP_X_IDEMPOTENCY_KEY='idem-dnc-1',
        )
        self.assertEqual(res_dnc.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_dnc.data.get('code'), 'INVALID_RECIPIENT_OR_CONSENT')
        self.assertIn('Do Not Contact', res_dnc.data.get('error', ''))

        # 2. Opted out lead
        self.lead_a.do_not_contact = False
        self.lead_a.consent_sms = False
        self.lead_a.consent_whatsapp = False
        self.lead_a.save(using='tenant_test')
        res_opt = self.client.post(
            f'/api/v1/tenant/leads/{self.lead_a.id}/call/',
            data={},
            format='json',
            HTTP_X_IDEMPOTENCY_KEY='idem-optout-1',
        )
        self.assertEqual(res_opt.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_opt.data.get('code'), 'INVALID_RECIPIENT_OR_CONSENT')
        self.assertIn('opted out', res_opt.data.get('error', ''))

        # 3. Invalid phone number
        self.lead_a.consent_sms = True
        self.lead_a.consent_whatsapp = True
        self.lead_a.phone_normalized = 'invalid_phone_string'
        self.lead_a.save(using='tenant_test')
        res_phone = self.client.post(
            f'/api/v1/tenant/leads/{self.lead_a.id}/call/',
            data={},
            format='json',
            HTTP_X_IDEMPOTENCY_KEY='idem-phone-1',
        )
        self.assertEqual(res_phone.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_phone.data.get('code'), 'INVALID_RECIPIENT_OR_CONSENT')

    def test_call_api_missing_integration_config_rejects_400(self):
        """Unconfigured provider integration returns 400 Bad Request with UNCONFIGURED code."""
        self.integration_a.configuration = {}
        self.integration_a.save(using='tenant_test')

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_editor}')
        with patch.dict('os.environ', {}, clear=True):
            response = self.client.post(
                f'/api/v1/tenant/leads/{self.lead_a.id}/call/',
                data={},
                format='json',
                HTTP_X_IDEMPOTENCY_KEY='idem-unconf-api',
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data.get('code'), 'UNCONFIGURED')
        self.assertEqual(response.data.get('status'), 'FAILED')
        self.assertIn('call_session_id', response.data)

    @patch('urllib.request.urlopen')
    def test_call_api_successful_mocked_dispatch_201(self, mock_urlopen):
        """Successful manual call request dispatches mocked transport and returns 201 with zero secrets."""
        mock_response = MagicMock()
        mock_response.getcode.return_value = 200
        mock_response.read.return_value = json.dumps({'attempt_id': 'sarvam_att_api_201'}).encode('utf-8')
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_editor}')
        with patch.dict('os.environ', {'SARVAM_API_KEY': 'test_sarvam_key_secret'}):
            response = self.client.post(
                f'/api/v1/tenant/leads/{self.lead_a.id}/call/',
                data={'custom_metadata': {'campaign': 'SpringPromo'}},
                format='json',
                HTTP_X_IDEMPOTENCY_KEY='idem-dispatch-201',
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.data
        self.assertIn('call_session_id', data)
        self.assertEqual(data['status'], 'INITIATED')
        self.assertEqual(data['provider'], 'SARVAM')
        self.assertEqual(data['direction'], 'OUTBOUND')
        self.assertEqual(data['recipient_phone'], '+919876543210')
        self.assertEqual(data['provider_attempt_id'], 'sarvam_att_api_201')
        self.assertFalse(data['is_duplicate'])

        # Security check: Zero secret exposure in API response
        resp_str = json.dumps(data)
        self.assertNotIn('test_sarvam_key_secret', resp_str)
        self.assertNotIn('secret_webhook_token_abc', resp_str)
        self.assertNotIn('transcript', resp_str)

    @patch('urllib.request.urlopen')
    def test_call_api_duplicate_idempotency_key_safe_replay_200(self, mock_urlopen):
        """Submitting the same idempotency key safely returns 200 OK without re-dispatching to provider."""
        mock_response = MagicMock()
        mock_response.getcode.return_value = 200
        mock_response.read.return_value = json.dumps({'attempt_id': 'sarvam_att_dup'}).encode('utf-8')
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_editor}')
        with patch.dict('os.environ', {'SARVAM_API_KEY': 'test_sarvam_key_secret'}):
            # First request creates session
            res1 = self.client.post(
                f'/api/v1/tenant/leads/{self.lead_a.id}/call/',
                data={},
                format='json',
                HTTP_X_IDEMPOTENCY_KEY='idem-dup-key-test',
            )
            self.assertEqual(res1.status_code, status.HTTP_201_CREATED)
            session_id = res1.data['call_session_id']

            # Reset mock to verify second call is NOT sent to provider
            mock_urlopen.reset_mock()
            res2 = self.client.post(
                f'/api/v1/tenant/leads/{self.lead_a.id}/call/',
                data={},
                format='json',
                HTTP_X_IDEMPOTENCY_KEY='idem-dup-key-test',
            )
            self.assertEqual(res2.status_code, status.HTTP_200_OK)
            self.assertEqual(res2.data['call_session_id'], session_id)
            self.assertTrue(res2.data['is_duplicate'])
            mock_urlopen.assert_not_called()

    @patch('urllib.request.urlopen')
    def test_call_api_concurrent_active_call_rejects_409(self, mock_urlopen):
        """Attempting to initiate another call while an active call is running returns 409 Conflict."""
        mock_response = MagicMock()
        mock_response.getcode.return_value = 200
        mock_response.read.return_value = json.dumps({'attempt_id': 'sarvam_att_active'}).encode('utf-8')
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_editor}')
        with patch.dict('os.environ', {'SARVAM_API_KEY': 'test_sarvam_key_secret'}):
            res1 = self.client.post(
                f'/api/v1/tenant/leads/{self.lead_a.id}/call/',
                data={},
                format='json',
                HTTP_X_IDEMPOTENCY_KEY='idem-call-1',
            )
            self.assertEqual(res1.status_code, status.HTTP_201_CREATED)

            # Second call with DIFFERENT idempotency key
            res2 = self.client.post(
                f'/api/v1/tenant/leads/{self.lead_a.id}/call/',
                data={},
                format='json',
                HTTP_X_IDEMPOTENCY_KEY='idem-call-2-concurrent',
            )
            self.assertEqual(res2.status_code, status.HTTP_409_CONFLICT)
            self.assertEqual(res2.data.get('code'), 'CALL_ALREADY_IN_PROGRESS')
            self.assertEqual(res2.data.get('call_session_id'), res1.data['call_session_id'])

    @patch('urllib.request.urlopen')
    def test_call_api_provider_timeout_returns_504(self, mock_urlopen):
        """Provider timeout returns 504 Gateway Timeout without leaking sensitive config, marking session uncertain."""
        mock_urlopen.side_effect = TimeoutError("Connection to Sarvam timed out after 15s")

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_editor}')
        with patch.dict('os.environ', {'SARVAM_API_KEY': 'test_sarvam_key_secret'}):
            response = self.client.post(
                f'/api/v1/tenant/leads/{self.lead_a.id}/call/',
                data={},
                format='json',
                HTTP_X_IDEMPOTENCY_KEY='idem-timeout-api',
            )

        self.assertEqual(response.status_code, status.HTTP_504_GATEWAY_TIMEOUT)
        self.assertEqual(response.data.get('code'), 'TIMEOUT_PENDING_RECONCILIATION')
        self.assertEqual(response.data.get('status'), 'INITIATED')
        self.assertIn('call_session_id', response.data)

        # Confirm session is kept in INITIATED with TIMEOUT_UNCONFIRMED
        session = CallSession.objects.using('tenant_test').get(id=response.data['call_session_id'])
        self.assertEqual(session.status, 'INITIATED')
        self.assertEqual(session.error_code, 'TIMEOUT_UNCONFIRMED')

    @patch('urllib.request.urlopen')
    def test_call_api_provider_http_failure_returns_502(self, mock_urlopen):
        """Provider 500 error returns 502 Bad Gateway without leaking provider secrets."""
        err_fp = io.BytesIO(b'{"detail": "Internal Sarvam voice cluster failure"}')
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url='https://apps.sarvam.ai/...',
            code=500,
            msg='Internal Server Error',
            hdrs={},
            fp=err_fp,
        )

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_editor}')
        with patch.dict('os.environ', {'SARVAM_API_KEY': 'test_sarvam_key_secret'}):
            response = self.client.post(
                f'/api/v1/tenant/leads/{self.lead_a.id}/call/',
                data={},
                format='json',
                HTTP_X_IDEMPOTENCY_KEY='idem-500-api',
            )

        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertEqual(response.data.get('code'), 'HTTP_500')
        self.assertEqual(response.data.get('status'), 'FAILED')
        self.assertIn('call_session_id', response.data)

    def test_call_api_missing_database_table_returns_503(self):
        """Missing database migration 0055 (crm_call_sessions table) returns 503 Service Unavailable."""
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_editor}')
        from django.db import connections
        with patch.object(connections['tenant_test'].introspection, 'table_names', return_value=['crm_leads', 'crm_lead_sources']):
            response = self.client.post(
                f'/api/v1/tenant/leads/{self.lead_a.id}/call/',
                data={},
                format='json',
                HTTP_X_IDEMPOTENCY_KEY='idem-table-missing',
            )

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(response.data.get('code'), 'MIGRATION_PENDING')

    def test_call_api_existing_crm_leads_list_regression_ok(self):
        """Existing CRM Leads API endpoints continue to function without regressions."""
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_editor}')
        response = self.client.get('/api/v1/tenant/leads/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data if isinstance(response.data, list) else response.data.get('results', [])
        lead_ids = [str(item['id']) for item in results]
        self.assertIn(str(self.lead_a.id), lead_ids)

    # -------------------------------------------------------------------------
    # PHASE 5A: Critical Safety Remediation Tests
    # -------------------------------------------------------------------------

    @patch('urllib.request.urlopen')
    def test_timeout_uncertainty_blocks_different_idempotency_key(self, mock_urlopen):
        """A timeout marks the session uncertain (INITIATED), blocking subsequent calls with different keys."""
        mock_urlopen.side_effect = TimeoutError("Carrier timeout")

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_editor}')
        with patch.dict('os.environ', {'SARVAM_API_KEY': 'test_sarvam_key_secret'}):
            # First attempt times out
            res1 = self.client.post(
                f'/api/v1/tenant/leads/{self.lead_a.id}/call/',
                data={},
                format='json',
                HTTP_X_IDEMPOTENCY_KEY='key-timeout-1',
            )
            self.assertEqual(res1.status_code, status.HTTP_504_GATEWAY_TIMEOUT)
            self.assertEqual(res1.data['code'], 'TIMEOUT_PENDING_RECONCILIATION')

            # Second attempt with different key for same lead MUST be blocked with 409
            res2 = self.client.post(
                f'/api/v1/tenant/leads/{self.lead_a.id}/call/',
                data={},
                format='json',
                HTTP_X_IDEMPOTENCY_KEY='key-timeout-2',
            )
            self.assertEqual(res2.status_code, status.HTTP_409_CONFLICT)
            self.assertEqual(res2.data['code'], 'CALL_ALREADY_IN_PROGRESS')

    @patch('urllib.request.urlopen')
    def test_timeout_uncertainty_same_key_retry_blocks_resubmission(self, mock_urlopen):
        """Replaying the same key for a timed-out uncertain session returns 504 without calling Sarvam."""
        mock_urlopen.side_effect = TimeoutError("Carrier timeout")

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_editor}')
        with patch.dict('os.environ', {'SARVAM_API_KEY': 'test_sarvam_key_secret'}):
            res1 = self.client.post(
                f'/api/v1/tenant/leads/{self.lead_a.id}/call/',
                data={},
                format='json',
                HTTP_X_IDEMPOTENCY_KEY='key-same-replay',
            )
            self.assertEqual(res1.status_code, status.HTTP_504_GATEWAY_TIMEOUT)
            self.assertEqual(mock_urlopen.call_count, 1)

            # Replay with same key
            res2 = self.client.post(
                f'/api/v1/tenant/leads/{self.lead_a.id}/call/',
                data={},
                format='json',
                HTTP_X_IDEMPOTENCY_KEY='key-same-replay',
            )
            self.assertEqual(res2.status_code, status.HTTP_504_GATEWAY_TIMEOUT)
            self.assertEqual(res2.data['code'], 'TIMEOUT_PENDING_RECONCILIATION')
            self.assertTrue(res2.data['is_duplicate'])
            # Sarvam must NOT have been called a second time
            self.assertEqual(mock_urlopen.call_count, 1)

    def test_late_webhook_reconciles_uncertain_timeout_session(self):
        """A late provider webhook resolves an uncertain TIMEOUT_UNCONFIRMED session to COMPLETED."""
        session = CallSession.objects.using('tenant_test').create(
            organization=self.org_a,
            lead=self.lead_a,
            provider='SARVAM',
            status='INITIATED',
            direction='OUTBOUND',
            user_phone_number='+919876543210',
            idempotency_key='key-recon-test',
            error_code='TIMEOUT_UNCONFIRMED',
            error_message='Request timed out awaiting reconciliation',
        )

        webhook_payload = {
            'attempt_id': 'attempt_reconciled_999',
            'status': 'connected',
            'duration': 42.0,
            'interaction_transcript': [{'role': 'agent', 'text': 'Hello'}],
            # Correct Sarvam schema
            'webhook_config': {
                'url': 'https://test.example.com/webhook',
                'metadata': {'call_session_id': str(session.id)},
            },
        }

        result = VoiceCallingService.record_call_webhook(self.org_a, webhook_payload, db_alias='tenant_test')
        self.assertEqual(result['status'], 'recorded')
        self.assertEqual(result['call_status'], 'COMPLETED')

        session.refresh_from_db()
        self.assertEqual(session.status, 'COMPLETED')
        self.assertEqual(session.error_code, '')
        self.assertEqual(session.provider_attempt_id, 'attempt_reconciled_999')
        self.assertEqual(session.duration_seconds, 42.0)

    def test_terminal_completed_cannot_be_overwritten_by_failed(self):
        """Once a session is COMPLETED, a conflicting FAILED webhook is strictly ignored."""
        session = CallSession.objects.using('tenant_test').create(
            organization=self.org_a,
            lead=self.lead_a,
            provider='SARVAM',
            status='COMPLETED',
            provider_attempt_id='attempt_term_001',
            direction='OUTBOUND',
            duration_seconds=35.0,
            user_phone_number='+919876543210',
        )

        failed_payload = {
            'attempt_id': 'attempt_term_001',
            'status': 'failed',
            'webhook_config': {
                'url': 'https://test.example.com/webhook',
                'metadata': {'call_session_id': str(session.id)},
            },
        }

        result = VoiceCallingService.record_call_webhook(self.org_a, failed_payload, db_alias='tenant_test')
        self.assertEqual(result['status'], 'ignored_terminal_conflict')

        session.refresh_from_db()
        self.assertEqual(session.status, 'COMPLETED')
        self.assertEqual(session.duration_seconds, 35.0)

    def test_terminal_failed_cannot_be_overwritten_by_completed(self):
        """Once a session is FAILED, a conflicting COMPLETED webhook is strictly ignored."""
        session = CallSession.objects.using('tenant_test').create(
            organization=self.org_a,
            lead=self.lead_a,
            provider='SARVAM',
            status='FAILED',
            provider_attempt_id='attempt_term_002',
            direction='OUTBOUND',
            user_phone_number='+919876543210',
        )

        completed_payload = {
            'attempt_id': 'attempt_term_002',
            'status': 'connected',
            'webhook_config': {
                'url': 'https://test.example.com/webhook',
                'metadata': {'call_session_id': str(session.id)},
            },
        }

        result = VoiceCallingService.record_call_webhook(self.org_a, completed_payload, db_alias='tenant_test')
        self.assertEqual(result['status'], 'ignored_terminal_conflict')

        session.refresh_from_db()
        self.assertEqual(session.status, 'FAILED')

    def test_duplicate_terminal_webhook_preserves_transcript_and_duration(self):
        """Duplicate identical terminal callbacks do not erase populated transcripts or durations."""
        session = CallSession.objects.using('tenant_test').create(
            organization=self.org_a,
            lead=self.lead_a,
            provider='SARVAM',
            status='COMPLETED',
            provider_attempt_id='attempt_term_003',
            direction='OUTBOUND',
            duration_seconds=60.0,
            transcript=[{'role': 'agent', 'text': 'Original dialogue'}],
            user_phone_number='+919876543210',
        )

        # Duplicate delivery with empty transcript and zero duration
        duplicate_payload = {
            'attempt_id': 'attempt_term_003',
            'status': 'connected',
            'duration': 0.0,
            'interaction_transcript': [],
            'webhook_config': {
                'url': 'https://test.example.com/webhook',
                'metadata': {'call_session_id': str(session.id)},
            },
        }

        result = VoiceCallingService.record_call_webhook(self.org_a, duplicate_payload, db_alias='tenant_test')
        self.assertEqual(result['status'], 'duplicate_terminal')

        session.refresh_from_db()
        self.assertEqual(session.duration_seconds, 60.0)
        self.assertEqual(session.transcript, [{'role': 'agent', 'text': 'Original dialogue'}])

    def test_late_non_terminal_callback_ignored_for_terminal_session(self):
        """A late ringing or in-progress status arriving for a completed session is ignored as out of order."""
        session = CallSession.objects.using('tenant_test').create(
            organization=self.org_a,
            lead=self.lead_a,
            provider='SARVAM',
            status='COMPLETED',
            provider_attempt_id='attempt_term_004',
            direction='OUTBOUND',
            duration_seconds=50.0,
            user_phone_number='+919876543210',
        )

        late_payload = {
            'attempt_id': 'attempt_term_004',
            'status': 'ringing',
            'webhook_config': {
                'url': 'https://test.example.com/webhook',
                'metadata': {'call_session_id': str(session.id)},
            },
        }

        result = VoiceCallingService.record_call_webhook(self.org_a, late_payload, db_alias='tenant_test')
        self.assertEqual(result['status'], 'ignored_out_of_order')

        reloaded = CallSession.objects.using('tenant_test').get(id=session.id)
        self.assertEqual(reloaded.status, 'COMPLETED')

    def test_webhook_with_malformed_session_id_falls_back_to_attempt_id(self):
        """Malformed or non-UUID call_session_id in metadata does not crash and safely falls back to attempt_id."""
        session = CallSession.objects.using('tenant_test').create(
            organization=self.org_a,
            lead=self.lead_a,
            provider='SARVAM',
            status='INITIATED',
            provider_attempt_id='attempt_malformed_test_123',
            direction='OUTBOUND',
            user_phone_number='+919876543210',
        )

        malformed_payload = {
            'attempt_id': 'attempt_malformed_test_123',
            'status': 'connected',
            'duration': 25.0,
            # Bad UUID in correct Sarvam location; must fall back to attempt_id matching
            'webhook_config': {
                'url': 'https://test.example.com/webhook',
                'metadata': {'call_session_id': 'not-a-valid-uuid-string-12345'},
            },
        }

        result = VoiceCallingService.record_call_webhook(self.org_a, malformed_payload, db_alias='tenant_test')
        self.assertEqual(result['status'], 'recorded')
        self.assertEqual(result['call_status'], 'COMPLETED')

        session.refresh_from_db()
        self.assertEqual(session.status, 'COMPLETED')
        self.assertEqual(session.duration_seconds, 25.0)

    # -------------------------------------------------------------------------
    # Regression Tests: Official Sarvam webhook_config.metadata Schema
    # Ref: https://docs.sarvam.ai/voice-agents/api/instant-outbound/webhook-payload.md
    # -------------------------------------------------------------------------

    def test_sarvam_official_schema_webhook_config_metadata_matches_session_by_id(self):
        """
        REGRESSION: Official Sarvam payload places call_session_id in webhook_config.metadata.
        The service must extract it from there and match the CallSession by primary key.
        This is the critical path that was broken (payload['metadata'] was read instead of
        payload['webhook_config']['metadata']).
        """
        session = CallSession.objects.using('tenant_test').create(
            organization=self.org_a,
            lead=self.lead_a,
            provider='SARVAM',
            status='INITIATED',
            direction='OUTBOUND',
            user_phone_number='+919876543210',
            # Deliberately no provider_attempt_id so ONLY session-ID match can succeed
        )

        # Exact shape Sarvam sends per official documentation
        official_sarvam_payload = {
            'attempt_id': 'attempt_official_schema_test_001',
            'status': 'connected',
            'channel_info': {
                'channel_type': 'v2v',
                'channel_provider': 'exotel',
                'agent_phone_number': '+918040000000',
            },
            'duration': 33.5,
            'interaction_id': '20260101/abc123def456',
            'failure_reason': None,
            'final_agent_variables': {'intent': 'TRIAL_BOOKED'},
            'interaction_transcript': [
                {'role': 'agent', 'en_text': 'Hi, calling from SWEAT!'},
                {'role': 'user', 'en_text': 'Yes, I want to join.'},
            ],
            # metadata lives here per official spec, NOT at top level
            'webhook_config': {
                'url': 'https://api.fitness.vibecopilot.ai/api/v1/webhooks/communications/sarvam/sweat/',
                'metadata': {
                    'call_session_id': str(session.id),
                    'lead_id': str(self.lead_a.id),
                },
            },
        }

        result = VoiceCallingService.record_call_webhook(
            self.org_a, official_sarvam_payload, db_alias='tenant_test'
        )

        self.assertEqual(result['status'], 'recorded', f"Expected 'recorded', got: {result}")
        self.assertEqual(result['call_session_id'], str(session.id))
        self.assertEqual(result['call_status'], 'COMPLETED')
        self.assertEqual(result['duration'], 33.5)

        session.refresh_from_db()
        self.assertEqual(session.status, 'COMPLETED')
        self.assertEqual(session.duration_seconds, 33.5)
        self.assertEqual(session.provider_attempt_id, 'attempt_official_schema_test_001')
        self.assertEqual(len(session.transcript), 2)
        self.assertEqual(session.final_agent_variables.get('intent'), 'TRIAL_BOOKED')

    def test_sarvam_schema_null_webhook_config_metadata_falls_back_to_attempt_id(self):
        """
        REGRESSION: When webhook_config.metadata is null (as Sarvam sends for no-answer/busy),
        the service must NOT crash and must fall back to matching by provider_attempt_id.
        """
        session = CallSession.objects.using('tenant_test').create(
            organization=self.org_a,
            lead=self.lead_a,
            provider='SARVAM',
            status='INITIATED',
            direction='OUTBOUND',
            provider_attempt_id='attempt_null_meta_001',
            user_phone_number='+919876543210',
        )

        # Sarvam sends metadata: null for unanswered calls
        no_answer_payload = {
            'attempt_id': 'attempt_null_meta_001',
            'status': 'no_answer',
            'channel_info': {
                'channel_type': 'v2v',
                'channel_provider': 'exotel',
                'agent_phone_number': '+918040000000',
            },
            'duration': None,
            'interaction_id': None,
            'failure_reason': None,
            'final_agent_variables': None,
            'interaction_transcript': None,
            'webhook_config': {
                'url': 'https://api.fitness.vibecopilot.ai/api/v1/webhooks/communications/sarvam/sweat/',
                'metadata': None,  # Sarvam sends null when no custom metadata was provided
            },
        }

        result = VoiceCallingService.record_call_webhook(
            self.org_a, no_answer_payload, db_alias='tenant_test'
        )

        self.assertEqual(result['status'], 'recorded')
        self.assertEqual(result['call_status'], 'NO_ANSWER')

        session.refresh_from_db()
        self.assertEqual(session.status, 'NO_ANSWER')

    def test_sarvam_schema_missing_webhook_config_falls_back_to_attempt_id(self):
        """
        REGRESSION: If webhook_config is entirely absent (e.g. Sarvam schema change or
        stripped by a proxy), the service must not crash and must fall back to attempt_id.
        """
        session = CallSession.objects.using('tenant_test').create(
            organization=self.org_a,
            lead=self.lead_a,
            provider='SARVAM',
            status='INITIATED',
            direction='OUTBOUND',
            provider_attempt_id='attempt_no_wc_001',
            user_phone_number='+919876543210',
        )

        payload_no_wc = {
            'attempt_id': 'attempt_no_wc_001',
            'status': 'busy',
            # No webhook_config key at all
        }

        result = VoiceCallingService.record_call_webhook(
            self.org_a, payload_no_wc, db_alias='tenant_test'
        )

        self.assertEqual(result['status'], 'recorded')
        self.assertEqual(result['call_status'], 'BUSY')

        session.refresh_from_db()
        self.assertEqual(session.status, 'BUSY')

    def test_parse_status_webhook_extracts_metadata_from_webhook_config(self):
        """
        REGRESSION: parse_status_webhook must extract echoed metadata from
        payload['webhook_config']['metadata'], not payload['metadata'].
        """
        from apps.tenant_core.communication.adapters.voice_sarvam import SarvamVoiceAdapter

        adapter = SarvamVoiceAdapter(
            configuration={'webhook_token': 'test_token'},
            secret_credentials={},
        )

        official_payload = {
            'attempt_id': 'attempt_parse_test_001',
            'status': 'connected',
            'duration': 20.0,
            'interaction_id': '20260101/xyz',
            'final_agent_variables': {'key': 'value'},
            'interaction_transcript': [{'role': 'agent', 'en_text': 'Hello'}],
            'webhook_config': {
                'url': 'https://example.com/webhook',
                'metadata': {
                    'call_session_id': 'some-uuid-123',
                    'lead_id': 'lead-456',
                },
            },
        }

        event = adapter.parse_status_webhook(official_payload)
        self.assertIsNotNone(event)
        self.assertEqual(event.provider_message_id, 'attempt_parse_test_001')
        self.assertEqual(event.status, 'COMPLETED')

        # The critical assertion: metadata must come from webhook_config.metadata
        self.assertEqual(
            event.raw_metadata['metadata']['call_session_id'],
            'some-uuid-123',
            "parse_status_webhook must extract metadata from webhook_config.metadata, not payload.metadata"
        )
        self.assertEqual(event.raw_metadata['metadata']['lead_id'], 'lead-456')
        self.assertEqual(event.raw_metadata['duration'], 20.0)

        # Also verify that a payload with null webhook_config does not crash
        null_wc_payload = {
            'attempt_id': 'attempt_parse_null_wc',
            'status': 'failed',
            'webhook_config': None,
        }
        event2 = adapter.parse_status_webhook(null_wc_payload)
        self.assertIsNotNone(event2)
        self.assertEqual(event2.raw_metadata['metadata'], {})

    def test_top_level_metadata_no_longer_used_for_session_matching(self):
        """
        REGRESSION GUARD: A payload with call_session_id at the OLD top-level 'metadata'
        key (not in webhook_config.metadata) must NOT match by session ID — it must fall
        back to attempt_id or return 'unmatched'. This confirms the fix is in effect.
        """
        session = CallSession.objects.using('tenant_test').create(
            organization=self.org_a,
            lead=self.lead_a,
            provider='SARVAM',
            status='INITIATED',
            direction='OUTBOUND',
            # No provider_attempt_id, so ONLY session-ID lookup can find it
        )

        old_style_payload = {
            'attempt_id': 'attempt_old_style_meta_999',
            'status': 'connected',
            'duration': 10.0,
            # Old (wrong) location — top-level metadata. This must NOT match the session.
            'metadata': {'call_session_id': str(session.id)},
        }

        result = VoiceCallingService.record_call_webhook(
            self.org_a, old_style_payload, db_alias='tenant_test'
        )

        # Must be 'unmatched': session ID not found (metadata ignored), no attempt_id match
        self.assertEqual(
            result['status'], 'unmatched',
            "Top-level payload.metadata must not be used for session ID matching after the fix."
        )

        # Original session must remain INITIATED — untouched
        session.refresh_from_db()
        self.assertEqual(session.status, 'INITIATED')

    # -------------------------------------------------------------------------
    # Comprehensive Voice Consent & Suppression Enforcement Tests
    # -------------------------------------------------------------------------
    def test_explicit_grant_via_consent_record_allows_call(self):
        """Lead with an active GRANTED ConsentRecord allows call dispatch and populates provenance."""
        with patch.dict('os.environ', {'SARVAM_API_KEY': 'test_sarvam_key_secret'}):
            provenance = VoiceCallingService.verify_lead_calling_eligibility(
                lead=self.lead_a,
                db_alias='tenant_test',
            )
        self.assertTrue(provenance['voice_consent_verified'])
        self.assertEqual(provenance['voice_consent_method'], 'DATABASE_CONSENT_RECORD')
        self.assertEqual(provenance['voice_consent_source'], 'WEB_FORM')
        self.assertIsNotNone(provenance['voice_consent_recorded_at'])

    def test_caller_supplied_metadata_cannot_authorize_call(self):
        """
        Caller-supplied custom_metadata (e.g. has_voice_consent=True) or request payload flags
        MUST NEVER authorize a call when trusted DB consent is absent.
        Fails closed with ValueError and creates no provider dispatch.
        """
        from apps.tenant_core.communication.adapters.voice_sarvam import SarvamVoiceAdapter
        lead_no_cr = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            first_name='Karan',
            last_name='Mehta',
            phone_normalized='+919876543222',
            current_status='NEW_LEAD',
            do_not_contact=False,
        )
        custom_metadata = {
            'has_voice_consent': True,
            'voice_consent_source': 'VERBAL_PHONE_INTAKE',
            'voice_consent_recorded_at': '2026-10-12T10:00:00Z',
        }
        with self.assertRaises(ValueError) as ctx:
            VoiceCallingService.verify_lead_calling_eligibility(
                lead=lead_no_cr,
                db_alias='tenant_test',
                custom_metadata=custom_metadata,
            )
        self.assertIn('does not have an explicit, verified voice-calling consent grant', str(ctx.exception))

        # Also verify initiate_outbound_call fails closed with zero provider dispatch
        with patch.object(SarvamVoiceAdapter, 'create_instant_outbound_call') as mock_provider:
            with patch.dict('os.environ', {'SARVAM_API_KEY': 'test_sarvam_key_secret'}):
                with self.assertRaises(ValueError):
                    VoiceCallingService.initiate_outbound_call(
                        lead=lead_no_cr,
                        idempotency_key='test-spoof-attempt',
                        custom_metadata=custom_metadata,
                        db_alias='tenant_test',
                    )
            mock_provider.assert_not_called()

    def test_caller_supplied_provenance_spoofing_is_stripped_and_overridden(self):
        """
        When a lead has a valid DB ConsentRecord, any caller-supplied spoofed consent keys
        (e.g. voice_consent_source='SPOOFED', consent_voice=True) are stripped and replaced
        with authoritative database provenance.
        """
        from apps.tenant_core.communication.adapters.voice_sarvam import SarvamVoiceAdapter, ProviderSendResult
        spoofed_metadata = {
            'voice_consent_source': 'MALICIOUS_SPOOFED_SOURCE',
            'voice_consent_method': 'MANUFACTURED_METHOD',
            'consent_voice': True,
            'has_voice_consent': True,
            'legitimate_campaign_id': 'promo_2026',
        }
        with patch.object(
            SarvamVoiceAdapter,
            'create_instant_outbound_call',
            return_value=ProviderSendResult(
                success=True,
                status='INITIATED',
                provider_message_id='attempt_anti_spoof_1',
            ),
        ) as mock_provider:
            with patch.dict('os.environ', {'SARVAM_API_KEY': 'test_sarvam_key_secret'}):
                session = VoiceCallingService.initiate_outbound_call(
                    lead=self.lead_a,
                    idempotency_key='test-key-anti-spoof-1',
                    custom_metadata=spoofed_metadata,
                    db_alias='tenant_test',
                )

        mock_provider.assert_called_once()
        self.assertEqual(session.metadata['voice_consent_source'], 'WEB_FORM')
        self.assertEqual(session.metadata['voice_consent_method'], 'DATABASE_CONSENT_RECORD')
        self.assertEqual(session.metadata['legitimate_campaign_id'], 'promo_2026')
        self.assertNotIn('MALICIOUS_SPOOFED_SOURCE', str(session.metadata))
        self.assertNotIn('MANUFACTURED_METHOD', str(session.metadata))

    def test_api_perimeter_rejects_unconsented_lead_even_with_metadata_consent(self):
        """
        POST /api/v1/tenant/leads/{id}/call/ must return 400 when caller passes has_voice_consent=True
        for an unconsented lead. No CallSession is created, no provider dispatch.
        """
        from apps.tenant_core.communication.adapters.voice_sarvam import SarvamVoiceAdapter
        lead_no_consent = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            first_name='Unconsented',
            last_name='Lead',
            phone_normalized='+919876543299',
            current_status='NEW_LEAD',
            do_not_contact=False,
        )
        url = f"/api/v1/tenant/leads/{lead_no_consent.id}/call/"
        with patch.object(SarvamVoiceAdapter, 'create_instant_outbound_call') as mock_provider:
            response = self.client.post(
                url,
                data=json.dumps({
                    'custom_metadata': {
                        'has_voice_consent': True,
                        'voice_consent_source': 'MANUFACTURED',
                    }
                }),
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {self.token_editor}',
                HTTP_X_TENANT_ID=str(self.tenant_a.id),
                HTTP_X_IDEMPOTENCY_KEY=str(uuid.uuid4()),
            )
            mock_provider.assert_not_called()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json().get('code'), 'INVALID_RECIPIENT_OR_CONSENT')
        self.assertIn('explicit, verified voice-calling consent grant', response.json().get('error', ''))

    def test_explicit_grant_via_model_attribute_allows_call(self):
        """Lead model instance with consent_voice=True is recognized as an explicit grant."""
        lead_model_consent = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            first_name='Ananya',
            last_name='Deshmukh',
            phone_normalized='+919876543233',
            current_status='NEW_LEAD',
            do_not_contact=False,
        )
        lead_model_consent.consent_voice = True
        lead_model_consent.consent_voice_source = 'IN_PERSON_KIOSK'
        lead_model_consent.consent_voice_recorded_at = timezone.now()

        provenance = VoiceCallingService.verify_lead_calling_eligibility(
            lead=lead_model_consent,
            db_alias='tenant_test',
        )
        self.assertTrue(provenance['voice_consent_verified'])
        self.assertEqual(provenance['voice_consent_method'], 'LEAD_MODEL_FIELD')
        self.assertEqual(provenance['voice_consent_source'], 'IN_PERSON_KIOSK')

    def test_missing_consent_fails_closed(self):
        """A lead with no ConsentRecord, no model flag, and no metadata MUST fail closed."""
        lead_unconsented = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            first_name='Pooja',
            last_name='Verma',
            phone_normalized='+919876543244',
            current_status='NEW_LEAD',
            do_not_contact=False,
        )
        with self.assertRaises(ValueError) as ctx:
            VoiceCallingService.verify_lead_calling_eligibility(
                lead=lead_unconsented,
                db_alias='tenant_test',
            )
        self.assertIn('does not have an explicit, verified voice-calling consent grant', str(ctx.exception))

    def test_null_consent_fails_closed(self):
        """Lead with null/None consent attributes and null metadata fails closed."""
        lead_null = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            first_name='Naveen',
            last_name='Reddy',
            phone_normalized='+919876543255',
            current_status='NEW_LEAD',
            do_not_contact=False,
        )
        lead_null.consent_voice = None
        with self.assertRaises(ValueError) as ctx:
            VoiceCallingService.verify_lead_calling_eligibility(
                lead=lead_null,
                db_alias='tenant_test',
                custom_metadata={'has_voice_consent': None},
            )
        self.assertIn('does not have an explicit, verified voice-calling consent grant', str(ctx.exception))

    def test_explicit_denial_fails_closed(self):
        """Lead with consent_voice=False or invocation metadata has_voice_consent=False is rejected."""
        # 1. Via metadata
        with self.assertRaises(ValueError) as ctx:
            VoiceCallingService.verify_lead_calling_eligibility(
                lead=self.lead_a,
                db_alias='tenant_test',
                custom_metadata={'has_voice_consent': False},
            )
        self.assertIn('Explicit voice calling consent was denied', str(ctx.exception))

        # 2. Via model attribute
        lead_denied = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            first_name='Suresh',
            last_name='Raina',
            phone_normalized='+919876543266',
            current_status='NEW_LEAD',
            do_not_contact=False,
        )
        lead_denied.consent_voice = False
        with self.assertRaises(ValueError) as ctx2:
            VoiceCallingService.verify_lead_calling_eligibility(
                lead=lead_denied,
                db_alias='tenant_test',
            )
        self.assertIn('opted out of voice communications', str(ctx2.exception))

    def test_withdrawal_fails_closed(self):
        """Lead with an explicit WITHDRAWN ConsentRecord for voice calling is rejected."""
        from apps.tenant_core.models_privacy import ProcessingPurpose, ConsentRecord
        purpose, _ = ProcessingPurpose.objects.using('tenant_test').get_or_create(
            code='VOICE_CALL',
            defaults={
                'name': 'Voice Calling Purpose',
                'description': 'Automated outbound calling',
            },
        )
        lead_withdrawn = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            first_name='Vikas',
            last_name='Kohli',
            phone_normalized='+919876543277',
            current_status='NEW_LEAD',
            do_not_contact=False,
        )
        ConsentRecord.objects.using('tenant_test').create(
            user=self.user_editor,
            purpose=purpose,
            subject_type='lead',
            subject_id=lead_withdrawn.id,
            status='WITHDRAWN',
        )

        with patch.dict('os.environ', {'SARVAM_API_KEY': 'test_sarvam_key_secret'}):
            with self.assertRaises(ValueError) as ctx:
                VoiceCallingService.initiate_outbound_call(
                    lead=lead_withdrawn,
                    idempotency_key='test-key-withdrawn-consent',
                    db_alias='tenant_test',
                )
            self.assertIn('withdrawn voice calling consent', str(ctx.exception))

    def test_dnc_blocks_dispatch_even_with_explicit_grant(self):
        """Do Not Contact flag strictly blocks dispatch even if lead has active voice consent."""
        self.lead_a.do_not_contact = True
        with self.assertRaises(ValueError) as ctx:
            VoiceCallingService.verify_lead_calling_eligibility(
                lead=self.lead_a,
                db_alias='tenant_test',
            )
        self.assertIn('marked Do Not Contact', str(ctx.exception))
        self.lead_a.do_not_contact = False

    def test_unrelated_channel_consent_sms_whatsapp_fails_closed(self):
        """Consent for SMS or WhatsApp is NEVER treated as consent for automated AI voice calls."""
        lead_sms_wa = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            first_name='Amit',
            last_name='Bansal',
            phone_normalized='+919876543288',
            current_status='NEW_LEAD',
            do_not_contact=False,
            consent_sms=True,
            consent_whatsapp=True,
            consent_email=True,
        )
        with self.assertRaises(ValueError) as ctx:
            VoiceCallingService.verify_lead_calling_eligibility(
                lead=lead_sms_wa,
                db_alias='tenant_test',
            )
        self.assertIn('does not have an explicit, verified voice-calling consent grant', str(ctx.exception))

    def test_unauthenticated_webhook_cannot_mutate_call_session(self):
        """An unauthenticated webhook request (bad token) is rejected 401 and does not alter the call session."""
        session = CallSession.objects.using('tenant_test').create(
            organization=self.org_a,
            lead=self.lead_a,
            provider='SARVAM',
            status='INITIATED',
            provider_attempt_id='attempt_auth_check_1',
            direction='OUTBOUND',
            user_phone_number='+919876543210',
        )

        response = self.client.post(
            f'/api/v1/webhooks/communications/sarvam/{self.tenant_a.id}/?token=wrong_secret_token',
            data={'attempt_id': 'attempt_auth_check_1', 'status': 'connected'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        reloaded = CallSession.objects.using('tenant_test').get(id=session.id)
        self.assertEqual(reloaded.status, 'INITIATED')

    @patch('urllib.request.urlopen')
    def test_concurrency_serialization_blocks_duplicate_dispatch(self, mock_urlopen):
        """Simultaneous outbound call requests for the same lead cannot place duplicate calls to provider."""
        mock_response = MagicMock()
        mock_response.getcode.return_value = 200
        mock_response.read.return_value = json.dumps({'attempt_id': 'attempt_concurrent_001'}).encode('utf-8')
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_editor}')
        with patch.dict('os.environ', {'SARVAM_API_KEY': 'test_sarvam_key_secret'}):
            # First request dispatches successfully
            res1 = self.client.post(
                f'/api/v1/tenant/leads/{self.lead_a.id}/call/',
                data={},
                format='json',
                HTTP_X_IDEMPOTENCY_KEY='key-concurrent-staff1',
            )
            self.assertEqual(res1.status_code, status.HTTP_201_CREATED)
            self.assertEqual(mock_urlopen.call_count, 1)

            # Concurrent request from Staff 2 for same lead with different key
            res2 = self.client.post(
                f'/api/v1/tenant/leads/{self.lead_a.id}/call/',
                data={},
                format='json',
                HTTP_X_IDEMPOTENCY_KEY='key-concurrent-staff2',
            )
            self.assertEqual(res2.status_code, status.HTTP_409_CONFLICT)
            self.assertEqual(res2.data.get('code'), 'CALL_ALREADY_IN_PROGRESS')
            # Critical: Sarvam HTTP client MUST NOT be called a second time
            self.assertEqual(mock_urlopen.call_count, 1)

    # -------------------------------------------------------------------------
    # 13. Voice Consent Status and Trust Boundary Regression Tests
    # -------------------------------------------------------------------------
    def test_voice_consent_status_missing_when_unspecified(self):
        """Lead with no voice consent record must return status='MISSING' and is_eligible=False."""
        lead = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            first_name='NoConsent',
            last_name='Lead',
            phone_normalized='+919876543210',
        )
        status_info = VoiceCallingService.get_lead_voice_consent_status(lead, db_alias='tenant_test')
        self.assertEqual(status_info['status'], 'MISSING')
        self.assertFalse(status_info['is_eligible'])
        self.assertIn('explicit, verified voice-calling consent grant', status_info['reason'])

    def test_voice_consent_status_granted_on_lead_model(self):
        """Lead with consent_voice=True attribute must return status='GRANTED' and is_eligible=True."""
        lead = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            first_name='Granted',
            last_name='Lead',
            phone_normalized='+919876543211',
        )
        lead.consent_voice = True
        lead.consent_voice_source = 'LANDING_PAGE'
        lead.consent_voice_recorded_at = timezone.now()

        status_info = VoiceCallingService.get_lead_voice_consent_status(lead, db_alias='tenant_test')
        self.assertEqual(status_info['status'], 'GRANTED')
        self.assertTrue(status_info['is_eligible'])
        self.assertEqual(status_info['source'], 'LANDING_PAGE')
        self.assertEqual(status_info['method'], 'LEAD_MODEL_FIELD')

    def test_voice_consent_status_withdrawn_on_lead_model(self):
        """Lead with consent_voice=False attribute must return status='WITHDRAWN' and is_eligible=False."""
        lead = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            first_name='Withdrawn',
            last_name='Lead',
            phone_normalized='+919876543212',
        )
        lead.consent_voice = False
        lead.consent_voice_source = 'SMS_OPT_OUT'
        lead.consent_voice_recorded_at = timezone.now()

        status_info = VoiceCallingService.get_lead_voice_consent_status(lead, db_alias='tenant_test')
        self.assertEqual(status_info['status'], 'WITHDRAWN')
        self.assertFalse(status_info['is_eligible'])
        self.assertIn('opted out of voice communications', status_info['reason'])

    def test_voice_consent_never_inferred_from_sms_or_whatsapp(self):
        """SMS, WhatsApp, and Email consent must NEVER authorize voice calls."""
        lead = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            first_name='MultiChannel',
            last_name='Lead',
            phone_normalized='+919876543213',
            consent_sms=True,
            consent_whatsapp=True,
            consent_email=True,
        )
        status_info = VoiceCallingService.get_lead_voice_consent_status(lead, db_alias='tenant_test')
        self.assertEqual(status_info['status'], 'MISSING')
        self.assertFalse(status_info['is_eligible'])

    def test_dnc_suppresses_granted_voice_consent(self):
        """Do Not Contact flag must take precedence over active voice consent."""
        lead = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            first_name='Dnc',
            last_name='Lead',
            phone_normalized='+919876543214',
            do_not_contact=True,
        )
        lead.consent_voice = True

        status_info = VoiceCallingService.get_lead_voice_consent_status(lead, db_alias='tenant_test')
        self.assertFalse(status_info['is_eligible'])
        self.assertIn('Do Not Contact', status_info['reason'])

        with self.assertRaises(ValueError) as ctx:
            VoiceCallingService.verify_lead_calling_eligibility(lead, db_alias='tenant_test')
        self.assertIn('Do Not Contact', str(ctx.exception))

    def test_consent_record_model_withdrawal_takes_precedence(self):
        """ConsentRecord table entry with status='WITHDRAWN' takes precedence."""
        from apps.tenant_core.models_privacy import ConsentRecord
        lead = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            first_name='TableWithdrawn',
            last_name='Lead',
            phone_normalized='+919876543215',
        )
        lead.consent_voice = True
        ConsentRecord.objects.using('tenant_test').create(
            user=self.user_editor,
            purpose=self.voice_purpose,
            subject_type='lead',
            subject_id=lead.id,
            status='WITHDRAWN',
            capture_source='PHONE_VERBAL',
            recorded_at=timezone.now(),
        )
        status_info = VoiceCallingService.get_lead_voice_consent_status(lead, db_alias='tenant_test')
        self.assertEqual(status_info['status'], 'WITHDRAWN')
        self.assertFalse(status_info['is_eligible'])

    # -------------------------------------------------------------------------
    # 14. Dynamic CRM Communication Channels Endpoint Tests
    # -------------------------------------------------------------------------
    def test_channels_endpoint_requires_permission(self):
        """GET /api/v1/tenant/crm/channels/ without crm.communications.view must return 403."""
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_readonly}')
        res = self.client.get('/api/v1/tenant/crm/channels/')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_channels_endpoint_not_configured_when_no_integration(self):
        """When no Sarvam integration exists, voice channel status must be NOT_CONFIGURED."""
        self.integration_a.status = 'INACTIVE'
        self.integration_a.save(using='tenant_test')

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_editor}')
        res = self.client.get('/api/v1/tenant/crm/channels/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        channels = {c['id']: c for c in res.data}
        self.assertIn('ai_calling', channels)
        voice = channels['ai_calling']
        self.assertEqual(voice['status'], 'NOT_CONFIGURED')
        self.assertFalse(voice['is_configured'])

    def test_channels_endpoint_configured_when_partial_integration(self):
        """When active Sarvam integration exists but missing credentials, status is CONFIGURED."""
        self.integration_a.status = 'ACTIVE'
        self.integration_a.save(using='tenant_test')

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_editor}')
        with patch.dict('os.environ', {}, clear=True):
            res = self.client.get('/api/v1/tenant/crm/channels/')
            self.assertEqual(res.status_code, status.HTTP_200_OK)
            channels = {c['id']: c for c in res.data}
            self.assertIn('ai_calling', channels)
            voice = channels['ai_calling']
            self.assertEqual(voice['status'], 'CONFIGURED')
            self.assertFalse(voice['is_configured'])

    def test_channels_endpoint_connected_and_masks_phone(self):
        """When integration is fully configured with credentials, status is CONNECTED and sender phone is masked."""
        self.integration_a.status = 'ACTIVE'
        self.integration_a.save(using='tenant_test')

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_editor}')
        with patch.dict('os.environ', {'SARVAM_API_KEY': 'sk_test_api_key_123'}):
            res = self.client.get('/api/v1/tenant/crm/channels/')
            self.assertEqual(res.status_code, status.HTTP_200_OK)
            channels = {c['id']: c for c in res.data}
            self.assertIn('ai_calling', channels)
            voice = channels['ai_calling']
            self.assertEqual(voice['status'], 'CONNECTED')
            self.assertTrue(voice['is_configured'])
            self.assertEqual(voice['sender_identity'], '+91******0704')

    def test_channels_endpoint_zero_secret_leakage(self):
        """Ensure response never contains api_key, tokens, or secret references."""
        self.integration_a.status = 'ACTIVE'
        self.integration_a.configuration['webhook_token'] = 'webhook_secret_token'
        self.integration_a.save(using='tenant_test')

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_editor}')
        with patch.dict('os.environ', {'SARVAM_API_KEY': 'sk_live_secret_leak_check'}):
            res = self.client.get('/api/v1/tenant/crm/channels/')
            self.assertEqual(res.status_code, status.HTTP_200_OK)
            raw_json = json.dumps(res.data)
            self.assertNotIn('sk_live_secret_leak_check', raw_json)
            self.assertNotIn('webhook_secret_token', raw_json)
            self.assertNotIn('env://SARVAM_API_KEY', raw_json)

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_editor}')
        with patch.dict('os.environ', {'SARVAM_API_KEY': 'sk_live_secret_leak_check'}):
            res = self.client.get('/api/v1/tenant/crm/channels/')
            self.assertEqual(res.status_code, status.HTTP_200_OK)
            raw_json = json.dumps(res.data)
            self.assertNotIn('sk_live_secret_leak_check', raw_json)
            self.assertNotIn('telephony_super_secret_token', raw_json)
            self.assertNotIn('webhook_secret_token', raw_json)
            self.assertNotIn('env://SARVAM_API_KEY', raw_json)

    # =========================================================================
    # Task 3 — Global Outbound Voice Calling Safety Gate Verification
    # =========================================================================

    @patch('urllib.request.urlopen')
    def test_safety_gate_both_flags_enabled_allows_dispatch(self, mock_urlopen):
        """
        When both COMMUNICATIONS_OUTBOUND_ENABLED and VOICE_CALLING_ENABLED are True,
        execution proceeds to provider-mocking logic and succeeds (HTTP 201).
        """
        mock_response = MagicMock()
        mock_response.getcode.return_value = 200
        mock_response.read.return_value = json.dumps({'attempt_id': 'att_gate_allowed_1'}).encode('utf-8')
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        with override_settings(COMMUNICATIONS_OUTBOUND_ENABLED=True, VOICE_CALLING_ENABLED=True):
            with patch.dict('os.environ', {'SARVAM_API_KEY': 'mock_secret'}):
                self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_editor}')
                response = self.client.post(
                    f"/api/v1/tenant/leads/{self.lead_a.id}/call/",
                    data={},
                    format='json',
                    HTTP_X_IDEMPOTENCY_KEY='gate-allowed-key-1',
                )

                self.assertEqual(response.status_code, status.HTTP_201_CREATED)
                self.assertEqual(response.data['status'], 'INITIATED')
                self.assertEqual(mock_urlopen.call_count, 1)

    @patch('urllib.request.urlopen')
    def test_safety_gate_communications_outbound_disabled_blocks_dispatch(self, mock_urlopen):
        """
        When COMMUNICATIONS_OUTBOUND_ENABLED=False (even if VOICE_CALLING_ENABLED=True),
        provider dispatch is blocked at both service and view level.
        """
        with override_settings(COMMUNICATIONS_OUTBOUND_ENABLED=False, VOICE_CALLING_ENABLED=True):
            # 1. Service direct call fails closed with ValueError
            with self.assertRaises(ValueError) as ctx:
                VoiceCallingService.initiate_outbound_call(
                    lead=self.lead_a,
                    idempotency_key='gate-comm-disabled-svc',
                    db_alias='tenant_test',
                )
            self.assertIn('Outbound communications are disabled in this deployment.', str(ctx.exception))
            mock_urlopen.assert_not_called()

            # 2. View returns HTTP 403 Forbidden with VOICE_CALLING_DISABLED
            self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_editor}')
            response = self.client.post(
                f"/api/v1/tenant/leads/{self.lead_a.id}/call/",
                data={},
                format='json',
                HTTP_X_IDEMPOTENCY_KEY='gate-comm-disabled-view',
            )

            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
            self.assertEqual(response.data.get('code'), 'VOICE_CALLING_DISABLED')
            mock_urlopen.assert_not_called()

    @patch('urllib.request.urlopen')
    def test_safety_gate_voice_calling_disabled_blocks_dispatch(self, mock_urlopen):
        """
        When VOICE_CALLING_ENABLED=False (even if COMMUNICATIONS_OUTBOUND_ENABLED=True),
        provider dispatch is blocked at both service and view level.
        """
        with override_settings(COMMUNICATIONS_OUTBOUND_ENABLED=True, VOICE_CALLING_ENABLED=False):
            # 1. Service direct call fails closed with ValueError
            with self.assertRaises(ValueError) as ctx:
                VoiceCallingService.initiate_outbound_call(
                    lead=self.lead_a,
                    idempotency_key='gate-voice-disabled-svc',
                    db_alias='tenant_test',
                )
            self.assertIn('Outbound voice calling is disabled in this deployment.', str(ctx.exception))
            mock_urlopen.assert_not_called()

            # 2. View returns HTTP 403 Forbidden with VOICE_CALLING_DISABLED
            self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_editor}')
            response = self.client.post(
                f"/api/v1/tenant/leads/{self.lead_a.id}/call/",
                data={},
                format='json',
                HTTP_X_IDEMPOTENCY_KEY='gate-voice-disabled-view',
            )

            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
            self.assertEqual(response.data.get('code'), 'VOICE_CALLING_DISABLED')
            mock_urlopen.assert_not_called()

    @patch('urllib.request.urlopen')
    def test_safety_gate_both_flags_disabled_blocks_dispatch(self, mock_urlopen):
        """
        When both COMMUNICATIONS_OUTBOUND_ENABLED=False and VOICE_CALLING_ENABLED=False,
        provider dispatch is strictly blocked.
        """
        with override_settings(COMMUNICATIONS_OUTBOUND_ENABLED=False, VOICE_CALLING_ENABLED=False):
            with self.assertRaises(ValueError):
                VoiceCallingService.initiate_outbound_call(
                    lead=self.lead_a,
                    idempotency_key='gate-both-disabled-svc',
                    db_alias='tenant_test',
                )
            mock_urlopen.assert_not_called()

            self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_editor}')
            response = self.client.post(
                f"/api/v1/tenant/leads/{self.lead_a.id}/call/",
                data={},
                format='json',
                HTTP_X_IDEMPOTENCY_KEY='gate-both-disabled-view',
            )

            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
            self.assertEqual(response.data.get('code'), 'VOICE_CALLING_DISABLED')
            mock_urlopen.assert_not_called()

    @patch('urllib.request.urlopen')
    def test_safety_gate_missing_voice_calling_enabled_fails_closed(self, mock_urlopen):
        """
        When VOICE_CALLING_ENABLED is completely absent / deleted from settings,
        the system must fail closed (treat as False) and block provider dispatch.
        """
        from django.conf import settings
        has_voice_flag = hasattr(settings, 'VOICE_CALLING_ENABLED')
        orig_val = getattr(settings, 'VOICE_CALLING_ENABLED', None)
        try:
            if has_voice_flag:
                delattr(settings._wrapped if hasattr(settings, '_wrapped') else settings, 'VOICE_CALLING_ENABLED')

            with override_settings(COMMUNICATIONS_OUTBOUND_ENABLED=True):
                with self.assertRaises(ValueError) as ctx:
                    VoiceCallingService.initiate_outbound_call(
                        lead=self.lead_a,
                        idempotency_key='gate-missing-svc',
                        db_alias='tenant_test',
                    )
                self.assertIn('Outbound voice calling is disabled in this deployment.', str(ctx.exception))
                mock_urlopen.assert_not_called()

                self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_editor}')
                response = self.client.post(
                    f"/api/v1/tenant/leads/{self.lead_a.id}/call/",
                    data={},
                    format='json',
                    HTTP_X_IDEMPOTENCY_KEY='gate-missing-view',
                )

                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
                self.assertEqual(response.data.get('code'), 'VOICE_CALLING_DISABLED')
                mock_urlopen.assert_not_called()
        finally:
            if has_voice_flag:
                setattr(settings._wrapped if hasattr(settings, '_wrapped') else settings, 'VOICE_CALLING_ENABLED', orig_val)

    @patch('urllib.request.urlopen')
    def test_safety_gate_consent_and_idempotency_preserved_when_enabled(self, mock_urlopen):
        """
        Confirms that when gates are enabled, existing consent enforcement and
        idempotency caching operate without degradation.
        """
        mock_response = MagicMock()
        mock_response.getcode.return_value = 200
        mock_response.read.return_value = json.dumps({'attempt_id': 'att_gate_preserve_1'}).encode('utf-8')
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        with override_settings(COMMUNICATIONS_OUTBOUND_ENABLED=True, VOICE_CALLING_ENABLED=True):
            with patch.dict('os.environ', {'SARVAM_API_KEY': 'mock_secret'}):
                lead_unconsented = Lead.objects.using('tenant_test').create(
                    organization=self.org_a,
                    branch=self.branch_a,
                    first_name='Unconsented',
                    last_name='Lead',
                    phone_normalized='+919876543291',
                    lead_source=self.lead_source,
                    current_status='NEW_LEAD',
                    do_not_contact=False,
                )
                self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_editor}')
                response = self.client.post(
                    f"/api/v1/tenant/leads/{lead_unconsented.id}/call/",
                    data={},
                    format='json',
                    HTTP_X_IDEMPOTENCY_KEY='gate-preserve-unconsented',
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(response.data.get('code'), 'INVALID_RECIPIENT_OR_CONSENT')
                mock_urlopen.assert_not_called()

                # Idempotency safe replay test
                response1 = self.client.post(
                    f"/api/v1/tenant/leads/{self.lead_a.id}/call/",
                    data={},
                    format='json',
                    HTTP_X_IDEMPOTENCY_KEY='gate-preserve-idem-key',
                )
                self.assertEqual(response1.status_code, status.HTTP_201_CREATED)
                self.assertEqual(mock_urlopen.call_count, 1)

                response2 = self.client.post(
                    f"/api/v1/tenant/leads/{self.lead_a.id}/call/",
                    data={},
                    format='json',
                    HTTP_X_IDEMPOTENCY_KEY='gate-preserve-idem-key',
                )
                self.assertEqual(response2.status_code, status.HTTP_200_OK)
                self.assertTrue(response2.data.get('is_duplicate'))
                self.assertEqual(mock_urlopen.call_count, 1)


# =============================================================================
# Task 2 — Safe PostgreSQL Concurrency Verification Suite
# =============================================================================
@override_settings(COMMUNICATIONS_OUTBOUND_ENABLED=True, VOICE_CALLING_ENABLED=True, VOICE_CALLING_ENFORCE_HOURS=False)
class SarvamPostgresConcurrencyTests(TransactionTestCase):
    """
    Dedicated transaction-capable concurrency test suite.
    Runs on PostgreSQL test database using real transaction boundaries and row-level locks.
    Asserts:
    1. Independent concurrent worker connections serialize via select_for_update().
    2. Concurrent dispatches with different idempotency keys cannot create duplicate active sessions.
    3. External Sarvam adapter is NEVER called more than once and is mocked safely.
    4. Database lock is released BEFORE external provider I/O.
    """
    databases = {'default', 'tenant_test'}

    def setUp(self):
        super().setUp()
        set_tenant_db_alias('tenant_test')
        from django.db import connections
        self.is_postgres = connections['tenant_test'].vendor == 'postgresql'

        # Isolated test tenant & organization
        self.tenant = Tenant.objects.using('default').create(
            code=f'CRM-PG-{uuid.uuid4().hex[:6]}',
            name='PG Test Tenant',
            slug=f'pg-test-{uuid.uuid4().hex[:6]}',
            status='ACTIVE',
        )
        self.org = Organization.objects.using('tenant_test').create(
            code=f'ORG-PG-{uuid.uuid4().hex[:6]}',
            name='PG Test Org',
            status='ACTIVE',
        )
        self.loc = Location.objects.using('tenant_test').create(
            organization=self.org,
            code=f'LOC-PG-{uuid.uuid4().hex[:6]}',
            name='PG Location',
            status='ACTIVE',
        )
        self.branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            code=f'BR-PG-{uuid.uuid4().hex[:6]}',
            name='PG Branch',
            status='ACTIVE',
        )
        self.lead = Lead.objects.using('tenant_test').create(
            organization=self.org,
            branch=self.branch,
            first_name='Aarav',
            last_name='Patel',
            phone_normalized='+919876543299',
            current_status='NEW_LEAD',
            do_not_contact=False,
        )
        # Explicit Voice Calling Consent via ConsentRecord
        from apps.tenant_core.models_privacy import ProcessingPurpose, ConsentRecord
        self.purpose, _ = ProcessingPurpose.objects.using('tenant_test').get_or_create(
            code='VOICE_CALL',
            defaults={'name': 'Voice Purpose', 'description': 'Voice calling'},
        )
        self.user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email=f'user_{uuid.uuid4().hex[:6]}@fitness.test',
            first_name='Admin',
            last_name='User',
        )
        ConsentRecord.objects.using('tenant_test').create(
            user=self.user,
            purpose=self.purpose,
            subject_type='lead',
            subject_id=self.lead.id,
            status='GRANTED',
            capture_source='WEB_FORM',
        )
        # Integration configuration
        self.integration = Integration.objects.using('tenant_test').create(
            integration_type='CALLING',
            provider='SARVAM',
            secret_reference='env://SARVAM_API_KEY',
            configuration={
                'org_id': 'org_pg_1',
                'workspace_id': 'ws_pg_1',
                'app_id': 'app_pg_1',
                'app_version': 1,
                'connection_id': 'conn_pg_1',
                'agent_phone_number': '+918044620704',
                'webhook_token': 'pg_test_token',
                'public_integration_id': str(self.tenant.id),
                'webhook_base_url': 'https://api.fitness.vibecopilot.ai',
            },
            status='ACTIVE',
        )

    def tearDown(self):
        # Safe cleanup of isolated test records
        CallSession.objects.using('tenant_test').filter(organization=self.org).delete()
        from apps.tenant_core.models_privacy import ConsentRecord
        ConsentRecord.objects.using('tenant_test').filter(subject_id=self.lead.id).delete()
        self.lead.delete(using='tenant_test')
        self.user.delete(using='tenant_test')
        self.branch.delete(using='tenant_test')
        self.loc.delete(using='tenant_test')
        self.integration.delete(using='tenant_test')
        self.org.delete(using='tenant_test')
        self.tenant.delete(using='default')
        super().tearDown()

    def test_postgres_concurrent_duplicate_dispatch_prevented(self):
        """
        Validates real PostgreSQL row locking and transaction serialization:
        1. Two concurrent workers attempt to dispatch outbound calls for the same lead with different idempotency keys.
        2. Mocks the Sarvam adapter and asserts Sarvam is NEVER called more than once.
        3. Asserts the database row lock is released BEFORE external provider I/O.
        4. Asserts exactly one active session is created in the database.
        """
        import concurrent.futures
        from django.db import connections, transaction
        from apps.tenant_core.communication.adapters.voice_sarvam import SarvamVoiceAdapter, ProviderSendResult

        if not self.is_postgres:
            self.skipTest("Skipping test: requires PostgreSQL database engine.")

        # Thread-safe tracking of lock state during external I/O
        lock_released_during_io = []

        def mock_create_call(*args, **kwargs):
            # Assert that database transaction/row lock is NOT held during provider HTTP call
            is_in_atomic = transaction.get_connection('tenant_test').in_atomic_block
            lock_released_during_io.append(not is_in_atomic)
            return ProviderSendResult(
                success=True,
                provider_message_id=f"attempt_pg_{uuid.uuid4().hex[:8]}",
                status='INITIATED',
            )

        with patch.object(SarvamVoiceAdapter, 'create_instant_outbound_call', side_effect=mock_create_call) as mock_provider:
            with patch.dict('os.environ', {'SARVAM_API_KEY': 'test_sarvam_key_secret'}):
                def worker_task(worker_id):
                    # Each thread gets its own database connection in Django and requires tenant DB routing context
                    set_tenant_db_alias('tenant_test')
                    connections['tenant_test'].close()  # Ensure clean thread connection
                    key = f"pg-key-worker-{worker_id}"
                    try:
                        session = VoiceCallingService.initiate_outbound_call(
                            lead=self.lead,
                            idempotency_key=key,
                            db_alias='tenant_test',
                        )
                        # Verify if this worker actually created the new session vs received active call collision
                        is_creator = (session.idempotency_key == key)
                        return {
                            'worker': worker_id,
                            'success': True,
                            'is_creator': is_creator,
                            'session_id': str(session.id),
                            'error': None,
                        }
                    except Exception as e:
                        return {'worker': worker_id, 'success': False, 'is_creator': False, 'session_id': None, 'error': str(e)}
                    finally:
                        connections['tenant_test'].close()

                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                    fut1 = executor.submit(worker_task, 1)
                    fut2 = executor.submit(worker_task, 2)
                    res1 = fut1.result()
                    res2 = fut2.result()

                results = [res1, res2]
                creators = [r for r in results if r['is_creator']]

                # Exactly one worker must succeed in creating the call session
                self.assertEqual(len(creators), 1, f"Expected exactly 1 creator, got {len(creators)}: {results}")

                # The Sarvam provider mock must be called EXACTLY once
                self.assertEqual(mock_provider.call_count, 1, "Sarvam provider MUST NOT be called more than once!")

                # Both workers returned the same single session ID
                self.assertEqual(res1['session_id'], res2['session_id'])

                # Confirm that the database lock was released BEFORE the external provider I/O
                self.assertTrue(len(lock_released_during_io) > 0)
                self.assertTrue(all(lock_released_during_io), "Database lock was improperly held during provider HTTP call!")

                # Confirm that only ONE CallSession was created in the database for this lead
                sessions = CallSession.objects.using('tenant_test').filter(organization=self.org, lead=self.lead)
                self.assertEqual(sessions.count(), 1)
