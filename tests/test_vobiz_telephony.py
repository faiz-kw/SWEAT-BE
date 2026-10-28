"""
backend/tests/test_vobiz_telephony.py — Comprehensive Unit & Integration Tests for Vobiz Telephony.

Validates:
- VobizVoiceAdapter XML generation (inbound, fallback, agent-bridge)
- 60-second billing pulse calculation
- Inbound webhook lead matching and 15s fallback VobizXML
- Hangup webhook calculation (is_meaningful >= 30s) and agent WRAP_UP transition
- Post-call disposition submission, lead stage advance, and AVAILABLE transition
"""

import json
from unittest.mock import patch, MagicMock
from django.test import TestCase, override_settings
from django.utils import timezone

from config.routers import set_tenant_db_alias
from apps.tenant_core.models_org import Organization, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_crm import Lead, CallSession, AgentPresence, LeadActivity
from apps.tenant_core.communication.adapters.voice_vobiz import VobizVoiceAdapter


class VobizTelephonyTests(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        super().setUp()
        set_tenant_db_alias('tenant_test')
        self.org = Organization.objects.create(
            name="SWEAT Elite Andheri",
            slug="sweat-andheri",
            code="SWEAT01",
        )
        self.branch = Branch.objects.create(
            organization=self.org,
            name="Andheri West",
            code="ANDHERI01",
        )
        self.rep = TenantUser.objects.create_user(
            email="priya@sweatelite.in",
            password="TestPassword123!",
            first_name="Priya",
            last_name="Sharma",
        )
        self.rep.organization_id = self.org.id
        self.rep.save()

        self.presence = AgentPresence.objects.create(
            organization=self.org,
            user=self.rep,
            sip_username="agent_priya",
            status="AVAILABLE",
        )

        self.lead = Lead.objects.create(
            organization=self.org,
            branch=self.branch,
            first_name="Vikram",
            last_name="Mehta",
            phone="+919876543210",
            status="NEW_LEAD",
            assigned_to=self.rep,
        )

    def test_vobiz_billing_pulse_calculation(self):
        """Vobiz standard 60-second pulse: rounds up to nearest 60 seconds."""
        self.assertEqual(VobizVoiceAdapter.calculate_billable_duration(0), 0)
        self.assertEqual(VobizVoiceAdapter.calculate_billable_duration(12), 60)
        self.assertEqual(VobizVoiceAdapter.calculate_billable_duration(60), 60)
        self.assertEqual(VobizVoiceAdapter.calculate_billable_duration(61), 120)
        self.assertEqual(VobizVoiceAdapter.calculate_billable_duration(184), 240)

    def test_inbound_xml_generation(self):
        """Generates compliant VobizXML with 15s timeout and fallback action."""
        xml = VobizVoiceAdapter.build_inbound_dial_xml(
            agent_sip_uri="sip:agent_priya@registrar.vobiz.ai",
            caller_id="+919200000001",
            fallback_action_url="https://crm.sweatelite.in/api/v1/telephony/dial-fallback/?session_id=123",
            timeout=15,
        )
        self.assertIn('<Dial', xml)
        self.assertIn('timeout="15"', xml)
        self.assertIn('callerId="+919200000001"', xml)
        self.assertIn('<User>sip:agent_priya@registrar.vobiz.ai</User>', xml)

    def test_agent_bridge_customer_xml_generation(self):
        """Generates whisper tone and bridges customer on PSTN."""
        xml = VobizVoiceAdapter.build_agent_bridge_customer_xml(
            customer_phone="+919876543210",
            caller_id="+919200000001",
            whisper_text="Connecting to Vikram Mehta.",
        )
        self.assertIn('<Speak voice="woman" language="en-IN">Connecting to Vikram Mehta.</Speak>', xml)
        self.assertIn('<Number>+919876543210</Number>', xml)

    def test_inbound_webhook_matches_lead_and_rings_agent(self):
        """Inbound call from known lead matches lead, marks agent ON_CALL, and returns XML."""
        payload = {
            'CallUUID': 'test-uuid-987',
            'From': '+919876543210',
            'To': '+919200000001',
            'Direction': 'inbound',
        }
        res = self.client.post('/api/v1/telephony/inbound/', data=payload)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res['Content-Type'], 'application/xml; charset=utf-8')
        content = res.content.decode('utf-8')
        self.assertIn('sip:agent_priya@registrar.vobiz.ai', content)
        self.assertIn('timeout="15"', content)

        # Verify CallSession created in DB
        session = CallSession.objects.filter(provider_attempt_id='test-uuid-987').first()
        self.assertIsNotNone(session)
        self.assertEqual(session.lead, self.lead)
        self.assertEqual(session.direction, 'INBOUND')
        self.assertEqual(session.call_mode, 'HUMAN_MANUAL')
        self.assertEqual(session.sales_rep, self.rep)

        # Verify agent marked ON_CALL
        self.presence.refresh_from_db()
        self.assertEqual(self.presence.status, 'ON_CALL')

    def test_hangup_webhook_updates_session_and_sets_wrap_up(self):
        """Hangup webhook sets duration, is_meaningful, and puts rep in WRAP_UP status."""
        session = CallSession.objects.create(
            organization=self.org,
            lead=self.lead,
            sales_rep=self.rep,
            provider='VOBIZ',
            provider_attempt_id='test-hangup-uuid',
            direction='INBOUND',
            call_mode='HUMAN_MANUAL',
            status='CONNECTED',
            started_at=timezone.now(),
        )
        self.presence.status = 'ON_CALL'
        self.presence.active_session = session
        self.presence.save()

        payload = {
            'CallUUID': 'test-hangup-uuid',
            'CallStatus': 'completed',
            'Duration': '184',
            'RecordingUrl': 'https://media.vobiz.ai/recordings/rec_98234.mp3',
            'HangupCause': 'NORMAL_CLEARING',
        }
        res = self.client.post('/api/v1/telephony/hangup/', data=payload)
        self.assertEqual(res.status_code, 200)

        session.refresh_from_db()
        self.assertEqual(session.duration_seconds, 184.0)
        self.assertEqual(session.bill_duration_seconds, 240)  # 184s rounded up to 240s
        self.assertTrue(session.is_meaningful)
        self.assertEqual(session.status, 'COMPLETED')
        self.assertEqual(session.recording_url, 'https://media.vobiz.ai/recordings/rec_98234.mp3')

        # Agent should be placed in WRAP_UP
        self.presence.refresh_from_db()
        self.assertEqual(self.presence.status, 'WRAP_UP')

    def test_disposition_submission_advances_lead_and_unlocks_agent(self):
        """Submitting disposition saves notes, updates lead to TRIAL_SCHEDULED, and resets agent to AVAILABLE."""
        session = CallSession.objects.create(
            organization=self.org,
            lead=self.lead,
            sales_rep=self.rep,
            provider='VOBIZ',
            provider_attempt_id='test-disp-uuid',
            direction='OUTBOUND',
            call_mode='HUMAN_MANUAL',
            status='COMPLETED',
            duration_seconds=120,
        )
        self.presence.status = 'WRAP_UP'
        self.presence.active_session = session
        self.presence.save()

        self.client.force_login(self.rep)
        payload = {
            'session_id': str(session.id),
            'disposition': 'TRIAL_BOOKED',
            'disposition_notes': 'Confirmed trial for tomorrow morning 7 AM.',
            'followup_date': '2026-10-28T07:00:00+05:30',
        }
        res = self.client.post('/api/v1/tenant/crm/telephony/disposition/', data=payload, content_type='application/json')
        self.assertEqual(res.status_code, 200)

        session.refresh_from_db()
        self.assertEqual(session.disposition, 'TRIAL_BOOKED')
        self.assertIn('Confirmed trial', session.disposition_notes)

        # Lead stage advanced
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, 'TRIAL_SCHEDULED')

        # Agent presence reset to AVAILABLE
        self.presence.refresh_from_db()
        self.assertEqual(self.presence.status, 'AVAILABLE')
        self.assertIsNone(self.presence.active_session)

        # LeadActivity logged
        activity = LeadActivity.objects.filter(lead=self.lead, activity_type='CALL').first()
        self.assertIsNotNone(activity)
        self.assertIn('Trial Booked', activity.notes)
