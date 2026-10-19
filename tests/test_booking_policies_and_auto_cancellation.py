import uuid
from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from django.core.exceptions import ValidationError

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
    PackageVersion,
    PackagePrice,
    ClassCategory,
    ClassTemplate,
    ClassOccurrence,
    Membership,
    MembershipEntitlement,
    Booking,
    BookingCancellation,
    BookingPolicyConfig,
)
from apps.tenant_core.models_audit_outbox import BusinessAuditEvent
from apps.tenant_core.services_booking_policy import BookingPolicyService
from apps.tenant_core.services_bookings import BookingWaitlistAttendanceService


class BookingPoliciesAndAutoCancellationTests(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        self.db = 'tenant_test'
        set_tenant_db_alias(self.db)

        self.org = Organization.objects.using(self.db).create(
            name="SWEAT Fitness Group",
            code=f"SWT-{uuid.uuid4().hex[:6]}",
            status="ACTIVE"
        )
        self.loc = Location.objects.using(self.db).create(
            organization=self.org,
            code=f"LOC-{uuid.uuid4().hex[:4]}",
            name="Mumbai Hub",
            city="Mumbai",
            status="ACTIVE"
        )
        self.branch_alpha = Branch.objects.using(self.db).create(
            organization=self.org,
            location=self.loc,
            name="Bandra West",
            code=f"BW-{uuid.uuid4().hex[:4]}",
            timezone="Asia/Kolkata"
        )
        self.branch_beta = Branch.objects.using(self.db).create(
            organization=self.org,
            location=self.loc,
            name="Juhu North",
            code=f"JN-{uuid.uuid4().hex[:4]}",
            timezone="Asia/Kolkata"
        )

        self.admin_user = TenantUser.objects.using(self.db).create(
            organization=self.org,
            email=f"admin-{uuid.uuid4().hex[:4]}@sweat.local",
            first_name="Admin",
            last_name="Manager",
            status="ACTIVE"
        )
        self.admin_user._db_alias = self.db

        # Catalog setup
        self.prog_cat = ProgramCategory.objects.using(self.db).create(
            organization=self.org,
            code=f"CAT-{uuid.uuid4().hex[:4]}",
            name="Strength & Conditioning",
            status="ACTIVE"
        )
        self.program = Program.objects.using(self.db).create(
            organization=self.org,
            category=self.prog_cat,
            code=f"PRG-{uuid.uuid4().hex[:4]}",
            name="SWEAT Elite",
            status="ACTIVE"
        )
        self.package = Package.objects.using(self.db).create(
            organization=self.org,
            program=self.program,
            code=f"PKG-{uuid.uuid4().hex[:4]}",
            name="Monthly Unlimited",
            status="ACTIVE"
        )
        self.pkg_v1 = PackageVersion.objects.using(self.db).create(
            package=self.package,
            version_number=1,
            name_snapshot="V1 Standard",
            duration_value=1,
            duration_unit='MONTH',
            effective_from=timezone.now(),
            status="ACTIVE",
            created_by_user=self.admin_user
        )
        self.pkg_price = PackagePrice.objects.using(self.db).create(
            package_version=self.pkg_v1,
            branch=self.branch_alpha,
            base_price=Decimal('5000.00'),
            effective_from=timezone.now(),
            status='ACTIVE',
            created_by_user=self.admin_user
        )

        self.class_cat = ClassCategory.objects.using(self.db).create(
            organization=self.org,
            code=f"CC-{uuid.uuid4().hex[:4]}",
            name="HIIT",
            status="ACTIVE"
        )
        self.class_tpl = ClassTemplate.objects.using(self.db).create(
            organization=self.org,
            category=self.class_cat,
            code=f"TPL-{uuid.uuid4().hex[:4]}",
            name="SWEAT Burn",
            default_capacity=10,
            status="ACTIVE"
        )

        # Base tenant policy config
        self.tenant_policy = BookingPolicyConfig.objects.using(self.db).create(
            organization=self.org,
            branch=None,
            auto_cancel_unpaid_enabled=True,
            auto_cancel_unpaid_minutes=30,
            auto_cancel_unconfirmed_enabled=True,
            auto_cancel_unconfirmed_minutes=60,
            cancellation_cutoff_hours=2,
            rescheduling_cutoff_hours=2,
            max_reschedules_per_booking=2,
            late_cancellation_allowed=True,
            late_cancellation_session_action='CONSUME',
            late_cancellation_fee=Decimal('150.00'),
            allowed_booking_types=['CLASS', 'PT_SESSION', 'FACILITY', 'TRIAL'],
            apply_to_existing_bookings=True,
            policy_effective_at=timezone.now() - timedelta(days=1),
            notification_channels=['WHATSAPP', 'IN_APP']
        )

    def _create_member(self, email_prefix: str, units: Decimal = Decimal('5.00')):
        u = TenantUser.objects.using(self.db).create(
            organization=self.org,
            email=f"{email_prefix}-{uuid.uuid4().hex[:6]}@sweat.local",
            first_name="Member",
            last_name="Test",
            status="ACTIVE"
        )
        u._db_alias = self.db

        prof = UserProfile.objects.using(self.db).create(
            user=u,
            member_number=f"MEM-{uuid.uuid4().hex[:6]}",
            first_name_snapshot="Member",
            last_name_snapshot="Test",
            preferred_branch=self.branch_alpha,
            member_status="ACTIVE",
        )
        today = timezone.now().date()
        m = Membership.objects.using(self.db).create(
            user_profile=prof,
            program=self.program,
            package=self.package,
            package_version=self.pkg_v1,
            package_price=self.pkg_price,
            purchase_branch=self.branch_alpha,
            home_branch=self.branch_alpha,
            membership_number=f"MB-{uuid.uuid4().hex[:6]}",
            start_date=today - timedelta(days=5),
            end_date=today + timedelta(days=25),
            status="ACTIVE",
            parq_status="COMPLETED"
        )
        ent = MembershipEntitlement.objects.using(self.db).create(
            membership=m,
            entitlement_type="GROUP_CLASS",
            allocated_units=units,
            consumed_units=Decimal('0.00'),
            is_unlimited=False,
            valid_from=timezone.now(),
            status="ACTIVE" if units > 0 else "EXHAUSTED"
        )
        return u, prof, m, ent

    def _create_occurrence(self, hours_from_now: float = 24, branch=None):
        start_time = timezone.now() + timedelta(hours=hours_from_now)
        end_time = start_time + timedelta(hours=1)
        return ClassOccurrence.objects.using(self.db).create(
            class_template=self.class_tpl,
            branch=branch or self.branch_alpha,
            occurrence_date=start_time.date(),
            start_at=start_time,
            end_at=end_time,
            capacity=10,
            status="SCHEDULED"
        )

    def test_01_auto_cancel_unpaid_booking_after_deadline(self):
        """Unpaid booking older than auto_cancel_unpaid_minutes is automatically cancelled."""
        u, prof, m, ent = self._create_member("unpaid-old")
        occ = self._create_occurrence(hours_from_now=12)

        booking_old = Booking.objects.using(self.db).create(
            branch=self.branch_alpha,
            occurrence=occ,
            user_profile=prof,
            membership=m,
            entitlement=ent,
            booking_number=f"BK-{uuid.uuid4().hex[:6]}",
            status="RESERVED",
            payment_status="UNPAID",
            confirmation_status="CONFIRMED",
            created_at=timezone.now() - timedelta(minutes=45),
            booked_at=timezone.now() - timedelta(minutes=45)
        )
        # Update created_at directly in DB to bypass auto_now_add
        Booking.objects.using(self.db).filter(id=booking_old.id).update(
            created_at=timezone.now() - timedelta(minutes=45),
            booked_at=timezone.now() - timedelta(minutes=45)
        )

        # Booking within deadline (15 minutes old)
        u2, prof2, m2, ent2 = self._create_member("unpaid-recent")
        booking_recent = Booking.objects.using(self.db).create(
            branch=self.branch_alpha,
            occurrence=occ,
            user_profile=prof2,
            membership=m2,
            entitlement=ent2,
            booking_number=f"BK-{uuid.uuid4().hex[:6]}",
            status="RESERVED",
            payment_status="UNPAID",
            confirmation_status="CONFIRMED"
        )
        Booking.objects.using(self.db).filter(id=booking_recent.id).update(
            created_at=timezone.now() - timedelta(minutes=15),
            booked_at=timezone.now() - timedelta(minutes=15)
        )

        # Run auto-cancellation scanner
        stats = BookingPolicyService.process_auto_cancellations(
            organization=self.org,
            branch=self.branch_alpha
        )

        booking_old.refresh_from_db()
        booking_recent.refresh_from_db()

        self.assertEqual(booking_old.status, "CANCELLED")
        self.assertEqual(booking_recent.status, "RESERVED")
        self.assertGreaterEqual(stats["auto_cancelled_count"], 1)

        # Verify audit record
        audit = BusinessAuditEvent.objects.using(self.db).filter(
            organization=self.org,
            action_code="BOOKING_AUTO_CANCELLED"
        ).first()
        self.assertIsNotNone(audit)
        self.assertIn("UNPAID", (audit.metadata or {}).get("reason", ""))

    def test_02_auto_cancel_unconfirmed_booking_after_deadline(self):
        """Unconfirmed booking past auto_cancel_unconfirmed_minutes is cancelled."""
        u, prof, m, ent = self._create_member("unconf-old")
        occ = self._create_occurrence(hours_from_now=18)

        booking = Booking.objects.using(self.db).create(
            branch=self.branch_alpha,
            occurrence=occ,
            user_profile=prof,
            membership=m,
            entitlement=ent,
            booking_number=f"BK-{uuid.uuid4().hex[:6]}",
            status="RESERVED",
            payment_status="PAID",
            confirmation_status="PENDING"
        )
        Booking.objects.using(self.db).filter(id=booking.id).update(
            created_at=timezone.now() - timedelta(minutes=75),
            booked_at=timezone.now() - timedelta(minutes=75)
        )

        stats = BookingPolicyService.process_auto_cancellations(
            organization=self.org,
            branch=self.branch_alpha
        )

        booking.refresh_from_db()
        self.assertEqual(booking.status, "CANCELLED")
        self.assertEqual(booking.confirmation_status, "DECLINED")

    def test_03_payment_and_confirmation_prevents_auto_cancellation(self):
        """A booking that has been both PAID and CONFIRMED is exempt from auto-cancellation."""
        u, prof, m, ent = self._create_member("paid-confirmed")
        occ = self._create_occurrence(hours_from_now=20)

        booking = Booking.objects.using(self.db).create(
            branch=self.branch_alpha,
            occurrence=occ,
            user_profile=prof,
            membership=m,
            entitlement=ent,
            booking_number=f"BK-{uuid.uuid4().hex[:6]}",
            status="CONFIRMED",
            payment_status="PAID",
            confirmation_status="CONFIRMED"
        )
        Booking.objects.using(self.db).filter(id=booking.id).update(
            created_at=timezone.now() - timedelta(hours=3),
            booked_at=timezone.now() - timedelta(hours=3)
        )

        stats = BookingPolicyService.process_auto_cancellations(
            organization=self.org,
            branch=self.branch_alpha
        )

        booking.refresh_from_db()
        self.assertEqual(booking.status, "CONFIRMED")
        self.assertEqual(stats["auto_cancelled_count"], 0)

    def test_04_early_cancellation_refunds_session_credit(self):
        """Cancelling before the cutoff window restores/refunds session credit to the member."""
        u, prof, m, ent = self._create_member("early-canceller", units=Decimal('5.00'))
        occ = self._create_occurrence(hours_from_now=10)  # Cutoff is 2 hours

        # Create confirmed booking and consume 1 unit
        booking = Booking.objects.using(self.db).create(
            branch=self.branch_alpha,
            occurrence=occ,
            user_profile=prof,
            membership=m,
            entitlement=ent,
            booking_number=f"BK-{uuid.uuid4().hex[:6]}",
            status="CONFIRMED",
            payment_status="PAID",
            confirmation_status="CONFIRMED"
        )
        ent.consumed_units += Decimal('1.00')
        ent.save(using=self.db)

        # Cancel booking through service
        cancelled_booking = BookingWaitlistAttendanceService.cancel_booking(
            booking=booking,
            cancelled_by_user=u,
            reason_text="Rescheduling day"
        )

        ent.refresh_from_db()
        booking.refresh_from_db()
        self.assertEqual(booking.status, "CANCELLED")
        self.assertEqual(ent.consumed_units, Decimal('0.00'))  # Fully refunded

        canc_rec = BookingCancellation.objects.using(self.db).filter(booking=booking).first()
        self.assertIsNotNone(canc_rec)
        self.assertTrue(canc_rec.within_cutoff)
        self.assertEqual(canc_rec.session_action_applied, "RESTORE")

    def test_05_late_cancellation_consumes_credit_per_policy(self):
        """Cancelling inside the 2-hour cutoff window treats cancellation as late and consumes session credit."""
        u, prof, m, ent = self._create_member("late-canceller", units=Decimal('5.00'))
        occ = self._create_occurrence(hours_from_now=1)  # 1 hour from now (within 2h cutoff)

        booking = Booking.objects.using(self.db).create(
            branch=self.branch_alpha,
            occurrence=occ,
            user_profile=prof,
            membership=m,
            entitlement=ent,
            booking_number=f"BK-{uuid.uuid4().hex[:6]}",
            status="CONFIRMED",
            payment_status="PAID",
            confirmation_status="CONFIRMED"
        )
        ent.consumed_units += Decimal('1.00')
        ent.save(using=self.db)

        cancelled_booking = BookingWaitlistAttendanceService.cancel_booking(
            booking=booking,
            cancelled_by_user=u,
            reason_text="Stuck in traffic"
        )

        ent.refresh_from_db()
        booking.refresh_from_db()
        self.assertEqual(booking.status, "CANCELLED")
        # Per policy (late_cancellation_session_action='CONSUME'), credit is retained as consumed
        self.assertEqual(ent.consumed_units, Decimal('1.00'))

        canc_rec = BookingCancellation.objects.using(self.db).filter(booking=booking).first()
        self.assertIsNotNone(canc_rec)
        self.assertFalse(canc_rec.within_cutoff)
        self.assertEqual(canc_rec.session_action_applied, "CONSUME")

    def test_06_rescheduling_cutoff_and_limit_enforcement(self):
        """Rescheduling is blocked inside cutoff and when max reschedules count is exceeded."""
        u, prof, m, ent = self._create_member("rescheduler")
        occ_soon = self._create_occurrence(hours_from_now=1)  # Within 2h cutoff
        occ_target = self._create_occurrence(hours_from_now=48)

        booking = Booking.objects.using(self.db).create(
            branch=self.branch_alpha,
            occurrence=occ_soon,
            user_profile=prof,
            membership=m,
            entitlement=ent,
            booking_number=f"BK-{uuid.uuid4().hex[:6]}",
            status="CONFIRMED",
            payment_status="PAID",
            confirmation_status="CONFIRMED"
        )

        # Attempt reschedule inside cutoff window -> Must raise ValidationError
        with self.assertRaises(ValidationError) as ctx:
            BookingWaitlistAttendanceService.reschedule_booking(
                booking=booking,
                to_occurrence=occ_target,
                rescheduled_by_user=u,
                reason_text="Need different slot"
            )
        self.assertIn("Rescheduling is not permitted", str(ctx.exception))

        # Test max reschedules limit:
        occ_future = self._create_occurrence(hours_from_now=12)
        booking.occurrence = occ_future
        booking.save(using=self.db)

        from apps.tenant_core.models import BookingReschedule
        BookingReschedule.objects.using(self.db).create(
            booking=booking,
            from_occurrence=occ_soon,
            to_occurrence=occ_future,
            reschedule_number=1,
            rescheduled_by_user=u
        )
        BookingReschedule.objects.using(self.db).create(
            booking=booking,
            from_occurrence=occ_future,
            to_occurrence=occ_target,
            reschedule_number=2,
            rescheduled_by_user=u
        )

        with self.assertRaises(ValidationError) as ctx:
            BookingWaitlistAttendanceService.reschedule_booking(
                booking=booking,
                to_occurrence=occ_target,
                rescheduled_by_user=u,
                reason_text="One more change"
            )
        self.assertIn("Maximum allowed reschedules", str(ctx.exception))

    def test_07_branch_override_takes_precedence(self):
        """Branch override overrides organization defaults for that branch, but other branches inherit org defaults."""
        # Create Branch Beta override with 15 minutes unpaid auto-cancel
        BookingPolicyConfig.objects.using(self.db).create(
            organization=self.org,
            branch=self.branch_beta,
            auto_cancel_unpaid_enabled=True,
            auto_cancel_unpaid_minutes=15,
            auto_cancel_unconfirmed_enabled=False,
            auto_cancel_unconfirmed_minutes=120,
            cancellation_cutoff_hours=4,
            rescheduling_cutoff_hours=4,
            max_reschedules_per_booking=1,
            late_cancellation_allowed=False,
            late_cancellation_session_action='CONSUME',
            late_cancellation_fee=Decimal('300.00'),
            allowed_booking_types=['CLASS'],
            notification_channels=['SMS']
        )

        # Effective policy for Branch Beta -> Branch Override
        policy_beta = BookingPolicyService.get_effective_policy(
            organization=self.org,
            branch=self.branch_beta
        )
        self.assertEqual(policy_beta['auto_cancel_unpaid_minutes'], 15)
        self.assertEqual(policy_beta['cancellation_cutoff_hours'], 4)
        self.assertFalse(policy_beta['late_cancellation_allowed'])

        # Effective policy for Branch Alpha -> Falls back to Tenant Default (30 min, 2h cutoff)
        policy_alpha = BookingPolicyService.get_effective_policy(
            organization=self.org,
            branch=self.branch_alpha
        )
        self.assertEqual(policy_alpha['auto_cancel_unpaid_minutes'], 30)
        self.assertEqual(policy_alpha['cancellation_cutoff_hours'], 2)
        self.assertTrue(policy_alpha['late_cancellation_allowed'])

    def test_08_apply_to_existing_bookings_scope_flag(self):
        """When apply_to_existing_bookings is False, older bookings created before policy_effective_at are not auto-cancelled."""
        u, prof, m, ent = self._create_member("prior-booking")
        occ = self._create_occurrence(hours_from_now=24)

        # Booking created 2 hours ago
        booking = Booking.objects.using(self.db).create(
            branch=self.branch_alpha,
            occurrence=occ,
            user_profile=prof,
            membership=m,
            entitlement=ent,
            booking_number=f"BK-{uuid.uuid4().hex[:6]}",
            status="RESERVED",
            payment_status="UNPAID",
            confirmation_status="CONFIRMED"
        )
        Booking.objects.using(self.db).filter(id=booking.id).update(
            created_at=timezone.now() - timedelta(hours=2),
            booked_at=timezone.now() - timedelta(hours=2)
        )
        booking.refresh_from_db()

        # Policy was set 10 minutes ago with apply_to_existing_bookings=False
        self.tenant_policy.policy_effective_at = timezone.now() - timedelta(minutes=10)
        self.tenant_policy.apply_to_existing_bookings = False
        self.tenant_policy.save(using=self.db)

        eligible, _, _ = BookingPolicyService.is_booking_eligible_for_auto_cancellation(
            booking=booking,
            policy=BookingPolicyService.get_effective_policy(self.org, self.branch_alpha)
        )
        self.assertFalse(eligible, "Older booking must NOT be eligible when apply_to_existing_bookings=False")

        # Now change to apply_to_existing_bookings=True
        self.tenant_policy.apply_to_existing_bookings = True
        self.tenant_policy.save(using=self.db)

        eligible_retro, _, _ = BookingPolicyService.is_booking_eligible_for_auto_cancellation(
            booking=booking,
            policy=BookingPolicyService.get_effective_policy(self.org, self.branch_alpha)
        )
        self.assertTrue(eligible_retro, "Older booking must be eligible when apply_to_existing_bookings=True")

    @patch('apps.tenant_core.communication.service.CommunicationService.send_communication')
    def test_09_notification_failure_handled_gracefully(self, mock_comm):
        """Failure in downstream notification delivery does not crash the auto-cancellation process."""
        mock_comm.side_effect = Exception("WhatsApp Gateway Timeout")

        u, prof, m, ent = self._create_member("notif-fail")
        occ = self._create_occurrence(hours_from_now=12)

        booking = Booking.objects.using(self.db).create(
            branch=self.branch_alpha,
            occurrence=occ,
            user_profile=prof,
            membership=m,
            entitlement=ent,
            booking_number=f"BK-{uuid.uuid4().hex[:6]}",
            status="RESERVED",
            payment_status="UNPAID",
            confirmation_status="CONFIRMED"
        )
        Booking.objects.using(self.db).filter(id=booking.id).update(
            created_at=timezone.now() - timedelta(minutes=45),
            booked_at=timezone.now() - timedelta(minutes=45)
        )

        # Should NOT raise an unhandled exception
        stats = BookingPolicyService.process_auto_cancellations(
            organization=self.org,
            branch=self.branch_alpha
        )

        booking.refresh_from_db()
        self.assertEqual(booking.status, "CANCELLED")
        self.assertGreaterEqual(stats["auto_cancelled_count"], 1)

    def test_10_branch_override_save_and_reset_roundtrip(self):
        """Branch override can be configured and then reset back to organization defaults."""
        from apps.tenant_core.views_bookings import BookingPolicyViewSet
        from rest_framework.test import APIRequestFactory, force_authenticate

        factory = APIRequestFactory()
        view = BookingPolicyViewSet.as_view({'post': 'create'})

        # 1. Save branch override via API
        req1 = factory.post('/api/v1/booking-policy/', {
            'branch_id': str(self.branch_beta.id),
            'auto_cancel_unpaid_minutes': 18,
            'cancellation_cutoff_hours': 4,
        }, format='json')
        force_authenticate(req1, user=self.admin_user)
        req1._tenant_db_alias = self.db

        res1 = view(req1)
        self.assertEqual(res1.status_code, 200)
        self.assertTrue(res1.data['is_branch_override'])
        self.assertEqual(res1.data['effective_policy']['auto_cancel_unpaid_minutes'], 18)
        self.assertEqual(res1.data['effective_policy']['cancellation_cutoff_hours'], 4)

        # 2. Reset branch override
        reset_view = BookingPolicyViewSet.as_view({'post': 'reset_branch_override'})
        req2 = factory.post('/api/v1/booking-policy/reset-branch-override/', {
            'branch_id': str(self.branch_beta.id)
        }, format='json')
        force_authenticate(req2, user=self.admin_user)
        req2._tenant_db_alias = self.db

        res2 = reset_view(req2)
        self.assertEqual(res2.status_code, 200)
        self.assertFalse(res2.data['is_branch_override'])
        # Reverts to org default (which is 30 mins)
        self.assertEqual(res2.data['effective_policy']['auto_cancel_unpaid_minutes'], 30)
