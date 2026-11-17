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
import uuid
from unittest.mock import patch, MagicMock
from django.test import TestCase, override_settings
from django.utils import timezone

from config.routers import set_tenant_db_alias
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_crm import Lead, CallSession, AgentPresence, LeadActivity
from apps.tenant_core.communication.adapters.voice_vobiz import VobizVoiceAdapter
from apps.tenant_core.views_telephony import push_agent_telephony_event
from rest_framework.test import APIClient


class VobizTelephonyTests(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        super().setUp()
        set_tenant_db_alias('tenant_test')
        self.org = Organization.objects.create(
            name="SWEAT Elite Andheri",
            code="SWEAT01",
        )
        self.location = Location.objects.create(
            organization=self.org,
            name="Mumbai Andheri",
            code="LOC01",
        )
        self.branch = Branch.objects.create(
            organization=self.org,
            location=self.location,
            name="Andheri West",
            code="ANDHERI01",
        )
        self.rep = TenantUser.objects.create(
            organization=self.org,
            email="priya@sweatelite.in",
            first_name="Priya",
            last_name="Sharma",
        )

        self.presence = AgentPresence.objects.create(
            organization=self.org,
            user=self.rep,
            sip_username="iqra24030269280035568",
            status="AVAILABLE",
        )

        from apps.tenant_core.models_crm import LeadSource
        self.source = LeadSource.objects.create(
            organization=self.org,
            name="Website Direct",
            code="WEB01",
        )
        self.lead = Lead.objects.create(
            organization=self.org,
            branch=self.branch,
            first_name="Vikram",
            last_name="Mehta",
            phone_normalized="+919876543210",
            current_status="NEW_LEAD",
            assigned_sales_user=self.rep,
            lead_source=self.source,
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
        self.assertIn('Connecting to Vikram Mehta.', xml)
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
        set_tenant_db_alias('tenant_test')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res['Content-Type'], 'application/xml; charset=utf-8')
        content = res.content.decode('utf-8')
        self.assertIn('<User>sip:iqra24030269280035568@registrar.vobiz.ai</User>', content)
        self.assertIn('timeout="35"', content)

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
        set_tenant_db_alias('tenant_test')
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

        from rest_framework.test import APIClient
        api_client = APIClient()
        api_client.force_authenticate(user=self.rep)
        payload = {
            'session_id': str(session.id),
            'disposition': 'TRIAL_BOOKED',
            'disposition_notes': 'Confirmed trial for tomorrow morning 7 AM.',
            'followup_date': '2026-10-28T07:00:00+05:30',
        }
        res = api_client.post('/api/v1/tenant/crm/telephony/disposition/', data=payload, format='json')
        set_tenant_db_alias('tenant_test')
        self.assertEqual(res.status_code, 200)

        session.refresh_from_db()
        self.assertEqual(session.disposition, 'TRIAL_BOOKED')
        self.assertIn('Confirmed trial', session.disposition_notes)

        # Lead stage advanced
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.current_status, 'TRIAL_BOOKED')

        # Agent presence reset to AVAILABLE
        self.presence.refresh_from_db()
        self.assertEqual(self.presence.status, 'AVAILABLE')
        self.assertIsNone(self.presence.active_session)

        # LeadActivity logged
        activity = LeadActivity.objects.filter(lead=self.lead, activity_type='CALL').first()
        self.assertIsNotNone(activity)
        self.assertIn('Trial Booked', activity.notes)

    # ── Regression: Redis publish payloads must be JSON serializable ──────────

    def _published_events(self, mock_from_url):
        """Returns (channel, decoded_event) for every Redis publish call."""
        publish = mock_from_url.return_value.publish
        return [(c.args[0], json.loads(c.args[1])) for c in publish.call_args_list]

    @patch('apps.tenant_core.views_telephony.redis.from_url')
    def test_push_event_serializes_uuid_values(self, mock_from_url):
        """A raw UUID in a payload must not block the Redis publish."""
        lead_uuid = uuid.uuid4()
        push_agent_telephony_event(str(self.rep.id), 'INCOMING_CALL_POPUP', {'lead_id': lead_uuid})

        mock_from_url.return_value.publish.assert_called_once()
        _, evt = self._published_events(mock_from_url)[0]
        self.assertEqual(evt['data']['lead_id'], str(lead_uuid))

    @patch('apps.tenant_core.views_telephony.redis.from_url')
    def test_fallback_popup_publishes_lead_id_as_string(self, mock_from_url):
        """Regression: overflow INCOMING_CALL_POPUP failed with 'UUID is not JSON serializable'."""
        secondary = TenantUser.objects.create(
            organization=self.org,
            email="neha@sweatelite.in",
            first_name="Neha",
            last_name="Rao",
        )
        AgentPresence.objects.create(
            organization=self.org,
            user=secondary,
            sip_username="neha0001",
            status="AVAILABLE",
            last_heartbeat=timezone.now(),
        )
        session = CallSession.objects.create(
            organization=self.org,
            lead=self.lead,
            sales_rep=self.rep,
            provider='VOBIZ',
            provider_attempt_id='test-fallback-uuid',
            direction='INBOUND',
            call_mode='HUMAN_MANUAL',
            status='RINGING',
            user_phone_number='+919876543210',
            started_at=timezone.now(),
        )

        res = self.client.post(
            f'/api/v1/telephony/dial-fallback/?session_id={session.id}',
            data={'CallUUID': 'test-fallback-uuid', 'DialStatus': 'no-answer'},
        )
        set_tenant_db_alias('tenant_test')
        self.assertEqual(res.status_code, 200)

        popups = [
            (channel, evt)
            for channel, evt in self._published_events(mock_from_url)
            if evt['event'] == 'INCOMING_CALL_POPUP'
        ]
        self.assertEqual(len(popups), 1)
        channel, evt = popups[0]
        self.assertTrue(channel.endswith(str(secondary.id).lower()))
        self.assertEqual(evt['data']['lead_id'], str(self.lead.id))

    @patch('apps.tenant_core.views_telephony.redis.from_url')
    def test_hangup_call_ended_publishes_lead_id_as_string(self, mock_from_url):
        """Regression: CALL_ENDED failed with 'UUID is not JSON serializable' for lead callers."""
        CallSession.objects.create(
            organization=self.org,
            lead=self.lead,
            sales_rep=self.rep,
            provider='VOBIZ',
            provider_attempt_id='test-ended-uuid',
            direction='INBOUND',
            call_mode='HUMAN_MANUAL',
            status='CONNECTED',
            started_at=timezone.now(),
        )

        res = self.client.post('/api/v1/telephony/hangup/', data={
            'CallUUID': 'test-ended-uuid',
            'CallStatus': 'completed',
            'Duration': '95',
        })
        set_tenant_db_alias('tenant_test')
        self.assertEqual(res.status_code, 200)

        ended = [evt for _, evt in self._published_events(mock_from_url) if evt['event'] == 'CALL_ENDED']
        self.assertEqual(len(ended), 1)
        self.assertEqual(ended[0]['data']['lead_id'], str(self.lead.id))

    # ── Regression: B-leg callbackUrl events must not close the call ──────────

    def test_inbound_xml_callback_url_points_to_dial_events(self):
        """callbackUrl must receive B-leg events, never the hangup webhook."""
        xml = VobizVoiceAdapter.build_inbound_dial_xml(
            agent_sip_uri="sip:agent_priya@registrar.vobiz.ai",
            caller_id="+919200000001",
            fallback_action_url="https://x/api/v1/telephony/dial-fallback/?session_id=1",
            dial_events_url="https://x/api/v1/telephony/dial-events/?session_id=1",
        )
        self.assertIn('callbackUrl="https://x/api/v1/telephony/dial-events/?session_id=1"', xml)
        self.assertNotIn('/hangup/', xml)

    @patch('apps.tenant_core.views_telephony.redis.from_url')
    def test_dial_events_never_change_session_or_presence(self, mock_from_url):
        """hangup / digits B-leg events are log-only: they never close the call or free the rep.

        (answer / connected are the only events that write, and only RINGING -> CONNECTED;
        see test_dial_events_answer_marks_connected_but_hangup_event_does_not.)
        """
        session = CallSession.objects.create(
            organization=self.org,
            lead=self.lead,
            sales_rep=self.rep,
            provider='VOBIZ',
            provider_attempt_id='test-dial-events-uuid',
            direction='INBOUND',
            call_mode='HUMAN_MANUAL',
            status='RINGING',
            started_at=timezone.now(),
        )
        self.presence.status = 'ON_CALL'
        self.presence.active_session = session
        self.presence.save()

        for action in ('hangup', 'digits'):
            with self.subTest(action=action):
                res = self.client.post(f'/api/v1/telephony/dial-events/?session_id={session.id}', data={
                    'CallUUID': 'test-dial-events-uuid',
                    'DialAction': action,
                    'DialBLegStatus': action,
                })
                set_tenant_db_alias('tenant_test')
                self.assertEqual(res.status_code, 200)
                session.refresh_from_db()
                self.presence.refresh_from_db()
                self.assertEqual(session.status, 'RINGING')
                self.assertIsNone(session.ended_at)
                self.assertEqual(self.presence.status, 'ON_CALL')
        mock_from_url.return_value.publish.assert_not_called()

    @patch('apps.tenant_core.views_telephony.redis.from_url')
    def test_primary_leg_end_keeps_session_ringing_during_fallback(self, mock_from_url):
        """Regression (prod 2026-11-16 19:07): primary B-leg hangup closed the session,
        so the overflow agent was auto-released from ON_CALL while their line rang."""
        secondary = TenantUser.objects.create(
            organization=self.org,
            email="neha@sweatelite.in",
            first_name="Neha",
            last_name="Rao",
        )
        AgentPresence.objects.create(
            organization=self.org,
            user=secondary,
            sip_username="neha0001",
            status="AVAILABLE",
            last_heartbeat=timezone.now(),
        )

        res = self.client.post('/api/v1/telephony/inbound/', data={
            'CallUUID': 'prod-replay-uuid',
            'From': '+919876543210',
            'To': '+919200000001',
        })
        set_tenant_db_alias('tenant_test')
        session = CallSession.objects.get(provider_attempt_id='prod-replay-uuid')
        self.assertIn(f'/api/v1/telephony/dial-events/?session_id={session.id}', res.content.decode())

        # 1. Primary agent's leg ends -> Vobiz callbackUrl hangup event
        self.client.post(f'/api/v1/telephony/dial-events/?session_id={session.id}', data={
            'CallUUID': 'prod-replay-uuid',
            'DialAction': 'hangup',
            'DialBLegDuration': '0',
        })
        set_tenant_db_alias('tenant_test')
        session.refresh_from_db()
        self.assertEqual(session.status, 'RINGING')
        self.assertIsNone(session.ended_at)

        # 2. Dial action reports no-answer -> overflow to the secondary agent
        res = self.client.post(f'/api/v1/telephony/dial-fallback/?session_id={session.id}', data={
            'CallUUID': 'prod-replay-uuid',
            'DialStatus': 'no-answer',
        })
        set_tenant_db_alias('tenant_test')
        self.assertIn('sip:neha0001@', res.content.decode())
        session.refresh_from_db()
        self.assertEqual(session.status, 'RINGING')
        self.assertEqual(session.sales_rep, secondary)

        # 3. Secondary's browser polls presence while ringing -> must stay ON_CALL
        api_client = APIClient()
        api_client.force_authenticate(user=secondary)
        res = api_client.get('/api/v1/tenant/crm/telephony/presence/')
        set_tenant_db_alias('tenant_test')
        self.assertEqual(res.data['status'], 'ON_CALL')

    # ── Phase 2b: answered vs. ring time on inbound calls ─────────────────────

    def _start_inbound_call(self, call_uuid):
        """Runs the real inbound webhook for the known lead and returns the new CallSession."""
        res = self.client.post('/api/v1/telephony/inbound/', data={
            'CallUUID': call_uuid,
            'From': '+919876543210',
            'To': '+919200000001',
        })
        set_tenant_db_alias('tenant_test')
        self.assertEqual(res.status_code, 200)
        return CallSession.objects.get(provider_attempt_id=call_uuid)

    @patch('apps.tenant_core.views_telephony.redis.from_url')
    def test_dial_events_answer_marks_connected_but_hangup_event_does_not(self, mock_from_url):
        """answer / connected move a ringing session to CONNECTED. hangup does not, and a
        finished session is never reopened."""
        session = CallSession.objects.create(
            organization=self.org,
            lead=self.lead,
            sales_rep=self.rep,
            provider='VOBIZ',
            provider_attempt_id='test-connected-uuid',
            direction='INBOUND',
            call_mode='HUMAN_MANUAL',
            status='RINGING',
            started_at=timezone.now(),
        )
        self.presence.status = 'ON_CALL'
        self.presence.active_session = session
        self.presence.save()
        url = f'/api/v1/telephony/dial-events/?session_id={session.id}'

        self.client.post(url, data={'CallUUID': 'test-connected-uuid', 'DialAction': 'hangup'})
        set_tenant_db_alias('tenant_test')
        session.refresh_from_db()
        self.assertEqual(session.status, 'RINGING')

        for action in ('answer', 'connected'):
            with self.subTest(action=action):
                res = self.client.post(url, data={'CallUUID': 'test-connected-uuid', 'DialAction': action})
                set_tenant_db_alias('tenant_test')
                self.assertEqual(res.status_code, 200)
                session.refresh_from_db()
                self.presence.refresh_from_db()
                self.assertEqual(session.status, 'CONNECTED')
                self.assertIsNone(session.ended_at)
                self.assertEqual(self.presence.status, 'ON_CALL')

        finished = CallSession.objects.create(
            organization=self.org,
            lead=self.lead,
            sales_rep=self.rep,
            provider='VOBIZ',
            provider_attempt_id='test-finished-uuid',
            direction='INBOUND',
            call_mode='HUMAN_MANUAL',
            status='COMPLETED',
            started_at=timezone.now(),
        )
        self.client.post(
            f'/api/v1/telephony/dial-events/?session_id={finished.id}',
            data={'CallUUID': 'test-finished-uuid', 'DialAction': 'answer'},
        )
        set_tenant_db_alias('tenant_test')
        finished.refresh_from_db()
        self.assertEqual(finished.status, 'COMPLETED')
        mock_from_url.return_value.publish.assert_not_called()

    @patch('apps.tenant_core.views_telephony.redis.from_url')
    def test_answered_inbound_call_completes_with_wrap_up(self, mock_from_url):
        """Agent leg answered -> the caller-leg hangup completes the call, puts the rep in
        WRAP_UP and sends CALL_ENDED. A duplicate hangup does not turn it into a missed call."""
        session = self._start_inbound_call('test-answered-uuid')
        self.client.post(f'/api/v1/telephony/dial-events/?session_id={session.id}', data={
            'CallUUID': 'test-answered-uuid',
            'DialAction': 'answer',
        })
        set_tenant_db_alias('tenant_test')
        session.refresh_from_db()
        self.assertEqual(session.status, 'CONNECTED')

        hangup = {'CallUUID': 'test-answered-uuid', 'CallStatus': 'completed', 'Duration': '60'}
        res = self.client.post('/api/v1/telephony/hangup/', data=hangup)
        set_tenant_db_alias('tenant_test')
        self.assertEqual(res.status_code, 200)

        session.refresh_from_db()
        self.presence.refresh_from_db()
        self.assertEqual(session.status, 'COMPLETED')
        self.assertTrue(session.is_meaningful)
        self.assertEqual(self.presence.status, 'WRAP_UP')
        names = [evt['event'] for _, evt in self._published_events(mock_from_url)]
        self.assertEqual(names.count('CALL_ENDED'), 1)
        self.assertNotIn('MISSED_CALL', names)

        # Vobiz sends duplicate hangups (seen in production nginx logs).
        self.client.post('/api/v1/telephony/hangup/', data=hangup)
        set_tenant_db_alias('tenant_test')
        session.refresh_from_db()
        self.assertEqual(session.status, 'COMPLETED')
        names = [evt['event'] for _, evt in self._published_events(mock_from_url)]
        self.assertNotIn('MISSED_CALL', names)

    @patch('apps.tenant_core.views_telephony.redis.from_url')
    def test_unanswered_inbound_call_with_ring_time_is_missed_not_completed(self, mock_from_url):
        """Regression (prod 2026-11-16 19:07): 40s of ringing was saved as COMPLETED and
        meaningful, sent CALL_ENDED, and never reached the Missed Calls desk."""
        session = self._start_inbound_call('test-unanswered-uuid')

        res = self.client.post('/api/v1/telephony/hangup/', data={
            'CallUUID': 'test-unanswered-uuid',
            'CallStatus': 'completed',
            'Duration': '40',
        })
        set_tenant_db_alias('tenant_test')
        self.assertEqual(res.status_code, 200)

        session.refresh_from_db()
        self.presence.refresh_from_db()
        self.assertEqual(session.status, 'NO_ANSWER')
        self.assertFalse(session.is_meaningful)
        self.assertEqual(self.presence.status, 'AVAILABLE')
        names = [evt['event'] for _, evt in self._published_events(mock_from_url)]
        self.assertIn('MISSED_CALL', names)
        self.assertNotIn('CALL_ENDED', names)

        api_client = APIClient()
        api_client.force_authenticate(user=self.rep)
        res = api_client.get('/api/v1/tenant/crm/telephony/missed-calls/')
        set_tenant_db_alias('tenant_test')
        self.assertEqual(res.status_code, 200)
        self.assertIn(str(session.id), [item['session_id'] for item in res.data['results']])

    @patch('apps.tenant_core.views_telephony.redis.from_url')
    def test_overflow_xml_callback_url_points_to_dial_events(self, mock_from_url):
        """The overflow agent's leg also reports answer events to /dial-events/."""
        xml = VobizVoiceAdapter.build_fallback_overflow_xml(
            secondary_sip_uri="sip:neha0001@registrar.vobiz.ai",
            caller_id="+919200000001",
            complete_action_url="https://x/api/v1/telephony/hangup/",
            dial_events_url="https://x/api/v1/telephony/dial-events/?session_id=1",
        )
        self.assertIn('callbackUrl="https://x/api/v1/telephony/dial-events/?session_id=1"', xml)
        xml_without = VobizVoiceAdapter.build_fallback_overflow_xml(
            secondary_sip_uri="sip:neha0001@registrar.vobiz.ai",
            caller_id="+919200000001",
            complete_action_url="https://x/api/v1/telephony/hangup/",
        )
        self.assertNotIn('callbackUrl', xml_without)

        secondary = TenantUser.objects.create(
            organization=self.org,
            email="neha@sweatelite.in",
            first_name="Neha",
            last_name="Rao",
        )
        AgentPresence.objects.create(
            organization=self.org,
            user=secondary,
            sip_username="neha0001",
            status="AVAILABLE",
            last_heartbeat=timezone.now(),
        )
        session = CallSession.objects.create(
            organization=self.org,
            lead=self.lead,
            sales_rep=self.rep,
            provider='VOBIZ',
            provider_attempt_id='test-overflow-xml-uuid',
            direction='INBOUND',
            call_mode='HUMAN_MANUAL',
            status='RINGING',
            user_phone_number='+919876543210',
            started_at=timezone.now(),
        )
        res = self.client.post(
            f'/api/v1/telephony/dial-fallback/?session_id={session.id}',
            data={'CallUUID': 'test-overflow-xml-uuid', 'DialStatus': 'no-answer'},
        )
        set_tenant_db_alias('tenant_test')
        self.assertEqual(res.status_code, 200)
        self.assertIn(f'/api/v1/telephony/dial-events/?session_id={session.id}', res.content.decode())
