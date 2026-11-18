"""
backend/tests/test_telephony_round_robin.py - Phase 5: round-robin call distribution.

Routing orders agents by last_call_assigned_at (oldest first, never-assigned first).
The agent is stamped when a call CLAIMS them (inbound view, overflow claim), never by the selector.

Every test runs on a frozen clock (``self.clock``), advanced explicitly, so results never depend on
wall time or on two timestamps being taken a microsecond apart.
"""

from datetime import timedelta
from unittest.mock import patch

from django.db import connections
from django.test import TestCase
from django.utils import timezone

from apps.tenant_core.models_crm import (
    AgentPresence,
    CallSession,
    Lead,
    LeadSource,
)
from apps.tenant_core.models_org import Branch, Location, Organization
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.services_telephony_config import TelephonyConfigService
from apps.tenant_core.services_telephony_fallback import handle_dial_fallback
from config.routers import set_tenant_db_alias

DB = 'tenant_test'
HOST = 'http://testserver'
REDIS = 'apps.tenant_core.views_telephony.redis.from_url'


class RoundRobinBase(TestCase):
    databases = frozenset({'default', 'tenant_test'})

    def setUp(self):
        super().setUp()
        set_tenant_db_alias(DB)
        self.clock = timezone.now()
        clock_patch = patch('django.utils.timezone.now', side_effect=lambda: self.clock)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)

        self.org = Organization.objects.create(name="SWEAT Elite", code="SWEAT01")
        location = Location.objects.create(organization=self.org, name="Mumbai", code="LOC01")
        self.branch = Branch.objects.create(
            organization=self.org, location=location, name="Andheri West", code="AND01",
        )
        source = LeadSource.objects.create(organization=self.org, name="Web", code="WEB01")
        # No assigned rep, so inbound calls are distributed by round robin.
        self.lead = Lead.objects.create(
            organization=self.org, branch=self.branch, first_name="Vikram", last_name="Mehta",
            phone_normalized="+919876543210", current_status="NEW_LEAD", lead_source=source,
        )

    def _agent(self, name, stamp_age=None, browser_age=10, home_branch=None):
        """Creates an agent. ``stamp_age`` is seconds since the last assigned call (None = never)."""
        user = TenantUser.objects.create(
            organization=self.org, email=f'{name}@sweatelite.in', first_name=name, last_name='X',
            home_branch=home_branch,
        )
        AgentPresence.objects.create(
            organization=self.org, user=user, sip_username=f'rr{name}0001', status='AVAILABLE',
            browser_heartbeat_at=self.clock - timedelta(seconds=browser_age),
            last_call_assigned_at=(
                self.clock - timedelta(seconds=stamp_age) if stamp_age is not None else None
            ),
        )
        return user

    def _presence(self, user):
        return AgentPresence.objects.using(DB).get(user=user)

    def _stamp(self, user):
        return self._presence(user).last_call_assigned_at

    def _select(self, **kwargs):
        return TelephonyConfigService.get_available_agent(db_alias=DB, org=self.org, **kwargs)

    def _tick(self, seconds=10):
        """Moves the frozen clock forward and keeps every browser heartbeat fresh."""
        self.clock += timedelta(seconds=seconds)
        AgentPresence.objects.using(DB).update(browser_heartbeat_at=self.clock)

    def _finish_all_calls(self):
        """Call ended and disposition done: every agent is free again."""
        AgentPresence.objects.using(DB).update(status='AVAILABLE', active_session=None)
        self._tick()

    def _inbound(self, call_uuid):
        res = self.client.post('/api/v1/telephony/inbound/', data={
            'CallUUID': call_uuid, 'From': '+919876543210', 'To': '+919200000001',
        })
        set_tenant_db_alias(DB)
        self.assertEqual(res.status_code, 200)
        return CallSession.objects.using(DB).get(provider_attempt_id=call_uuid)


class RoundRobinSelectorTests(RoundRobinBase):

    def test_ordering_is_tested_on_postgres(self):
        """NULLS FIRST must be exercised on the production engine, not a fallback."""
        self.assertEqual(connections[DB].vendor, 'postgresql')

    def test_never_called_agent_goes_before_called_agents(self):
        self._agent('a', stamp_age=10)
        never = self._agent('b', stamp_age=None)
        self._agent('c', stamp_age=500)

        self.assertEqual(self._select().user_id, never.id)

    def test_oldest_stamp_goes_first(self):
        oldest = self._agent('a', stamp_age=300)
        self._agent('b', stamp_age=200)
        self._agent('c', stamp_age=100)

        self.assertEqual(self._select().user_id, oldest.id)

    def test_ties_are_stable(self):
        self._agent('a')
        self._agent('b')
        self._agent('c')

        picks = {self._select().user_id for _ in range(5)}

        self.assertEqual(len(picks), 1)

    def test_selector_never_stamps(self):
        """Lead auto-assignment and AI transfer call the selector without taking a call."""
        users = [self._agent('a'), self._agent('b')]

        for _ in range(3):
            self._select()

        self.assertEqual([self._stamp(u) for u in users], [None, None])

    def test_assigned_rep_keeps_priority_over_round_robin(self):
        self._agent('a', stamp_age=900)
        preferred = self._agent('b', stamp_age=1)  # most recently assigned

        self.assertEqual(self._select(preferred_user=preferred).user_id, preferred.id)

    def test_round_robin_applies_inside_the_caller_branch(self):
        outside = self._agent('out', stamp_age=None)  # never called, but not in the branch
        recent = self._agent('in1', stamp_age=5, home_branch=self.branch)
        older = self._agent('in2', stamp_age=50, home_branch=self.branch)

        picked = self._select(caller_branch=self.branch)

        self.assertEqual(picked.user_id, older.id)
        self.assertNotIn(picked.user_id, {outside.id, recent.id})

    def test_agent_without_live_browser_is_skipped_even_if_least_recent(self):
        self._agent('a', stamp_age=900, browser_age=3600)  # oldest stamp, browser closed
        live = self._agent('b', stamp_age=10)

        self.assertEqual(self._select().user_id, live.id)


@patch(REDIS)
class RoundRobinCallFlowTests(RoundRobinBase):

    def setUp(self):
        super().setUp()
        self.agents = [self._agent(name) for name in ('a', 'b', 'c', 'd')]

    def test_four_consecutive_calls_go_to_four_different_reps_then_wrap(self, mock_from_url):
        chosen = []
        for index in range(5):
            session = self._inbound(f'rr-call-{index}')
            chosen.append(session.sales_rep_id)
            self._finish_all_calls()  # each call ends before the next, so nobody drops out of the pool

        self.assertEqual(len(set(chosen[:4])), 4, chosen)
        self.assertEqual(chosen[4], chosen[0])

    def test_inbound_call_stamps_only_the_rep_it_rang(self, mock_from_url):
        called_at = self.clock

        session = self._inbound('rr-stamp-uuid')

        stamps = {u.id: self._stamp(u) for u in self.agents}
        self.assertEqual(stamps[session.sales_rep_id], called_at)
        self.assertEqual([s for uid, s in stamps.items() if uid != session.sales_rep_id], [None] * 3)

    def test_missed_primary_goes_to_the_back_and_overflow_claim_stamps(self, mock_from_url):
        first = self._inbound('rr-miss-uuid')
        missed_user = first.sales_rep_id
        self._tick()

        claimed_at = self.clock
        res = self.client.post(
            f'/api/v1/telephony/dial-fallback/?session_id={first.id}&attempt=0',
            data={'CallUUID': 'rr-miss-uuid', 'DialStatus': 'no-answer'},
        )
        set_tenant_db_alias(DB)
        self.assertEqual(res.status_code, 200)
        first.refresh_from_db()
        overflow_user = first.sales_rep_id
        self.assertNotEqual(overflow_user, missed_user)
        self.assertEqual(self._stamp(TenantUser.objects.using(DB).get(id=overflow_user)), claimed_at)

        self._finish_all_calls()
        later = []
        for index in range(3):
            later.append(self._inbound(f'rr-after-miss-{index}').sales_rep_id)
            self._finish_all_calls()

        # The two never-called reps go first, then the rep who missed the primary ring (oldest stamp).
        self.assertNotIn(later[0], {missed_user, overflow_user})
        self.assertNotIn(later[1], {missed_user, overflow_user})
        self.assertNotEqual(later[0], later[1])
        self.assertEqual(later[2], missed_user)

    def test_tried_rep_with_the_oldest_stamp_is_not_offered_again(self, mock_from_url):
        """Exclusion must hold by itself: ordering alone would pick the tried rep first."""
        for user in self.agents:
            AgentPresence.objects.using(DB).filter(user=user).delete()
        tried = self._agent('t', stamp_age=1000)    # oldest stamp
        second = self._agent('u', stamp_age=500)
        third = self._agent('v', stamp_age=400)
        session = CallSession.objects.create(
            organization=self.org, lead=self.lead, sales_rep=tried, provider='VOBIZ',
            provider_attempt_id='rr-tried-uuid', direction='INBOUND', call_mode='HUMAN_MANUAL',
            status='RINGING', user_phone_number='+919876543210', started_at=self.clock,
        )
        AgentPresence.objects.using(DB).filter(user=tried).update(status='ON_CALL', active_session=session)

        def hop(attempt):
            return handle_dial_fallback(
                alias=DB, session_id=str(session.id), call_uuid='rr-tried-uuid',
                dial_status='no-answer', attempt=attempt, host=HOST,
            )

        hop(0)
        session.refresh_from_db()
        self.assertEqual(session.sales_rep_id, second.id)
        hop(1)
        session.refresh_from_db()
        self.assertEqual(session.sales_rep_id, third.id)
        self.assertEqual(session.tried_reps[0], str(tried.id))

        # Proof the ordering alone would have re-offered the tried rep: they are free again
        # and still have the oldest stamp.
        self.assertEqual(self._presence(tried).status, 'AVAILABLE')
        self.assertEqual(self._select().user_id, tried.id)
