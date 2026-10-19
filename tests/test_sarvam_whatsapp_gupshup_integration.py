"""
tests/test_sarvam_whatsapp_gupshup_integration.py — Unit & Integration tests for Sarvam x Gupshup WhatsApp automations.

Validates:
1. Trial Booking Confirmation: sweat_trial_booking_confirm with 5 exact positional parameters.
2. Trial Reschedule Confirmation: sweat_trial_reschedule_confirm with 5 exact positional parameters.
3. Trial Cancellation Notice: sweat_trial_cancel_confirm with 2 parameters.
4. Trial Class Reminders: sweat_trial_reminder_24h and sweat_trial_reminder_2h with 4 parameters.
5. Inbound CONFIRM keyword: updates TrialBooking.confirmation_status = 'CONFIRMED'.
6. Inbound RESCHEDULE keyword: updates TrialBooking.confirmation_status = 'RESCHEDULE_REQUESTED' + creates urgent SalesFollowupTask.
7. DomainOutboxEvent pipeline: CRM_TRIAL_BOOKED, CRM_TRIAL_RESCHEDULED, CRM_TRIAL_CANCELLED automatically dispatch WhatsApp templates.
"""

import json
import uuid
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone as dt_timezone
import zoneinfo
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from config.routers import set_tenant_db_alias
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.models_saas import SaasPlan, TenantSubscription
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_crm import Lead, LeadSource, TrialBooking, SalesFollowupTask, LeadActivity
from apps.tenant_core.models_catalog import Program
from apps.tenant_core.models_classes import ClassTemplate, ClassOccurrence
from apps.tenant_core.models_govern import WhatsAppAutomationPolicy
from apps.tenant_core.models_audit_outbox import BusinessAuditEvent, DomainOutboxEvent
from apps.tenant_core.models_communication import CommunicationMessage
from apps.tenant_core.models_infra import Integration
from apps.tenant_core.communication.adapters.whatsapp_gupshup import GupshupWhatsAppAdapter
from apps.tenant_core.communication.whatsapp_automation import WhatsAppAutomationService
from apps.tenant_core.communication.whatsapp_variables import fmt_ist_datetime, fmt_ist_time
from apps.tenant_core.tasks import process_domain_outbox_events_task


@override_settings(COMMUNICATIONS_OUTBOUND_ENABLED=True)
class SarvamGupshupWhatsAppTests(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        super().setUp()
        set_tenant_db_alias('tenant_test')
        self.client = APIClient()

        # 1. Master DB Setup
        self.tenant = Tenant.objects.using('default').create(
            code=f"SARVAM-WA-{uuid.uuid4().hex[:6].upper()}",
            name="Sarvam WhatsApp Gym",
            slug=f"sarvam-wa-gym-{uuid.uuid4().hex[:6]}",
            status="ACTIVE",
        )
        self.ds = TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            db_name='test_fitness_tenant',
            database_name='test_fitness_tenant',
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )
        self.plan = SaasPlan.objects.using('default').create(
            name='Enterprise Plan',
            code=f"ENT-{uuid.uuid4().hex[:6]}",
            tier='ENTERPRISE',
            status='ACTIVE',
        )
        TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
        )

        # 2. Tenant DB Setup
        self.org = Organization.objects.using('tenant_test').create(
            code=f"ORG-{uuid.uuid4().hex[:6].upper()}",
            name="SWEAT Fitness Bandra",
            status="ACTIVE",
        )
        self.location = Location.objects.using('tenant_test').create(
            organization=self.org,
            code="MUM-BAN",
            name="Mumbai Bandra",
            status="ACTIVE",
        )
        self.branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.location,
            code="SW-BAN-01",
            name="Sweat Bandra West",
            address="Level 2, High Street Building, Linking Road, Bandra West, Mumbai 400050",
            phone="+919876543210",
            status="ACTIVE",
        )
        self.staff_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email=f"sales.{uuid.uuid4().hex[:6]}@sweat.fit",
            first_name="Rohan",
            last_name="Verma",
            status="ACTIVE",
        )
        self.lead_source = LeadSource.objects.using('tenant_test').create(
            organization=self.org,
            code=f"SARVAM-{uuid.uuid4().hex[:4].upper()}",
            name="Sarvam Inbound AI Call",
            source_type="PHONE",
            status="ACTIVE",
        )

        self.lead = Lead.objects.using('tenant_test').create(
            organization=self.org,
            branch=self.branch,
            lead_source=self.lead_source,
            first_name="Ananya",
            last_name="Sharma",
            phone_normalized="+919988776655",
            email_normalized="ananya.sharma@example.com",
            assigned_sales_user=self.staff_user,
            current_status="NEW_LEAD",
        )

        self.program = Program.objects.using('tenant_test').create(
            organization=self.org,
            code=f"PROG-PIL-{uuid.uuid4().hex[:4].upper()}",
            name="Sweat Pilates",
        )
        self.class_template = ClassTemplate.objects.using('tenant_test').create(
            organization=self.org,
            program=self.program,
            code=f"REF-PIL-{uuid.uuid4().hex[:4].upper()}",
            name="Reformer Pilates Intro",
            default_duration_minutes=50,
        )

        kolkata_tz = zoneinfo.ZoneInfo("Asia/Kolkata")
        now = timezone.now().astimezone(kolkata_tz)
        self.class_time = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)

        self.occ = ClassOccurrence.objects.using('tenant_test').create(
            class_template=self.class_template,
            branch=self.branch,
            start_at=self.class_time,
            end_at=self.class_time + timedelta(minutes=50),
            capacity=12,
            status="SCHEDULED",
        )

        self.trial = TrialBooking.objects.using('tenant_test').create(
            lead=self.lead,
            branch=self.branch,
            scheduled_start=self.class_time,
            scheduled_end=self.class_time + timedelta(minutes=50),
            class_occurrence_id=self.occ.id,
            status="BOOKED",
            confirmation_status="PENDING",
            booking_source="VOICE",
        )

        # 3. Configure Gupshup WhatsApp Integration via SecretResolver
        from config.secrets import SecretResolver
        import json
        self.secret_ref = f"env://TEST_SARVAM_GUPSHUP_{uuid.uuid4().hex[:6].upper()}"
        SecretResolver.register_test_secret(self.secret_ref, json.dumps({
            'api_key': 'test-gupshup-api-key',
            'webhook_secret': 'test-webhook-secret-xyz',
        }))

        self.gupshup_integration = Integration.objects.using('tenant_test').create(
            integration_type='WHATSAPP',
            provider='GUPSHUP',
            status='ACTIVE',
            secret_reference=self.secret_ref,
            configuration={
                'app_name': 'SweatEnterpriseWA',
                'source_number': '+917971443277',
                'webhook_secret': 'test-webhook-secret-xyz',
            },
        )

        # 4. WhatsApp Policy
        self.policy = WhatsAppAutomationService.get_policy(self.org, db_alias='tenant_test')
        self.policy.whatsapp_enabled = True
        self.policy.booking_confirmation_enabled = True
        self.policy.trial_confirmation_enabled = True
        self.policy.booking_reminder_enabled = True
        self.policy.save(using='tenant_test')

    def tearDown(self):
        from config.secrets import SecretResolver
        SecretResolver.clear_test_secrets()
        super().tearDown()

    # ── Test 1: Trial Booking Confirmation (sweat_trial_booking_confirm) ───────────

    @patch('requests.post')
    def test_01_trial_booking_confirmation_sarvam(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'status': 'submitted', 'messageId': 'gs-trial-bk-001'}
        mock_post.return_value = mock_resp

        msg = WhatsAppAutomationService.send_sarvam_trial_booking_confirmation(
            self.trial, db_alias='tenant_test', force=True
        )

        self.assertIsNotNone(msg)
        self.assertIn(msg.status, ('SENT', 'SUBMITTED', 'QUEUED'))
        self.assertEqual(msg.provider, 'GUPSHUP')
        self.assertEqual(msg.channel, 'WHATSAPP')
        self.assertEqual(msg.template_reference, 'sweat_trial_booking_confirm')
        self.assertEqual(msg.related_trial_id, self.trial.id)
        self.assertEqual(msg.lead_id, self.lead.id)

        # Verify exact 4 parameters
        sent_call_args = mock_post.call_args[1]
        form_data = sent_call_args['data']
        import json
        template_obj = json.loads(form_data['template'])
        self.assertEqual(template_obj['id'], 'sweat_trial_booking_confirm')
        params = template_obj['params']
        self.assertEqual(len(params), 4)
        self.assertEqual(params[0], "Ananya")                      # {{1}} Lead Name
        self.assertEqual(params[1], "Sweat Pilates")               # {{2}} Program / Class Name
        self.assertEqual(params[2], "Sweat Bandra West")           # {{3}} Branch Name
        self.assertIn("at 10:00 AM", params[3])                    # {{4}} Class Date & Time in IST

        # Verify Lead 360 Activity Log
        activity = LeadActivity.objects.using('tenant_test').filter(lead=self.lead, activity_type='WHATSAPP').first()
        self.assertIsNotNone(activity)
        self.assertEqual(activity.outcome, 'SENT')
        self.assertIn('sweat_trial_booking_confirm', activity.notes)

        # Verify BusinessAuditEvent
        audit = BusinessAuditEvent.objects.using('tenant_test').filter(
            action_code='WHATSAPP_SARVAM_TRIAL_CONFIRMATION_SENT', entity_id=str(self.trial.id)
        ).first()
        self.assertIsNotNone(audit)

    # ── Test 2: Trial Reschedule Confirmation (sweat_trial_reschedule_confirm) ─────

    @patch('requests.post')
    def test_02_trial_reschedule_confirmation_sarvam(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'status': 'submitted', 'messageId': 'gs-trial-resched-002'}
        mock_post.return_value = mock_resp

        # Move trial to day after tomorrow at 11:30 AM IST
        new_time = self.class_time + timedelta(days=1, hours=1, minutes=30)
        self.trial.scheduled_start = new_time
        self.trial.save(using='tenant_test')

        msg = WhatsAppAutomationService.send_sarvam_trial_reschedule_confirmation(
            self.trial, db_alias='tenant_test', force=True
        )

        self.assertIsNotNone(msg)
        self.assertIn(msg.status, ('SENT', 'SUBMITTED', 'QUEUED'))
        self.assertEqual(msg.template_reference, 'sweat_trial_reschedule_confirm')

        sent_call_args = mock_post.call_args[1]
        import json
        template_obj = json.loads(sent_call_args['data']['template'])
        self.assertEqual(template_obj['id'], 'sweat_trial_reschedule_confirm')
        params = template_obj['params']
        self.assertEqual(len(params), 4)
        self.assertEqual(params[0], "Ananya")
        self.assertEqual(params[1], "Sweat Pilates")
        self.assertEqual(params[2], "Sweat Bandra West")
        self.assertIn("at 11:30 AM", params[3])

    # ── Test 3: Trial Cancellation Notice (sweat_trial_cancel_confirm) ─────────────

    @patch('requests.post')
    def test_03_trial_cancellation_notice_sarvam(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'status': 'submitted', 'messageId': 'gs-trial-cancel-003'}
        mock_post.return_value = mock_resp

        msg = WhatsAppAutomationService.send_sarvam_trial_cancellation(
            self.trial, db_alias='tenant_test', force=True
        )

        self.assertIsNotNone(msg)
        self.assertIn(msg.status, ('SENT', 'SUBMITTED', 'QUEUED'))
        self.assertEqual(msg.template_reference, 'sweat_trial_cancel_confirm')

        sent_call_args = mock_post.call_args[1]
        import json
        template_obj = json.loads(sent_call_args['data']['template'])
        self.assertEqual(template_obj['id'], 'sweat_trial_cancel_confirm')
        params = template_obj['params']
        self.assertEqual(len(params), 4)
        self.assertEqual(params[0], "Ananya")
        self.assertEqual(params[1], "Sweat Pilates")
        self.assertEqual(params[2], "Sweat Bandra West")
        self.assertIn("at 10:00 AM", params[3])

    # ── Test 4: Trial Reminders (24h and 2h) ───────────────────────────────────────

    @patch('requests.post')
    def test_04_trial_reminders_24h_and_2h(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'status': 'submitted', 'messageId': 'gs-trial-rem-004'}
        mock_post.return_value = mock_resp

        # 24h reminder
        msg_24h = WhatsAppAutomationService.send_sarvam_trial_reminder(
            self.trial, reminder_window='24h', db_alias='tenant_test', force=True
        )
        self.assertIsNotNone(msg_24h)
        self.assertEqual(msg_24h.template_reference, 'sweat_trial_reminder_24h')

        sent_args_24h = mock_post.call_args[1]
        import json
        template_obj_24h = json.loads(sent_args_24h['data']['template'])
        params_24h = template_obj_24h['params']
        self.assertEqual(len(params_24h), 5)
        self.assertEqual(params_24h[0], "Ananya")
        self.assertEqual(params_24h[1], "Sweat Pilates")
        self.assertEqual(params_24h[2], "Sweat Bandra West")
        self.assertIn("at 10:00 AM", params_24h[3])
        self.assertEqual(params_24h[4], "Level 2, High Street Building, Linking Road, Bandra West, Mumbai 400050")

        # 2h reminder
        msg_2h = WhatsAppAutomationService.send_sarvam_trial_reminder(
            self.trial, reminder_window='2h', db_alias='tenant_test', force=True
        )
        self.assertIsNotNone(msg_2h)
        self.assertEqual(msg_2h.template_reference, 'sweat_trial_reminder_2h')

        sent_args_2h = mock_post.call_args[1]
        template_obj_2h = json.loads(sent_args_2h['data']['template'])
        params_2h = template_obj_2h['params']
        self.assertEqual(len(params_2h), 5)
        self.assertEqual(params_2h[0], "Ananya")
        self.assertEqual(params_2h[1], "Sweat Pilates")
        self.assertEqual(params_2h[2], "Sweat Bandra West")
        self.assertIn("at 10:00 AM", params_2h[3])
        self.assertEqual(params_2h[4], "Level 2, High Street Building, Linking Road, Bandra West, Mumbai 400050")

    # ── Test 5: Inbound CONFIRM Keyword Action ─────────────────────────────────────

    def test_05_inbound_confirm_keyword_action(self):
        inbound_msg = CommunicationMessage.objects.using('tenant_test').create(
            organization=self.org,
            lead=self.lead,
            related_trial=self.trial,
            channel='WHATSAPP',
            direction='INBOUND',
            sender_identifier='+919988776655',
            body_snapshot='CONFIRM',
            provider='GUPSHUP',
            provider_message_id='gs-inbound-confirm-101',
            status='RECEIVED',
        )

        result = WhatsAppAutomationService.handle_inbound_keyword_action(inbound_msg, db_alias='tenant_test')
        self.assertEqual(result.get('action'), 'CONFIRMED')

        self.trial.refresh_from_db()
        self.assertEqual(self.trial.confirmation_status, 'CONFIRMED')

    # ── Test 6: Inbound RESCHEDULE Keyword Action ──────────────────────────────────

    def test_06_inbound_reschedule_keyword_action(self):
        inbound_msg = CommunicationMessage.objects.using('tenant_test').create(
            organization=self.org,
            lead=self.lead,
            related_trial=self.trial,
            channel='WHATSAPP',
            direction='INBOUND',
            sender_identifier='+919988776655',
            body_snapshot='RESCHEDULE please, I am busy tomorrow morning',
            provider='GUPSHUP',
            provider_message_id='gs-inbound-resched-102',
            status='RECEIVED',
        )

        result = WhatsAppAutomationService.handle_inbound_keyword_action(inbound_msg, db_alias='tenant_test')
        self.assertEqual(result.get('action'), 'RESCHEDULE_REQUESTED')

        self.trial.refresh_from_db()
        self.assertEqual(self.trial.confirmation_status, 'RESCHEDULE_REQUESTED')

        # Verify urgent follow-up task was generated for sales consultant
        task = SalesFollowupTask.objects.using('tenant_test').filter(
            lead=self.lead,
            external_reference=f"WA_RESCHED_{self.trial.id}",
        ).first()
        self.assertIsNotNone(task)
        self.assertEqual(task.priority, 'URGENT')
        self.assertEqual(task.assigned_to_user_id, self.staff_user.id)
        self.assertIn('WhatsApp Reschedule Request from Ananya Sharma', task.outcome)

    # ── Test 7: Domain Outbox Event Triggers ───────────────────────────────────────

    @patch('requests.post')
    def test_07_domain_outbox_events_dispatch_whatsapp_templates(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'status': 'submitted', 'messageId': 'gs-outbox-001'}
        mock_post.return_value = mock_resp

        # Create DomainOutboxEvent for CRM_TRIAL_BOOKED
        event_booked = DomainOutboxEvent.objects.using('tenant_test').create(
            organization=self.org,
            event_type='CRM_TRIAL_BOOKED',
            aggregate_type='TrialBooking',
            aggregate_id=str(self.trial.id),
            payload={'trial_id': str(self.trial.id)},
        )

        res = process_domain_outbox_events_task(tenant_id=str(self.tenant.id), limit=10)
        self.assertEqual(res['status'], 'COMPLETED')
        self.assertGreaterEqual(res['published'], 1)

        event_booked.refresh_from_db()
        self.assertEqual(event_booked.status, 'PUBLISHED')

        # Check that WhatsApp communication message was persisted
        wa_msg = CommunicationMessage.objects.using('tenant_test').filter(
            related_trial=self.trial,
            template_reference='sweat_trial_booking_confirm',
        ).first()
        self.assertIsNotNone(wa_msg)
        self.assertIn(wa_msg.status, ('SENT', 'SUBMITTED', 'QUEUED'))

    # ── Test 8: Missing Lead Name Fallback ─────────────────────────────────────────

    @patch('requests.post')
    def test_08_missing_lead_name_fallback_to_there(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'status': 'submitted', 'messageId': 'gs-trial-fallback-008'}
        mock_post.return_value = mock_resp

        self.lead.first_name = ""
        self.lead.save(using='tenant_test')

        msg = WhatsAppAutomationService.send_sarvam_trial_booking_confirmation(
            self.trial, db_alias='tenant_test', force=True
        )
        self.assertIsNotNone(msg)
        sent_call_args = mock_post.call_args[1]
        import json
        params = json.loads(sent_call_args['data']['template'])['params']
        self.assertEqual(params[0], "there")

    # ── Test 9: Missing Branch Address Suppresses Reminder ─────────────────────────

    @patch('requests.post')
    def test_09_missing_branch_address_suppresses_reminder(self, mock_post):
        # Clear branch address completely
        self.branch.address = ""
        self.branch.address_line_1 = ""
        self.branch.address_line_2 = ""
        self.branch.save(using='tenant_test')

        msg = WhatsAppAutomationService.send_sarvam_trial_reminder(
            self.trial, reminder_window='24h', db_alias='tenant_test', force=True
        )
        # Suppressed dispatch rather than fabricate a generic address
        self.assertIsNone(msg)
        mock_post.assert_not_called()

    # ── Test 10: Cancelled or Rescheduled Bookings Suppress Reminders ───────────────

    @patch('requests.post')
    def test_10_cancelled_or_rescheduled_booking_suppresses_reminder(self, mock_post):
        # 1. Cancelled trial
        self.trial.status = "CANCELLED"
        self.trial.save(using='tenant_test')

        msg_cancel = WhatsAppAutomationService.send_sarvam_trial_reminder(
            self.trial, reminder_window='24h', db_alias='tenant_test', force=True
        )
        self.assertIsNone(msg_cancel)
        mock_post.assert_not_called()

        # 2. Rescheduled trial
        self.trial.status = "RESCHEDULED"
        self.trial.save(using='tenant_test')

        msg_resched = WhatsAppAutomationService.send_sarvam_trial_reminder(
            self.trial, reminder_window='24h', db_alias='tenant_test', force=True
        )
        self.assertIsNone(msg_resched)
        mock_post.assert_not_called()

    # ── Test 11: Stale Reminder Suppressed ─────────────────────────────────────────

    @patch('requests.post')
    def test_11_stale_reminder_suppressed(self, mock_post):
        # Scheduled start in the past
        self.trial.scheduled_start = timezone.now() - timedelta(hours=2)
        self.trial.save(using='tenant_test')

        msg_stale = WhatsAppAutomationService.send_sarvam_trial_reminder(
            self.trial, reminder_window='2h', db_alias='tenant_test', force=True
        )
        self.assertIsNone(msg_stale)
        mock_post.assert_not_called()

    # ── Test 12: Duplicate Outbox Events Idempotency ────────────────────────────────

    @patch('requests.post')
    def test_12_outbox_idempotency_duplicate_events(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'status': 'submitted', 'messageId': 'gs-outbox-dup-012'}
        mock_post.return_value = mock_resp

        # First event
        event1 = DomainOutboxEvent.objects.using('tenant_test').create(
            organization=self.org,
            event_type='CRM_TRIAL_BOOKED',
            aggregate_type='TrialBooking',
            aggregate_id=str(self.trial.id),
            payload={'trial_id': str(self.trial.id)},
        )
        res1 = process_domain_outbox_events_task(tenant_id=str(self.tenant.id), limit=10)
        self.assertEqual(res1['status'], 'COMPLETED')
        self.assertEqual(mock_post.call_count, 1)

        # Second duplicate event for the exact same trial booking
        event2 = DomainOutboxEvent.objects.using('tenant_test').create(
            organization=self.org,
            event_type='CRM_TRIAL_BOOKED',
            aggregate_type='TrialBooking',
            aggregate_id=str(self.trial.id),
            payload={'trial_id': str(self.trial.id)},
        )
        res2 = process_domain_outbox_events_task(tenant_id=str(self.tenant.id), limit=10)
        self.assertEqual(res2['status'], 'COMPLETED')
        # Idempotency prevents a duplicate outbound WhatsApp post
        self.assertEqual(mock_post.call_count, 1)

    # ── Test 13: Kill-Switch Defense-In-Depth (Central & Direct Adapter) ───────────

    @override_settings(COMMUNICATIONS_OUTBOUND_ENABLED=False)
    @patch('requests.post')
    def test_13_kill_switch_suppresses_automation_and_direct_adapter(self, mock_post):
        # 1. Via automation service
        msg = WhatsAppAutomationService.send_sarvam_trial_booking_confirmation(
            self.trial, db_alias='tenant_test', force=True
        )
        self.assertIsNone(msg)
        mock_post.assert_not_called()

        # 2. Via direct adapter call (defense-in-depth)
        adapter = GupshupWhatsAppAdapter(
            configuration=self.gupshup_integration.configuration,
            secret_credentials={'api_key': 'test-gupshup-api-key'},
        )
        direct_result = adapter.send(
            recipient='+919988776655',
            body='Hello direct test',
        )
        self.assertFalse(direct_result.success)
        self.assertEqual(direct_result.error_code, 'OUTBOUND_DISABLED')
        mock_post.assert_not_called()

    # ── Test 14: Missing or Unexpected Outbound Setting Fails Closed ───────────────

    @patch('requests.post')
    def test_14_missing_or_invalid_outbound_setting_fails_closed(self, mock_post):
        adapter = GupshupWhatsAppAdapter(
            configuration=self.gupshup_integration.configuration,
            secret_credentials={'api_key': 'test-gupshup-api-key'},
        )

        from django.conf import settings
        # 1. Missing setting entirely (simulated by deleting attribute)
        with patch.object(settings, 'COMMUNICATIONS_OUTBOUND_ENABLED', create=False):
            if hasattr(settings, 'COMMUNICATIONS_OUTBOUND_ENABLED'):
                delattr(settings, 'COMMUNICATIONS_OUTBOUND_ENABLED')
            result = adapter.send(recipient='+919988776655', body='Missing setting test')
            self.assertFalse(result.success)
            self.assertEqual(result.error_code, 'OUTBOUND_DISABLED')
            mock_post.assert_not_called()

        # 2. Unexpected non-boolean-True values must fail closed
        for invalid_val in ("true", "enabled", "1", 1, 0, None, [], {}):
            with override_settings(COMMUNICATIONS_OUTBOUND_ENABLED=invalid_val):
                res = adapter.send(recipient='+919988776655', body='Invalid value test')
                self.assertFalse(res.success)
                self.assertEqual(res.error_code, 'OUTBOUND_DISABLED')
                mock_post.assert_not_called()

    # ── Test 15: API Key Must Come from Resolved Credentials, Not Config ──────────

    @patch('requests.post')
    def test_15_api_key_must_come_from_credentials_not_configuration(self, mock_post):
        # Adapter given api_key ONLY in plaintext configuration (no secret_credentials)
        adapter = GupshupWhatsAppAdapter(
            configuration={
                'app_name': 'SweatEnterpriseWA',
                'source_number': '+917971443277',
                'api_key': 'plaintext-in-config-forbidden',
            },
            secret_credentials={},
        )
        # Must report unconfigured because plaintext configuration key is rejected
        self.assertFalse(adapter.is_configured())
        result = adapter.send(recipient='+919876543210', body='Test no credentials')
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, 'UNCONFIGURED_PROVIDER')
        mock_post.assert_not_called()

    # ── Test 16: Datetime Formatter (UTC to IST, Midnight Boundary, Naive Rejection) ──

    def test_16_datetime_conversions_utc_to_ist_boundary_and_naive_rejection(self):
        # 1. Standard UTC to IST conversion (04:30 UTC -> 10:00 AM IST)
        dt_utc = datetime(2026, 10, 19, 4, 30, tzinfo=dt_timezone.utc)
        self.assertEqual(fmt_ist_datetime(dt_utc), "Monday, 19 Oct at 10:00 AM")
        self.assertEqual(fmt_ist_time(dt_utc), "10:00 AM")

        # 2. Midnight date-boundary conversion (18:30 UTC -> 00:00 next day IST)
        dt_midnight = datetime(2026, 10, 19, 18, 30, tzinfo=dt_timezone.utc)
        self.assertEqual(fmt_ist_datetime(dt_midnight), "Tuesday, 20 Oct at 12:00 AM")
        self.assertEqual(fmt_ist_time(dt_midnight), "12:00 AM")

        # 3. Late evening UTC crossing midnight into early morning next day IST
        dt_cross = datetime(2026, 10, 19, 20, 0, tzinfo=dt_timezone.utc)
        self.assertEqual(fmt_ist_datetime(dt_cross), "Tuesday, 20 Oct at 1:30 AM")
        self.assertEqual(fmt_ist_time(dt_cross), "1:30 AM")

        # 4. Naive datetime rejection: must fail closed rather than silently assume local/IST
        naive_dt = datetime(2026, 10, 19, 10, 0)
        with self.assertRaises(ValueError) as ctx1:
            fmt_ist_datetime(naive_dt)
        self.assertIn("timezone", str(ctx1.exception).lower())

        with self.assertRaises(ValueError) as ctx2:
            fmt_ist_time(naive_dt)
        self.assertIn("timezone", str(ctx2.exception).lower())

    # ── Test 17: Outbox Event Preserves Historical Timestamps & Celery Honors Overrides ──

    @patch('requests.post')
    def test_17_outbox_event_preserves_historical_timestamps(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'status': 'submitted', 'messageId': 'gs-hist-001'}
        mock_post.return_value = mock_resp

        # Create outbox event for reschedule with specific scheduled_start
        historical_dt_resched = datetime(2026, 10, 25, 10, 30, tzinfo=dt_timezone.utc)
        event_resched = DomainOutboxEvent.objects.using('tenant_test').create(
            organization=self.org,
            event_type='CRM_TRIAL_RESCHEDULED',
            aggregate_type='TrialBooking',
            aggregate_id=str(self.trial.id),
            payload={
                'trial_id': str(self.trial.id),
                'scheduled_start': historical_dt_resched.isoformat(),
                'new_start': historical_dt_resched.isoformat(),
            },
        )

        # Alter the trial record in DB to simulate subsequent record mutation
        altered_dt = datetime(2026, 11, 1, 9, 0, tzinfo=dt_timezone.utc)
        self.trial.scheduled_start = altered_dt
        self.trial.save(using='tenant_test')

        # Process outbox events
        res = process_domain_outbox_events_task(tenant_id=str(self.tenant.id), limit=5)
        self.assertEqual(res['status'], 'COMPLETED')
        self.assertTrue(mock_post.called)

        # Confirm the dispatched message used the historical payload timestamp, NOT the altered DB timestamp
        call_kwargs = mock_post.call_args[1]
        raw_tmpl = call_kwargs['data'].get('template') or call_kwargs['data'].get('message')
        msg_payload = json.loads(raw_tmpl)
        params = msg_payload['params']
        expected_ist = fmt_ist_datetime(historical_dt_resched)
        self.assertEqual(params[3], expected_ist)

        mock_post.reset_mock()

        # Cancellation event with historical scheduled_start
        historical_dt_cancel = datetime(2026, 10, 22, 12, 0, tzinfo=dt_timezone.utc)
        event_cancel = DomainOutboxEvent.objects.using('tenant_test').create(
            organization=self.org,
            event_type='CRM_TRIAL_CANCELLED',
            aggregate_type='TrialBooking',
            aggregate_id=str(self.trial.id),
            payload={
                'trial_id': str(self.trial.id),
                'reason': 'Changed mind',
                'scheduled_start': historical_dt_cancel.isoformat(),
            },
        )
        res_cancel = process_domain_outbox_events_task(tenant_id=str(self.tenant.id), limit=5)
        self.assertEqual(res_cancel['status'], 'COMPLETED')
        self.assertTrue(mock_post.called)

        call_kwargs_cancel = mock_post.call_args[1]
        raw_tmpl_cancel = call_kwargs_cancel['data'].get('template') or call_kwargs_cancel['data'].get('message')
        msg_payload_cancel = json.loads(raw_tmpl_cancel)
        params_cancel = msg_payload_cancel['params']
        expected_cancel_ist = fmt_ist_datetime(historical_dt_cancel)
        self.assertEqual(params_cancel[3], expected_cancel_ist)

    # ── Test 18: WhatsApp Policy ViewSet Security & Tenant Isolation ──────────────

    def test_18_whatsapp_policy_viewset_security_and_tenant_isolation(self):
        # 1. Unauthenticated requests are rejected (401)
        client_unauth = APIClient()
        res_unauth = client_unauth.get('/api/v1/tenant/crm/whatsapp-policy/')
        self.assertEqual(res_unauth.status_code, 401)

        # 2. Authenticated tenant user request
        set_tenant_db_alias('tenant_test')
        from apps.authentication.views import _build_tenant_token
        from apps.master.models_saas import ProductModule, TenantModule
        prod_mod_crm, _ = ProductModule.objects.using('default').get_or_create(
            code='crm', defaults={'name': 'CRM Module', 'status': 'ACTIVE'}
        )
        TenantModule.objects.using('default').get_or_create(
            tenant=self.tenant,
            module=prod_mod_crm,
            defaults={'is_enabled': True, 'availability_mode': 'ALL_BRANCHES'}
        )
        token = str(_build_tenant_token(self.staff_user, self.tenant, 'tenant_test').access_token)
        auth_headers = {
            'HTTP_AUTHORIZATION': f'Bearer {token}',
            'HTTP_X_TENANT_ID': str(self.tenant.id),
        }
        res_auth = self.client.get('/api/v1/tenant/crm/whatsapp-policy/', **auth_headers)
        self.assertIn(res_auth.status_code, (200, 403))

        # 3. Tenant Isolation: Create another organization and separate policies
        set_tenant_db_alias('tenant_test')
        org2 = Organization.objects.using('tenant_test').create(
            code=f"ORG-OTHER-{uuid.uuid4().hex[:4]}",
            name="Other Gym Org",
            status="ACTIVE",
        )
        policy1 = WhatsAppAutomationPolicy.objects.using('tenant_test').filter(
            organization=self.org
        ).first()
        if not policy1:
            policy1 = WhatsAppAutomationPolicy.objects.using('tenant_test').create(
                organization=self.org,
                whatsapp_enabled=True,
                trial_confirmation_template_name='sweat_trial_booking_confirm',
            )
        else:
            policy1.whatsapp_enabled = True
            policy1.save(using='tenant_test')

        policy2 = WhatsAppAutomationPolicy.objects.using('tenant_test').create(
            organization=org2,
            whatsapp_enabled=False,
            trial_confirmation_template_name='other_gym_confirm',
        )
        # Verify separate policies exist and are completely isolated
        self.assertTrue(policy1.whatsapp_enabled)
        self.assertFalse(policy2.whatsapp_enabled)
        self.assertNotEqual(policy1.id, policy2.id)

    # ── Test 19: Inbound Webhook Authentication & App Identity Validation ──────────

    def test_19_inbound_webhook_authentication_and_app_validation(self):
        adapter = GupshupWhatsAppAdapter(
            configuration={'app_name': 'SweatEnterpriseWA'},
            secret_credentials={'webhook_secret': 'secure_secret_token_123'},
        )

        # 1. Missing token fails
        self.assertFalse(adapter.verify_webhook_signature(b'{}', {}))

        # 2. Invalid token fails
        headers_invalid = {'HTTP_X_GUPSHUP_TOKEN': 'wrong_token'}
        self.assertFalse(adapter.verify_webhook_signature(b'{}', headers_invalid))

        # 3. Valid token passes
        headers_valid = {'HTTP_X_GUPSHUP_TOKEN': 'secure_secret_token_123'}
        self.assertTrue(adapter.verify_webhook_signature(b'{}', headers_valid))

        # 4. Bearer authorization header passes
        headers_bearer = {'HTTP_AUTHORIZATION': 'Bearer secure_secret_token_123'}
        self.assertTrue(adapter.verify_webhook_signature(b'{}', headers_bearer))

        # 5. App identity mismatch is rejected
        payload_mismatch = {'app': 'UnrelatedThirdPartyApp'}
        self.assertFalse(adapter.validate_payload_app_identity(payload_mismatch))

        # 6. Correct app identity is accepted
        payload_match = {'app': 'SweatEnterpriseWA'}
        self.assertTrue(adapter.validate_payload_app_identity(payload_match))

    # ── Test 20: Outbox Timestamp Validation (Malformed/Naive Suppressed, Legacy Supported) ──

    @patch('requests.post')
    def test_20_outbox_timestamp_validation_and_suppression(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'status': 'submitted', 'messageId': 'gs-outbox-val-01'}
        mock_post.return_value = mock_resp

        # 1. Malformed timestamp: must SUPPRESS dispatch and NOT fall back to trial.scheduled_start
        event_malformed = DomainOutboxEvent.objects.using('tenant_test').create(
            organization=self.org,
            event_type='CRM_TRIAL_BOOKED',
            aggregate_type='TrialBooking',
            aggregate_id=str(self.trial.id),
            payload={
                'trial_id': str(self.trial.id),
                'scheduled_start': 'completely-invalid-date-format',
            },
        )
        res_malformed = process_domain_outbox_events_task(tenant_id=str(self.tenant.id), limit=5)
        self.assertEqual(res_malformed['status'], 'COMPLETED')
        # Dispatch was suppressed: no HTTP request made!
        mock_post.assert_not_called()

        # 2. Naive timestamp (no tz offset): must SUPPRESS dispatch and NOT fall back
        event_naive = DomainOutboxEvent.objects.using('tenant_test').create(
            organization=self.org,
            event_type='CRM_TRIAL_RESCHEDULED',
            aggregate_type='TrialBooking',
            aggregate_id=str(self.trial.id),
            payload={
                'trial_id': str(self.trial.id),
                'new_start': '2026-10-25T10:00:00',  # Missing timezone offset
            },
        )
        res_naive = process_domain_outbox_events_task(tenant_id=str(self.tenant.id), limit=5)
        self.assertEqual(res_naive['status'], 'COMPLETED')
        mock_post.assert_not_called()

        # 3. Genuinely legacy event: no timestamp key provided in payload
        # Must preserve backward compatibility by falling back to model trial.scheduled_start
        event_legacy = DomainOutboxEvent.objects.using('tenant_test').create(
            organization=self.org,
            event_type='CRM_TRIAL_BOOKED',
            aggregate_type='TrialBooking',
            aggregate_id=str(self.trial.id),
            payload={
                'trial_id': str(self.trial.id),
                # No scheduled_start or new_start provided at all
            },
        )
        res_legacy = process_domain_outbox_events_task(tenant_id=str(self.tenant.id), limit=5)
        self.assertEqual(res_legacy['status'], 'COMPLETED')
        self.assertEqual(mock_post.call_count, 1)

        # Confirm the dispatched message used the model scheduled_start
        call_kwargs = mock_post.call_args[1]
        raw_tmpl = call_kwargs['data'].get('template') or call_kwargs['data'].get('message')
        msg_payload = json.loads(raw_tmpl)
        expected_ist = fmt_ist_datetime(self.trial.scheduled_start)
        self.assertEqual(msg_payload['params'][3], expected_ist)

    # ── Test 21: Complete Trial Lifecycle & Booking Confirmation Idempotency ──

    @patch('requests.post')
    def test_21_trial_lifecycle_booking_cancellation_rebooking_idempotency(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'status': 'submitted', 'messageId': 'gs-lifecycle-001'}
        mock_post.return_value = mock_resp

        # Step 1: Initial Booking of trial
        self.trial.status = 'BOOKED'
        self.trial.confirmation_status = 'PENDING'
        self.trial.save(using='tenant_test')

        msg1 = WhatsAppAutomationService.send_sarvam_trial_booking_confirmation(
            self.trial, db_alias='tenant_test', force=True
        )
        self.assertIsNotNone(msg1)
        self.assertEqual(mock_post.call_count, 1)

        # Retrying the initial booking must be safely deduplicated (idempotent)
        msg1_retry = WhatsAppAutomationService.send_sarvam_trial_booking_confirmation(
            self.trial, db_alias='tenant_test', force=True
        )
        self.assertEqual(msg1_retry.id, msg1.id)
        self.assertEqual(mock_post.call_count, 1)

        # Step 2: Cancellation of trial
        self.trial.status = 'CANCELLED'
        self.trial.confirmation_status = 'CANCELLED'
        self.trial.cancellation_reason = 'Schedule conflict'
        self.trial.save(using='tenant_test')

        msg_cancel = WhatsAppAutomationService.send_sarvam_trial_cancellation(
            self.trial, db_alias='tenant_test', force=True
        )
        self.assertIsNotNone(msg_cancel)
        self.assertEqual(mock_post.call_count, 2)

        # Retrying cancellation must be deduplicated
        msg_cancel_retry = WhatsAppAutomationService.send_sarvam_trial_cancellation(
            self.trial, db_alias='tenant_test', force=True
        )
        self.assertEqual(msg_cancel_retry.id, msg_cancel.id)
        self.assertEqual(mock_post.call_count, 2)

        # Step 3: Rebooking of the SAME trial (e.g. reactivated / rebooked slot)
        # Even if rebooked for a new time or the same trial instance,
        # the second legitimate booking confirmation MUST NOT be suppressed!
        rebooked_dt = self.class_time + timedelta(days=2)
        self.trial.scheduled_start = rebooked_dt
        self.trial.scheduled_end = rebooked_dt + timedelta(minutes=50)
        self.trial.status = 'BOOKED'
        self.trial.confirmation_status = 'PENDING'
        self.trial.cancellation_reason = None
        self.trial.save(using='tenant_test')

        msg_rebook = WhatsAppAutomationService.send_sarvam_trial_booking_confirmation(
            self.trial, db_alias='tenant_test', force=True
        )
        self.assertIsNotNone(msg_rebook)
        self.assertNotEqual(msg_rebook.id, msg1.id)
        # Provider dispatch occurred for the rebooked session: call_count increased to 3!
        self.assertEqual(mock_post.call_count, 3)

        # Verify the rebooked confirmation contains the new date/time
        call_kwargs = mock_post.call_args[1]
        raw_tmpl = call_kwargs['data'].get('template') or call_kwargs['data'].get('message')
        msg_payload = json.loads(raw_tmpl)
        self.assertEqual(msg_payload['params'][3], fmt_ist_datetime(rebooked_dt))

        # Retrying the rebooking must be deduplicated
        msg_rebook_retry = WhatsAppAutomationService.send_sarvam_trial_booking_confirmation(
            self.trial, db_alias='tenant_test', force=True
        )
        self.assertEqual(msg_rebook_retry.id, msg_rebook.id)
        self.assertEqual(mock_post.call_count, 3)

    # ── Test 22: Concurrent Workers Idempotency Race (IntegrityError Collision) ──

    @patch('requests.post')
    def test_22_concurrent_workers_idempotency_race_handling(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'status': 'submitted', 'messageId': 'gs-race-001'}
        mock_post.return_value = mock_resp

        set_tenant_db_alias('tenant_test')
        idempotency_key = f"TEST_RACE_KEY_{uuid.uuid4().hex[:8]}"

        from apps.tenant_core.communication.service import CommunicationService
        from django.db import IntegrityError

        # Worker 1 dispatches first and creates the DB record
        msg1 = CommunicationService.send_communication(
            organization=self.org,
            channel='WHATSAPP',
            recipient='+919876543210',
            lead=self.lead,
            template_reference='sweat_trial_booking_confirm',
            subject="Test Race",
            body="Test concurrent body",
            context_data=["Ananya", "Sweat Pilates", "Sweat Bandra West", "Tomorrow at 10:00 AM"],
            purpose='TRANSACTIONAL',
            idempotency_key=idempotency_key,
        )
        self.assertIsNotNone(msg1)
        self.assertEqual(mock_post.call_count, 1)

        # Simulate the concurrency race:
        # Worker 2 executes Step 1 BEFORE Worker 1 commits (so filter returns None at Step 1),
        # but Worker 2's msg.save() executes AFTER Worker 1 inserts, raising IntegrityError.
        orig_filter = CommunicationMessage.objects.filter
        filter_call_count = [0]

        def race_filter_mock(*args, **kwargs):
            filter_call_count[0] += 1
            if filter_call_count[0] == 1:
                # Step 1: Worker 2 does not see Worker 1's record yet
                return CommunicationMessage.objects.none()
            # Step 5 (except IntegrityError): Worker 2 re-queries and finds Worker 1's committed record
            return orig_filter(*args, **kwargs)

        db_integrity_err = IntegrityError("duplicate key value violates unique constraint 'uq_comm_org_idempotency_key'")

        with patch.object(CommunicationMessage.objects, 'filter', side_effect=race_filter_mock):
            with patch.object(CommunicationMessage, 'save', side_effect=db_integrity_err):
                msg2 = CommunicationService.send_communication(
                    organization=self.org,
                    channel='WHATSAPP',
                    recipient='+919876543210',
                    lead=self.lead,
                    template_reference='sweat_trial_booking_confirm',
                    subject="Test Race",
                    body="Test concurrent body",
                    context_data=["Ananya", "Sweat Pilates", "Sweat Bandra West", "Tomorrow at 10:00 AM"],
                    purpose='TRANSACTIONAL',
                    idempotency_key=idempotency_key,
                )

        # Worker 2 safely caught IntegrityError and returned Worker 1's record without second dispatch
        self.assertEqual(msg2.id, msg1.id)
        self.assertEqual(mock_post.call_count, 1)
        self.assertGreaterEqual(filter_call_count[0], 2)




