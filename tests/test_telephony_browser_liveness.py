"""Phase 3: agents are routable only while their browser is alive (browser_heartbeat_at)."""

import json
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.tenant_core.models_crm import AgentPresence, CallSession, Lead, LeadSource
from apps.tenant_core.models_org import Branch, Location, Organization
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.services_telephony_config import TelephonyConfigService
from config.routers import set_tenant_db_alias

DB = 'tenant_test'
FRESH = 10    # seconds since the last browser heartbeat: browser is open
STALE = 600   # seconds: browser was closed long ago
PRESENCE_URL = '/api/v1/tenant/crm/telephony/presence/'


class BrowserLivenessRoutingTests(TestCase):
    databases = frozenset({'default', 'tenant_test'})

    def setUp(self):
        super().setUp()
        set_tenant_db_alias(DB)
        self.org = Organization.objects.create(name="SWEAT Elite", code="SWEATLV")
        location = Location.objects.create(organization=self.org, name="Mumbai", code="LOCLV")
        self.andheri = Branch.objects.create(
            organization=self.org, location=location, name="Andheri West", code="ANDLV",
        )
        self.bandra = Branch.objects.create(
            organization=self.org, location=location, name="Bandra", code="BANLV",
        )
        source = LeadSource.objects.create(organization=self.org, name="Website", code="WEBLV")
        self.lead = Lead.objects.create(
            organization=self.org,
            branch=self.andheri,
            first_name="Vikram",
            last_name="Mehta",
            phone_normalized="+919876543210",
            current_status="NEW_LEAD",
            lead_source=source,
        )

    def _agent(self, email, sip, branch, browser_age, status='AVAILABLE'):
        """Creates an agent. browser_age = seconds since the last browser heartbeat (None = never)."""
        user = TenantUser.objects.create(
            organization=self.org,
            email=email,
            first_name=email.split('@')[0].title(),
            last_name="Rep",
            home_branch=branch,
        )
        return AgentPresence.objects.create(
            organization=self.org,
            user=user,
            sip_username=sip,
            status=status,
            browser_heartbeat_at=(
                None if browser_age is None else timezone.now() - timedelta(seconds=browser_age)
            ),
        )

    def _route(self, **kwargs):
        return TelephonyConfigService.get_available_agent(db_alias=DB, org=self.org, **kwargs)

    @staticmethod
    def _published(mock_from_url):
        publish = mock_from_url.return_value.publish
        return [(c.args[0], json.loads(c.args[1])) for c in publish.call_args_list]

    # ── Routing service ──────────────────────────────────────────────────────

    def test_stale_branch_agent_is_skipped_and_live_agent_is_chosen(self):
        """Replay of the 2026-11-16 incident: the branch rep's browser is closed, another rep is live."""
        stale = self._agent('aashna@sweatelite.in', 'sipbranch01', self.andheri, STALE)
        live = self._agent('iqra@sweatelite.in', 'siplive01', self.bandra, FRESH)

        selected = self._route(caller_branch=self.andheri)

        self.assertEqual(selected.user_id, live.user_id)
        stale.refresh_from_db()
        self.assertEqual(stale.status, 'AVAILABLE')  # skipped, not modified

    def test_agent_who_never_sent_a_browser_heartbeat_is_not_routable(self):
        self._agent('aashna@sweatelite.in', 'sipbranch01', self.andheri, None)

        self.assertIsNone(self._route(caller_branch=self.andheri))

    def test_live_branch_agent_still_wins_over_live_agent_from_another_branch(self):
        branch_agent = self._agent('aashna@sweatelite.in', 'sipbranch01', self.andheri, FRESH)
        self._agent('iqra@sweatelite.in', 'siplive01', self.bandra, FRESH)

        selected = self._route(caller_branch=self.andheri)

        self.assertEqual(selected.user_id, branch_agent.user_id)

    def test_stale_preferred_rep_is_skipped(self):
        preferred = self._agent('aashna@sweatelite.in', 'sipbranch01', self.andheri, STALE)
        live = self._agent('iqra@sweatelite.in', 'siplive01', self.bandra, FRESH)

        selected = self._route(preferred_user=preferred.user, caller_branch=self.andheri)

        self.assertEqual(selected.user_id, live.user_id)

    def test_liveness_window_is_configurable(self):
        agent = self._agent('aashna@sweatelite.in', 'sipbranch01', self.andheri, 60)

        self.assertEqual(self._route().user_id, agent.user_id)  # default 120 s window
        with override_settings(TELEPHONY_BROWSER_LIVENESS_SECONDS=30):
            self.assertIsNone(self._route())

    def test_self_heal_only_frees_agents_whose_browser_is_alive(self):
        """Step 4 must not hand a call to an ON_CALL agent whose browser is gone."""
        gone = self._agent('aashna@sweatelite.in', 'sipbranch01', self.andheri, STALE, status='ON_CALL')

        self.assertIsNone(self._route(caller_branch=self.andheri))
        gone.refresh_from_db()
        self.assertEqual(gone.status, 'ON_CALL')

        alive = self._agent('iqra@sweatelite.in', 'siplive01', self.bandra, FRESH, status='ON_CALL')
        selected = self._route(caller_branch=self.andheri)

        self.assertEqual(selected.user_id, alive.user_id)
        alive.refresh_from_db()
        self.assertEqual(alive.status, 'AVAILABLE')

    # ── Heartbeat endpoint ───────────────────────────────────────────────────

    def test_presence_put_records_browser_heartbeat_and_get_does_not(self):
        presence = self._agent('iqra@sweatelite.in', 'siplive01', self.bandra, STALE)
        before = presence.browser_heartbeat_at
        client = APIClient()
        client.force_authenticate(user=presence.user)

        res = client.get(PRESENCE_URL)
        set_tenant_db_alias(DB)
        self.assertEqual(res.status_code, 200)
        presence.refresh_from_db()
        self.assertEqual(presence.browser_heartbeat_at, before)

        res = client.put(PRESENCE_URL, {'status': 'AVAILABLE'}, format='json')
        set_tenant_db_alias(DB)
        self.assertEqual(res.status_code, 200)
        presence.refresh_from_db()
        self.assertGreater(presence.browser_heartbeat_at, before)
        self.assertLess(timezone.now() - presence.browser_heartbeat_at, timedelta(seconds=30))

    # ── Webhooks ─────────────────────────────────────────────────────────────

    @patch('apps.tenant_core.views_telephony.redis.from_url')
    def test_overflow_does_not_ring_agent_with_stale_browser(self, mock_from_url):
        """last_heartbeat is auto_now (fresh on create) but the browser is closed: must not be rung."""
        primary = self._agent('aashna@sweatelite.in', 'sipprimary01', self.andheri, FRESH, status='ON_CALL')
        stale = self._agent('iqra@sweatelite.in', 'sipstale01', self.bandra, STALE)
        session = CallSession.objects.create(
            organization=self.org,
            lead=self.lead,
            sales_rep=primary.user,
            provider='VOBIZ',
            provider_attempt_id='liveness-overflow-uuid',
            direction='INBOUND',
            call_mode='HUMAN_MANUAL',
            status='RINGING',
            user_phone_number='+919876543210',
            started_at=timezone.now(),
        )

        res = self.client.post(
            f'/api/v1/telephony/dial-fallback/?session_id={session.id}',
            data={'CallUUID': 'liveness-overflow-uuid', 'DialStatus': 'no-answer'},
        )
        set_tenant_db_alias(DB)

        self.assertEqual(res.status_code, 200)
        self.assertNotIn('sipstale01', res.content.decode())
        session.refresh_from_db()
        stale.refresh_from_db()
        self.assertEqual(session.status, 'NO_ANSWER')
        self.assertEqual(stale.status, 'AVAILABLE')

    @patch('apps.tenant_core.views_telephony.redis.from_url')
    def test_inbound_call_skips_offline_branch_agent_and_rings_live_agent(self, mock_from_url):
        """End-to-end replay: popup and ring go to the live agent, the offline one is untouched."""
        stale = self._agent('aashna@sweatelite.in', 'sipbranch01', self.andheri, STALE)
        live = self._agent('iqra@sweatelite.in', 'siplive01', self.bandra, FRESH)

        res = self.client.post('/api/v1/telephony/inbound/', data={
            'CallUUID': 'liveness-inbound-uuid',
            'From': '+919876543210',
            'To': '+919200000001',
        })
        set_tenant_db_alias(DB)

        self.assertEqual(res.status_code, 200)
        body = res.content.decode()
        self.assertIn('sip:siplive01@', body)
        self.assertNotIn('sipbranch01', body)
        stale.refresh_from_db()
        live.refresh_from_db()
        self.assertEqual(stale.status, 'AVAILABLE')
        self.assertEqual(live.status, 'ON_CALL')
        session = CallSession.objects.get(provider_attempt_id='liveness-inbound-uuid')
        self.assertEqual(session.sales_rep_id, live.user_id)
        channels = [channel for channel, _ in self._published(mock_from_url)]
        self.assertEqual(len(channels), 1)
        self.assertTrue(channels[0].endswith(str(live.user_id).lower()))

    @patch('apps.tenant_core.views_telephony.redis.from_url')
    def test_inbound_call_with_no_live_agent_is_logged_as_missed(self, mock_from_url):
        self._agent('aashna@sweatelite.in', 'sipbranch01', self.andheri, STALE)

        res = self.client.post('/api/v1/telephony/inbound/', data={
            'CallUUID': 'liveness-nobody-uuid',
            'From': '+919876543210',
            'To': '+919200000001',
        })
        set_tenant_db_alias(DB)

        self.assertEqual(res.status_code, 200)
        body = res.content.decode()
        self.assertIn('<Speak', body)
        self.assertNotIn('<Dial', body)
        session = CallSession.objects.get(provider_attempt_id='liveness-nobody-uuid')
        self.assertEqual(session.status, 'NO_ANSWER')
        events = [evt['event'] for _, evt in self._published(mock_from_url)]
        self.assertIn('MISSED_CALL', events)
