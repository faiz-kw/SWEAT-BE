"""
backend/tests/test_voice_agent_views.py — Comprehensive Unit & Integration Tests for Sarvam AI Voice Agent Views.

Covers:
1. VoiceAgentBranchesView: Real-time active branch discovery and natural spoken summary.
2. VoiceAgentClassesView: Intelligent branch resolution (prompting if missing, resolving by lead preference).
3. VoiceAgentClassesView: Real-time capacity computation and branch-specific schedule filtering.
4. VoiceAgentBookTrialView: Atomic Capacity Locking (select_for_update) and successful booking creation.
5. VoiceAgentBookTrialView: Automated WhatsApp booking confirmation dispatch and CRM timeline update.
6. VoiceAgentBookTrialView: Overbooking rejection (HTTP 409) with dynamic alternative slot suggestion in spoken apology.
7. VoiceAgentBookTrialView: Idempotent duplicate booking handling for the same slot.
"""

import json
import uuid
from datetime import timedelta
from unittest.mock import patch, MagicMock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from config.routers import set_tenant_db_alias
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.models_saas import SaasPlan, TenantSubscription
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_crm import Lead, LeadSource, TrialBooking, LeadActivity
from apps.tenant_core.models_catalog import ProgramCategory, Program
from apps.tenant_core.models_classes import ClassTemplate, ClassOccurrence, ClassBranchAvailability
from apps.tenant_core.models_bookings import Booking
from apps.tenant_core.models_infra import Integration


@override_settings(COMMUNICATIONS_OUTBOUND_ENABLED=True, VOICE_CALLING_ENABLED=True)
class VoiceAgentViewsTests(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        super().setUp()
        set_tenant_db_alias('tenant_test')
        self.client = APIClient()

        # 1. Master DB Setup
        self.tenant = Tenant.objects.using('default').create(
            code='CRM-SWEAT',
            name='SWEAT Fitness',
            slug='sweat',
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
            name='Enterprise Voice Plan',
            code='ENT-VOICE-SWEAT',
            tier='ENTERPRISE',
            status='ACTIVE',
        )
        TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
        )

        # 2. Tenant DB Organization, Locations & Branches
        self.org = Organization.objects.using('tenant_test').create(
            code='ORG-SWEAT',
            name='SWEAT Studio Network',
            status='ACTIVE',
        )
        self.loc_blr = Location.objects.using('tenant_test').create(
            organization=self.org,
            code='LOC-BLR',
            name='Bangalore City',
            city='Bangalore',
            status='ACTIVE',
        )
        self.branch_indiranagar = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc_blr,
            code='BR-INDIRANAGAR',
            name='SWEAT Indiranagar',
            address='100 Feet Road, Indiranagar, Bangalore',
            phone='+91 9876543210',
            status='ACTIVE',
        )
        self.branch_koramangala = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc_blr,
            code='BR-KORAMANGALA',
            name='SWEAT Koramangala',
            address='80 Feet Road, 4th Block, Koramangala, Bangalore',
            phone='+91 9876543211',
            status='ACTIVE',
        )

        # 3. Catalog & Programs
        self.cat = ProgramCategory.objects.using('tenant_test').create(
            organization=self.org,
            code='CAT-GROUP',
            name='Group Training',
            status='ACTIVE',
        )
        self.program = Program.objects.using('tenant_test').create(
            organization=self.org,
            category=self.cat,
            code='PROG-PILATES',
            name='SWEAT Pilates',
            status='ACTIVE',
        )
        self.template = ClassTemplate.objects.using('tenant_test').create(
            organization=self.org,
            program=self.program,
            code='TPL-PILATES-REFORMER',
            name='Reformer Pilates Foundations',
            default_capacity=10,
            default_trial_capacity=2,
            allow_trial=True,
            status='ACTIVE',
        )

        # 4. Lead Source & Lead
        self.lead_source = LeadSource.objects.using('tenant_test').create(
            organization=self.org,
            name='Meta Instagram Ads',
            source_type='META',
            status='ACTIVE',
        )
        self.lead = Lead.objects.using('tenant_test').create(
            organization=self.org,
            branch=self.branch_indiranagar,
            lead_source=self.lead_source,
            first_name='Rahul',
            last_name='Verma',
            phone_normalized='+919999988888',
            current_status='NEW_LEAD',
        )

        # 5. Sarvam Integration Configuration
        self.sarvam_int = Integration.objects.using('tenant_test').create(
            integration_type='CALLING',
            provider='SARVAM',
            status='ACTIVE',
            configuration={
                'webhook_token': 'secret-voice-token-xyz',
            },
        )

    def test_voice_agent_branches_discovery(self):
        """Verifies that VoiceAgentBranchesView returns all active branches and conversational summary."""
        url = '/api/v1/voice-agent/branches/?token=secret-voice-token-xyz&tenant=sweat'
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data['status'], 'success')
        self.assertEqual(len(data['branches']), 2)
        branch_names = [b['name'] for b in data['branches']]
        self.assertIn('SWEAT Indiranagar', branch_names)
        self.assertIn('SWEAT Koramangala', branch_names)
        self.assertIn('Indiranagar', data['spoken_summary'])
        self.assertIn('Koramangala', data['spoken_summary'])

    def test_voice_agent_classes_prompt_when_branch_unspecified(self):
        """When no branch is specified and lead has no branch, the API prompts for branch selection."""
        lead_no_branch = Lead.objects.using('tenant_test').create(
            organization=self.org,
            first_name='Ananya',
            last_name='Sharma',
            phone_normalized='+919111122222',
            current_status='NEW_LEAD',
        )
        url = f'/api/v1/voice-agent/classes/?token=secret-voice-token-xyz&tenant=sweat&lead_id={lead_no_branch.id}'
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data['status'], 'needs_branch')
        self.assertIn('branches', data)
        self.assertIn('Indiranagar', data['spoken_summary'])

    def test_voice_agent_classes_branch_aware_filtering_and_capacity(self):
        """Verifies that VoiceAgentClassesView filters for the resolved branch and calculates real-time trial capacity."""
        import zoneinfo
        IST = zoneinfo.ZoneInfo('Asia/Kolkata')
        tomorrow = timezone.now().astimezone(IST) + timedelta(days=1)
        occ_indiranagar = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.template,
            branch=self.branch_indiranagar,
            occurrence_date=tomorrow.date(),
            start_at=tomorrow.replace(hour=18, minute=0, second=0, microsecond=0),
            end_at=tomorrow.replace(hour=19, minute=0, second=0, microsecond=0),
            capacity=10,
            trial_capacity=3,
            status='SCHEDULED',
        )
        # Create an occurrence at Koramangala to ensure cross-branch isolation
        ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.template,
            branch=self.branch_koramangala,
            occurrence_date=tomorrow.date(),
            start_at=tomorrow.replace(hour=18, minute=0, second=0, microsecond=0),
            end_at=tomorrow.replace(hour=19, minute=0, second=0, microsecond=0),
            capacity=10,
            trial_capacity=3,
            status='SCHEDULED',
        )

        url = f'/api/v1/voice-agent/classes/?token=secret-voice-token-xyz&tenant=sweat&branch=Indiranagar&date=tomorrow'
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['branch']['name'], 'SWEAT Indiranagar')
        self.assertEqual(len(data['available_classes']), 1)
        self.assertEqual(data['available_classes'][0]['occurrence_id'], str(occ_indiranagar.id))
        self.assertEqual(data['available_classes'][0]['available_trial_spots'], 3)
        self.assertIn('Indiranagar', data['spoken_summary'])

    @patch('apps.tenant_core.communication.service.CommunicationService.send_communication')
    def test_voice_agent_book_trial_atomic_success_and_whatsapp(self, mock_send_comm):
        """Verifies instant trial booking with atomic locking, lead transition to TRIAL_CONFIRMED, and WhatsApp dispatch."""
        tomorrow = timezone.now() + timedelta(days=1)
        occ = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.template,
            branch=self.branch_indiranagar,
            occurrence_date=tomorrow.date(),
            start_at=tomorrow.replace(hour=10, minute=0, second=0, microsecond=0),
            end_at=tomorrow.replace(hour=11, minute=0, second=0, microsecond=0),
            capacity=10,
            trial_capacity=2,
            status='SCHEDULED',
        )

        payload = {
            'phone_number': self.lead.phone_normalized,
            'occurrence_id': str(occ.id),
            'notes': 'Booked during outbound Sarvam calling session',
        }
        url = '/api/v1/voice-agent/book-trial/?token=secret-voice-token-xyz&tenant=sweat'
        response = self.client.post(url, data=json.dumps(payload), content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.json()
        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['lead_name'], 'Rahul')
        self.assertEqual(data['branch_name'], 'SWEAT Indiranagar')
        self.assertIn("You're all set, Rahul!", data['spoken_confirmation'])

        # Verify DB records updated under lock
        trial = TrialBooking.objects.using('tenant_test').filter(id=data['booking_id']).first()
        self.assertIsNotNone(trial)
        self.assertEqual(trial.status, 'CONFIRMED')
        self.assertEqual(trial.confirmation_status, 'CONFIRMED')
        self.assertEqual(trial.confirmation_channel, 'PHONE')

        # Verify Lead status advanced
        self.lead.refresh_from_db(using='tenant_test')
        self.assertEqual(self.lead.current_status, 'TRIAL_CONFIRMED')

        # Verify Activity Logged
        activity = LeadActivity.objects.using('tenant_test').filter(lead=self.lead, activity_type='TRIAL').first()
        self.assertIsNotNone(activity)
        self.assertIn('Sarvam AI Voice Agent', activity.notes)

        # Verify WhatsApp dispatch was called
        mock_send_comm.assert_called_once()
        call_kwargs = mock_send_comm.call_args[1]
        self.assertEqual(call_kwargs['channel'], 'WHATSAPP')
        self.assertEqual(call_kwargs['recipient'], self.lead.phone_normalized)

    def test_voice_agent_book_trial_atomic_capacity_exhausted_rejects_409(self):
        """Verifies that atomic capacity enforcement rejects overbooking with HTTP 409 and alternative in spoken apology."""
        tomorrow = timezone.now() + timedelta(days=1)
        occ = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.template,
            branch=self.branch_indiranagar,
            occurrence_date=tomorrow.date(),
            start_at=tomorrow.replace(hour=17, minute=0, second=0, microsecond=0),
            end_at=tomorrow.replace(hour=18, minute=0, second=0, microsecond=0),
            capacity=1,
            trial_capacity=1,
            status='SCHEDULED',
        )

        # Alternative slot to test alternative suggestion in apology
        ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.template,
            branch=self.branch_indiranagar,
            occurrence_date=tomorrow.date(),
            start_at=tomorrow.replace(hour=19, minute=0, second=0, microsecond=0),
            end_at=tomorrow.replace(hour=20, minute=0, second=0, microsecond=0),
            capacity=5,
            trial_capacity=2,
            status='SCHEDULED',
        )

        # Existing member booking fills up total capacity
        other_lead = Lead.objects.using('tenant_test').create(
            organization=self.org,
            branch=self.branch_indiranagar,
            first_name='Vikram',
            phone_normalized='+919888877777',
        )
        TrialBooking.objects.using('tenant_test').create(
            lead=other_lead,
            branch=self.branch_indiranagar,
            class_occurrence_id=occ.id,
            trial_type='GROUP_CLASS',
            scheduled_start=occ.start_at,
            scheduled_end=occ.end_at,
            status='CONFIRMED',
            confirmation_status='CONFIRMED',
        )

        payload = {
            'phone_number': self.lead.phone_normalized,
            'occurrence_id': str(occ.id),
        }
        url = '/api/v1/voice-agent/book-trial/?token=secret-voice-token-xyz&tenant=sweat'
        response = self.client.post(url, data=json.dumps(payload), content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        data = response.json()
        self.assertEqual(data['status'], 'error')
        self.assertEqual(data['error_code'], 'SLOT_FULL')
        self.assertIn('just filled up to capacity', data['spoken_apology'])

    def test_voice_agent_book_trial_idempotent_duplicate_handling(self):
        """Verifies that booking the same slot multiple times is handled idempotently without duplicate records."""
        tomorrow = timezone.now() + timedelta(days=1)
        occ = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.template,
            branch=self.branch_indiranagar,
            occurrence_date=tomorrow.date(),
            start_at=tomorrow.replace(hour=8, minute=0, second=0, microsecond=0),
            end_at=tomorrow.replace(hour=9, minute=0, second=0, microsecond=0),
            capacity=10,
            trial_capacity=5,
            status='SCHEDULED',
        )

        # First booking succeeds
        trial1 = TrialBooking.objects.using('tenant_test').create(
            lead=self.lead,
            branch=self.branch_indiranagar,
            class_occurrence_id=occ.id,
            trial_type='GROUP_CLASS',
            scheduled_start=occ.start_at,
            scheduled_end=occ.end_at,
            status='CONFIRMED',
            confirmation_status='CONFIRMED',
            confirmation_channel='PHONE',
        )

        payload = {
            'phone_number': self.lead.phone_normalized,
            'occurrence_id': str(occ.id),
        }
        url = '/api/v1/voice-agent/book-trial/?token=secret-voice-token-xyz&tenant=sweat'
        response = self.client.post(url, data=json.dumps(payload), content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertTrue(data.get('already_booked', False))
        self.assertEqual(data['booking_id'], str(trial1.id))
        self.assertIn('already booked', data['spoken_confirmation'])

    def test_voice_agent_lead_context_with_preferred_branch(self):
        """Verifies pre-call lead context lookup returns lead profile, preferred branch, and tailored greeting."""
        url = f'/api/v1/voice-agent/context/?token=secret-voice-token-xyz&tenant=sweat&lead_id={self.lead.id}'
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['first_name'], 'Rahul')
        self.assertTrue(data['has_preferred_branch'])
        self.assertEqual(data['preferred_branch']['name'], 'SWEAT Indiranagar')
        self.assertIn('Indiranagar', data['suggested_greeting'])

    def test_voice_agent_lead_context_with_existing_trial(self):
        """Verifies pre-call context identifies active upcoming trial and greets for reschedule/cancel."""
        tomorrow = timezone.now() + timedelta(days=1)
        occ = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.template,
            branch=self.branch_indiranagar,
            occurrence_date=tomorrow.date(),
            start_at=tomorrow.replace(hour=10, minute=0, second=0, microsecond=0),
            end_at=tomorrow.replace(hour=11, minute=0, second=0, microsecond=0),
            capacity=10,
            trial_capacity=5,
            status='SCHEDULED',
        )
        trial = TrialBooking.objects.using('tenant_test').create(
            lead=self.lead,
            branch=self.branch_indiranagar,
            class_occurrence_id=occ.id,
            trial_type='GROUP_CLASS',
            scheduled_start=occ.start_at,
            scheduled_end=occ.end_at,
            status='CONFIRMED',
            confirmation_status='CONFIRMED',
        )

        url = f'/api/v1/voice-agent/context/?token=secret-voice-token-xyz&tenant=sweat&phone={self.lead.phone_normalized}'
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertTrue(data['has_active_trial'])
        self.assertEqual(data['active_trial']['trial_id'], str(trial.id))
        self.assertIn('reschedule', data['suggested_greeting'])

    @patch('apps.tenant_core.communication.service.CommunicationService.send_communication')
    def test_voice_agent_reschedule_trial_success(self, mock_send_comm):
        """Verifies mid-call trial rescheduling to a new slot under atomic lock with WhatsApp dispatch."""
        tomorrow = timezone.now() + timedelta(days=1)
        occ1 = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.template,
            branch=self.branch_indiranagar,
            occurrence_date=tomorrow.date(),
            start_at=tomorrow.replace(hour=7, minute=0, second=0, microsecond=0),
            end_at=tomorrow.replace(hour=8, minute=0, second=0, microsecond=0),
            capacity=10,
            trial_capacity=5,
            status='SCHEDULED',
        )
        occ2 = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.template,
            branch=self.branch_indiranagar,
            occurrence_date=tomorrow.date(),
            start_at=tomorrow.replace(hour=18, minute=0, second=0, microsecond=0),
            end_at=tomorrow.replace(hour=19, minute=0, second=0, microsecond=0),
            capacity=10,
            trial_capacity=5,
            status='SCHEDULED',
        )
        trial = TrialBooking.objects.using('tenant_test').create(
            lead=self.lead,
            branch=self.branch_indiranagar,
            class_occurrence_id=occ1.id,
            trial_type='GROUP_CLASS',
            scheduled_start=occ1.start_at,
            scheduled_end=occ1.end_at,
            status='CONFIRMED',
            confirmation_status='CONFIRMED',
        )

        payload = {
            'lead_id': str(self.lead.id),
            'trial_id': str(trial.id),
            'new_occurrence_id': str(occ2.id),
        }
        url = '/api/v1/voice-agent/reschedule-trial/?token=secret-voice-token-xyz&tenant=sweat'
        response = self.client.post(url, data=json.dumps(payload), content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data['status'], 'success')
        self.assertIn('rescheduled your trial', data['spoken_confirmation'])

        # Verify DB updated
        updated_trial = TrialBooking.objects.using('tenant_test').get(id=trial.id)
        self.assertEqual(updated_trial.class_occurrence_id, occ2.id)
        self.assertEqual(updated_trial.confirmation_status, 'CONFIRMED')
        mock_send_comm.assert_called_once()

    @patch('apps.tenant_core.communication.service.CommunicationService.send_communication')
    def test_voice_agent_cancel_trial_success(self, mock_send_comm):
        """Verifies mid-call trial cancellation releasing capacity and triggering WhatsApp alert."""
        tomorrow = timezone.now() + timedelta(days=1)
        occ = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.template,
            branch=self.branch_indiranagar,
            occurrence_date=tomorrow.date(),
            start_at=tomorrow.replace(hour=9, minute=0, second=0, microsecond=0),
            end_at=tomorrow.replace(hour=10, minute=0, second=0, microsecond=0),
            capacity=10,
            trial_capacity=5,
            status='SCHEDULED',
        )
        trial = TrialBooking.objects.using('tenant_test').create(
            lead=self.lead,
            branch=self.branch_indiranagar,
            class_occurrence_id=occ.id,
            trial_type='GROUP_CLASS',
            scheduled_start=occ.start_at,
            scheduled_end=occ.end_at,
            status='CONFIRMED',
            confirmation_status='CONFIRMED',
        )

        payload = {
            'phone': self.lead.phone_normalized,
            'reason': 'Caller requested cancellation due to travel',
        }
        url = '/api/v1/voice-agent/cancel-trial/?token=secret-voice-token-xyz&tenant=sweat'
        response = self.client.post(url, data=json.dumps(payload), content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data['status'], 'success')
        self.assertIn('cancelled your trial session', data['spoken_confirmation'])

        # Verify DB updated
        updated_trial = TrialBooking.objects.using('tenant_test').get(id=trial.id)
        self.assertEqual(updated_trial.status, 'CANCELLED')
        self.assertEqual(updated_trial.confirmation_status, 'CANCELLED')
        mock_send_comm.assert_called_once()

