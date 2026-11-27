"""
Comprehensive tests for the multi-studio TimeWatch / Dahua turnstile fleet:
- TurnstileDevice model (base_url, category matching, Django Admin registration)
- TurnstileActiveTokensView / TurnstileDeviceListView / TurnstileStaleTokensView
  (HMAC auth, automatic upcoming-booking token generation, start_at serialization,
   cancelled token revocation list, UUID/code branch resolution)
- TurnstileVerifyPunchView (gateway API-key guard, branch isolation, room-level
  category routing, preservation of CONSUMED_ENTERED across MobileQRPassView re-opens)
- Booking cancellation & reschedule synchronization with AccessPass
- MobileDirectTurnstileOpenView permission enforcement (never AllowAny)
- dahua_rpc.py & sweat_turnstile_daemon.py unit checks with mocked HTTP
"""

import hashlib
import hmac
import importlib.util
import os
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.contrib import admin
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.master.models import Tenant, TenantDataSource
from apps.tenant_core import dahua_rpc
from apps.tenant_core.communication.whatsapp_tokens import generate_trial_action_token
from apps.tenant_core.context import set_tenant_db_alias
from apps.tenant_core.models import (
    AccessEvent,
    AccessPass,
    Booking,
    Branch,
    ClassCategory,
    ClassOccurrence,
    ClassTemplate,
    Lead,
    Location,
    Organization,
    Program,
    ProgramCategory,
    TenantUser,
    TrialBooking,
    TurnstileDevice,
    UserProfile,
)
from apps.tenant_core.serializers_bookings import AccessEventSerializer
from apps.tenant_core.services_access_control import (
    generate_booking_qr_token,
    generate_trial_qr_token,
    hash_qr_token,
)
from apps.tenant_core.services_bookings import BookingWaitlistAttendanceService
from apps.tenant_core.services_crm import CRMLeadService
from apps.tenant_core.views_bookings import TurnstileVerifyPunchView
from apps.tenant_core.views_mobile import MobileDirectTurnstileOpenView, MobileQRPassView
from apps.tenant_core.views_public_trial import PublicTrialPassView
from apps.tenant_core.views_turnstile import (
    TurnstileActiveTokensView,
    TurnstileDeviceListView,
    TurnstileStaleTokensView,
)


def _make_hmac_bearer(secret: str, branch_ref: str) -> str:
    now_utc = timezone.now().replace(minute=0, second=0, microsecond=0)
    hour_epoch = int(now_utc.timestamp())
    msg = f'SWEAT:active-tokens:{branch_ref}:{hour_epoch}'.encode()
    digest = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return f'Bearer {digest}'


class TurnstileFleetTests(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        self.db = 'tenant_test'
        self.factory = APIRequestFactory()
        self.sync_secret = 'unit-test-turnstile-sync-secret'

        self.tenant = Tenant.objects.using('default').create(
            name='SWEAT Turnstile Test',
            code=f'STT-{uuid.uuid4().hex[:6]}',
            slug=f'sweat-flt-{uuid.uuid4().hex[:6]}',
            status='ACTIVE',
        )
        self.org = Organization.objects.using(self.db).create(
            name='SWEAT Org',
            code=f'ORG-{uuid.uuid4().hex[:6]}',
            status='ACTIVE',
        )
        self.loc = Location.objects.using(self.db).create(
            organization=self.org,
            code=f'LOC-{uuid.uuid4().hex[:4]}',
            name='Mumbai West',
            city='Mumbai',
            status='ACTIVE',
        )
        self.branch_malad = Branch.objects.using(self.db).create(
            organization=self.org,
            location=self.loc,
            code=f'MAL-{uuid.uuid4().hex[:4]}',
            name='Sweat Malad West',
            status='ACTIVE',
        )
        self.branch_andheri = Branch.objects.using(self.db).create(
            organization=self.org,
            location=self.loc,
            code=f'AND-{uuid.uuid4().hex[:4]}',
            name='Sweat Andheri West',
            status='ACTIVE',
        )

        self.user = TenantUser.objects.using(self.db).create(
            organization=self.org,
            email=f'flt-{uuid.uuid4().hex[:6]}@example.com',
            first_name='Aarav',
            last_name='Mehta',
            status='ACTIVE',
        )
        self.profile = UserProfile.objects.using(self.db).create(
            user=self.user,
            first_name_snapshot='Aarav',
            last_name_snapshot='Mehta',
            member_number=f'MEM-{uuid.uuid4().hex[:6].upper()}',
            preferred_branch=self.branch_malad,
            member_status='ACTIVE',
        )

        self.cat_pilates = ClassCategory.objects.using(self.db).create(
            organization=self.org, name='Pilates', code='PILATES', status='ACTIVE'
        )
        self.cat_bootcamp = ClassCategory.objects.using(self.db).create(
            organization=self.org, name='Bootcamp', code='BOOTCAMP', status='ACTIVE'
        )
        prog_cat = ProgramCategory.objects.using(self.db).create(
            organization=self.org, name='Studio', code=f'PC-{uuid.uuid4().hex[:4]}', status='ACTIVE'
        )
        prog = Program.objects.using(self.db).create(
            organization=self.org, category=prog_cat, name='Reformer', code=f'PR-{uuid.uuid4().hex[:4]}', status='ACTIVE'
        )
        self.tpl_pilates = ClassTemplate.objects.using(self.db).create(
            organization=self.org, program=prog, category=self.cat_pilates,
            name='Reformer Flow', code=f'TPL-P-{uuid.uuid4().hex[:4]}',
            default_capacity=15, default_duration_minutes=50, allow_reschedule=True, status='ACTIVE',
        )
        self.tpl_bootcamp = ClassTemplate.objects.using(self.db).create(
            organization=self.org, program=prog, category=self.cat_bootcamp,
            name='Sweat Bootcamp', code=f'TPL-B-{uuid.uuid4().hex[:4]}',
            default_capacity=20, default_duration_minutes=50, allow_reschedule=True, status='ACTIVE',
        )

        now = timezone.now()
        self.occ_andheri_pilates = ClassOccurrence.objects.using(self.db).create(
            branch=self.branch_andheri,
            class_template=self.tpl_pilates,
            occurrence_date=now.date(),
            start_at=now + timedelta(minutes=10),
            end_at=now + timedelta(minutes=60),
            capacity=15,
            status='SCHEDULED',
        )
        self.occ_later_pilates = ClassOccurrence.objects.using(self.db).create(
            branch=self.branch_andheri,
            class_template=self.tpl_pilates,
            occurrence_date=(now + timedelta(hours=4)).date(),
            start_at=now + timedelta(hours=4),
            end_at=now + timedelta(hours=5),
            capacity=15,
            status='SCHEDULED',
        )

        self.booking_pilates = Booking.objects.using(self.db).create(
            booking_number=f'BKG-{uuid.uuid4().hex[:8].upper()}',
            user_profile=self.profile,
            occurrence=self.occ_andheri_pilates,
            branch=self.branch_andheri,
            booking_type='MEMBER',
            booking_source='MOBILE_APP',
            status='CONFIRMED',
            payment_status='PAID',
            confirmation_status='CONFIRMED',
            confirmed_at=now,
        )

        # TurnstileDevice rows for Andheri (Pilates vs Bootcamp) & Malad (universal)
        self.dev_andheri_pilates = TurnstileDevice.objects.using(self.db).create(
            branch=self.branch_andheri,
            name='Andheri Pilates Entry',
            direction='ENTRY',
            host='192.168.1.108',
            port=80,
            class_category_code='PILATES',
            is_active=True,
        )
        self.dev_andheri_bootcamp = TurnstileDevice.objects.using(self.db).create(
            branch=self.branch_andheri,
            name='Andheri Bootcamp Entry',
            direction='ENTRY',
            host='192.168.1.111',
            port=80,
            class_category_code='BOOTCAMP',
            is_active=True,
        )
        self.dev_malad_inactive = TurnstileDevice.objects.using(self.db).create(
            branch=self.branch_malad,
            name='Malad Old Spare',
            direction='ENTRY',
            host='192.168.0.199',
            port=80,
            is_active=False,
        )

    def test_turnstile_device_model_and_admin_registration(self):
        self.assertTrue(TurnstileDevice in admin.site._registry)
        self.assertEqual(self.dev_andheri_pilates.base_url, 'http://192.168.1.108')
        dev_port = TurnstileDevice(host='203.0.113.10', port=8114, class_category_code='')
        self.assertEqual(dev_port.base_url, 'http://203.0.113.10:8114')
        self.assertTrue(dev_port.matches_category('PILATES'))
        self.assertTrue(dev_port.matches_category(''))
        self.assertTrue(self.dev_andheri_pilates.matches_category('pilates'))
        self.assertFalse(self.dev_andheri_pilates.matches_category('BOOTCAMP'))
        self.assertFalse(self.dev_andheri_pilates.matches_category(''))

    def test_active_tokens_auto_provisions_upcoming_confirmed_bookings_and_hmac(self):
        """
        Confirmed bookings within 3h MUST be returned by active-tokens even BEFORE
        the member opens MobileQRPassView, and must include valid `occurrence_start`.
        """
        view = TurnstileActiveTokensView.as_view()

        # 1. Missing branch_id -> 400
        res_400 = view(self.factory.get('/api/v1/tenant/access/turnstile/active-tokens/'))
        self.assertEqual(res_400.status_code, 400)

        # 2. Invalid HMAC -> 401
        with patch.dict(os.environ, {'TURNSTILE_SYNC_SECRET': self.sync_secret}):
            req_bad = self.factory.get(
                f'/api/v1/tenant/access/turnstile/active-tokens/?branch_id={self.branch_andheri.id}',
                HTTP_AUTHORIZATION='Bearer wrongtoken',
            )
            self.assertEqual(view(req_bad).status_code, 401)

            # 3. Valid HMAC -> 200 and ensures AccessPass exists with start_at populated
            auth = _make_hmac_bearer(self.sync_secret, str(self.branch_andheri.id))
            req_ok = self.factory.get(
                f'/api/v1/tenant/access/turnstile/active-tokens/?branch_id={self.branch_andheri.id}',
                HTTP_AUTHORIZATION=auth,
            )
            req_ok._tenant_alias = self.db
            res_ok = view(req_ok)

            self.assertEqual(res_ok.status_code, 200)
            self.assertEqual(res_ok.data['count'], 1)
            item = res_ok.data['tokens'][0]
            self.assertEqual(len(item['token']), 22)
            self.assertEqual(item['class_category_code'], 'PILATES')
            self.assertIsNotNone(item['occurrence_start'])
            self.assertTrue(AccessPass.objects.using(self.db).filter(booking=self.booking_pilates, status='ACTIVE').exists())

            # 4. Non-UUID branch code resolves cleanly without 500
            auth_code = _make_hmac_bearer(self.sync_secret, self.branch_andheri.code)
            req_code = self.factory.get(
                f'/api/v1/tenant/access/turnstile/active-tokens/?branch_id={self.branch_andheri.code}',
                HTTP_AUTHORIZATION=auth_code,
            )
            req_code._tenant_alias = self.db
            res_code = view(req_code)
            self.assertEqual(res_code.status_code, 200)
            self.assertEqual(res_code.data['count'], 1)

            # 5. Unknown non-UUID branch returns 404 (not 500)
            auth_missing = _make_hmac_bearer(self.sync_secret, 'no-such-studio')
            req_missing = self.factory.get(
                '/api/v1/tenant/access/turnstile/active-tokens/?branch_id=no-such-studio',
                HTTP_AUTHORIZATION=auth_missing,
            )
            req_missing._tenant_alias = self.db
            self.assertEqual(view(req_missing).status_code, 404)

    def test_devices_and_stale_tokens_endpoints(self):
        dev_view = TurnstileDeviceListView.as_view()
        stale_view = TurnstileStaleTokensView.as_view()

        with patch.dict(os.environ, {'TURNSTILE_SYNC_SECRET': self.sync_secret}):
            auth_a = _make_hmac_bearer(self.sync_secret, str(self.branch_andheri.id))
            req_d = self.factory.get(
                f'/api/v1/tenant/access/turnstile/devices/?branch_id={self.branch_andheri.id}',
                HTTP_AUTHORIZATION=auth_a,
            )
            req_d._tenant_alias = self.db
            res_d = dev_view(req_d)
            self.assertEqual(res_d.status_code, 200)
            self.assertEqual(res_d.data['count'], 2)

            # Inactive Malad device must NOT be returned
            auth_m = _make_hmac_bearer(self.sync_secret, str(self.branch_malad.id))
            req_m = self.factory.get(
                f'/api/v1/tenant/access/turnstile/devices/?branch_id={self.branch_malad.id}',
                HTTP_AUTHORIZATION=auth_m,
            )
            req_m._tenant_alias = self.db
            res_m = dev_view(req_m)
            self.assertEqual(res_m.data['count'], 0)

            # Old expired AccessPass returned by stale-tokens
            from django.conf import settings
            tok = generate_booking_qr_token(str(self.booking_pilates.id), settings.SECRET_KEY)
            AccessPass.objects.using(self.db).create(
                user_profile=self.profile,
                booking=self.booking_pilates,
                branch=self.branch_andheri,
                token_hash=hash_qr_token(tok),
                valid_from=timezone.now() - timedelta(days=10),
                valid_until=timezone.now() - timedelta(days=9),
                status='EXPIRED',
                updated_at=timezone.now() - timedelta(days=8),
            )
            req_s = self.factory.get(
                f'/api/v1/tenant/access/turnstile/stale-tokens/?branch_id={self.branch_andheri.id}&days=7',
                HTTP_AUTHORIZATION=auth_a,
            )
            req_s._tenant_alias = self.db
            res_s = stale_view(req_s)
            self.assertEqual(res_s.status_code, 200)
            self.assertIn(tok, res_s.data['tokens'])

    def test_verify_punch_gateway_key_branch_and_room_routing(self):
        from django.conf import settings
        tok = generate_booking_qr_token(str(self.booking_pilates.id), settings.SECRET_KEY)
        AccessPass.objects.using(self.db).create(
            user_profile=self.profile,
            booking=self.booking_pilates,
            branch=self.branch_andheri,
            token_hash=hash_qr_token(tok),
            valid_from=timezone.now() - timedelta(minutes=15),
            valid_until=timezone.now() + timedelta(hours=2),
            status='ACTIVE',
        )
        view = TurnstileVerifyPunchView.as_view()

        with patch.dict(os.environ, {'TURNSTILE_GATEWAY_API_KEY': 'gw-secret-999'}):
            # 1. Missing API key -> 401
            req_401 = self.factory.post(
                '/api/v1/tenant/access/turnstile/verify-punch/',
                {'card_no': tok, 'branch_id': str(self.branch_andheri.id), 'device_reference': 'Andheri Pilates Entry'},
                format='json',
            )
            req_401._tenant_alias = self.db
            self.assertEqual(view(req_401).status_code, 401)

            # 2. Wrong branch (Malad gate scanning Andheri booking) -> DENIED WRONG_BRANCH
            req_br = self.factory.post(
                '/api/v1/tenant/access/turnstile/verify-punch/',
                {'card_no': tok, 'branch_id': str(self.branch_malad.id), 'device_reference': 'Malad Entry'},
                format='json',
                HTTP_X_TURNSTILE_API_KEY='gw-secret-999',
            )
            req_br._tenant_alias = self.db
            res_br = view(req_br)
            self.assertEqual(res_br.status_code, 200)
            self.assertFalse(res_br.data['allow_access'])
            self.assertEqual(res_br.data['reason'], 'WRONG_BRANCH')

            # 3. Wrong room gate (Andheri Bootcamp gate scanning Pilates booking) -> DENIED WRONG_ROOM_GATE
            req_rm = self.factory.post(
                '/api/v1/tenant/access/turnstile/verify-punch/',
                {'card_no': tok, 'branch_id': str(self.branch_andheri.id), 'device_reference': 'Andheri Bootcamp Entry'},
                format='json',
                HTTP_X_TURNSTILE_API_KEY='gw-secret-999',
            )
            req_rm._tenant_alias = self.db
            res_rm = view(req_rm)
            self.assertEqual(res_rm.status_code, 200)
            self.assertFalse(res_rm.data['allow_access'])
            self.assertEqual(res_rm.data['reason'], 'WRONG_ROOM_GATE')

            # 4. Correct branch + Pilates gate -> GRANTED
            req_ok = self.factory.post(
                '/api/v1/tenant/access/turnstile/verify-punch/',
                {'card_no': tok, 'branch_id': str(self.branch_andheri.id), 'device_reference': 'Andheri Pilates Entry'},
                format='json',
                HTTP_X_TURNSTILE_API_KEY='gw-secret-999',
            )
            req_ok._tenant_alias = self.db
            res_ok = view(req_ok)
            self.assertEqual(res_ok.status_code, 200)
            self.assertTrue(res_ok.data['allow_access'])

            # 5. Re-opening MobileQRPassView AFTER entry must NOT overwrite CONSUMED_ENTERED back to ACTIVE
            qr_view = MobileQRPassView.as_view()
            req_qr = self.factory.get(f'/api/v1/mobile/bookings/{self.booking_pilates.id}/qr-pass/')
            req_qr._tenant_alias = self.db
            force_authenticate(req_qr, user=self.user)
            res_qr = qr_view(req_qr, booking_id=self.booking_pilates.id)
            self.assertEqual(res_qr.status_code, 200)
            self.assertEqual(res_qr.data['status'], 'COMPLETED')
            ap = AccessPass.objects.using(self.db).get(booking=self.booking_pilates)
            self.assertEqual(ap.status, 'CONSUMED_ENTERED')

    def test_cancel_and_reschedule_synchronize_access_pass(self):
        from django.conf import settings
        now = timezone.now()
        occ_resched_from = ClassOccurrence.objects.using(self.db).create(
            branch=self.branch_andheri,
            class_template=self.tpl_pilates,
            occurrence_date=(now + timedelta(hours=2, minutes=30)).date(),
            start_at=now + timedelta(hours=2, minutes=30),
            end_at=now + timedelta(hours=3, minutes=20),
            capacity=15,
            status='SCHEDULED',
        )
        booking_future = Booking.objects.using(self.db).create(
            booking_number=f'BKG-{uuid.uuid4().hex[:8].upper()}',
            user_profile=self.profile,
            occurrence=occ_resched_from,
            branch=self.branch_andheri,
            booking_type='MEMBER',
            booking_source='MOBILE_APP',
            status='CONFIRMED',
            payment_status='PAID',
            confirmation_status='CONFIRMED',
            confirmed_at=now,
        )
        tok = generate_booking_qr_token(str(booking_future.id), settings.SECRET_KEY)
        ap = AccessPass.objects.using(self.db).create(
            user_profile=self.profile,
            booking=booking_future,
            branch=self.branch_andheri,
            token_hash=hash_qr_token(tok),
            valid_from=now + timedelta(hours=2, minutes=10),
            valid_until=now + timedelta(hours=4, minutes=20),
            status='ACTIVE',
        )

        # Reschedule updates valid_from / valid_until (outside 2h cutoff)
        old_until = ap.valid_until
        BookingWaitlistAttendanceService.reschedule_booking(
            booking=booking_future,
            to_occurrence=self.occ_later_pilates,
            rescheduled_by_user=self.user,
            db_alias=self.db,
        )
        ap.refresh_from_db(using=self.db)
        self.assertGreater(ap.valid_until, old_until)

        # Cancel marks AccessPass CANCELLED & active-tokens includes it under revoked_tokens
        BookingWaitlistAttendanceService.cancel_booking(
            booking=booking_future,
            cancelled_by_user=self.user,
            db_alias=self.db,
        )
        ap.refresh_from_db(using=self.db)
        self.assertEqual(ap.status, 'CANCELLED')

        with patch.dict(os.environ, {'TURNSTILE_SYNC_SECRET': self.sync_secret}):
            auth = _make_hmac_bearer(self.sync_secret, str(self.branch_andheri.id))
            req = self.factory.get(
                f'/api/v1/tenant/access/turnstile/active-tokens/?branch_id={self.branch_andheri.id}',
                HTTP_AUTHORIZATION=auth,
            )
            req._tenant_alias = self.db
            res = TurnstileActiveTokensView.as_view()(req)
            self.assertIn(tok, res.data['revoked_tokens'])
            # booking_future cancelled, only booking_pilates remains active
            tokens_returned = [t['token'] for t in res.data['tokens']]
            self.assertNotIn(tok, tokens_returned)

    def test_mobile_direct_open_requires_authentication(self):
        view = MobileDirectTurnstileOpenView.as_view()
        req = self.factory.post('/api/v1/mobile/turnstile/open-direct/', {}, format='json')
        res = view(req)
        self.assertIn(res.status_code, (401, 403))

    def test_dahua_rpc_and_daemon_helpers_mocked(self):
        with patch.dict(os.environ, {'DAHUA_ADMIN_USER': 'admin', 'DAHUA_ADMIN_PASS': 'testpw'}):
            # Verify subscribe_events has no NameError and parses CardNo cleanly
            stream_mock = MagicMock()
            stream_mock.read.side_effect = [
                b'--myboundary\r\nCode=AccessControl;"CardNo":"ABC123"\r\n--myboundary',
                b'',
            ]
            ctx = MagicMock()
            ctx.__enter__.return_value = stream_mock
            ctx.__exit__.return_value = False
            opener_mock = MagicMock()
            opener_mock.open.return_value = ctx
            with patch('urllib.request.build_opener', return_value=opener_mock):
                events = list(dahua_rpc.subscribe_events('http://127.0.0.1:9999'))
            self.assertEqual(events, ['ABC123'])

        # Test universal daemon sends X-Turnstile-Api-Key, branch_id, and device_reference
        daemon_path = Path(__file__).resolve().parents[2] / 'sweat_turnstile_daemon.py'
        spec = importlib.util.spec_from_file_location('sweat_turnstile_daemon_test', daemon_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules['sweat_turnstile_daemon_test'] = mod
        spec.loader.exec_module(mod)

        with patch.dict(os.environ, {
            'SWEAT_BRANCH_ID': str(self.branch_andheri.id),
            'TURNSTILE_SYNC_SECRET': self.sync_secret,
            'TURNSTILE_GATEWAY_API_KEY': 'gw-key-xyz',
            'DAHUA_ADMIN_PASS': 'pw123',
        }):
            mod._init_config()
            term = mod.TerminalConfig(
                name='Andheri Pilates Entry',
                base_url='http://203.0.113.10:8108',
                direction='ENTRY',
                class_category_code='PILATES',
                is_active=True,
                branch_id=str(self.branch_andheri.id),
            )
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {'allow_access': True, 'user_name': 'Aarav'}
            with patch('requests.post', return_value=mock_resp) as post_spy:
                res = mod._verify_token_with_cloud('TOKEN123', 'ENTRY', terminal=term)
            self.assertTrue(res['allow_access'])
            _, kwargs = post_spy.call_args
            self.assertEqual(kwargs['headers']['X-Turnstile-Api-Key'], 'gw-key-xyz')
            self.assertEqual(kwargs['json']['branch_id'], str(self.branch_andheri.id))
            self.assertEqual(kwargs['json']['device_reference'], 'Andheri Pilates Entry')

            # Verify _listen_terminal dispatches single small (<512B) AccessControl event via readline()
            import threading as _th
            stop_ev = _th.Event()
            lines = [
                b'--myboundary\r\n',
                b'Content-Type: text/plain\r\n',
                b'Code=AccessControl;action=Pulse;index=0;data={\r\n',
                b'  "CardNo" : "SmallEventTok123"\r\n',
                b'}\r\n',
                b'',
            ]
            line_iter = iter(lines)
            stream_mock2 = MagicMock()
            stream_mock2.headers = {'Content-Type': 'multipart/x-mixed-replace; boundary=myboundary'}
            def _next_line():
                val = next(line_iter, b'')
                if not val:
                    stop_ev.set()
                return val
            stream_mock2.readline.side_effect = _next_line
            ctx2 = MagicMock()
            ctx2.__enter__.return_value = stream_mock2
            ctx2.__exit__.return_value = False
            opener2 = MagicMock()
            opener2.open.return_value = ctx2

            captured = []
            with patch('urllib.request.build_opener', return_value=opener2), \
                 patch.object(mod, '_process_scan', side_effect=lambda c, d, t: captured.append((c, d, t.name))):
                mod._listen_terminal(term, stop_ev)
                import time as _t; _t.sleep(0.05)
            self.assertEqual(captured, [('SmallEventTok123', 'ENTRY', 'Andheri Pilates Entry')])

            # Verify _process_scan on ENTRY opens gate and deletes card from terminal
            with patch.object(mod, '_verify_token_with_cloud', return_value={'allow_access': True, 'user_name': 'Aarav'}), \
                 patch.object(mod, '_open_physical_gate') as open_spy, \
                 patch.object(mod, '_delete_cards_batch') as del_spy:
                mod._process_scan('SmallEventTok123', 'ENTRY', term)
            open_spy.assert_called_once()
            del_spy.assert_called_once_with(term, ['SmallEventTok123'])

    def test_trial_booking_active_tokens_public_pass_punch_and_lifecycle(self):
        from django.conf import settings
        TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            db_name='test_fitness_tenant',
            database_name='test_fitness_tenant',
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )
        now = timezone.now()
        lead = Lead.objects.using(self.db).create(
            organization=self.org,
            branch=self.branch_andheri,
            first_name='Riya',
            last_name='Sharma',
            phone_normalized='+919820012345',
            email_normalized='riya.trial@example.com',
        )
        trial = TrialBooking.objects.using(self.db).create(
            lead=lead,
            user_profile=None,
            branch=self.branch_andheri,
            class_occurrence_id=self.occ_andheri_pilates.id,
            trial_type='PILATES',
            scheduled_start=self.occ_andheri_pilates.start_at,
            scheduled_end=self.occ_andheri_pilates.end_at,
            status='CONFIRMED',
            confirmation_status='CONFIRMED',
        )
        trial_token = generate_trial_qr_token(str(trial.id), settings.SECRET_KEY)
        self.assertEqual(len(trial_token), 22)

        # 1. active-tokens returns trial token with class category metadata & creates AccessPass
        with patch.dict(os.environ, {'TURNSTILE_SYNC_SECRET': self.sync_secret}):
            auth = _make_hmac_bearer(self.sync_secret, str(self.branch_andheri.id))
            req_sync = self.factory.get(
                f'/api/v1/tenant/access/turnstile/active-tokens/?branch_id={self.branch_andheri.id}',
                HTTP_AUTHORIZATION=auth,
            )
            req_sync._tenant_alias = self.db
            res_sync = TurnstileActiveTokensView.as_view()(req_sync)
            self.assertEqual(res_sync.status_code, 200)
            by_tok = {t['token']: t for t in res_sync.data['tokens']}
            self.assertIn(trial_token, by_tok)
            self.assertEqual(by_tok[trial_token]['class_category_code'], 'PILATES')
            self.assertEqual(by_tok[trial_token]['display_name'], 'Riya Sharma')

        # 2. PublicTrialPassView renders deterministic 22-char token (not legacy SWEAT:TRIAL:<uuid>)
        signed_pass_tok = generate_trial_action_token(trial.id, 'PASS', tenant_id=self.tenant.id)
        req_pub = self.factory.get(f'/p/{signed_pass_tok}/')
        res_pub = PublicTrialPassView.as_view()(req_pub, token=signed_pass_tok)
        self.assertEqual(res_pub.status_code, 200)
        body_html = res_pub.content.decode('utf-8')
        self.assertIn(trial_token, body_html)
        self.assertNotIn(f'SWEAT:TRIAL:{trial.id}', body_html)

        # 3. TurnstileVerifyPunchView enforces room gate, grants ENTRY, marks Trial ATTENDED, and completes EXIT
        punch_view = TurnstileVerifyPunchView.as_view()
        with patch.dict(os.environ, {'TURNSTILE_GATEWAY_API_KEY': 'gw-trial-key'}):
            # Wrong room gate (Bootcamp) denied
            req_wrong = self.factory.post(
                '/api/v1/tenant/access/turnstile/verify-punch/',
                {'card_no': trial_token, 'branch_id': str(self.branch_andheri.id), 'device_reference': 'Andheri Bootcamp Entry'},
                format='json',
                HTTP_X_TURNSTILE_API_KEY='gw-trial-key',
            )
            req_wrong._tenant_alias = self.db
            res_wrong = punch_view(req_wrong)
            self.assertFalse(res_wrong.data['allow_access'])
            self.assertEqual(res_wrong.data['reason'], 'WRONG_ROOM_GATE')

            # Correct room gate (Pilates) grants ENTRY & marks trial ATTENDED
            req_in = self.factory.post(
                '/api/v1/tenant/access/turnstile/verify-punch/',
                {'card_no': trial_token, 'branch_id': str(self.branch_andheri.id), 'device_reference': 'Andheri Pilates Entry'},
                format='json',
                HTTP_X_TURNSTILE_API_KEY='gw-trial-key',
            )
            req_in._tenant_alias = self.db
            res_in = punch_view(req_in)
            self.assertEqual(res_in.status_code, 200)
            self.assertTrue(res_in.data['allow_access'])
            self.assertEqual(res_in.data['direction'], 'ENTRY')
            self.assertEqual(res_in.data['user_name'], 'Riya Sharma')

            trial.refresh_from_db(using=self.db)
            self.assertEqual(trial.status, 'ATTENDED')
            ap = AccessPass.objects.using(self.db).get(trial_booking=trial)
            self.assertEqual(ap.status, 'CONSUMED_ENTERED')

            evt = AccessEvent.objects.using(self.db).filter(trial_booking=trial, event_type='ENTRY').first()
            self.assertIsNotNone(evt)
            self.assertIsNone(evt.user_profile)
            self.assertEqual(AccessEventSerializer(evt).data['member_name'], 'Riya Sharma (Trial)')

            # Duplicate ENTRY replay denied
            req_replay = self.factory.post(
                '/api/v1/tenant/access/turnstile/verify-punch/',
                {'card_no': trial_token, 'branch_id': str(self.branch_andheri.id), 'device_reference': 'Andheri Pilates Entry'},
                format='json',
                HTTP_X_TURNSTILE_API_KEY='gw-trial-key',
            )
            req_replay._tenant_alias = self.db
            res_replay = punch_view(req_replay)
            self.assertFalse(res_replay.data['allow_access'])
            self.assertEqual(res_replay.data['reason'], 'REPLAY_ATTEMPT')

            # EXIT scan completes pass lifecycle
            req_out = self.factory.post(
                '/api/v1/tenant/access/turnstile/verify-punch/',
                {'card_no': trial_token, 'branch_id': str(self.branch_andheri.id), 'device_reference': 'Andheri Pilates Exit'},
                format='json',
                HTTP_X_TURNSTILE_API_KEY='gw-trial-key',
            )
            req_out._tenant_alias = self.db
            res_out = punch_view(req_out)
            self.assertTrue(res_out.data['allow_access'])
            self.assertEqual(res_out.data['direction'], 'EXIT')
            ap.refresh_from_db(using=self.db)
            self.assertEqual(ap.status, 'CONSUMED_EXITED')

    def test_trial_reschedule_and_cancel_sync_access_pass(self):
        from django.conf import settings
        TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            db_name='test_fitness_tenant',
            database_name='test_fitness_tenant',
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )
        self.tpl_pilates.allow_trial = True
        self.tpl_pilates.default_trial_capacity = 5
        self.tpl_pilates.save(using=self.db, update_fields=['allow_trial', 'default_trial_capacity'])

        now = timezone.now()
        occ_future_trial = ClassOccurrence.objects.using(self.db).create(
            branch=self.branch_andheri,
            class_template=self.tpl_pilates,
            occurrence_date=(now + timedelta(hours=2)).date(),
            start_at=now + timedelta(hours=2),
            end_at=now + timedelta(hours=2, minutes=50),
            capacity=15,
            trial_capacity=5,
            status='SCHEDULED',
        )
        self.occ_later_pilates.trial_capacity = 5
        self.occ_later_pilates.save(using=self.db, update_fields=['trial_capacity'])

        lead = Lead.objects.using(self.db).create(
            organization=self.org,
            branch=self.branch_andheri,
            first_name='Kabir',
            last_name='Nair',
            phone_normalized='+919820099999',
            email_normalized='kabir.trial@example.com',
        )
        trial = TrialBooking.objects.using(self.db).create(
            lead=lead,
            branch=self.branch_andheri,
            class_occurrence_id=occ_future_trial.id,
            trial_type='PILATES',
            scheduled_start=occ_future_trial.start_at,
            scheduled_end=occ_future_trial.end_at,
            status='BOOKED',
            confirmation_status='PENDING',
        )
        tok = generate_trial_qr_token(str(trial.id), settings.SECRET_KEY)
        ap = AccessPass.objects.using(self.db).create(
            trial_booking=trial,
            branch=self.branch_andheri,
            token_hash=hash_qr_token(tok),
            pass_type='TRIAL_BOOKING',
            valid_from=occ_future_trial.start_at - timedelta(minutes=20),
            valid_until=occ_future_trial.end_at + timedelta(minutes=60),
            status='ACTIVE',
        )

        # Reschedule updates valid_until to new occurrence window
        old_until = ap.valid_until
        with patch('apps.tenant_core.services_crm.CRMLeadService._dispatch_trial_reschedule_whatsapp'):
            CRMLeadService.reschedule_trial(
                trial=trial,
                new_class_occurrence_id=self.occ_later_pilates.id,
                actor_user=self.user,
                db_alias=self.db,
            )
        ap.refresh_from_db(using=self.db)
        self.assertGreater(ap.valid_until, old_until)

        # Cancel revokes pass, returns in revoked_tokens, blocks public pass HTML & punch
        CRMLeadService.cancel_trial(trial=trial, reason='No longer needed', actor_user=self.user, db_alias=self.db)
        ap.refresh_from_db(using=self.db)
        self.assertEqual(ap.status, 'CANCELLED')

        with patch.dict(os.environ, {'TURNSTILE_SYNC_SECRET': self.sync_secret}):
            auth = _make_hmac_bearer(self.sync_secret, str(self.branch_andheri.id))
            req_sync = self.factory.get(
                f'/api/v1/tenant/access/turnstile/active-tokens/?branch_id={self.branch_andheri.id}',
                HTTP_AUTHORIZATION=auth,
            )
            req_sync._tenant_alias = self.db
            res_sync = TurnstileActiveTokensView.as_view()(req_sync)
            self.assertIn(tok, res_sync.data['revoked_tokens'])
            self.assertNotIn(tok, [t['token'] for t in res_sync.data['tokens']])

        signed_pass_tok = generate_trial_action_token(trial.id, 'PASS', tenant_id=self.tenant.id)
        req_pub = self.factory.get(f'/p/{signed_pass_tok}/')
        res_pub = PublicTrialPassView.as_view()(req_pub, token=signed_pass_tok)
        self.assertIn('Pass No Longer Valid', res_pub.content.decode('utf-8'))

        with patch.dict(os.environ, {'TURNSTILE_GATEWAY_API_KEY': 'gw-trial-key'}):
            req_punch = self.factory.post(
                '/api/v1/tenant/access/turnstile/verify-punch/',
                {'card_no': tok, 'branch_id': str(self.branch_andheri.id), 'device_reference': 'Andheri Pilates Entry'},
                format='json',
                HTTP_X_TURNSTILE_API_KEY='gw-trial-key',
            )
            req_punch._tenant_alias = self.db
            res_punch = TurnstileVerifyPunchView.as_view()(req_punch)
            self.assertFalse(res_punch.data['allow_access'])

    def test_dahua_failcode_286064919_remove_fallback_and_doorcard_events(self):
        import importlib.util
        import json
        import threading
        from django.conf import settings
        from apps.tenant_core import dahua_rpc

        daemon_path = os.path.join(os.path.dirname(str(settings.BASE_DIR)), 'sweat_turnstile_daemon.py')
        spec = importlib.util.spec_from_file_location('sweat_turnstile_daemon_v2_test', daemon_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules['sweat_turnstile_daemon_v2_test'] = mod
        spec.loader.exec_module(mod)

        class _FakeResp:
            def __init__(self, payload):
                self._payload = payload
                self.text = json.dumps(payload)

            def json(self):
                return self._payload

        with patch.dict(os.environ, {
            'SWEAT_BRANCH_ID': 'e6a23bd5-cdf7-4fd9-a921-9bc848db4052',
            'TURNSTILE_SYNC_SECRET': self.sync_secret,
            'TURNSTILE_GATEWAY_API_KEY': 'gw-key-xyz',
            'DAHUA_ADMIN_PASS': 'pw123',
        }):
            mod._init_config()

            # 1. dahua_rpc.register_card treats FailCodes [286064919] as already-registered (True)
            sess_dup = MagicMock()
            sess_dup.post.side_effect = [
                _FakeResp({'id': 10, 'result': True}),
                _FakeResp({'error': {'code': 268632336, 'detail': {'FailCodes': [286064919]}, 'message': 'Batch Process Error'}, 'id': 11, 'result': False}),
            ]
            with patch.object(dahua_rpc, '_dahua_login', return_value=(sess_dup, 'sid-1')):
                self.assertTrue(dahua_rpc.register_card('http://192.168.1.119', 'P123', 'Test', 'DupTok123456'))

            # 2. dahua_rpc.delete_cards falls back to AccessCard.removeMulti when deleteMulti returns False
            sess_del = MagicMock()
            sess_del.post.side_effect = [
                _FakeResp({'id': 20, 'result': False}),
                _FakeResp({'id': 21, 'result': True}),
            ]
            with patch.object(dahua_rpc, '_dahua_login', return_value=(sess_del, 'sid-2')):
                self.assertTrue(dahua_rpc.delete_cards('http://192.168.1.119', ['Tok123']))
            self.assertEqual(sess_del.post.call_args_list[0].kwargs['json']['method'], 'AccessCard.deleteMulti')
            self.assertEqual(sess_del.post.call_args_list[1].kwargs['json']['method'], 'AccessCard.removeMulti')

            # 3. Daemon _register_cards_batch & _delete_cards_batch handle 286064919 and removeMulti fallback
            term = mod.TerminalConfig(
                name='Goregaon Entry',
                base_url='http://192.168.1.119',
                direction='ENTRY',
                class_category_code='',
                is_active=True,
                branch_id='e6a23bd5-cdf7-4fd9-a921-9bc848db4052',
            )
            mod._synced_cards.clear()
            mod._card_users.clear()
            mod._purged_cards.clear()
            mod._confirmed_revoked.clear()
            mod._last_seen.clear()

            d_sess_reg = MagicMock()
            d_sess_reg.post.side_effect = [
                _FakeResp({'id': 10, 'result': True}),
                _FakeResp({'error': {'code': 268632336, 'detail': {'FailCodes': [286064919]}, 'message': 'Batch Process Error'}, 'id': 11, 'result': False}),
            ]
            with patch.object(mod, '_dahua_login', return_value=(d_sess_reg, 'sid-3')):
                ok_count = mod._register_cards_batch(
                    term,
                    [('P920faed', 'Test User', 'TokDup2860', '2026-11-25 21:00:00', '2026-11-25 23:00:00')],
                )
            self.assertEqual(ok_count, 1)
            self.assertIn(('http://192.168.1.119', 'TokDup2860'), mod._synced_cards)

            # Falls back through deleteMulti -> removeMulti -> AccessUser.removeMulti
            d_sess_del = MagicMock()
            d_sess_del.post.side_effect = [
                _FakeResp({'id': 20, 'result': False}),
                _FakeResp({'id': 21, 'result': False}),
                _FakeResp({'id': 22, 'result': True}),
            ]
            with patch.object(mod, '_dahua_login', return_value=(d_sess_del, 'sid-4')):
                self.assertTrue(mod._delete_cards_batch(term, ['TokDup2860']))
            self.assertEqual(d_sess_del.post.call_args_list[2].kwargs['json']['method'], 'AccessUser.removeMulti')
            self.assertEqual(d_sess_del.post.call_args_list[2].kwargs['json']['params'], {'UserIDList': ['P920faed']})
            self.assertNotIn(('http://192.168.1.119', 'TokDup2860'), mod._synced_cards)

            # Anti-resync guard prevents an in-flight sync batch from re-pushing a just-purged token
            with patch.object(mod, '_dahua_login') as login_spy:
                re_pushed = mod._register_cards_batch(
                    term,
                    [('P920faed', 'Test User', 'TokDup2860', '2026-11-25 21:00:00', '2026-11-25 23:00:00')],
                )
            self.assertEqual(re_pushed, 0)
            login_spy.assert_not_called()

            # 4. _listen_terminal parses Code=_DoorCard_ and skips RFID Code=DoorCard ("Number" only)
            lines = [
                b'--myboundary\r\n',
                b'Content-Type: text/plain\r\n',
                b'Code=DoorCard;action=Pulse;index=0;data={\n',
                b'   "Number" : "0238107985"\n',
                b'}\r\n',
                b'Code=_DoorCard_;action=Pulse;index=0;data={\n',
                b'   "CardNo" : "DoorCardTok99"\n',
                b'}\r\n',
                b'',
            ]
            line_iter = iter(lines)
            stop_ev = threading.Event()
            done_ev = threading.Event()
            stream_mock = MagicMock()
            stream_mock.headers = {'Content-Type': 'multipart/x-mixed-replace; boundary=myboundary'}

            def _next_line():
                val = next(line_iter, b'')
                if not val:
                    stop_ev.set()
                return val

            stream_mock.readline.side_effect = _next_line
            ctx = MagicMock()
            ctx.__enter__.return_value = stream_mock
            ctx.__exit__.return_value = False
            fake_opener = MagicMock()
            fake_opener.open.return_value = ctx

            captured = []

            def _record_scan(card_no, direction, t):
                captured.append((card_no, direction))
                done_ev.set()

            with patch('urllib.request.build_opener', return_value=fake_opener), \
                 patch.object(mod, '_process_scan', side_effect=_record_scan):
                mod._listen_terminal(term, stop_ev)
                self.assertTrue(done_ev.wait(timeout=1.0))
            self.assertEqual(captured, [('DoorCardTok99', 'ENTRY')])

            # 5. _process_scan purges card BEFORE opening gate on ENTRY
            call_order = []
            with patch.object(mod, '_verify_token_with_cloud', return_value={'allow_access': True, 'user_name': 'Kunal'}), \
                 patch.object(mod, '_delete_cards_batch', side_effect=lambda t, cards: call_order.append(('delete', list(cards))) or True), \
                 patch.object(mod, '_open_physical_gate', side_effect=lambda url, lbl: call_order.append(('open', url)) or True):
                mod._process_scan('DoorCardTok99', 'ENTRY', term)
            self.assertEqual(call_order, [('delete', ['DoorCardTok99']), ('open', 'http://192.168.1.119')])
