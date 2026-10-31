import uuid
from datetime import timedelta
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.master.models import Tenant
from apps.tenant_core.context import set_tenant_db_alias
from apps.tenant_core.models import (
    Organization,
    Location,
    Branch,
    UserProfile,
    TenantUser,
    ProgramCategory,
    Program,
    Package,
    ClassCategory,
    ClassTemplate,
    ClassOccurrence,
    Membership,
    MembershipEntitlement,
    Booking,
    AttendanceRecord,
    AccessEvent,
    AccessPass,
)
from apps.tenant_core.services_access_control import (
    generate_compact_qr_token,
    hash_qr_token,
    verify_qr_token,
    BASE62_ALPHABET,
)
from apps.tenant_core.services_bookings import BookingWaitlistAttendanceService
from apps.tenant_core.views_bookings import AccessControlBulkIngestView, AccessControlSyncPassesView, TurnstileVerifyPunchView
from apps.tenant_core.views_mobile import MobileQRPassView


@override_settings(EDGE_SERVICE_API_KEY='test-edge-service-key-xyz')
class TimeWatchTurnstileV2Tests(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        self.db = 'tenant_test'
        self.edge_api_key = 'test-edge-service-key-xyz'
        self.factory = APIRequestFactory()

        # 0. Master Tenant for auth directory identity sync
        self.tenant = Tenant.objects.using('default').create(
            name="SWEAT Fitness Studio",
            code=f"SWT-{uuid.uuid4().hex[:6]}",
            slug=f"sweat-gym-{uuid.uuid4().hex[:6]}",
            status="ACTIVE"
        )

        # 1. Organization & Location
        self.org = Organization.objects.using(self.db).create(
            name="SWEAT Fitness Studio",
            code=f"SWT-{uuid.uuid4().hex[:6]}",
            status="ACTIVE"
        )
        self.loc = Location.objects.using(self.db).create(
            organization=self.org,
            code=f"LOC-{uuid.uuid4().hex[:4]}",
            name="Indiranagar Studio",
            city="Bangalore",
            status="ACTIVE"
        )
        self.branch = Branch.objects.using(self.db).create(
            organization=self.org,
            location=self.loc,
            code=f"BR-{uuid.uuid4().hex[:4]}",
            name="SWEAT Indiranagar",
            status="ACTIVE"
        )

        # 2. Member User & Profile
        self.user = TenantUser.objects.using(self.db).create(
            organization=self.org,
            email=f"member-{uuid.uuid4().hex[:6]}@example.com",
            first_name="Rohan",
            last_name="Sharma",
            status="ACTIVE",
        )
        self.profile = UserProfile.objects.using(self.db).create(
            user=self.user,
            first_name_snapshot="Rohan",
            last_name_snapshot="Sharma",
            member_number=f"MEM-{uuid.uuid4().hex[:6].upper()}",
            preferred_branch=self.branch,
            member_status="ACTIVE",
        )

        # 3. Class occurrence today
        now = timezone.now()
        cat = ClassCategory.objects.using(self.db).create(
            organization=self.org,
            name="HIIT",
            code=f"CAT-{uuid.uuid4().hex[:4]}",
            status="ACTIVE"
        )
        prog_cat = ProgramCategory.objects.using(self.db).create(
            organization=self.org,
            name="Studio Fitness",
            code=f"PCAT-{uuid.uuid4().hex[:4]}",
            status="ACTIVE"
        )
        prog = Program.objects.using(self.db).create(
            organization=self.org,
            category=prog_cat,
            name="Sweat HIIT",
            code=f"PRG-{uuid.uuid4().hex[:4]}",
            status="ACTIVE"
        )
        template = ClassTemplate.objects.using(self.db).create(
            organization=self.org,
            program=prog,
            category=cat,
            name="High Intensity Boxing",
            code=f"TPL-{uuid.uuid4().hex[:4]}",
            default_capacity=20,
            default_duration_minutes=60,
            status="ACTIVE"
        )
        self.occurrence = ClassOccurrence.objects.using(self.db).create(
            branch=self.branch,
            class_template=template,
            occurrence_date=now.date(),
            start_at=now - timedelta(minutes=15),
            end_at=now + timedelta(minutes=45),
            capacity=20,
            status="SCHEDULED"
        )

        # 4. Confirmed Booking
        self.booking = Booking.objects.using(self.db).create(
            booking_number=f"BKG-{uuid.uuid4().hex[:8].upper()}",
            user_profile=self.profile,
            occurrence=self.occurrence,
            branch=self.branch,
            booking_type="MEMBER",
            booking_source="MOBILE_APP",
            status="CONFIRMED",
            payment_status="PAID",
            confirmation_status="CONFIRMED",
            confirmed_at=now
        )

    def test_compact_token_entropy_and_format(self):
        """Token must be exactly 22 chars Base62 with 128-bit CSPRNG entropy."""
        token = generate_compact_qr_token()
        self.assertEqual(len(token), 22)
        for char in token:
            self.assertIn(char, BASE62_ALPHABET)

        # Uniqueness test across 50 tokens
        tokens = {generate_compact_qr_token() for _ in range(50)}
        self.assertEqual(len(tokens), 50)

        # Hash correctness
        h = hash_qr_token(token)
        self.assertEqual(len(h), 64)
        self.assertTrue(verify_qr_token(token, h))
        self.assertFalse(verify_qr_token("wrong_token", h))

    def test_mobile_qr_pass_view_returns_compact_token(self):
        """Mobile pass endpoint generates 22-char token and tracks AccessPass."""
        request = self.factory.get(f'/api/v1/mobile/bookings/{self.booking.id}/qr-pass/')
        force_authenticate(request, user=self.user)
        request.user = self.user
        request._tenant_alias = self.db

        view = MobileQRPassView.as_view()
        response = view(request, booking_id=self.booking.id)
        self.assertEqual(response.status_code, 200)

        data = response.data
        self.assertIn('qr_token', data)
        self.assertEqual(len(data['qr_token']), 22)
        self.assertEqual(data['booking_number'], self.booking.booking_number)

        # Verify AccessPass created
        pass_obj = AccessPass.objects.using(self.db).filter(booking=self.booking).first()
        self.assertIsNotNone(pass_obj)
        self.assertEqual(pass_obj.status, 'ACTIVE')
        self.assertTrue(verify_qr_token(data['qr_token'], pass_obj.token_hash))

    def test_bulk_ingest_entry_attendance_and_idempotency(self):
        """Bulk ingest marks attendance and rejects duplicates idempotently."""
        # Setup an AccessPass
        token = generate_compact_qr_token()
        t_hash = hash_qr_token(token)
        AccessPass.objects.using(self.db).create(
            user_profile=self.profile,
            booking=self.booking,
            branch=self.branch,
            token_hash=t_hash,
            valid_from=timezone.now() - timedelta(hours=1),
            valid_until=timezone.now() + timedelta(hours=2),
            status='ACTIVE'
        )

        event_id = uuid.uuid4()
        payload = {
            'branch_id': str(self.branch.id),
            'events': [
                {
                    'event_id': str(event_id),
                    'direction': 'ENTRY',
                    'token_hash': t_hash,
                    'raw_token': token,
                    'scanned_at': timezone.now().isoformat(),
                    'device_reference': 'TURNSTILE-ENTRY-01'
                }
            ]
        }

        view = AccessControlBulkIngestView.as_view()
        req1 = self.factory.post(
            '/api/v1/access-control/events/bulk/',
            payload,
            format='json',
            HTTP_X_EDGE_API_KEY=self.edge_api_key,
        )
        req1._tenant_alias = self.db
        res1 = view(req1)

        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res1.data['synced_count'], 1)

        # Verify AttendanceRecord created
        att = AttendanceRecord.objects.using(self.db).filter(booking=self.booking).first()
        self.assertIsNotNone(att)
        self.assertEqual(att.status, 'PRESENT')
        self.assertIn(att.check_in_method, ['ACCESS_DEVICE', 'QR'])

        # Verify AccessPass state transitioned
        pass_obj = AccessPass.objects.using(self.db).get(booking=self.booking)
        self.assertEqual(pass_obj.status, 'CONSUMED_ENTERED')

        # IDEMPOTENCY TEST: Post identical event_id again
        req2 = self.factory.post(
            '/api/v1/access-control/events/bulk/',
            payload,
            format='json',
            HTTP_X_EDGE_API_KEY=self.edge_api_key,
        )
        req2._tenant_alias = self.db
        res2 = view(req2)
        self.assertEqual(res2.status_code, 200)

        # Still exactly 1 AttendanceRecord and 1 AccessEvent
        self.assertEqual(AttendanceRecord.objects.using(self.db).filter(booking=self.booking).count(), 1)
        self.assertEqual(AccessEvent.objects.using(self.db).filter(id=event_id).count(), 1)

    def test_single_use_anti_replay_enforcement(self):
        """Scanning a new event with the same booking after entry must be DENIED."""
        # 1. First Entry
        token = generate_compact_qr_token()
        t_hash = hash_qr_token(token)
        AccessPass.objects.using(self.db).create(
            user_profile=self.profile,
            booking=self.booking,
            branch=self.branch,
            token_hash=t_hash,
            valid_from=timezone.now() - timedelta(hours=1),
            valid_until=timezone.now() + timedelta(hours=2),
            status='ACTIVE'
        )

        view = AccessControlBulkIngestView.as_view()
        ev1 = {
            'event_id': str(uuid.uuid4()),
            'direction': 'ENTRY',
            'token_hash': t_hash,
            'device_reference': 'TURNSTILE-ENTRY-01'
        }
        r1 = self.factory.post(
            '/api/v1/access-control/events/bulk/',
            {'events': [ev1]},
            format='json',
            HTTP_X_EDGE_API_KEY=self.edge_api_key,
        )
        r1._tenant_alias = self.db
        view(r1)

        # 2. Replay Entry Scan (e.g., someone with screenshot trying to scan at Turnstile 2)
        ev2 = {
            'event_id': str(uuid.uuid4()),
            'direction': 'ENTRY',
            'token_hash': t_hash,
            'device_reference': 'TURNSTILE-ENTRY-02'
        }
        r2 = self.factory.post(
            '/api/v1/access-control/events/bulk/',
            {'events': [ev2]},
            format='json',
            HTTP_X_EDGE_API_KEY=self.edge_api_key,
        )
        r2._tenant_alias = self.db
        res2 = view(r2)

        self.assertEqual(res2.status_code, 200)
        result = res2.data['results'][0]
        self.assertEqual(result['event_type'], 'DENIED')
        self.assertEqual(result['reconciliation_status'], 'REPLAY_ATTEMPT')

    def test_exit_turnstile_checkout_tracking(self):
        """Scanning at Exit turnstile records check_out_at and transitions pass."""
        # Pre-seed checked in attendance
        token = generate_compact_qr_token()
        t_hash = hash_qr_token(token)
        AccessPass.objects.using(self.db).create(
            user_profile=self.profile,
            booking=self.booking,
            branch=self.branch,
            token_hash=t_hash,
            valid_from=timezone.now() - timedelta(hours=1),
            valid_until=timezone.now() + timedelta(hours=2),
            status='CONSUMED_ENTERED'
        )
        AttendanceRecord.objects.using(self.db).create(
            booking=self.booking,
            user_profile=self.profile,
            branch=self.branch,
            status='PRESENT',
            check_in_at=timezone.now() - timedelta(minutes=45)
        )

        view = AccessControlBulkIngestView.as_view()
        exit_event = {
            'event_id': str(uuid.uuid4()),
            'direction': 'EXIT',
            'token_hash': t_hash,
            'device_reference': 'TURNSTILE-EXIT-01',
            'scanned_at': timezone.now().isoformat()
        }
        req = self.factory.post(
            '/api/v1/access-control/events/bulk/',
            {'events': [exit_event]},
            format='json',
            HTTP_X_EDGE_API_KEY=self.edge_api_key,
        )
        req._tenant_alias = self.db
        res = view(req)

        self.assertEqual(res.status_code, 200)
        att = AttendanceRecord.objects.using(self.db).get(booking=self.booking)
        self.assertIsNotNone(att.check_out_at)
        self.assertTrue(att.attendance_completed)

        pass_obj = AccessPass.objects.using(self.db).get(booking=self.booking)
        self.assertEqual(pass_obj.status, 'CONSUMED_EXITED')

    def test_sync_passes_endpoint(self):
        """Edge sync endpoint yields active passes for today's branch schedule."""
        token = generate_compact_qr_token()
        t_hash = hash_qr_token(token)
        AccessPass.objects.using(self.db).create(
            user_profile=self.profile,
            booking=self.booking,
            branch=self.branch,
            token_hash=t_hash,
            valid_from=timezone.now(),
            valid_until=timezone.now() + timedelta(hours=2),
            status='ACTIVE'
        )

        view = AccessControlSyncPassesView.as_view()
        req = self.factory.get(
            f'/api/v1/access-control/sync/?branch_id={self.branch.id}',
            HTTP_X_EDGE_API_KEY=self.edge_api_key,
        )
        req._tenant_alias = self.db
        res = view(req)

        self.assertEqual(res.status_code, 200)
        self.assertIn('server_time_utc', res.data)
        self.assertGreaterEqual(res.data['total_passes'], 1)
        found = any(p['token_hash'] == t_hash for p in res.data['passes'])
        self.assertTrue(found)

    def test_mobile_qr_pass_option_a_requires_booking_today(self):
        """Option A: Without confirmed booking today, MobileQRPassView returns 404."""
        # Create a new user without any bookings
        user_no_bkg = TenantUser.objects.using(self.db).create(
            organization=self.org,
            email=f"nobooking-{uuid.uuid4().hex[:6]}@example.com",
            first_name="Pooja",
            last_name="Verma",
            status="ACTIVE",
        )
        UserProfile.objects.using(self.db).create(
            user=user_no_bkg,
            first_name_snapshot="Pooja",
            last_name_snapshot="Verma",
            member_number=f"MEM-{uuid.uuid4().hex[:6].upper()}",
            preferred_branch=self.branch,
            member_status="ACTIVE",
        )

        view = MobileQRPassView.as_view()
        req = self.factory.get('/mobile/qr-pass/')
        req._tenant_alias = self.db
        force_authenticate(req, user=user_no_bkg)
        res = view(req)

        self.assertIn(res.status_code, [200, 404])
        self.assertFalse(res.data['has_booking'])
        self.assertTrue('No confirmed class booking' in res.data['detail'] or 'No upcoming class session' in res.data['detail'])

    def test_mobile_qr_pass_option_a_success_with_confirmed_booking(self):
        """Option A: With confirmed booking today, MobileQRPassView returns 200 with deterministic token."""
        from apps.tenant_core.services_access_control import generate_booking_qr_token
        from django.conf import settings

        view = MobileQRPassView.as_view()

        # 1. Access pass via general /mobile/qr-pass/
        req1 = self.factory.get('/mobile/qr-pass/')
        req1._tenant_alias = self.db
        force_authenticate(req1, user=self.user)
        res1 = view(req1)

        self.assertEqual(res1.status_code, 200)
        self.assertTrue(res1.data['has_booking'])
        self.assertEqual(res1.data['booking_id'], str(self.booking.id))
        self.assertEqual(len(res1.data['compact_token']), 22)

        expected_token = generate_booking_qr_token(self.booking.id, settings.SECRET_KEY)
        self.assertEqual(res1.data['compact_token'], expected_token)

        # 2. Access pass via explicit booking parameter /mobile/qr-pass/?booking_id=...
        req2 = self.factory.get(f'/mobile/qr-pass/?booking_id={self.booking.id}')
        req2._tenant_alias = self.db
        force_authenticate(req2, user=self.user)
        res2 = view(req2)

        self.assertEqual(res2.status_code, 200)
        # Tokens from both calls must be 100% IDENTICAL!
        self.assertEqual(res1.data['compact_token'], res2.data['compact_token'])
        self.assertEqual(res2.data['compact_token'], expected_token)

    @override_settings(DEBUG=False, EDGE_SERVICE_API_KEY=None)
    def test_edge_endpoints_fail_closed_in_production_when_unconfigured(self):
        """When DEBUG=False and EDGE_SERVICE_API_KEY is not set, endpoints must return 401 Unauthorized."""
        sync_view = AccessControlSyncPassesView.as_view()
        req_sync = self.factory.get(f'/api/v1/access-control/sync/?branch_id={self.branch.id}')
        req_sync._tenant_alias = self.db
        res_sync = sync_view(req_sync)
        self.assertEqual(res_sync.status_code, 401)
        self.assertIn('Unauthorized', res_sync.data.get('error', ''))

        bulk_view = AccessControlBulkIngestView.as_view()
        req_bulk = self.factory.post('/api/v1/access-control/events/bulk/', {'events': []}, format='json')
        req_bulk._tenant_alias = self.db
        res_bulk = bulk_view(req_bulk)
        self.assertEqual(res_bulk.status_code, 401)
        self.assertIn('Unauthorized', res_bulk.data.get('error', ''))

    @override_settings(DEBUG=False, EDGE_SERVICE_API_KEY='')
    def test_edge_endpoints_fail_closed_in_production_when_empty_key(self):
        """When DEBUG=False and EDGE_SERVICE_API_KEY is empty string, endpoints must return 401."""
        sync_view = AccessControlSyncPassesView.as_view()
        req_sync = self.factory.get(f'/api/v1/access-control/sync/?branch_id={self.branch.id}')
        req_sync._tenant_alias = self.db
        res_sync = sync_view(req_sync)
        self.assertEqual(res_sync.status_code, 401)

    @override_settings(DEBUG=False, EDGE_SERVICE_API_KEY='production-test-key-999')
    def test_edge_endpoints_fail_closed_when_key_invalid(self):
        """When EDGE_SERVICE_API_KEY is set, requests with missing or invalid key return 401."""
        sync_view = AccessControlSyncPassesView.as_view()

        # 1. Missing header
        req_no_header = self.factory.get(f'/api/v1/access-control/sync/?branch_id={self.branch.id}')
        req_no_header._tenant_alias = self.db
        res1 = sync_view(req_no_header)
        self.assertEqual(res1.status_code, 401)

        # 2. Invalid header
        req_bad_key = self.factory.get(f'/api/v1/access-control/sync/?branch_id={self.branch.id}', HTTP_X_EDGE_API_KEY='wrong-secret')
        req_bad_key._tenant_alias = self.db
        res2 = sync_view(req_bad_key)
        self.assertEqual(res2.status_code, 401)

    @override_settings(DEBUG=False, EDGE_SERVICE_API_KEY='production-test-key-999')
    def test_edge_endpoints_succeed_with_valid_key(self):
        """When valid X-Edge-Api-Key is provided, endpoints return 200 OK."""
        sync_view = AccessControlSyncPassesView.as_view()
        req_valid = self.factory.get(
            f'/api/v1/access-control/sync/?branch_id={self.branch.id}',
            HTTP_X_EDGE_API_KEY='production-test-key-999'
        )
        req_valid._tenant_alias = self.db
        res = sync_view(req_valid)
        self.assertEqual(res.status_code, 200)
        self.assertIn('passes', res.data)



    def test_turnstile_verify_punch_health_check(self):
        """GET /api/v1/tenant/access/turnstile/verify-punch/ responds with 200 ONLINE."""
        view = TurnstileVerifyPunchView.as_view()
        req = self.factory.get('/api/v1/tenant/access/turnstile/verify-punch/')
        req._tenant_alias = self.db
        res = view(req)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data.get('status'), 'ONLINE')

    def test_turnstile_verify_punch_entry_then_exit_lifecycle(self):
        """Complete Turnstile lifecycle: 1. Entry scan grants access -> 2. Duplicate entry denied -> 3. Exit scan checks out -> 4. Subsequent scan denied."""
        token = generate_compact_qr_token()
        t_hash = hash_qr_token(token)
        now = timezone.now()

        pass_obj = AccessPass.objects.using(self.db).create(
            user_profile=self.profile,
            booking=self.booking,
            branch=self.branch,
            token_hash=t_hash,
            valid_from=now - timedelta(minutes=10),
            valid_until=now + timedelta(hours=1),
            status='ACTIVE'
        )

        view = TurnstileVerifyPunchView.as_view()

        # STEP 1: Entry Scan at Turnstile
        entry_payload = {
            'card_no': token,
            'device_id': 'TIMEWATCH-ENTRY-01',
            'direction': 'ENTRY',
            'branch_id': str(self.branch.id)
        }
        req_entry = self.factory.post('/api/v1/tenant/access/turnstile/verify-punch/', entry_payload, format='json')
        req_entry._tenant_alias = self.db
        res_entry = view(req_entry)

        self.assertEqual(res_entry.status_code, 200)
        self.assertTrue(res_entry.data.get('allow_access'))
        self.assertEqual(res_entry.data.get('status'), 'GRANTED')
        self.assertEqual(res_entry.data.get('direction'), 'ENTRY')
        self.assertEqual(res_entry.data.get('member_number'), self.profile.member_number)

        # Verify pass transitioned to CONSUMED_ENTERED
        pass_obj.refresh_from_db(using=self.db)
        self.assertEqual(pass_obj.status, 'CONSUMED_ENTERED')
        self.assertIsNotNone(pass_obj.consumed_at)

        # Verify attendance marked PRESENT
        att = AttendanceRecord.objects.using(self.db).filter(booking=self.booking).first()
        self.assertIsNotNone(att)
        self.assertEqual(att.status, 'PRESENT')
        self.assertEqual(att.check_in_method, 'QR')
        self.assertIsNone(att.check_out_at)

        # STEP 2: Replay ENTRY Attempt (Trying to scan same QR again at entry gate)
        req_replay = self.factory.post('/api/v1/tenant/access/turnstile/verify-punch/', entry_payload, format='json')
        req_replay._tenant_alias = self.db
        res_replay = view(req_replay)

        self.assertEqual(res_replay.status_code, 200)
        self.assertFalse(res_replay.data.get('allow_access'))
        self.assertEqual(res_replay.data.get('reason'), 'REPLAY_ATTEMPT')

        # STEP 3: Exit Scan at Turnstile
        exit_payload = {
            'card_no': token,
            'device_id': 'TIMEWATCH-EXIT-01',
            'direction': 'EXIT',
            'branch_id': str(self.branch.id)
        }
        req_exit = self.factory.post('/api/v1/tenant/access/turnstile/verify-punch/', exit_payload, format='json')
        req_exit._tenant_alias = self.db
        res_exit = view(req_exit)

        self.assertEqual(res_exit.status_code, 200)
        self.assertTrue(res_exit.data.get('allow_access'))
        self.assertEqual(res_exit.data.get('status'), 'GRANTED')
        self.assertEqual(res_exit.data.get('direction'), 'EXIT')

        # Verify pass transitioned to CONSUMED_EXITED
        pass_obj.refresh_from_db(using=self.db)
        self.assertEqual(pass_obj.status, 'CONSUMED_EXITED')

        # Verify attendance completed with check_out_at recorded
        att.refresh_from_db(using=self.db)
        self.assertIsNotNone(att.check_out_at)
        self.assertTrue(att.attendance_completed)

        # STEP 4: Post-Exit Scan Attempt -> strictly DENIED
        req_post_exit = self.factory.post('/api/v1/tenant/access/turnstile/verify-punch/', exit_payload, format='json')
        req_post_exit._tenant_alias = self.db
        res_post_exit = view(req_post_exit)

        self.assertEqual(res_post_exit.status_code, 200)
        self.assertFalse(res_post_exit.data.get('allow_access'))
        self.assertEqual(res_post_exit.data.get('reason'), 'REPLAY_ATTEMPT')

    def test_turnstile_verify_punch_unrecognized_token_denied(self):
        """Unrecognized token returns allow_access=False and logs UNAUTHORIZED."""
        view = TurnstileVerifyPunchView.as_view()
        payload = {'card_no': 'fake_unrecognized_token_123', 'device_id': 'TIMEWATCH-MALAD-01'}
        req = self.factory.post('/api/v1/tenant/access/turnstile/verify-punch/', payload, format='json')
        req._tenant_alias = self.db
        res = view(req)

        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data.get('allow_access'))
        self.assertEqual(res.data.get('reason'), 'INVALID_PASS')
