"""
backend/tests/test_telephony_fallback_chain.py - Phase 4: inbound overflow chain and idempotent hangup.

Covers:
- overflow chain order across reps, tried_reps filtering, max attempts, exhaustion
- idempotency of /dial-fallback/ and /hangup/ (Vobiz retries callbacks)
- cancel stops the chain
- guarded release of the missed rep, and no reuse of a rep who is mid-call
- Dial-result hits (event=dial) on /hangup/ and the late call-level hangup
- raw XML escaping of query strings and the response-time log line
"""

import json
import re
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.tenant_core.models_crm import (
    AgentPresence,
    CallSession,
    Lead,
    LeadActivity,
    LeadSource,
)
from apps.tenant_core.models_org import Branch, Location, Organization
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.services_telephony_config import TelephonyConfigService
from apps.tenant_core.services_telephony_fallback import (
    EMPTY_XML,
    HANGUP_XML,
    handle_dial_fallback,
)
from config.routers import set_tenant_db_alias

DB = 'tenant_test'
HOST = 'http://testserver'
SERVICE_LOGGER = 'apps.tenant_core.services_telephony_fallback'
VIEW_LOGGER = 'apps.tenant_core.views_telephony'


class FallbackChainBase(TestCase):
    databases = frozenset({'default', 'tenant_test'})

    def setUp(self):
        super().setUp()
        set_tenant_db_alias(DB)
        self.org = Organization.objects.create(name="SWEAT Elite", code="SWEAT01")
        location = Location.objects.create(organization=self.org, name="Mumbai", code="LOC01")
        self.branch = Branch.objects.create(
            organization=self.org, location=location, name="Andheri West", code="AND01",
        )
        source = LeadSource.objects.create(organization=self.org, name="Web", code="WEB01")
        self.lead = Lead.objects.create(
            organization=self.org, branch=self.branch, first_name="Vikram", last_name="Mehta",
            phone_normalized="+919876543210", current_status="NEW_LEAD", lead_source=source,
        )
        self.primary = self._agent('primary@sweatelite.in', 'primary0001', stagger=500)
        # Newest heartbeat first: A, B, C, D are chosen in that order.
        self.rep_a = self._agent('a@sweatelite.in', 'repa0001', stagger=0)
        self.rep_b = self._agent('b@sweatelite.in', 'repb0001', stagger=10)
        self.rep_c = self._agent('c@sweatelite.in', 'repc0001', stagger=20)
        self.rep_d = self._agent('d@sweatelite.in', 'repd0001', stagger=30)

    def _agent(self, email, sip, stagger=0, browser_age=10):
        user = TenantUser.objects.create(
            organization=self.org, email=email, first_name=email.split('@')[0], last_name='X',
        )
        presence = AgentPresence.objects.create(
            organization=self.org, user=user, sip_username=sip, status='AVAILABLE',
            browser_heartbeat_at=timezone.now() - timedelta(seconds=browser_age),
        )
        # last_heartbeat is auto_now, so stagger it with update() to make the order deterministic.
        AgentPresence.objects.filter(pk=presence.pk).update(
            last_heartbeat=timezone.now() - timedelta(seconds=stagger),
        )
        return user

    def _presence(self, user):
        return AgentPresence.objects.using(DB).get(user=user)

    def _go_offline(self, *users):
        AgentPresence.objects.using(DB).filter(user__in=users).update(
            browser_heartbeat_at=timezone.now() - timedelta(hours=1),
        )

    def _inbound_session(self, call_uuid, rep=None, with_lead=True, status='RINGING'):
        rep = rep or self.primary
        session = CallSession.objects.create(
            organization=self.org, lead=self.lead if with_lead else None, sales_rep=rep,
            provider='VOBIZ', provider_attempt_id=call_uuid, direction='INBOUND',
            call_mode='HUMAN_MANUAL', status=status, user_phone_number='+919876543210',
            started_at=timezone.now(),
        )
        AgentPresence.objects.using(DB).filter(user=rep).update(status='ON_CALL', active_session=session)
        return session

    def _outbound_session(self, call_uuid, status='RINGING'):
        session = CallSession.objects.create(
            organization=self.org, lead=self.lead, sales_rep=self.primary, provider='VOBIZ',
            provider_attempt_id=call_uuid, direction='OUTBOUND', call_mode='HUMAN_MANUAL',
            status=status, user_phone_number='+919876543210', started_at=timezone.now(),
        )
        AgentPresence.objects.using(DB).filter(user=self.primary).update(status='ON_CALL', active_session=session)
        return session

    def _fallback(self, session, dial_status='no-answer', attempt=None):
        return handle_dial_fallback(
            alias=DB, session_id=str(session.id), call_uuid=session.provider_attempt_id,
            dial_status=dial_status, attempt=attempt, host=HOST,
        )

    @staticmethod
    def _names(events):
        return [evt_type for _, evt_type, _ in events]

    @staticmethod
    def _published(mock_from_url):
        publish = mock_from_url.return_value.publish
        return [json.loads(c.args[1]) for c in publish.call_args_list]


class OverflowChainTests(FallbackChainBase):

    def test_chain_visits_reps_in_order_and_fourth_rep_answers(self):
        """Primary -> A -> B -> C. C answers, so D is never rung."""
        session = self._inbound_session('chain-uuid')

        first = self._fallback(session, attempt=0)
        session.refresh_from_db()
        self.assertIn('sip:repa0001@', first.xml)
        self.assertIn('<Speak', first.xml)
        self.assertIn('&amp;attempt=1', first.xml)
        self.assertEqual(session.tried_reps, [str(self.primary.id)])
        self.assertEqual(session.sales_rep, self.rep_a)
        self.assertEqual(self._presence(self.primary).status, 'AVAILABLE')
        self.assertEqual(self._presence(self.rep_a).status, 'ON_CALL')
        self.assertEqual(self._names(first.events), ['INCOMING_CALL_POPUP'])

        second = self._fallback(session, attempt=1)
        session.refresh_from_db()
        self.assertIn('sip:repb0001@', second.xml)
        self.assertNotIn('<Speak', second.xml)  # whisper only on the first overflow hop
        self.assertEqual(session.tried_reps, [str(self.primary.id), str(self.rep_a.id)])
        self.assertEqual(self._presence(self.rep_a).status, 'AVAILABLE')

        third = self._fallback(session, attempt=2)
        session.refresh_from_db()
        self.assertIn('sip:repc0001@', third.xml)
        self.assertEqual(session.sales_rep, self.rep_c)
        self.assertEqual(len(session.tried_reps), 3)

        # C answers: Dial result is `completed`. Chain ends, nothing closes, D untouched.
        done = self._fallback(session, dial_status='completed', attempt=3)
        session.refresh_from_db()
        self.assertEqual(done.xml, HANGUP_XML)
        self.assertIsNone(session.ended_at)
        self.assertEqual(session.sales_rep, self.rep_c)
        self.assertEqual(self._presence(self.rep_d).status, 'AVAILABLE')
        self.assertEqual(self._presence(self.rep_c).status, 'ON_CALL')

    def test_chain_skips_offline_reps(self):
        self._go_offline(self.rep_a)
        session = self._inbound_session('skip-offline-uuid')

        outcome = self._fallback(session, attempt=0)

        self.assertIn('sip:repb0001@', outcome.xml)
        self.assertNotIn('sip:repa0001@', outcome.xml)

    def test_exclude_user_ids_is_honoured_by_routing(self):
        agent = TelephonyConfigService.get_available_agent(db_alias=DB, org=self.org)
        self.assertEqual(agent.user_id, self.rep_a.id)

        skip_a = TelephonyConfigService.get_available_agent(
            db_alias=DB, org=self.org, exclude_user_ids=[str(self.rep_a.id)],
        )
        self.assertEqual(skip_a.user_id, self.rep_b.id)

        skip_ab = TelephonyConfigService.get_available_agent(
            db_alias=DB, org=self.org, exclude_user_ids={self.rep_a.id, self.rep_b.id},
        )
        self.assertEqual(skip_ab.user_id, self.rep_c.id)

    @override_settings(TELEPHONY_MAX_OVERFLOW_ATTEMPTS_AFTER_PRIMARY=1)
    def test_max_overflow_attempts_closes_call_and_logs_warning(self):
        session = self._inbound_session('max-attempts-uuid')
        self._fallback(session, attempt=0)  # overflow #1 rings A

        with self.assertLogs(SERVICE_LOGGER, level='WARNING') as logs:
            outcome = self._fallback(session, attempt=1)  # A fails too: limit reached

        session.refresh_from_db()
        self.assertTrue(any('max overflow attempts' in line for line in logs.output))
        self.assertEqual(session.status, 'NO_ANSWER')
        self.assertEqual(session.hangup_cause, 'OVERFLOW_MAX_ATTEMPTS')
        self.assertIsNotNone(session.ended_at)
        self.assertIn('All consultants', outcome.xml)
        self.assertEqual(self._presence(self.rep_a).status, 'AVAILABLE')
        self.assertEqual(self._presence(self.rep_b).status, 'AVAILABLE')  # B was never rung
        self.assertEqual(LeadActivity.objects.filter(lead=self.lead, outcome='MISSED').count(), 1)
        self.assertIn('MISSED_CALL', self._names(outcome.events))

    def test_no_available_rep_closes_call_once(self):
        self._go_offline(self.rep_a, self.rep_b, self.rep_c, self.rep_d)
        session = self._inbound_session('no-agent-uuid')

        with self.assertLogs(SERVICE_LOGGER, level='WARNING'):
            outcome = self._fallback(session, attempt=0)

        session.refresh_from_db()
        self.assertEqual(session.hangup_cause, 'OVERFLOW_NO_AGENT')
        self.assertIsNotNone(session.ended_at)
        self.assertEqual(self._names(outcome.events).count('MISSED_CALL'), 5)  # every agent in the org
        self.assertEqual(LeadActivity.objects.filter(lead=self.lead, outcome='MISSED').count(), 1)

        repeat = self._fallback(session, attempt=0)
        self.assertEqual(repeat.xml, HANGUP_XML)
        self.assertEqual(repeat.events, [])
        self.assertEqual(LeadActivity.objects.filter(lead=self.lead, outcome='MISSED').count(), 1)


class FallbackIdempotencyTests(FallbackChainBase):

    def test_duplicate_fallback_replays_without_side_effects_and_stale_is_empty(self):
        session = self._inbound_session('dup-fallback-uuid')

        original = self._fallback(session, attempt=0)
        replay = self._fallback(session, attempt=0)

        session.refresh_from_db()
        self.assertEqual(replay.xml, original.xml)
        self.assertEqual(replay.events, [])
        self.assertEqual(session.tried_reps, [str(self.primary.id)])
        self.assertEqual(session.sales_rep, self.rep_a)
        self.assertEqual(self._presence(self.rep_a).status, 'ON_CALL')

        self._fallback(session, attempt=1)  # chain moves on to B
        stale = self._fallback(session, attempt=0)
        session.refresh_from_db()
        self.assertEqual(stale.xml, EMPTY_XML)
        self.assertEqual(stale.events, [])
        self.assertEqual(session.sales_rep, self.rep_b)
        self.assertEqual(len(session.tried_reps), 2)

    def test_missing_attempt_is_a_legacy_in_flight_call(self):
        """A call that started before the deploy has no attempt param. It still advances once."""
        session = self._inbound_session('legacy-uuid')

        outcome = self._fallback(session, attempt=None)

        session.refresh_from_db()
        self.assertIn('sip:repa0001@', outcome.xml)
        self.assertEqual(session.tried_reps, [str(self.primary.id)])

    def test_release_does_not_clobber_a_newer_assignment(self):
        """The missed rep already took another call: their presence row must be left alone."""
        session = self._inbound_session('release-guard-uuid')
        other = self._outbound_session('release-guard-other-uuid')
        AgentPresence.objects.using(DB).filter(user=self.primary).update(
            status='ON_CALL', active_session=other,
        )

        self._fallback(session, attempt=0)

        presence = self._presence(self.primary)
        self.assertEqual(presence.status, 'ON_CALL')
        self.assertEqual(presence.active_session_id, other.id)

    def test_rep_with_live_session_is_not_released_and_reused(self):
        """Step-4 self-heal must not free a rep who is mid-call with a fresh heartbeat."""
        self._go_offline(self.rep_a, self.rep_b, self.rep_c)
        busy_session = CallSession.objects.create(
            organization=self.org, sales_rep=self.rep_d, provider='VOBIZ',
            provider_attempt_id='busy-rep-uuid', direction='INBOUND', call_mode='HUMAN_MANUAL',
            status='CONNECTED', user_phone_number='+919999999999', started_at=timezone.now(),
        )
        AgentPresence.objects.using(DB).filter(user=self.rep_d).update(
            status='ON_CALL', active_session=busy_session, last_heartbeat=timezone.now(),
        )
        session = self._inbound_session('live-session-uuid')

        with self.assertLogs(SERVICE_LOGGER, level='WARNING'):
            outcome = self._fallback(session, attempt=0)

        session.refresh_from_db()
        busy = self._presence(self.rep_d)
        self.assertEqual(session.hangup_cause, 'OVERFLOW_NO_AGENT')
        self.assertNotIn('sip:repd0001@', outcome.xml)
        self.assertEqual(busy.status, 'ON_CALL')
        self.assertEqual(busy.active_session_id, busy_session.id)


class CancelTests(FallbackChainBase):

    def test_cancel_records_tried_rep_before_closing_and_stops_the_chain(self):
        session = self._inbound_session('cancel-uuid')

        outcome = self._fallback(session, dial_status='cancel', attempt=0)

        session.refresh_from_db()
        self.assertEqual(outcome.xml, HANGUP_XML)
        self.assertEqual(session.status, 'NO_ANSWER')
        self.assertEqual(session.hangup_cause, 'CALLER_CANCELLED')
        self.assertIsNotNone(session.ended_at)
        # The rep is recorded as tried in the same save that closes the call.
        self.assertEqual(session.tried_reps, [str(self.primary.id)])
        self.assertEqual(self._presence(self.primary).status, 'AVAILABLE')
        self.assertEqual(LeadActivity.objects.filter(lead=self.lead, outcome='MISSED').count(), 1)
        self.assertIn('MISSED_CALL', self._names(outcome.events))

        # A late /dial-fallback/ retry must not dial the next rep.
        late = self._fallback(session, dial_status='no-answer', attempt=1)
        session.refresh_from_db()
        self.assertEqual(late.xml, HANGUP_XML)
        self.assertEqual(late.events, [])
        self.assertEqual(session.sales_rep, self.primary)
        self.assertEqual(session.tried_reps, [str(self.primary.id)])
        self.assertEqual(self._presence(self.rep_a).status, 'AVAILABLE')
        self.assertEqual(LeadActivity.objects.filter(lead=self.lead, outcome='MISSED').count(), 1)

    @patch('apps.tenant_core.views_telephony.redis.from_url')
    def test_late_hangup_after_cancel_is_a_noop(self, mock_from_url):
        session = self._inbound_session('cancel-hangup-uuid')
        self._fallback(session, dial_status='cancel', attempt=0)

        res = self.client.post('/api/v1/telephony/hangup/', data={
            'CallUUID': 'cancel-hangup-uuid', 'CallStatus': 'completed', 'Duration': '12',
        })
        set_tenant_db_alias(DB)

        session.refresh_from_db()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(session.status, 'NO_ANSWER')
        self.assertEqual(session.hangup_cause, 'CALLER_CANCELLED')
        self.assertEqual(session.duration_seconds, 12)  # back-filled
        self.assertEqual(self._published(mock_from_url), [])


@patch('apps.tenant_core.views_telephony.redis.from_url')
class HangupTests(FallbackChainBase):

    def setUp(self):
        super().setUp()
        # A recording URL queues an archive task: keep the broker out of these tests.
        archive = patch('apps.tenant_core.tasks_recordings.archive_vobiz_recording_task')
        self.archive_task = archive.start()
        self.addCleanup(archive.stop)

    def _hangup(self, query='', **data):
        res = self.client.post(f'/api/v1/telephony/hangup/{query}', data=data)
        set_tenant_db_alias(DB)
        self.assertEqual(res.status_code, 200)
        return res

    def _event_names(self, mock_from_url):
        return [evt['event'] for evt in self._published(mock_from_url)]

    def test_duplicate_hangup_does_not_repeat_call_ended_or_clobber_presence(self, mock_from_url):
        session = self._inbound_session('dup-hangup-uuid', status='CONNECTED')

        self._hangup(CallUUID='dup-hangup-uuid', CallStatus='completed', Duration='60')
        self.assertEqual(self._event_names(mock_from_url), ['CALL_ENDED'])
        self.assertEqual(self._presence(self.primary).status, 'WRAP_UP')

        # The rep moves on to a newer call, then Vobiz repeats the hangup with more data.
        newer = self._outbound_session('dup-hangup-newer-uuid')
        self._hangup(
            CallUUID='dup-hangup-uuid', CallStatus='completed', Duration='90',
            RecordUrl='https://rec.example/a.mp3',
        )

        session.refresh_from_db()
        presence = self._presence(self.primary)
        self.assertEqual(self._event_names(mock_from_url), ['CALL_ENDED'])  # still exactly one
        self.assertEqual(session.status, 'COMPLETED')
        self.assertEqual(session.duration_seconds, 90)
        self.assertEqual(session.recording_url, 'https://rec.example/a.mp3')
        self.assertEqual(presence.status, 'ON_CALL')
        self.assertEqual(presence.active_session_id, newer.id)

    def test_dial_result_hit_closes_outbound_once_and_late_hangup_backfills(self, mock_from_url):
        session = self._outbound_session('dial-result-uuid', status='CONNECTED')

        self._hangup(
            f'?session_id={session.id}&event=dial',
            CallUUID='dial-result-uuid', DialStatus='completed', DialBLegDuration='70',
        )
        session.refresh_from_db()
        self.assertEqual(session.status, 'COMPLETED')
        self.assertTrue(session.is_meaningful)
        self.assertEqual(self._presence(self.primary).status, 'WRAP_UP')
        self.assertEqual(self._event_names(mock_from_url), ['CALL_ENDED'])

        # Late call-level hangup (no marker): back-fills only, no second CALL_ENDED.
        self._hangup(
            f'?session_id={session.id}',
            CallUUID='dial-result-uuid', CallStatus='completed', Duration='75',
            RecordUrl='https://rec.example/b.mp3',
        )
        session.refresh_from_db()
        self.assertEqual(self._event_names(mock_from_url), ['CALL_ENDED'])
        self.assertEqual(session.status, 'COMPLETED')
        self.assertEqual(session.duration_seconds, 75)
        self.assertEqual(session.recording_url, 'https://rec.example/b.mp3')
        self.assertEqual(self._presence(self.primary).status, 'WRAP_UP')

    def test_dial_result_completed_without_duration_or_connect_is_not_answered(self, mock_from_url):
        session = self._outbound_session('dial-zero-uuid', status='RINGING')

        self._hangup(
            f'?session_id={session.id}&event=dial',
            CallUUID='dial-zero-uuid', DialStatus='completed', Duration='0',
        )

        session.refresh_from_db()
        presence = self._presence(self.primary)
        self.assertEqual(session.status, 'NO_ANSWER')
        self.assertFalse(session.is_meaningful)
        self.assertEqual(presence.status, 'AVAILABLE')
        self.assertIsNone(presence.active_session_id)
        self.assertEqual(self._event_names(mock_from_url), [])

    def test_dial_result_unanswered_statuses_close_and_release_the_rep(self, mock_from_url):
        for dial_status in ('busy', 'no-answer', 'failed', 'cancel'):
            with self.subTest(dial_status=dial_status):
                session = self._outbound_session(f'dial-{dial_status}-uuid')

                self._hangup(
                    f'?session_id={session.id}&event=dial',
                    CallUUID=f'dial-{dial_status}-uuid', DialStatus=dial_status,
                )

                session.refresh_from_db()
                presence = self._presence(self.primary)
                self.assertEqual(session.status, 'NO_ANSWER')
                self.assertIsNotNone(session.ended_at)
                self.assertEqual(presence.status, 'AVAILABLE')
                self.assertIsNone(presence.active_session_id)
        self.assertEqual(self._event_names(mock_from_url), [])


@patch('apps.tenant_core.views_telephony.redis.from_url')
class WebhookResponseTests(FallbackChainBase):

    def test_overflow_xml_body_escapes_ampersands(self, mock_from_url):
        session = self._inbound_session('escape-uuid')

        res = self.client.post(
            f'/api/v1/telephony/dial-fallback/?session_id={session.id}&attempt=0',
            data={'CallUUID': 'escape-uuid', 'DialStatus': 'no-answer'},
        )
        set_tenant_db_alias(DB)

        body = res.content.decode()
        self.assertEqual(res.status_code, 200)
        self.assertIn(f'dial-fallback/?session_id={session.id}&amp;attempt=1', body)
        self.assertIsNone(re.search(r'&(?!amp;)', body), f'bare ampersand in XML: {body}')

    def test_primary_dial_xml_carries_attempt_zero_escaped(self, mock_from_url):
        self.lead.assigned_sales_user = self.rep_a
        self.lead.save()

        res = self.client.post('/api/v1/telephony/inbound/', data={
            'CallUUID': 'primary-escape-uuid', 'From': '+919876543210', 'To': '+919200000001',
        })
        set_tenant_db_alias(DB)

        body = res.content.decode()
        session = CallSession.objects.get(provider_attempt_id='primary-escape-uuid')
        self.assertIn(f'dial-fallback/?session_id={session.id}&amp;attempt=0', body)
        self.assertIsNone(re.search(r'&(?!amp;)', body), f'bare ampersand in XML: {body}')

    def test_fallback_view_logs_response_time(self, mock_from_url):
        session = self._inbound_session('timing-uuid')

        with self.assertLogs(VIEW_LOGGER, level='INFO') as logs:
            self.client.post(
                f'/api/v1/telephony/dial-fallback/?session_id={session.id}&attempt=0',
                data={'CallUUID': 'timing-uuid', 'DialStatus': 'no-answer'},
            )
        set_tenant_db_alias(DB)

        line = next((entry for entry in logs.output if 'telephony.dial_fallback' in entry), '')
        self.assertRegex(line, r'elapsed_ms=\d+ handled_ms=\d+ events=\d+')
