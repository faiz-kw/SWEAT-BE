"""
backend/tests/test_telephony_inbound_claim.py - Phase 5b: atomic claim of the agent on inbound calls.

Two calls can pick the same agent before either has marked them ON_CALL. The claim is one conditional
UPDATE (AVAILABLE -> ON_CALL): the loser matches zero rows and selects again.

A real thread race is flaky, so the race is simulated deterministically: the selector is wrapped so that
the agent it returns is taken by "another call" (ON_CALL with a live session) right after selection.
"""

import json
from unittest.mock import patch

from apps.tenant_core.models_crm import AgentPresence, CallSession
from apps.tenant_core.services_telephony_config import TelephonyConfigService
from config.routers import set_tenant_db_alias
from tests.test_telephony_round_robin import RoundRobinBase

DB = 'tenant_test'
REDIS = 'apps.tenant_core.views_telephony.redis.from_url'


@patch(REDIS)
class InboundClaimTests(RoundRobinBase):

    def setUp(self):
        super().setUp()
        self.agents = {name: self._agent(name) for name in ('a', 'b', 'c', 'd')}

    def _other_call(self, user, call_uuid):
        """A live call on another line: the session that a competing inbound call attached to the agent."""
        return CallSession.objects.create(
            organization=self.org, lead=self.lead, sales_rep=user, provider='VOBIZ',
            provider_attempt_id=call_uuid, direction='INBOUND', call_mode='HUMAN_MANUAL',
            status='CONNECTED', user_phone_number='+919000000000', started_at=self.clock,
        )

    def _lose_the_first_claims(self, how_many):
        """Wraps the selector: the first ``how_many`` agents it returns are taken by a competing call."""
        real = TelephonyConfigService.get_available_agent
        taken = []

        def racing(*args, **kwargs):
            result = real(*args, **kwargs)
            if result is not None and len(taken) < how_many:
                competing = self._other_call(result.user, f'rival-{len(taken)}-uuid')
                AgentPresence.objects.using(DB).filter(pk=result.pk).update(
                    status='ON_CALL', active_session=competing,
                )
                taken.append((result.user_id, competing.id))
            return result

        patcher = patch.object(TelephonyConfigService, 'get_available_agent', side_effect=racing)
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return taken, mock

    def test_single_call_claims_the_rep_with_session_stamp_and_heartbeat(self, mock_from_url):
        called_at = self.clock

        session = self._inbound('claim-single-uuid')

        presence = self._presence(session.sales_rep)
        self.assertEqual(presence.status, 'ON_CALL')
        self.assertEqual(presence.active_session_id, session.id)
        self.assertEqual(presence.last_call_assigned_at, called_at)
        self.assertEqual(presence.last_heartbeat, called_at)

    def test_lost_claim_rings_the_next_rep_and_leaves_the_winner_untouched(self, mock_from_url):
        taken, _ = self._lose_the_first_claims(1)

        session = self._inbound('claim-lost-uuid')

        loser_user_id, rival_session_id = taken[0]
        winner = self._presence(session.sales_rep)
        rival = AgentPresence.objects.using(DB).get(user_id=loser_user_id)
        self.assertNotEqual(session.sales_rep_id, loser_user_id)
        self.assertEqual(winner.status, 'ON_CALL')
        self.assertEqual(winner.active_session_id, session.id)
        # The rep taken by the competing call keeps that call and is not stamped for this one.
        self.assertEqual(rival.active_session_id, rival_session_id)
        self.assertIsNone(rival.last_call_assigned_at)

    def test_no_rep_left_after_a_lost_claim_is_a_missed_call(self, mock_from_url):
        for name in ('b', 'c', 'd'):
            AgentPresence.objects.using(DB).filter(user=self.agents[name]).delete()
        taken, _ = self._lose_the_first_claims(1)

        res = self.client.post('/api/v1/telephony/inbound/', data={
            'CallUUID': 'claim-missed-uuid', 'From': '+919876543210', 'To': '+919200000001',
        })
        set_tenant_db_alias(DB)

        session = CallSession.objects.using(DB).get(provider_attempt_id='claim-missed-uuid')
        busy = AgentPresence.objects.using(DB).get(user_id=taken[0][0])
        events = [json.loads(c.args[1])['event'] for c in mock_from_url.return_value.publish.call_args_list]
        self.assertEqual(res.status_code, 200)
        self.assertIn('currently on active calls', res.content.decode())
        self.assertEqual(session.status, 'NO_ANSWER')
        self.assertIsNone(session.sales_rep_id)
        self.assertIn('MISSED_CALL', events)
        self.assertNotIn('INCOMING_CALL_POPUP', events)
        self.assertEqual(busy.active_session_id, taken[0][1])  # the busy rep was not rung or overwritten

    def test_claim_attempts_are_bounded(self, mock_from_url):
        taken, selector = self._lose_the_first_claims(10)  # every selected rep is taken

        session = self._inbound('claim-bounded-uuid')

        self.assertEqual(selector.call_count, TelephonyConfigService.MAX_CLAIM_ATTEMPTS)
        self.assertEqual(len(taken), TelephonyConfigService.MAX_CLAIM_ATTEMPTS)
        self.assertEqual(session.status, 'NO_ANSWER')
        self.assertIsNone(session.sales_rep_id)
        untouched = AgentPresence.objects.using(DB).exclude(user_id__in=[uid for uid, _ in taken])
        self.assertEqual([p.status for p in untouched], ['AVAILABLE'])

    def test_assigned_rep_who_lost_the_claim_is_replaced_by_round_robin(self, mock_from_url):
        self.lead.assigned_sales_user = self.agents['c']
        self.lead.save()
        taken, _ = self._lose_the_first_claims(1)

        session = self._inbound('claim-assigned-uuid')

        self.assertEqual(taken[0][0], self.agents['c'].id)  # the assigned rep was tried first
        self.assertNotEqual(session.sales_rep_id, self.agents['c'].id)
        self.assertEqual(self._presence(session.sales_rep).active_session_id, session.id)

    def test_overflow_claim_uses_the_same_atomic_claim(self, mock_from_url):
        """The overflow chain shares the helper: a lost claim there also selects the next rep."""
        from apps.tenant_core.services_telephony_fallback import handle_dial_fallback

        session = self._inbound('claim-overflow-uuid')
        first_rep_id = session.sales_rep_id
        self._tick()
        taken, _ = self._lose_the_first_claims(1)

        handle_dial_fallback(
            alias=DB, session_id=str(session.id), call_uuid='claim-overflow-uuid',
            dial_status='no-answer', attempt=0, host='http://testserver',
        )

        session.refresh_from_db()
        self.assertNotIn(session.sales_rep_id, {first_rep_id, taken[0][0]})
        self.assertEqual(self._presence(session.sales_rep).active_session_id, session.id)
