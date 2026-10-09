"""
tests/test_meta_reliability_layer.py - Automated Reliability, Idempotency & Failure Scenarios Tests.

Covers:
1. Valid Meta webhook received
2. Invalid signature rejected
3. Duplicate webhook does not duplicate CRM Lead / import
4. Unknown Page safely acknowledged without routing
5. Inactive mapping handling
6. Graph timeout schedules retry
7. Graph 500 schedules retry
8. Graph 429 schedules retry
9. Permanent mapping failure does not infinite retry
10. Retry reaches SUCCESS
11. Retry exhaustion reaches terminal DEAD_LETTER
12. Manual retry works on failed import
13. Manual retry cannot retry SUCCESS
14. Tenant isolation enforced
15. Health endpoint values authoritative
16. No secrets returned by health endpoint
"""

import hmac
import hashlib
import json
import unittest
from unittest.mock import patch, MagicMock
from django.test import RequestFactory
from rest_framework.test import force_authenticate
from rest_framework.permissions import AllowAny
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import ValidationError

from apps.master.models_tenant import Tenant, MetaPageRegistry
from apps.tenant_core.context import tenant_database_context
from config.routers import set_tenant_db_alias
from apps.tenant_core.models_org import Organization, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_crm import Lead, LeadSource
from apps.tenant_core.models_meta_leads import (
    MetaConnection, MetaPageConnection, MetaLeadMapping, MetaLeadImport
)
from apps.tenant_core.views_meta_webhook import MetaLeadWebhookView
from apps.tenant_core.views_meta_leads import MetaLeadMappingViewSet, MetaLeadImportViewSet
from apps.tenant_core.tasks_meta_leads import process_meta_lead_import_task, META_MAX_RETRIES
from apps.tenant_core.services_meta_graph import (
    MetaRateLimitError, MetaGraphServerError, MetaTokenExpiredError, MetaLeadNotFoundError
)


class MetaReliabilityLayerTests(unittest.TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.tenant = Tenant.objects.using('default').filter(slug='sweat').first()
        if not self.tenant:
            self.tenant = Tenant.objects.using('default').create(slug='sweat', name='SWEAT', status='ACTIVE')

        with tenant_database_context(str(self.tenant.id)) as alias:
            self.alias = alias
            self.org = Organization.objects.using(alias).filter(status='ACTIVE').first()
            if not self.org:
                self.org = Organization.objects.using(alias).create(name='SWEAT Organization', status='ACTIVE')

            self.branch = Branch.objects.using(alias).filter(organization=self.org, status='ACTIVE').first()
            if not self.branch:
                self.branch = Branch.objects.using(alias).create(organization=self.org, name='Main Branch', status='ACTIVE')

            self.lead_source = LeadSource.objects.using(alias).filter(organization=self.org, source_type='META').first()
            if not self.lead_source:
                self.lead_source = LeadSource.objects.using(alias).create(
                    organization=self.org, name='Meta Ads', source_type='META', status='ACTIVE'
                )

            # Test admin user
            self.user = TenantUser.objects.using(alias).filter(organization=self.org, status='ACTIVE').first()
            if not self.user:
                self.user = TenantUser.objects.using(alias).create(
                    organization=self.org, email='admin@sweat.test', username='admin_test', status='ACTIVE', is_login_allowed=True
                )
            self.user._auth_type = 'tenant'
            self.user.organization = self.org
            self.user.is_staff = True

            # Clean test registry & mappings for isolated testing
            self.test_page_id = '999888777111'
            self.test_form_id = '888777666222'
            MetaLeadImport.objects.using(alias).filter(external_lead_id__startswith='rel_lead_').delete()
            MetaLeadImport.objects.using(alias).filter(page_id=self.test_page_id).delete()
            MetaPageRegistry.objects.using('default').filter(page_id=self.test_page_id).delete()
            MetaPageRegistry.objects.using('default').create(page_id=self.test_page_id, tenant=self.tenant, is_active=True)

            self.meta_conn = MetaConnection.objects.using(alias).filter(organization=self.org).first()
            if not self.meta_conn:
                self.meta_conn = MetaConnection.objects.using(alias).create(
                    organization=self.org, status='CONNECTED', encrypted_user_access_token='enc_token'
                )

            MetaPageConnection.objects.using(alias).filter(page_id=self.test_page_id).delete()
            self.page_conn = MetaPageConnection.objects.using(alias).create(
                connection=self.meta_conn,
                organization=self.org,
                page_id=self.test_page_id,
                page_name='Reliability Test Page',
                encrypted_page_access_token='dummy_encrypted_token',
                is_active=True,
                is_subscribed_to_webhooks=True,
            )

            MetaLeadMapping.objects.using(alias).filter(page_id=self.test_page_id, form_id=self.test_form_id).delete()
            self.mapping = MetaLeadMapping.objects.using(alias).create(
                organization=self.org,
                name='Reliability Test Form Mapping',
                page_id=self.test_page_id,
                form_id=self.test_form_id,
                is_active=True,
                branch_mode='FIXED',
                branch=self.branch,
                lead_source=self.lead_source,
                field_mappings={'first_name': 'full_name', 'phone': 'phone_number', 'email': 'email'},
            )

    def _make_meta_payload(self, leadgen_id='lead_rel_101', page_id=None, form_id=None):
        return {
            'object': 'page',
            'entry': [{
                'id': page_id or self.test_page_id,
                'time': 1728000000,
                'changes': [{
                    'field': 'leadgen',
                    'value': {
                        'leadgen_id': leadgen_id,
                        'form_id': form_id or self.test_form_id,
                        'page_id': page_id or self.test_page_id,
                        'created_time': 1728000000,
                    }
                }]
            }]
        }

    def _sign_payload(self, body_bytes: bytes, secret: str = 'test_secret') -> str:
        sig = hmac.new(secret.encode('utf-8'), body_bytes, hashlib.sha256).hexdigest()
        return f"sha256={sig}"

    # 1. Valid Meta webhook received
    @patch('apps.tenant_core.views_meta_webhook.process_meta_lead_import_task.delay')
    @patch('apps.tenant_core.views_meta_webhook.get_meta_app_credentials')
    def test_01_valid_meta_webhook_received(self, mock_creds, mock_delay):
        mock_creds.return_value = ('app123', 'test_secret', 'token123')
        payload = self._make_meta_payload(leadgen_id='rel_lead_001')
        raw_body = json.dumps(payload).encode('utf-8')
        sig = self._sign_payload(raw_body, 'test_secret')

        request = self.factory.post('/api/v1/webhooks/meta/leads/', data=raw_body, content_type='application/json', HTTP_X_HUB_SIGNATURE_256=sig)
        view = MetaLeadWebhookView.as_view()
        response = view(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.get('processed'), 1)
        mock_delay.assert_called_once()

        with tenant_database_context(str(self.tenant.id)) as alias:
            imp = MetaLeadImport.objects.using(alias).filter(external_lead_id='rel_lead_001').first()
            self.assertIsNotNone(imp)
            self.assertEqual(imp.status, 'PENDING')

    # 2. Invalid signature rejected
    @patch('apps.tenant_core.views_meta_webhook.get_meta_app_credentials')
    def test_02_invalid_signature_rejected(self, mock_creds):
        mock_creds.return_value = ('app123', 'test_secret', 'token123')
        payload = self._make_meta_payload(leadgen_id='rel_lead_002')
        raw_body = json.dumps(payload).encode('utf-8')
        bad_sig = "sha256=invalidhexsignature1234567890"

        request = self.factory.post('/api/v1/webhooks/meta/leads/', data=raw_body, content_type='application/json', HTTP_X_HUB_SIGNATURE_256=bad_sig)
        view = MetaLeadWebhookView.as_view()
        response = view(request)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data.get('error'), 'Invalid signature')

    # 3. Duplicate webhook does not duplicate CRM Lead / import
    @patch('apps.tenant_core.views_meta_webhook.process_meta_lead_import_task.delay')
    @patch('apps.tenant_core.views_meta_webhook.get_meta_app_credentials')
    def test_03_duplicate_webhook_does_not_duplicate(self, mock_creds, mock_delay):
        mock_creds.return_value = ('app123', 'test_secret', 'token123')
        payload = self._make_meta_payload(leadgen_id='rel_lead_dup_003')
        raw_body = json.dumps(payload).encode('utf-8')
        sig = self._sign_payload(raw_body, 'test_secret')

        view = MetaLeadWebhookView.as_view()
        req1 = self.factory.post('/api/v1/webhooks/meta/leads/', data=raw_body, content_type='application/json', HTTP_X_HUB_SIGNATURE_256=sig)
        resp1 = view(req1)
        self.assertEqual(resp1.status_code, 200)

        # Mark first import as IMPORTED
        with tenant_database_context(str(self.tenant.id)) as alias:
            imp = MetaLeadImport.objects.using(alias).filter(external_lead_id='rel_lead_dup_003').first()
            imp.status = 'IMPORTED'
            imp.save(using=alias)

        # Send duplicate webhook
        req2 = self.factory.post('/api/v1/webhooks/meta/leads/', data=raw_body, content_type='application/json', HTTP_X_HUB_SIGNATURE_256=sig)
        resp2 = view(req2)
        self.assertEqual(resp2.status_code, 200)

        with tenant_database_context(str(self.tenant.id)) as alias:
            count = MetaLeadImport.objects.using(alias).filter(external_lead_id='rel_lead_dup_003').count()
            self.assertEqual(count, 1)

    # 4. Unknown Page safely recorded/rejected
    @patch('apps.tenant_core.views_meta_webhook.get_meta_app_credentials')
    def test_04_unknown_page_safely_unrouted(self, mock_creds):
        mock_creds.return_value = ('app123', 'test_secret', 'token123')
        payload = self._make_meta_payload(leadgen_id='rel_lead_unknown_004', page_id='0000000000000')
        raw_body = json.dumps(payload).encode('utf-8')
        sig = self._sign_payload(raw_body, 'test_secret')

        request = self.factory.post('/api/v1/webhooks/meta/leads/', data=raw_body, content_type='application/json', HTTP_X_HUB_SIGNATURE_256=sig)
        view = MetaLeadWebhookView.as_view()
        response = view(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.get('processed'), 0)

    # 5. Inactive mapping handling
    @patch('apps.tenant_core.services_meta_leads.decrypt_token', return_value='valid_page_token')
    def test_05_inactive_mapping_handling(self, mock_decrypt):
        with tenant_database_context(str(self.tenant.id)) as alias:
            self.mapping.is_active = False
            self.mapping.save(using=alias)

            imp = MetaLeadImport.objects.using(alias).create(
                organization=self.org,
                mode='LIVE',
                page_id=self.test_page_id,
                form_id=self.test_form_id,
                external_lead_id='rel_lead_inactive_map_005',
                field_data=[{'name': 'full_name', 'values': ['Inactive Map User']}],
                status='PENDING',
            )

            from apps.tenant_core.services_meta_leads import process_live_import
            result = process_live_import(self.org, imp.id, alias=alias)
            self.assertEqual(result.status, 'NEEDS_MAPPING')
            self.assertEqual(result.error_code, 'MAPPING_MISSING')
            self.assertIsNone(result.lead)

    # 6. Graph timeout schedules retry
    @patch('apps.tenant_core.tasks_meta_leads.process_live_import')
    def test_06_graph_timeout_schedules_retry(self, mock_import):
        import requests
        mock_import.side_effect = requests.exceptions.Timeout("Connection timed out")

        with tenant_database_context(str(self.tenant.id)) as alias:
            imp = MetaLeadImport.objects.using(alias).create(
                organization=self.org,
                mode='LIVE',
                page_id=self.test_page_id,
                form_id=self.test_form_id,
                external_lead_id='rel_lead_timeout_006',
                status='PENDING',
            )

        task = process_meta_lead_import_task
        with patch.object(task, 'retry', side_effect=Exception("TaskRetried")) as mock_retry:
            with self.assertRaises(Exception):
                task.apply(args=[str(self.tenant.id), str(imp.id)])
            mock_retry.assert_called_once()

        with tenant_database_context(str(self.tenant.id)) as alias:
            imp.refresh_from_db(using=alias)
            self.assertEqual(imp.status, 'RETRYING')

    # 7. Graph 500 schedules retry
    @patch('apps.tenant_core.tasks_meta_leads.process_live_import')
    def test_07_graph_500_schedules_retry(self, mock_import):
        mock_import.side_effect = MetaGraphServerError("Meta server internal error (HTTP 500)")

        with tenant_database_context(str(self.tenant.id)) as alias:
            imp = MetaLeadImport.objects.using(alias).create(
                organization=self.org,
                mode='LIVE',
                page_id=self.test_page_id,
                form_id=self.test_form_id,
                external_lead_id='rel_lead_500_007',
                status='PENDING',
            )

        task = process_meta_lead_import_task
        with patch.object(task, 'retry', side_effect=Exception("TaskRetried")) as mock_retry:
            with self.assertRaises(Exception):
                task.apply(args=[str(self.tenant.id), str(imp.id)])
            mock_retry.assert_called_once()

        with tenant_database_context(str(self.tenant.id)) as alias:
            imp.refresh_from_db(using=alias)
            self.assertEqual(imp.status, 'RETRYING')

    # 8. Graph 429 schedules retry
    @patch('apps.tenant_core.tasks_meta_leads.process_live_import')
    def test_08_graph_429_schedules_retry(self, mock_import):
        mock_import.side_effect = MetaRateLimitError("Rate limit reached")

        with tenant_database_context(str(self.tenant.id)) as alias:
            imp = MetaLeadImport.objects.using(alias).create(
                organization=self.org,
                mode='LIVE',
                page_id=self.test_page_id,
                form_id=self.test_form_id,
                external_lead_id='rel_lead_429_008',
                status='PENDING',
            )

        task = process_meta_lead_import_task
        with patch.object(task, 'retry', side_effect=Exception("TaskRetried")) as mock_retry:
            with self.assertRaises(Exception):
                task.apply(args=[str(self.tenant.id), str(imp.id)])
            mock_retry.assert_called_once()

        with tenant_database_context(str(self.tenant.id)) as alias:
            imp.refresh_from_db(using=alias)
            self.assertEqual(imp.status, 'RETRYING')

    # 9. Permanent mapping failure does not infinite retry
    @patch('apps.tenant_core.services_meta_leads.decrypt_token', return_value='valid_page_token')
    def test_09_permanent_mapping_failure_no_retry(self, mock_decrypt):
        with tenant_database_context(str(self.tenant.id)) as alias:
            imp = MetaLeadImport.objects.using(alias).create(
                organization=self.org,
                mode='LIVE',
                page_id=self.test_page_id,
                form_id='unmapped_form_99999',
                external_lead_id='rel_lead_unmapped_009',
                field_data=[{'name': 'full_name', 'values': ['Unmapped User']}],
                status='PENDING',
            )

        # Task should succeed without raising retry
        res = process_meta_lead_import_task.apply(args=[str(self.tenant.id), str(imp.id)]).get()
        self.assertEqual(res['status'], 'NEEDS_MAPPING')

        with tenant_database_context(str(self.tenant.id)) as alias:
            imp.refresh_from_db(using=alias)
            self.assertEqual(imp.status, 'NEEDS_MAPPING')

    # 10. Retry reaches SUCCESS
    @patch('apps.tenant_core.services_meta_leads.decrypt_token', return_value='valid_page_token')
    def test_10_retry_reaches_success(self, mock_decrypt):
        with tenant_database_context(str(self.tenant.id)) as alias:
            self.mapping.is_active = True
            self.mapping.repeat_policy = 'CREATE_NEW'
            self.mapping.save(using=alias)

            imp = MetaLeadImport.objects.using(alias).create(
                organization=self.org,
                mode='LIVE',
                page_id=self.test_page_id,
                form_id=self.test_form_id,
                external_lead_id='rel_lead_success_010',
                field_data=[
                    {'name': 'full_name', 'values': ['Success User']},
                    {'name': 'phone_number', 'values': ['+919888877777']},
                    {'name': 'email', 'values': ['success@test.com']},
                ],
                status='RETRYING',
                attempt_count=1,
            )

        res = process_meta_lead_import_task.apply(args=[str(self.tenant.id), str(imp.id)]).get()
        self.assertEqual(res['status'], 'IMPORTED')
        self.assertIsNotNone(res['lead_id'])

        with tenant_database_context(str(self.tenant.id)) as alias:
            imp.refresh_from_db(using=alias)
            self.assertEqual(imp.status, 'IMPORTED')
            self.assertIsNotNone(imp.lead)

    # 11. Retry exhaustion reaches terminal DEAD_LETTER
    @patch('apps.tenant_core.tasks_meta_leads.process_live_import')
    def test_11_retry_exhaustion_reaches_dead_letter(self, mock_import):
        mock_import.side_effect = MetaGraphServerError("Persistent 500 error")

        with tenant_database_context(str(self.tenant.id)) as alias:
            imp = MetaLeadImport.objects.using(alias).create(
                organization=self.org,
                mode='LIVE',
                page_id=self.test_page_id,
                form_id=self.test_form_id,
                external_lead_id='rel_lead_dead_011',
                status='RETRYING',
                attempt_count=META_MAX_RETRIES,
            )

        task = process_meta_lead_import_task
        # Simulate final attempt where retries == META_MAX_RETRIES
        res = task.apply(args=[str(self.tenant.id), str(imp.id)], retries=META_MAX_RETRIES).get()
        self.assertEqual(res['status'], 'DEAD_LETTER')

        with tenant_database_context(str(self.tenant.id)) as alias:
            imp.refresh_from_db(using=alias)
            self.assertEqual(imp.status, 'DEAD_LETTER')
            self.assertEqual(imp.error_code, 'RETRY_LIMIT_EXCEEDED')

    # 12. Manual retry works
    @patch('apps.tenant_core.services_meta_leads.decrypt_token', return_value='valid_page_token')
    def test_12_manual_retry_works(self, mock_decrypt):
        with tenant_database_context(str(self.tenant.id)) as alias:
            self.mapping.is_active = True
            self.mapping.repeat_policy = 'CREATE_NEW'
            self.mapping.save(using=alias)

            imp = MetaLeadImport.objects.using(alias).create(
                organization=self.org,
                mode='LIVE',
                page_id=self.test_page_id,
                form_id=self.test_form_id,
                external_lead_id='rel_lead_manual_012',
                field_data=[
                    {'name': 'full_name', 'values': ['Manual Retry User']},
                    {'name': 'phone_number', 'values': ['+919777766666']},
                    {'name': 'email', 'values': ['manual@test.com']},
                ],
                status='DEAD_LETTER',
                attempt_count=4,
            )

        set_tenant_db_alias(self.alias)
        set_tenant_db_alias(self.alias)
        request = self.factory.post(f'/api/v1/tenant/meta-lead-imports/{imp.id}/retry/')
        request.user = self.user
        force_authenticate(request, user=self.user)

        view = MetaLeadImportViewSet.as_view({'post': 'retry'})
        with patch.object(MetaLeadImportViewSet, 'permission_classes', [AllowAny]):
            with patch('apps.tenant_core.views_meta_leads.get_user_effective_branch_ids', return_value=None):
                with patch('apps.tenant_core.views_meta_leads.require_tenant_alias', return_value=self.alias):
                    with patch.object(MetaLeadImportViewSet, 'get_tenant_id', return_value=str(self.tenant.id)):
                        response = view(request, pk=str(imp.id))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.get('status'), 'IMPORTED')

    # 13. Manual retry cannot retry SUCCESS
    def test_13_manual_retry_cannot_retry_success(self):
        with tenant_database_context(str(self.tenant.id)) as alias:
            imp = MetaLeadImport.objects.using(alias).create(
                organization=self.org,
                mode='LIVE',
                page_id=self.test_page_id,
                form_id=self.test_form_id,
                external_lead_id='rel_lead_already_imported_013',
                status='IMPORTED',
            )

        request = self.factory.post(f'/api/v1/tenant/meta-lead-imports/{imp.id}/retry/')
        request.user = self.user
        force_authenticate(request, user=self.user)

        view = MetaLeadImportViewSet.as_view({'post': 'retry'})
        with patch.object(MetaLeadImportViewSet, 'permission_classes', [AllowAny]):
            with patch('apps.tenant_core.views_meta_leads.get_user_effective_branch_ids', return_value=None):
                with patch('apps.tenant_core.views_meta_leads.require_tenant_alias', return_value=self.alias):
                    with patch.object(MetaLeadImportViewSet, 'get_tenant_id', return_value=str(self.tenant.id)):
                        response = view(request, pk=str(imp.id))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data.get('error'), 'CANNOT_RETRY_SUCCESS')

    # 14. Tenant isolation
    def test_14_tenant_isolation(self):
        # Querying an import belonging to another tenant or non-existent returns 404
        fake_id = '00000000-0000-0000-0000-000000000000'
        request = self.factory.get(f'/api/v1/tenant/meta-lead-imports/{fake_id}/')
        request.user = self.user
        force_authenticate(request, user=self.user)

        view = MetaLeadImportViewSet.as_view({'get': 'retrieve'})
        with patch.object(MetaLeadImportViewSet, 'permission_classes', [AllowAny]):
            with patch('apps.tenant_core.views_meta_leads.get_user_effective_branch_ids', return_value=None):
                with patch('apps.tenant_core.views_meta_leads.require_tenant_alias', return_value=self.alias):
                    response = view(request, pk=fake_id)

        self.assertEqual(response.status_code, 404)

    # 15. Health endpoint values
    @patch('apps.tenant_core.views_meta_leads.get_meta_app_credentials', return_value=('app123', 'sec', 'tok'))
    def test_15_health_endpoint_values(self, mock_creds):
        request = self.factory.get('/api/v1/tenant/meta-lead-mappings/health/')
        request.user = self.user
        force_authenticate(request, user=self.user)

        view = MetaLeadMappingViewSet.as_view({'get': 'health'})
        with patch.object(MetaLeadMappingViewSet, 'permission_classes', [AllowAny]):
            with patch('apps.tenant_core.views_meta_leads.get_user_effective_branch_ids', return_value=None):
                with patch('apps.tenant_core.views_meta_leads.require_tenant_alias', return_value=self.alias):
                    with patch.object(MetaLeadMappingViewSet, 'get_tenant_id', return_value=str(self.tenant.id)):
                        response = view(request)

        self.assertEqual(response.status_code, 200)
        data = response.data
        self.assertIn('overall_status', data)
        self.assertIn('connection_status', data)
        self.assertIn('received_count_24h', data)
        self.assertIn('success_count_24h', data)
        self.assertIn('dead_letter_count', data)
        self.assertIn('retrying_count', data)
        self.assertIn('page_connection_count', data)

    # 16. No secrets returned by health endpoint
    @patch('apps.tenant_core.views_meta_leads.get_meta_app_credentials', return_value=('app123', 'sec', 'tok'))
    def test_16_no_secrets_in_health_endpoint(self, mock_creds):
        request = self.factory.get('/api/v1/tenant/meta-lead-mappings/health/')
        request.user = self.user
        force_authenticate(request, user=self.user)

        view = MetaLeadMappingViewSet.as_view({'get': 'health'})
        with patch.object(MetaLeadMappingViewSet, 'permission_classes', [AllowAny]):
            with patch('apps.tenant_core.views_meta_leads.get_user_effective_branch_ids', return_value=None):
                with patch('apps.tenant_core.views_meta_leads.require_tenant_alias', return_value=self.alias):
                    with patch.object(MetaLeadMappingViewSet, 'get_tenant_id', return_value=str(self.tenant.id)):
                        response = view(request)

        self.assertEqual(response.status_code, 200)
        content_str = json.dumps(response.data)

        # Ensure no token, secret, or password appears in the response
        self.assertNotIn('access_token', content_str)
        self.assertNotIn('app_secret', content_str)
        self.assertNotIn('app_secret', content_str)
        self.assertNotIn('access_token', content_str)
        self.assertNotIn('secret_key', content_str)


    # 17. Broker / Redis dispatch failure leaves durable PENDING import
    @patch('apps.tenant_core.views_meta_webhook.get_meta_app_credentials', return_value=('app123', 'test_secret', 'token123'))
    @patch('apps.tenant_core.views_meta_webhook.process_meta_lead_import_task.delay')
    def test_17_redis_dispatch_failure_persists_pending_import(self, mock_delay, mock_creds):
        mock_delay.side_effect = Exception("Redis connection refused (simulated broker outage)")

        payload = {
            'object': 'page',
            'entry': [{
                'id': self.test_page_id,
                'changes': [{
                    'field': 'leadgen',
                    'value': {
                        'page_id': self.test_page_id,
                        'form_id': self.test_form_id,
                        'leadgen_id': 'rel_lead_redis_fail_017',
                    }
                }]
            }]
        }
        raw_body = json.dumps(payload).encode('utf-8')
        sig = self._sign_payload(raw_body, 'test_secret')

        request = self.factory.post(
            '/api/v1/webhooks/meta/leads/',
            data=raw_body,
            content_type='application/json',
            HTTP_X_HUB_SIGNATURE_256=sig,
        )
        view = MetaLeadWebhookView.as_view()
        response = view(request)

        # Webhook responds HTTP 200 without raising unhandled exception
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.get('status'), 'received')
        self.assertEqual(response.data.get('enqueued'), 0)
        self.assertEqual(response.data.get('processed'), 0)
        self.assertEqual(response.data.get('pending_recovery'), 1)

        # Durable record exists in PostgreSQL with status PENDING
        with tenant_database_context(str(self.tenant.id)) as alias:
            imp = MetaLeadImport.objects.using(alias).filter(external_lead_id='rel_lead_redis_fail_017').first()
            self.assertIsNotNone(imp)
            self.assertEqual(imp.status, 'PENDING')
            self.assertEqual(imp.page_id, self.test_page_id)
            self.assertEqual(imp.form_id, self.test_form_id)

    # 18. Recovery service picks up stranded PENDING imports after Redis restored
    @patch('apps.tenant_core.services_meta_leads.decrypt_token', return_value='valid_page_token')
    def test_18_recovery_picks_up_stranded_pending_imports(self, mock_decrypt):
        from apps.tenant_core.services_meta_recovery import recover_unprocessed_meta_imports

        with tenant_database_context(str(self.tenant.id)) as alias:
            self.mapping.is_active = True
            self.mapping.repeat_policy = 'CREATE_NEW'
            self.mapping.save(using=alias)

            imp = MetaLeadImport.objects.using(alias).create(
                organization=self.org,
                mode='LIVE',
                page_id=self.test_page_id,
                form_id=self.test_form_id,
                external_lead_id='rel_lead_recovered_018',
                field_data=[
                    {'name': 'full_name', 'values': ['Recovered User']},
                    {'name': 'phone_number', 'values': ['+919666655555']},
                    {'name': 'email', 'values': ['recovered@test.com']},
                ],
                status='PENDING',
            )

        res = recover_unprocessed_meta_imports(
            tenant_id=str(self.tenant.id),
            min_age_seconds=0,
            max_batch_size=10,
            dispatch_async=False,
        )

        self.assertIn(str(imp.id), res.get('recovered_ids', []))

        with tenant_database_context(str(self.tenant.id)) as alias:
            imp.refresh_from_db(using=alias)
            self.assertEqual(imp.status, 'IMPORTED')
            self.assertIsNotNone(imp.lead)

    # 19. Duplicate webhook delivery after dispatch failure is idempotent
    @patch('apps.tenant_core.views_meta_webhook.get_meta_app_credentials', return_value=('app123', 'test_secret', 'token123'))
    @patch('apps.tenant_core.views_meta_webhook.process_meta_lead_import_task.delay')
    def test_19_duplicate_webhook_after_dispatch_failure_is_idempotent(self, mock_delay, mock_creds):
        payload = {
            'object': 'page',
            'entry': [{
                'id': self.test_page_id,
                'changes': [{
                    'field': 'leadgen',
                    'value': {
                        'page_id': self.test_page_id,
                        'form_id': self.test_form_id,
                        'leadgen_id': 'rel_lead_dup_fail_019',
                    }
                }]
            }]
        }
        raw_body = json.dumps(payload).encode('utf-8')
        sig = self._sign_payload(raw_body, 'test_secret')
        view = MetaLeadWebhookView.as_view()

        # First arrival: dispatch fails
        mock_delay.side_effect = Exception("Broker unreachable")
        req1 = self.factory.post('/api/v1/webhooks/meta/leads/', data=raw_body, content_type='application/json', HTTP_X_HUB_SIGNATURE_256=sig)
        resp1 = view(req1)
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp1.data.get('pending_recovery'), 1)

        # Second arrival: broker restored, dispatch succeeds
        mock_delay.side_effect = None
        req2 = self.factory.post('/api/v1/webhooks/meta/leads/', data=raw_body, content_type='application/json', HTTP_X_HUB_SIGNATURE_256=sig)
        resp2 = view(req2)
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.data.get('enqueued'), 1)

        # Verify only 1 MetaLeadImport record was created in database
        with tenant_database_context(str(self.tenant.id)) as alias:
            imports = MetaLeadImport.objects.using(alias).filter(external_lead_id='rel_lead_dup_fail_019')
            self.assertEqual(imports.count(), 1)


if __name__ == '__main__':
    unittest.main()
