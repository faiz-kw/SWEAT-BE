"""
backend/tests/test_telephony_pool_and_load_fixes.py — Regression tests for Vobiz WebRTC pool exhaustion,
auto-heal collisions, concurrency guards, outbound calling guard, read-only GET presence,
SSE worker recycling, and inbound routing exclusion of unassigned/placeholder accounts.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.tenant_core.models_crm import AgentPresence, CallSession, Lead, LeadSource
from apps.tenant_core.models_org import Branch, Location, Organization
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.services_telephony_config import TelephonyConfigService
from config.routers import set_tenant_db_alias


class TelephonyPoolAndLoadFixesTests(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        super().setUp()
        set_tenant_db_alias('tenant_test')
        self.org = Organization.objects.create(name="SWEAT Test Org", code="SWPOOL01")
        self.location = Location.objects.create(organization=self.org, name="Mumbai", code="LOCP01")
        self.branch = Branch.objects.create(
            organization=self.org,
            location=self.location,
            name="Bandra",
            code="BANDRA01",
        )
        self.source = LeadSource.objects.create(organization=self.org, name="Walk-In", code="WALK01")

        self.user1 = TenantUser.objects.create(
            organization=self.org,
            email="agent1@sweat.test",
            first_name="Agent",
            last_name="One",
        )
        self.user2 = TenantUser.objects.create(
            organization=self.org,
            email="agent2@sweat.test",
            first_name="Agent",
            last_name="Two",
        )
        self.user3 = TenantUser.objects.create(
            organization=self.org,
            email="admin3@sweat.test",
            first_name="Admin",
            last_name="Three",
        )
        self.user4 = TenantUser.objects.create(
            organization=self.org,
            email="admin4@sweat.test",
            first_name="Admin",
            last_name="Four",
        )

    def test_third_and_fourth_users_get_unassigned_without_integrity_error(self):
        """When both Vobiz SIP endpoints are claimed, 3rd+ users receive unique unassigned_<id> placeholders."""
        c1 = TelephonyConfigService.get_agent_webrtc_credentials(self.user1, db_alias='tenant_test')
        c2 = TelephonyConfigService.get_agent_webrtc_credentials(self.user2, db_alias='tenant_test')
        c3 = TelephonyConfigService.get_agent_webrtc_credentials(self.user3, db_alias='tenant_test')
        c4 = TelephonyConfigService.get_agent_webrtc_credentials(self.user4, db_alias='tenant_test')

        self.assertTrue(c1['is_registered_endpoint'])
        self.assertEqual(c1['username'], 'iqra24030269280035568')
        self.assertTrue(c2['is_registered_endpoint'])
        self.assertEqual(c2['username'], 'sweatagent104201155654570218059')

        for creds, user in ((c3, self.user3), (c4, self.user4)):
            self.assertFalse(creds['is_registered_endpoint'])
            self.assertFalse(creds['available'])
            self.assertFalse(creds['can_make_calls'])
            self.assertEqual(creds['username'], '')
            self.assertEqual(creds['status'], 'UNASSIGNED')
            self.assertEqual(creds['line_status'], 'UNASSIGNED')
            self.assertTrue(creds['sip_username'].startswith('unassigned_'))
            presence = AgentPresence.objects.using('tenant_test').get(user=user)
            self.assertEqual(presence.status, 'OFFLINE')
            self.assertEqual(presence.sip_username, creds['sip_username'])

    def test_auto_heal_does_not_collide_when_pool_exhausted_and_upgrades_when_freed(self):
        """Auto-heal normalizes legacy agent_ rows safely when pool is full, then upgrades once a line opens."""
        TelephonyConfigService.get_agent_webrtc_credentials(self.user1, db_alias='tenant_test')
        TelephonyConfigService.get_agent_webrtc_credentials(self.user2, db_alias='tenant_test')

        AgentPresence.objects.using('tenant_test').create(
            organization=self.org,
            user=self.user3,
            sip_username='agent_legacy3',
            status='AVAILABLE',
        )

        # Pool is full -> must NOT collide with primary_endpoint
        c3_full = TelephonyConfigService.get_agent_webrtc_credentials(self.user3, db_alias='tenant_test')
        self.assertFalse(c3_full['is_registered_endpoint'])
        self.assertTrue(c3_full['sip_username'].startswith('unassigned_'))

        p3 = AgentPresence.objects.using('tenant_test').get(user=self.user3)
        self.assertEqual(p3.status, 'OFFLINE')

        # Calling again while still full must leave unassigned_ intact
        c3_again = TelephonyConfigService.get_agent_webrtc_credentials(self.user3, db_alias='tenant_test')
        self.assertEqual(c3_again['sip_username'], c3_full['sip_username'])

        # Free user2's endpoint -> user3 auto-upgrades to the freed endpoint
        AgentPresence.objects.using('tenant_test').filter(user=self.user2).delete()
        c3_upgraded = TelephonyConfigService.get_agent_webrtc_credentials(self.user3, db_alias='tenant_test')
        self.assertTrue(c3_upgraded['is_registered_endpoint'])
        self.assertEqual(c3_upgraded['username'], 'sweatagent104201155654570218059')

    def test_click_to_call_rejects_unassigned_agent_with_400(self):
        """Outbound Click-to-Call rejects unassigned admins instead of routing broken carrier calls."""
        TelephonyConfigService.get_agent_webrtc_credentials(self.user1, db_alias='tenant_test')
        TelephonyConfigService.get_agent_webrtc_credentials(self.user2, db_alias='tenant_test')
        TelephonyConfigService.get_agent_webrtc_credentials(self.user3, db_alias='tenant_test')

        lead = Lead.objects.create(
            organization=self.org,
            branch=self.branch,
            first_name="Rohan",
            last_name="Kapoor",
            phone_normalized="+919820011223",
            current_status="NEW_LEAD",
            assigned_sales_user=self.user3,
            lead_source=self.source,
        )

        from rest_framework.test import APIRequestFactory, force_authenticate
        from apps.tenant_core.views_telephony import ClickToCallView

        factory = APIRequestFactory()
        req = factory.post(
            '/api/v1/tenant/crm/telephony/click-to-call/',
            data={'lead_id': str(lead.id)},
            format='json',
        )
        force_authenticate(req, user=self.user3)
        set_tenant_db_alias('tenant_test')
        res = ClickToCallView.as_view()(req)

        self.assertEqual(res.status_code, 400, msg=getattr(res, 'data', None))
        self.assertEqual(res.data.get('code'), 'NO_TELEPHONY_LINE')
        self.assertEqual(CallSession.objects.using('tenant_test').filter(lead=lead).count(), 0)

    def test_presence_get_is_readonly_and_unassigned_put_remains_offline(self):
        """GET /presence/ performs zero heartbeat DB writes; PUT /presence/ keeps unassigned agents OFFLINE."""
        TelephonyConfigService.get_agent_webrtc_credentials(self.user1, db_alias='tenant_test')
        TelephonyConfigService.get_agent_webrtc_credentials(self.user2, db_alias='tenant_test')
        TelephonyConfigService.get_agent_webrtc_credentials(self.user3, db_alias='tenant_test')

        old_hb = timezone.now() - timedelta(minutes=5)
        AgentPresence.objects.using('tenant_test').filter(user=self.user1).update(last_heartbeat=old_hb)

        from contextlib import contextmanager
        from rest_framework.test import APIRequestFactory, force_authenticate
        from apps.tenant_core.views_telephony import AgentPresenceView

        @contextmanager
        def _tenant_ctx():
            set_tenant_db_alias('tenant_test')
            yield 'tenant_test'

        factory = APIRequestFactory()
        req1 = factory.get('/api/v1/tenant/crm/telephony/presence/')
        force_authenticate(req1, user=self.user1)
        with patch('apps.tenant_core.views_telephony.resolve_telephony_tenant_context', side_effect=_tenant_ctx):
            res = AgentPresenceView.as_view()(req1)

        set_tenant_db_alias('tenant_test')
        self.assertEqual(res.status_code, 200, msg=getattr(res, 'data', None))
        self.assertTrue(res.data.get('is_registered_endpoint'))

        p1 = AgentPresence.objects.using('tenant_test').get(user=self.user1)
        self.assertEqual(p1.last_heartbeat, old_hb)

        # Unassigned admin cannot force status=AVAILABLE via PUT
        req3 = factory.put(
            '/api/v1/tenant/crm/telephony/presence/',
            data={'status': 'AVAILABLE'},
            format='json',
        )
        force_authenticate(req3, user=self.user3)
        with patch('apps.tenant_core.views_telephony.resolve_telephony_tenant_context', side_effect=_tenant_ctx):
            put_res = AgentPresenceView.as_view()(req3)

        set_tenant_db_alias('tenant_test')
        self.assertEqual(put_res.status_code, 200, msg=getattr(put_res, 'data', None))
        self.assertEqual(put_res.data['status'], 'OFFLINE')
        self.assertFalse(put_res.data['is_registered_endpoint'])
        self.assertEqual(AgentPresence.objects.using('tenant_test').get(user=self.user3).status, 'OFFLINE')

    def test_inbound_routing_skips_unassigned_preferred_user(self):
        """Inbound call routing excludes unassigned_/agent_ placeholders even if set as preferred lead owner."""
        TelephonyConfigService.get_agent_webrtc_credentials(self.user1, db_alias='tenant_test')
        TelephonyConfigService.get_agent_webrtc_credentials(self.user2, db_alias='tenant_test')
        TelephonyConfigService.get_agent_webrtc_credentials(self.user3, db_alias='tenant_test')

        # Even if user3 somehow had AVAILABLE in DB, routing must ignore unassigned_ SIP usernames
        AgentPresence.objects.using('tenant_test').filter(user=self.user3).update(status='AVAILABLE')
        AgentPresence.objects.using('tenant_test').filter(user=self.user2).update(status='OFFLINE')

        selected = TelephonyConfigService.get_available_agent(
            preferred_user=self.user3,
            caller_branch=self.branch,
            org=self.org,
            db_alias='tenant_test',
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected.user_id, self.user1.id)
        self.assertEqual(selected.sip_username, 'iqra24030269280035568')

    def test_sse_stream_recycles_within_bounded_lifetime(self):
        """TelephonyEventsSseView yields finite chunks and terminates cleanly instead of holding WSGI workers forever."""
        fake_pubsub = MagicMock()
        fake_pubsub.get_message.return_value = None
        fake_redis = MagicMock()
        fake_redis.pubsub.return_value = fake_pubsub

        fake_tok = {'sub': str(self.user1.id), 'tid': str(self.org.id)}
        client = APIClient()

        # Advance monotonic clock: start=0, first check=10 (<45), second check=50 (>=45 -> break)
        with patch('rest_framework_simplejwt.tokens.AccessToken', return_value=fake_tok), \
             patch('redis.from_url', return_value=fake_redis), \
             patch('apps.tenant_core.views_telephony.connections.close_all') as mock_close_all, \
             patch('apps.tenant_core.views_telephony.time.monotonic', side_effect=[0.0, 10.0, 50.0]):
            res = client.get('/api/v1/tenant/crm/telephony/events/?token=dummy-jwt')
            self.assertEqual(res.status_code, 200)
            chunks = b''.join(res.streaming_content).decode('utf-8')

        self.assertIn('event: CONNECTED', chunks)
        self.assertIn(': stream_recycle', chunks)
        self.assertEqual(mock_close_all.call_count, 2)
        fake_pubsub.unsubscribe.assert_called_once()
        fake_pubsub.close.assert_called_once()
        fake_redis.close.assert_called_once()
