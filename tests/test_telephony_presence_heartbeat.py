"""
backend/tests/test_telephony_presence_heartbeat.py - Phase F: the heartbeat is a pure liveness ping.

The browser sends {"heartbeat": true} with no status. The server refreshes the liveness timestamps and
decides the status. Requests without the flag (the agent's own choice, the close-tab OFFLINE beacon,
old frontend builds) behave exactly as before.

Runs on the frozen clock of RoundRobinBase, so no test depends on wall time.
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import override_settings
from rest_framework.test import APIClient

from apps.tenant_core.models_crm import AgentPresence, CallSession
from config.routers import set_tenant_db_alias
from tests.test_telephony_round_robin import RoundRobinBase

DB = 'tenant_test'
URL = '/api/v1/tenant/crm/telephony/presence/'
PRESENCE_SERVICE = 'apps.tenant_core.services_telephony_presence'
REDIS = 'apps.tenant_core.views_telephony.redis.from_url'
VIEW_LOGGER_PATH = 'apps.tenant_core.views_telephony.logger'


class HeartbeatTests(RoundRobinBase):

    def setUp(self):
        super().setUp()
        self.user = self._agent('hb')
        self.api = APIClient()
        self.api.force_authenticate(user=self.user)

    def _put(self, **body):
        res = self.api.put(URL, body, format='json')
        set_tenant_db_alias(DB)
        self.assertEqual(res.status_code, 200, res.data)
        return res

    def _call(self, started_minutes_ago=5, ended=False):
        """Puts the agent ON_CALL with a session that started the given number of minutes ago."""
        session = CallSession.objects.create(
            organization=self.org, lead=self.lead, sales_rep=self.user, provider='VOBIZ',
            provider_attempt_id=f'hb-call-{started_minutes_ago}-{ended}', direction='INBOUND',
            call_mode='HUMAN_MANUAL', status='COMPLETED' if ended else 'CONNECTED',
            user_phone_number='+919000000000',
            started_at=self.clock - timedelta(minutes=started_minutes_ago),
            ended_at=self.clock - timedelta(minutes=1) if ended else None,
        )
        AgentPresence.objects.using(DB).filter(user=self.user).update(
            status='ON_CALL', active_session=session,
            browser_heartbeat_at=self.clock - timedelta(seconds=100),
            last_heartbeat=self.clock - timedelta(seconds=100),
        )
        return session

    def test_heartbeat_during_a_live_call_keeps_status_and_session_but_refreshes_timestamps(self):
        session = self._call()

        self._put(heartbeat=True)

        presence = self._presence(self.user)
        self.assertEqual(presence.status, 'ON_CALL')
        self.assertEqual(presence.active_session_id, session.id)
        self.assertEqual(presence.browser_heartbeat_at, self.clock)
        self.assertEqual(presence.last_heartbeat, self.clock)

    @override_settings(TELEPHONY_MAX_CALL_DURATION_MINUTES=60)
    def test_heartbeat_after_the_maximum_call_duration_frees_the_agent_and_warns(self):
        self._call(started_minutes_ago=61)

        with self.assertLogs(PRESENCE_SERVICE, level='WARNING'):
            self._put(heartbeat=True)

        presence = self._presence(self.user)
        self.assertEqual(presence.status, 'AVAILABLE')
        self.assertIsNone(presence.active_session_id)
        self.assertEqual(presence.browser_heartbeat_at, self.clock)

    @override_settings(TELEPHONY_MAX_CALL_DURATION_MINUTES=60)
    def test_heartbeat_just_inside_the_maximum_still_protects_the_call(self):
        session = self._call(started_minutes_ago=59)

        self._put(heartbeat=True)

        presence = self._presence(self.user)
        self.assertEqual(presence.status, 'ON_CALL')
        self.assertEqual(presence.active_session_id, session.id)

    def test_request_without_the_flag_behaves_as_before(self):
        self._call()

        self._put(status='AVAILABLE')

        presence = self._presence(self.user)
        self.assertEqual(presence.status, 'AVAILABLE')
        self.assertIsNone(presence.active_session_id)

    def test_explicit_offline_mid_call_goes_through(self):
        self._call()

        self._put(status='OFFLINE')

        self.assertEqual(self._presence(self.user).status, 'OFFLINE')

    def test_heartbeat_ignores_a_status_in_the_body(self):
        session = self._call()

        self._put(heartbeat=True, status='OFFLINE')

        presence = self._presence(self.user)
        self.assertEqual(presence.status, 'ON_CALL')
        self.assertEqual(presence.active_session_id, session.id)

    def test_heartbeat_brings_an_offline_agent_back(self):
        AgentPresence.objects.using(DB).filter(user=self.user).update(status='OFFLINE')

        self._put(heartbeat=True)

        self.assertEqual(self._presence(self.user).status, 'AVAILABLE')

    def test_heartbeat_after_the_call_ended_frees_the_agent(self):
        self._call(ended=True)

        self._put(heartbeat=True)

        presence = self._presence(self.user)
        self.assertEqual(presence.status, 'AVAILABLE')
        self.assertIsNone(presence.active_session_id)

    def test_unassigned_line_stays_offline(self):
        AgentPresence.objects.using(DB).filter(user=self.user).update(sip_username='unassigned_abc123')

        self._put(heartbeat=True)

        self.assertEqual(self._presence(self.user).status, 'OFFLINE')

    def test_trace_logging_is_off_by_default(self):
        with patch(VIEW_LOGGER_PATH) as mock_logger:
            self._put(heartbeat=True, tab_id='tab-1')

        traced = [c for c in mock_logger.info.call_args_list if 'presence heartbeat' in c.args[0]]
        self.assertEqual(traced, [])

    @override_settings(TELEPHONY_HEARTBEAT_TRACE=True)
    def test_trace_logging_names_the_writer_and_sanitises_the_tab_id(self):
        with patch(VIEW_LOGGER_PATH) as mock_logger:
            self._put(heartbeat=True, tab_id='tab-1\nFAKE log line')

        traced = [c for c in mock_logger.info.call_args_list if 'presence heartbeat' in c.args[0]]
        self.assertEqual(len(traced), 1)
        _, user_id, tab_id, previous, new = traced[0].args
        self.assertEqual(user_id, self.user.id)
        self.assertEqual(tab_id, 'tab-1FAKElogline')
        self.assertEqual((previous, new), ('AVAILABLE', 'AVAILABLE'))

    @patch(REDIS)
    def test_heartbeat_does_not_free_an_agent_claimed_by_an_inbound_call(self, mock_from_url):
        """With Phase 5b: after the inbound claim, a heartbeat must not make the agent routable again."""
        other = self._agent('other', stamp_age=5)  # called more recently, so the agent without a stamp goes first

        first = self._inbound('hb-claim-uuid-1')
        self.assertEqual(first.sales_rep_id, self.user.id)

        self._put(heartbeat=True)
        self.assertEqual(self._presence(self.user).status, 'ON_CALL')

        second = self._inbound('hb-claim-uuid-2')
        self.assertEqual(second.sales_rep_id, other.id)
