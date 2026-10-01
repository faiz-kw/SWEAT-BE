import hashlib
import hmac
import uuid
from unittest.mock import patch
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework.exceptions import PermissionDenied, ValidationError
from config.routers import set_tenant_db_alias
from apps.tenant_core.meta_lead_rules import map_answers, verify_signature
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_crm import Lead, LeadSource, LeadAttribution, CRMAgentAssignmentConfig
from apps.tenant_core.models_meta_leads import MetaLeadMapping, MetaLeadImport
from apps.tenant_core.models_audit_outbox import BusinessAuditEvent
from apps.tenant_core.services_meta_leads import receive_simulation, process_simulation, simulator_enabled
from apps.tenant_core.communication.service import CommunicationService


class MetaSignatureTests(SimpleTestCase):
    def test_signature_validates_exact_bytes_and_fails_closed(self):
        raw = b'{"entry":[]}'
        signature = 'sha256=' + hmac.new(b'test-only-secret', raw, hashlib.sha256).hexdigest()
        self.assertTrue(verify_signature(raw, signature, 'test-only-secret'))
        self.assertFalse(verify_signature(raw + b' ', signature, 'test-only-secret'))
        self.assertFalse(verify_signature(raw, signature, ''))
        self.assertFalse(verify_signature(raw, '', 'test-only-secret'))
        self.assertFalse(verify_signature(raw, 'sha256=' + 'é' * 64, 'test-only-secret'))

    def test_mapping_rejects_duplicate_questions_and_multi_value_contact(self):
        with self.assertRaises(ValueError):
            map_answers([{'name': 'name', 'values': ['A']}, {'name': 'name', 'values': ['B']}], {'full_name': 'name'})
        with self.assertRaises(ValueError):
            map_answers([{'name': 'email', 'values': ['a@example.test', 'b@example.test']}], {'email': 'email'})


@override_settings(DEBUG=True)
class MetaDevelopmentTests(TestCase):
    databases = {'default', 'tenant_test', 'tenant_other'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        self.org = Organization.objects.create(code='META-TEST', name='Test tenant', status='ACTIVE')
        self.other_org = Organization.objects.create(code='OTHER', name='Other organisation', status='ACTIVE')
        location = Location.objects.create(organization=self.org, code='LOC', name='Test location')
        self.branch = Branch.objects.create(organization=self.org, location=location, code='BR', name='Test branch')
        self.source = LeadSource.objects.create(organization=self.org, code='META-TEST', name='Meta test', source_type='META')
        CRMAgentAssignmentConfig.objects.create(organization=self.org, assignment_mode_allowed='MANUAL',
                                               auto_assignment_strategy='MANUAL_ONLY', allow_unassigned_fallback=True,
                                               notify_manager_on_unassigned=False)
        self.user = TenantUser.objects.create(organization=self.org, email='admin@example.test', first_name='Admin')
        self.user.is_superuser = True
        self.user._auth_type = 'tenant'
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.mapping = MetaLeadMapping.objects.create(organization=self.org, name='Test mapping', page_id='test-page', form_id='test-form',
            branch=self.branch, lead_source=self.source, field_mappings={'full_name': 'full_name', 'email': 'email'}, is_active=True)
        self.payload = {'page_id': 'test-page', 'form_id': 'test-form', 'external_lead_id': 'submission-1',
                        'field_data': [{'name': 'full_name', 'values': ['Test Person']}, {'name': 'email', 'values': ['person@example.test']}]}

    def tearDown(self):
        set_tenant_db_alias(None)
        super().tearDown()

    def test_real_crm_creation_and_duplicate_delivery(self):
        event, created = receive_simulation(self.org, self.payload, self.user)
        self.assertTrue(created)
        self.assertEqual(event.status, 'IMPORTED')
        self.assertEqual(event.lead.branch_id, self.branch.id)
        self.assertEqual(event.lead.first_name, 'Test')
        self.assertEqual(event.lead.last_name, 'Person')
        second, created = receive_simulation(self.org, self.payload, self.user)
        self.assertFalse(created)
        self.assertEqual(second.id, event.id)
        self.assertEqual(Lead.objects.count(), 1)
        self.assertEqual(LeadAttribution.objects.count(), 1)
        self.assertTrue(LeadAttribution.objects.first().raw_metadata['is_test'])

    def test_changed_payload_same_external_id_rejected(self):
        receive_simulation(self.org, self.payload)
        with self.assertRaises(ValidationError):
            receive_simulation(self.org, {**self.payload, 'form_id': 'another-form'})
        self.assertEqual(Lead.objects.count(), 1)

    def test_unmapped_receipt_is_durable_and_recovers(self):
        self.mapping.is_active = False
        self.mapping.save()
        event, _ = receive_simulation(self.org, self.payload)
        self.assertEqual(event.status, 'NEEDS_MAPPING')
        self.assertEqual(Lead.objects.count(), 0)
        self.mapping.is_active = True
        self.mapping.version = 2
        self.mapping.save()
        recovered = process_simulation(self.org, event.id)
        self.assertEqual(recovered.status, 'IMPORTED')
        self.assertEqual(recovered.mapping_version, 2)
        self.assertEqual(recovered.attempt_count, 2)

    def test_unknown_branch_is_held_without_first_branch_fallback(self):
        self.mapping.branch_mode = 'ANSWER'
        self.mapping.branch_field = 'location'
        self.mapping.branch_answers = {'known': str(self.branch.id)}
        self.mapping.save()
        event, _ = receive_simulation(self.org, self.payload)
        self.assertEqual(event.status, 'NEEDS_ASSIGNMENT')
        self.assertEqual(Lead.objects.count(), 0)

    def test_answer_branch_normalization(self):
        self.mapping.branch_mode = 'ANSWER'
        self.mapping.branch_field = 'location'
        self.mapping.branch_answers = {'known': str(self.branch.id)}
        self.mapping.save()
        self.payload['field_data'].append({'name': 'location', 'values': [' KNOWN ']})
        event, _ = receive_simulation(self.org, self.payload)
        self.assertEqual(event.status, 'IMPORTED')

    def test_repeat_enquiry_is_retained_for_review(self):
        receive_simulation(self.org, self.payload)
        event, _ = receive_simulation(self.org, {**self.payload, 'external_lead_id': 'submission-2'})
        self.assertEqual(event.status, 'NEEDS_REVIEW')
        self.assertEqual(MetaLeadImport.objects.count(), 2)
        self.assertEqual(Lead.objects.count(), 1)

    def test_explicit_new_enquiry_policy(self):
        self.mapping.repeat_policy = 'CREATE_NEW'
        self.mapping.save()
        receive_simulation(self.org, self.payload)
        event, _ = receive_simulation(self.org, {**self.payload, 'external_lead_id': 'submission-2'})
        self.assertEqual(event.status, 'IMPORTED')
        self.assertEqual(Lead.objects.count(), 2)

    def test_failure_rolls_back_lead_and_keeps_receipt(self):
        with patch('apps.tenant_core.services_crm.CRMLeadService.record_lead_attribution', side_effect=RuntimeError('private-secret')):
            event, _ = receive_simulation(self.org, self.payload)
        self.assertEqual(event.status, 'FAILED')
        self.assertNotIn('private-secret', event.error_message)
        self.assertEqual(Lead.objects.count(), 0)
        self.assertEqual(MetaLeadImport.objects.count(), 1)
        self.assertEqual(process_simulation(self.org, event.id).status, 'IMPORTED')

    def test_production_disabled(self):
        with override_settings(DEBUG=False):
            self.assertFalse(simulator_enabled())
            with self.assertRaises(PermissionDenied):
                receive_simulation(self.org, self.payload)
        self.assertEqual(MetaLeadImport.objects.count(), 0)

    def test_outbound_enabled_prevents_simulation(self):
        with override_settings(COMMUNICATIONS_OUTBOUND_ENABLED=True):
            with self.assertRaises(PermissionDenied):
                receive_simulation(self.org, self.payload)

    def test_outbound_service_does_not_call_provider(self):
        with patch('apps.tenant_core.communication.service.CommunicationProviderRegistry.resolve_for_tenant') as resolve:
            with self.assertRaises(ValueError):
                CommunicationService.send_communication(self.org, 'EMAIL', 'test@example.test')
            resolve.assert_not_called()

    def test_simulator_lead_cannot_send_after_outbound_is_reenabled(self):
        event, _ = receive_simulation(self.org, self.payload)
        with override_settings(COMMUNICATIONS_OUTBOUND_ENABLED=True):
            with patch('apps.tenant_core.communication.service.CommunicationProviderRegistry.resolve_for_tenant') as resolve:
                with self.assertRaises(ValueError):
                    CommunicationService.send_communication(self.org, 'EMAIL', 'test@example.test', lead=event.lead)
                resolve.assert_not_called()

    def test_create_mapping_through_api(self):
        payload = {'name': 'Second form', 'page_id': 'another-page', 'form_id': 'another-form',
                   'branch_mode': 'FIXED', 'branch': str(self.branch.id), 'lead_source': str(self.source.id),
                   'field_mappings': {'full_name': 'customer', 'email': 'contact'}, 'is_active': True}
        response = self.client.post('/meta-lead-mappings/', payload, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.client.post('/meta-lead-mappings/', payload, format='json').status_code, 400)

    def test_branch_answer_normalization_rejects_conflicts(self):
        response = self.client.patch(f'/meta-lead-mappings/{self.mapping.id}/', {
            'expected_version': 1, 'branch_mode': 'ANSWER', 'branch_field': 'branch',
            'branch_answers': {'Studio': str(self.branch.id), ' studio ': str(self.branch.id)},
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_config_pause_retains_import_history(self):
        event, _ = receive_simulation(self.org, self.payload)
        response = self.client.patch(f'/meta-lead-mappings/{self.mapping.id}/', {'expected_version': 1, 'is_active': False}, format='json')
        self.assertEqual(response.status_code, 200)
        second, _ = receive_simulation(self.org, {**self.payload, 'external_lead_id': 'paused-submission'})
        self.assertEqual(second.status, 'NEEDS_MAPPING')
        self.assertEqual(MetaLeadImport.objects.get(id=event.id).status, 'IMPORTED')

    def test_missing_tenant_context_fails_closed(self):
        set_tenant_db_alias(None)
        with self.assertRaises(PermissionDenied):
            receive_simulation(self.org, self.payload)

    def test_other_tenant_database_does_not_see_import(self):
        receive_simulation(self.org, self.payload)
        self.assertEqual(MetaLeadImport.objects.using('tenant_other').count(), 0)
        self.assertEqual(Lead.objects.using('tenant_other').count(), 0)

    def test_api_mapping_serialization_and_metadata(self):
        res = self.client.get('/meta-lead-mappings/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['results'][0]['branch'], str(self.branch.id))
        res = self.client.get('/meta-lead-mappings/metadata/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['connection_status'], 'NOT_CONNECTED')
        self.assertFalse(res.data['live_available'])

    def test_api_simulate_and_retry_imported_is_noop(self):
        res = self.client.post('/meta-lead-imports/simulate/', self.payload, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        res = self.client.post(f"/meta-lead-imports/{res.data['id']}/retry/", {}, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(Lead.objects.count(), 1)

    def test_stale_mapping_update_rejected_and_edit_audited(self):
        url = f'/meta-lead-mappings/{self.mapping.id}/'
        res = self.client.patch(url, {'name': 'Changed', 'expected_version': 1}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['version'], 2)
        res = self.client.patch(url, {'name': 'Stale', 'expected_version': 1}, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertTrue(BusinessAuditEvent.objects.filter(action_code='META_MAPPING_UPDATED').exists())

    def test_cross_organisation_mapping_and_import_access_denied(self):
        event, _ = receive_simulation(self.org, self.payload)
        self.user.organization = self.other_org
        self.user.save()
        self.assertEqual(self.client.get(f'/meta-lead-imports/{event.id}/').status_code, 404)
        self.assertEqual(self.client.patch(f'/meta-lead-mappings/{self.mapping.id}/', {'expected_version': 1}, format='json').status_code, 404)

    def test_arbitrary_foreign_branch_rejected(self):
        res = self.client.patch(f'/meta-lead-mappings/{self.mapping.id}/', {'branch': str(uuid.uuid4()), 'expected_version': 1}, format='json')
        self.assertEqual(res.status_code, 400)

    def test_anonymous_and_platform_users_rejected(self):
        self.client = APIClient()
        self.assertIn(self.client.get('/meta-lead-imports/').status_code, [401, 403])
        self.user._auth_type = 'platform'
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get('/meta-lead-imports/').status_code, 403)

    def test_branch_scoped_user_cannot_access_org_imports(self):
        with patch('apps.tenant_core.views_meta_leads.get_user_effective_branch_ids', return_value={str(self.branch.id)}):
            self.assertEqual(self.client.get('/meta-lead-imports/').status_code, 403)

    def test_field_defaults_and_expanded_native_fields(self):
        self.mapping.field_mappings = {
            'first_name': 'fname',
            'last_name': 'lname',
            'email': 'email',
            'gender': 'gender',
            'occupation': 'occupation',
            'consent_whatsapp': 'whatsapp_consent',
        }
        self.mapping.field_defaults = {'country': 'India', 'fitness_goal': 'Weight Loss'}
        self.mapping.save()
        payload = {
            'page_id': 'test-page', 'form_id': 'test-form', 'external_lead_id': 'expanded-fields-1',
            'field_data': [
                {'name': 'fname', 'values': ['Aarav']},
                {'name': 'lname', 'values': ['Patel']},
                {'name': 'email', 'values': ['aarav@example.test']},
                {'name': 'gender', 'values': ['Male']},
                {'name': 'occupation', 'values': ['Engineer']},
                {'name': 'whatsapp_consent', 'values': ['yes']},
            ]
        }
        event, _ = receive_simulation(self.org, payload)
        self.assertEqual(event.status, 'IMPORTED')
        lead = event.lead
        self.assertEqual(lead.first_name, 'Aarav')
        self.assertEqual(lead.last_name, 'Patel')
        self.assertEqual(lead.gender, 'Male')
        self.assertEqual(lead.occupation, 'Engineer')
        self.assertEqual(lead.country, 'India')
        self.assertEqual(lead.fitness_goal, 'Weight Loss')
        self.assertTrue(lead.consent_whatsapp)

    def test_configurable_branch_routing_fallback(self):
        loc2 = Location.objects.create(organization=self.org, code='LOC2', name='Location 2')
        fb_branch = Branch.objects.create(organization=self.org, location=loc2, code='BR2', name='Fallback Branch')
        self.mapping.branch_mode = 'ANSWER'
        self.mapping.branch_field = 'center'
        self.mapping.branch_answers = {'downtown': str(self.branch.id)}
        self.mapping.unmatched_branch_policy = 'FALLBACK_BRANCH'
        self.mapping.fallback_branch = fb_branch
        self.mapping.save()

        # Unknown branch answer routes to fallback branch
        payload = {
            **self.payload,
            'external_lead_id': 'fb-test-1',
            'field_data': [*self.payload['field_data'], {'name': 'center', 'values': ['suburbs']}],
        }
        event, _ = receive_simulation(self.org, payload)
        self.assertEqual(event.status, 'IMPORTED')
        self.assertEqual(event.lead.branch_id, fb_branch.id)

    def test_configurable_initial_stage(self):
        self.mapping.initial_stage = 'INTERESTED'
        self.mapping.save()
        payload = {**self.payload, 'external_lead_id': 'stage-test-1'}
        event, _ = receive_simulation(self.org, payload)
        self.assertEqual(event.status, 'IMPORTED')
        self.assertEqual(event.lead.current_status, 'INTERESTED')

    def test_automated_followup_task_creation(self):
        from apps.tenant_core.models_crm import SalesFollowupTask
        self.mapping.create_followup_task = True
        self.mapping.followup_task_type = 'CALL'
        self.mapping.followup_due_hours = 6
        self.mapping.save()
        payload = {**self.payload, 'external_lead_id': 'followup-test-1'}
        event, _ = receive_simulation(self.org, payload, actor_user=self.user)
        self.assertEqual(event.status, 'IMPORTED')
        task = SalesFollowupTask.objects.filter(lead=event.lead).first()
        self.assertIsNotNone(task)
        self.assertEqual(task.task_type, 'CALL')
        self.assertEqual(task.priority, 'HIGH')
        self.assertEqual(task.status, 'PENDING')

    def test_audit_history_endpoint(self):
        res = self.client.patch(f'/meta-lead-mappings/{self.mapping.id}/', {'name': 'Audited Name', 'expected_version': 1}, format='json')
        self.assertEqual(res.status_code, 200)
        history_res = self.client.get(f'/meta-lead-mappings/{self.mapping.id}/audit-history/')
        self.assertEqual(history_res.status_code, 200)
        results = history_res.data['results']
        self.assertTrue(len(results) >= 1)
        self.assertEqual(results[0]['action_code'], 'META_MAPPING_UPDATED')
        self.assertEqual(results[0]['actor_name'], 'Admin')
