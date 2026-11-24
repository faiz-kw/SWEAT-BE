import json
import uuid
from unittest.mock import patch, MagicMock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.tenant_core.models_org import Organization, Branch, Location
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_crm import Lead, CallSession, LeadActivity, AgentPresence, TrialBooking
from apps.tenant_core.models_classes import ClassTemplate, ClassOccurrence
from apps.tenant_core.models_govern import WhatsAppAutomationPolicy
from apps.tenant_core.models_communication import CommunicationMessage
from apps.tenant_core.communication.whatsapp_automation import WhatsAppAutomationService
from apps.tenant_core.communication.adapters.whatsapp_gupshup import GupshupWhatsAppAdapter
from apps.tenant_core.services_crm import CRMLeadService
from config.routers import set_tenant_db_alias


@override_settings(COMMUNICATIONS_OUTBOUND_ENABLED=True, WHATSAPP_SANDBOX_MODE=True)
class CallDispositionWhatsAppDeliveryTests(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        self.org = Organization.objects.create(name="SWEAT Fitness", code="SWT01")
        self.location = Location.objects.create(organization=self.org, name="Mumbai", code="LOC01", city="Mumbai")
        self.branch = Branch.objects.create(organization=self.org, location=self.location, name="Andheri West", code="BR01")
        self.rep = TenantUser.objects.create(
            organization=self.org,
            home_branch=self.branch,
            email="sales.rep@sweatelite.in",
            first_name="Rohan",
            last_name="Sharma",
            status="ACTIVE",
        )
        self.lead = Lead.objects.create(
            organization=self.org,
            branch=self.branch,
            first_name="Farhan",
            last_name="Khan",
            phone_normalized="+917738678323",
            current_status="NEW_LEAD",
        )
        self.presence = AgentPresence.objects.create(
            organization=self.org,
            user=self.rep,
            sip_username="rohan0001",
            status="WRAP_UP",
            last_heartbeat=timezone.now(),
        )
        self.policy = WhatsAppAutomationPolicy.objects.create(
            organization=self.org,
            whatsapp_enabled=True,
        )
        self.class_template = ClassTemplate.objects.create(
            organization=self.org,
            code="SWT_PILAT",
            name="Sweat Reformer Pilates",
            allow_trial=True,
            default_trial_capacity=5,
        )
        occ_start = timezone.now() + timezone.timedelta(days=1)
        self.occurrence = ClassOccurrence.objects.create(
            class_template=self.class_template,
            branch=self.branch,
            occurrence_date=occ_start.date(),
            start_at=occ_start,
            end_at=occ_start + timezone.timedelta(minutes=60),
            capacity=10,
            trial_capacity=5,
            status="SCHEDULED",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.rep)

    def tearDown(self):
        set_tenant_db_alias(None)

    @patch('apps.tenant_core.communication.adapters.whatsapp_gupshup.GupshupWhatsAppAdapter.send')
    def test_01_disposition_no_answer_dispatches_whatsapp(self, mock_send):
        """Selecting No Answer with send_whatsapp=True sends template WhatsApp follow-up and logs activity."""
        mock_send.return_value = MagicMock(
            success=True,
            status='SUBMITTED',
            provider_message_id='gupshup_msg_test_001',
            error_code='',
            error_message='',
            raw_response={'status': 'submitted', 'messageId': 'gupshup_msg_test_001'},
        )

        session = CallSession.objects.create(
            organization=self.org,
            branch=self.branch,
            lead=self.lead,
            sales_rep=self.rep,
            provider='VOBIZ',
            provider_attempt_id='sess_uuid_001',
            direction='OUTBOUND',
            call_mode='HUMAN_MANUAL',
            status='COMPLETED',
            user_phone_number=self.lead.phone_normalized,
            duration_seconds=0,
        )

        payload = {
            'session_id': str(session.id),
            'disposition': 'NO_ANSWER',
            'disposition_notes': 'Call rang out, no answer.',
            'send_whatsapp': True,
            'whatsapp_message': 'Hi Farhan, I tried reaching you from SWEAT Elite.',
            'caller_phone': self.lead.phone_normalized,
            'lead_id': str(self.lead.id),
            'lead_name': 'Farhan Khan',
            'duration_seconds': 0,
            'direction': 'OUTBOUND',
        }

        res = self.client.post('/api/v1/tenant/crm/telephony/disposition/', data=payload, format='json')
        set_tenant_db_alias('tenant_test')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['whatsapp_sent'])
        self.assertEqual(res.data['whatsapp_message_id'], 'gupshup_msg_test_001')
        self.assertIsNone(res.data['whatsapp_error'])

        # Verify WhatsApp dispatch parameters
        mock_send.assert_called_once()
        kwargs = mock_send.call_args.kwargs
        self.assertEqual(kwargs['recipient'], self.lead.phone_normalized)
        self.assertEqual(kwargs['template_vars'], {'1': 'Farhan'})

        # Verify LeadActivity recorded with SENT outcome
        activity = LeadActivity.objects.filter(lead=self.lead, activity_type='WHATSAPP').first()
        self.assertIsNotNone(activity)
        self.assertEqual(activity.outcome, 'SENT')
        self.assertIn('WhatsApp follow-up dispatched', activity.notes)

    @patch('apps.tenant_core.communication.adapters.whatsapp_gupshup.GupshupWhatsAppAdapter.send')
    def test_02_direct_dial_auto_creates_lead_and_sends_whatsapp(self, mock_send):
        """Direct dial to an unregistered number auto-creates a NEW_LEAD and delivers WhatsApp outreach."""
        mock_send.return_value = MagicMock(
            success=True,
            status='SUBMITTED',
            provider_message_id='gupshup_direct_002',
            error_code='',
            error_message='',
            raw_response={'status': 'submitted'},
        )

        new_number = '+919876543210'
        new_sess_id = str(uuid.uuid4())
        payload = {
            'session_id': new_sess_id,
            'disposition': 'BUSY',
            'disposition_notes': 'Customer busy, sent follow-up',
            'send_whatsapp': True,
            'caller_phone': new_number,
            'lead_name': 'Direct Prospect',
            'duration_seconds': 0,
            'direction': 'OUTBOUND',
        }

        res = self.client.post('/api/v1/tenant/crm/telephony/disposition/', data=payload, format='json')
        set_tenant_db_alias('tenant_test')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['whatsapp_sent'])

        # Verify lead auto-created with status NEW_LEAD
        created_lead = Lead.objects.filter(phone_normalized__icontains='9876543210').first()
        self.assertIsNotNone(created_lead)
        self.assertEqual(created_lead.current_status, 'NEW_LEAD')
        self.assertEqual(created_lead.first_name, 'Direct')

        # Verify WhatsApp activity logged against the new lead
        activity = LeadActivity.objects.filter(lead=created_lead, activity_type='WHATSAPP').first()
        self.assertIsNotNone(activity)
        self.assertEqual(activity.outcome, 'SENT')

    @patch('apps.tenant_core.communication.adapters.whatsapp_gupshup.GupshupWhatsAppAdapter.send')
    def test_03_whatsapp_failure_returns_meaningful_error_no_silent_failure(self, mock_send):
        """When WhatsApp dispatch fails at provider, CRM returns actual error and logs FAILED activity."""
        mock_send.return_value = MagicMock(
            success=False,
            status='FAILED',
            provider_message_id=None,
            error_code='INVALID_RECIPIENT',
            error_message='Recipient phone number is invalid or not on WhatsApp.',
            raw_response={'error': 'INVALID_RECIPIENT'},
        )

        session = CallSession.objects.create(
            organization=self.org,
            branch=self.branch,
            lead=self.lead,
            sales_rep=self.rep,
            provider='VOBIZ',
            provider_attempt_id='sess_fail_003',
            direction='OUTBOUND',
            call_mode='HUMAN_MANUAL',
            status='COMPLETED',
            user_phone_number=self.lead.phone_normalized,
        )

        payload = {
            'session_id': str(session.id),
            'disposition': 'NO_ANSWER',
            'send_whatsapp': True,
            'caller_phone': self.lead.phone_normalized,
            'lead_id': str(self.lead.id),
        }

        res = self.client.post('/api/v1/tenant/crm/telephony/disposition/', data=payload, format='json')
        set_tenant_db_alias('tenant_test')
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data['whatsapp_sent'])
        self.assertIn('Recipient phone number is invalid', res.data['whatsapp_error'])

        # Verify LeadActivity logs FAILED outcome with actual reason
        activity = LeadActivity.objects.filter(lead=self.lead, activity_type='WHATSAPP').first()
        self.assertIsNotNone(activity)
        self.assertEqual(activity.outcome, 'FAILED')
        self.assertIn('Recipient phone number is invalid', activity.notes)

    def test_04_phone_normalization_adds_91_to_10_digit_indian_numbers(self):
        """10-digit Indian numbers are normalized with 91 country code prefix."""
        adapter = GupshupWhatsAppAdapter()
        self.assertEqual(adapter._normalize_phone('7738678323'), '917738678323')
        self.assertEqual(adapter._normalize_phone('+917738678323'), '917738678323')
        self.assertEqual(adapter._normalize_phone('07738678323'), '917738678323')
        self.assertEqual(adapter._normalize_phone('917738678323'), '917738678323')

    @patch('apps.tenant_core.communication.adapters.whatsapp_gupshup.GupshupWhatsAppAdapter.send')
    def test_05_idempotency_prevents_duplicate_whatsapp_when_already_submitted(self, mock_send):
        """Resubmitting the same session does not send duplicate WhatsApp if already submitted."""
        mock_send.return_value = MagicMock(
            success=True,
            status='SUBMITTED',
            provider_message_id='gupshup_idemp_005',
            error_code='',
            error_message='',
        )

        session = CallSession.objects.create(
            organization=self.org,
            branch=self.branch,
            lead=self.lead,
            sales_rep=self.rep,
            provider='VOBIZ',
            provider_attempt_id='sess_idemp_005',
            direction='OUTBOUND',
            status='COMPLETED',
            user_phone_number=self.lead.phone_normalized,
        )

        # First dispatch
        msg1 = WhatsAppAutomationService.send_unanswered_call_followup(session=session, lead=self.lead)
        self.assertIsNotNone(msg1)
        self.assertEqual(mock_send.call_count, 1)

        # Second dispatch with same session -> idempotency returns existing, no second HTTP call
        msg2 = WhatsAppAutomationService.send_unanswered_call_followup(session=session, lead=self.lead)
        self.assertEqual(msg1.id, msg2.id)
        self.assertEqual(mock_send.call_count, 1)

    @patch('apps.tenant_core.communication.adapters.whatsapp_gupshup.GupshupWhatsAppAdapter.send')
    def test_06_trial_booking_dispatches_whatsapp_confirmation(self, mock_send):
        """Booking a trial via CRMLeadService dispatches WhatsApp confirmation with approved template."""
        mock_send.return_value = MagicMock(
            success=True,
            status='SUBMITTED',
            provider_message_id='gupshup_tb_006',
            error_code='',
            error_message='',
        )

        self.policy.whatsapp_enabled = True
        self.policy.trial_confirmation_enabled = True
        self.policy.trial_confirmation_template_name = 'sweat_trial_booking_confirm'
        self.policy.save()

        trial = CRMLeadService.book_trial(
            lead=self.lead,
            branch=self.branch,
            class_occurrence_id=self.occurrence.id,
            scheduled_start=self.occurrence.start_at,
            trial_type='GROUP_CLASS',
            booking_source='WEB',
            actor_user=self.rep,
            db_alias='tenant_test',
        )

        self.assertIsNotNone(trial)
        self.assertTrue(getattr(trial, '_whatsapp_sent', False))
        self.assertEqual(getattr(trial, '_whatsapp_message_id', None), 'gupshup_tb_006')

        # Verify adapter was called with template 'sweat_trial_booking_confirm' and 4 parameters
        self.assertEqual(mock_send.call_count, 1)
        call_kwargs = mock_send.call_args[1]
        self.assertEqual(call_kwargs.get('template_ref'), 'sweat_trial_booking_confirm')
        params = call_kwargs.get('template_vars', [])
        self.assertEqual(len(params), 4)
        self.assertEqual(params[0], 'Farhan')
        self.assertEqual(params[2], 'Andheri West')

        # Verify LeadActivity was recorded
        activity = LeadActivity.objects.filter(lead=self.lead, activity_type='WHATSAPP').first()
        self.assertIsNotNone(activity)
        self.assertEqual(activity.outcome, 'SENT')
        self.assertIn('sweat_trial_booking_confirm', activity.notes)

    @patch('apps.tenant_core.communication.adapters.whatsapp_gupshup.GupshupWhatsAppAdapter.send')
    def test_07_trial_booking_succeeds_even_if_whatsapp_fails(self, mock_send):
        """Trial booking succeeds and database record persists even if WhatsApp provider throws an error."""
        mock_send.side_effect = Exception("WhatsApp provider timeout or connection failure")

        self.policy.whatsapp_enabled = True
        self.policy.trial_confirmation_enabled = True
        self.policy.save()

        occ2_start = timezone.now() + timezone.timedelta(days=2)
        occ2 = ClassOccurrence.objects.create(
            class_template=self.class_template,
            branch=self.branch,
            occurrence_date=occ2_start.date(),
            start_at=occ2_start,
            end_at=occ2_start + timezone.timedelta(minutes=60),
            capacity=10,
            trial_capacity=5,
            status="SCHEDULED",
        )
        trial = CRMLeadService.book_trial(
            lead=self.lead,
            branch=self.branch,
            class_occurrence_id=occ2.id,
            scheduled_start=occ2.start_at,
            trial_type='GROUP_CLASS',
            booking_source='WEB',
            actor_user=self.rep,
            db_alias='tenant_test',
        )

        # Booking is successful and committed in database
        self.assertIsNotNone(trial)
        self.assertIsNotNone(trial.id)
        db_trial = TrialBooking.objects.filter(id=trial.id).first()
        self.assertIsNotNone(db_trial)
        self.assertEqual(db_trial.status, 'BOOKED')

        # WhatsApp status flag indicates non-fatal failure
        self.assertFalse(getattr(trial, '_whatsapp_sent', True))
        self.assertIn('WhatsApp provider timeout', getattr(trial, '_whatsapp_error', ''))
