import uuid
from decimal import Decimal
from datetime import timedelta
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
    PackageEntitlementDefinition,
    ClassCategory,
    ClassTemplate,
    ClassOccurrence,
    Membership,
    MembershipEntitlement,
    BookingPolicySet,
    BookingCancellationRule,
    AttendancePolicySet,
    AttendancePenaltyRule,
    MemberAttendanceState,
    AttendancePolicyEvent,
    Booking,
    BookingStatusHistory,
    BookingCancellation,
    BookingWaitlistEvent,
    AttendanceRecord,
    AccessEvent,
)
from apps.tenant_core.services_bookings import BookingWaitlistAttendanceService


class Layer2Phase9BookingAttendanceTests(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        self.org = Organization.objects.create(
            name="Sweat Ops Fitness",
            code=f"SWT-{uuid.uuid4().hex[:6]}",
            status="ACTIVE"
        )
        self.loc = Location.objects.create(
            organization=self.org,
            code=f"LOC-{uuid.uuid4().hex[:4]}",
            name="Downtown Center",
            city="New York",
            status="ACTIVE"
        )
        self.branch = Branch.objects.create(
            organization=self.org,
            location=self.loc,
            name="Downtown Studio",
            code=f"DWT-{uuid.uuid4().hex[:4]}",
            timezone="UTC"
        )
        self.user = TenantUser.objects.create(
            organization=self.org,
            email=f"member-{uuid.uuid4().hex[:6]}@example.com",
            first_name="Alex",
            last_name="Runner",
            status="ACTIVE"
        )
        self.member_profile = UserProfile.objects.create(
            user=self.user,
            member_number=f"MEM-{uuid.uuid4().hex[:6]}",
            first_name_snapshot="Alex",
            last_name_snapshot="Runner",
            preferred_branch=self.branch,
            member_status="ACTIVE"
        )

        # 2nd user for waitlist tests
        self.user2 = TenantUser.objects.create(
            organization=self.org,
            email=f"waitlist-{uuid.uuid4().hex[:6]}@example.com",
            first_name="Sam",
            last_name="Waiter",
            status="ACTIVE"
        )
        self.member_profile2 = UserProfile.objects.create(
            user=self.user2,
            member_number=f"MEM-{uuid.uuid4().hex[:6]}",
            first_name_snapshot="Sam",
            last_name_snapshot="Waiter",
            preferred_branch=self.branch,
            member_status="ACTIVE"
        )

        # Catalog & Class Occurrence setup
        self.prog_cat = ProgramCategory.objects.create(
            organization=self.org,
            name="High Intensity",
            code=f"HI-{uuid.uuid4().hex[:4]}"
        )
        self.program = Program.objects.create(
            organization=self.org,
            category=self.prog_cat,
            name="Sweat Bootcamp",
            code=f"SBC-{uuid.uuid4().hex[:4]}"
        )
        self.cls_cat = ClassCategory.objects.create(
            organization=self.org,
            name="Studio HIIT",
            code=f"SHIIT-{uuid.uuid4().hex[:4]}"
        )
        self.cls_tmpl = ClassTemplate.objects.create(
            organization=self.org,
            category=self.cls_cat,
            program=self.program,
            name="Sweat 45 Express",
            code=f"S45-{uuid.uuid4().hex[:4]}",
            default_duration_minutes=45,
            default_capacity=1
        )
        self.start_time = timezone.now() + timedelta(days=1)
        self.occurrence = ClassOccurrence.objects.create(
            class_template=self.cls_tmpl,
            branch=self.branch,
            start_at=self.start_time,
            end_at=self.start_time + timedelta(minutes=45),
            capacity=1,
            status='SCHEDULED'
        )

        # Active Membership & Entitlements for Alex
        self.package = Package.objects.create(
            organization=self.org,
            program=self.program,
            name="Bootcamp 10 Pack",
            code=f"BC10-{uuid.uuid4().hex[:4]}",
            status="ACTIVE"
        )
        self.pkg_ver = PackageVersion.objects.create(
            package=self.package,
            version_number=1,
            name_snapshot="Bootcamp 10 Pack v1",
            duration_value=3,
            duration_unit='MONTH',
            effective_from=timezone.now(),
            status="ACTIVE",
            created_by_user=self.user
        )
        self.ent_def = PackageEntitlementDefinition.objects.create(
            package_version=self.pkg_ver,
            entitlement_type="CLASS_SESSIONS",
            allocated_units=Decimal('10.00'),
            is_unlimited=False,
        )
        self.membership = Membership.objects.create(
            user_profile=self.member_profile,
            package=self.package,
            package_version=self.pkg_ver,
            home_branch=self.branch,
            purchase_branch=self.branch,
            membership_number=f"MB-{uuid.uuid4().hex[:6].upper()}",
            start_date=timezone.now().date(),
            end_date=(timezone.now() + timedelta(days=90)).date(),
            status="ACTIVE"
        )
        self.entitlement = MembershipEntitlement.objects.create(
            membership=self.membership,
            source_definition=self.ent_def,
            entitlement_type="CLASS_SESSIONS",
            allocated_units=Decimal('10.00'),
            consumed_units=Decimal('0.00'),
            valid_from=timezone.now(),
            valid_until=timezone.now() + timedelta(days=90)
        )

        # Booking Policy
        self.booking_policy = BookingPolicySet.objects.create(
            organization=self.org,
            branch=self.branch,
            version_number=1,
            allow_waitlist=True,
            waitlist_capacity=10,
            status='ACTIVE'
        )

        # Cancellation Rule
        self.cancel_rule = BookingCancellationRule.objects.create(
            organization=self.org,
            booking_policy_set=self.booking_policy,
            rule_name="Standard Early Cancellation",
            cancellation_type="EARLY_CANCEL",
            session_action="RESTORE",
            min_minutes_before=60,
            priority=100,
            status="ACTIVE"
        )

        # Attendance Policy & Penalty Rules
        self.att_policy = AttendancePolicySet.objects.create(
            organization=self.org,
            branch=self.branch,
            name="Standard Attendance Policy",
            version_number=1,
            normal_max_advance_bookings=5,
            restricted_max_advance_bookings=1,
            restriction_threshold=2,
            reset_on_successful_attendance=True,
            status='ACTIVE'
        )
        self.att_rule = AttendancePenaltyRule.objects.create(
            attendance_policy_set=self.att_policy,
            rule_code="CONSECUTIVE_NO_SHOW",
            sequence_from=2,
            base_session_units=Decimal('1.00'),
            additional_penalty_units=Decimal('1.00'),
            status='ACTIVE'
        )

    def test_booking_creation_with_entitlement_consumption(self):
        """Test booking reservation consumes member entitlement and confirms booking."""
        booking = BookingWaitlistAttendanceService.create_booking(
            user_profile=self.member_profile,
            occurrence=self.occurrence,
            booking_type='MEMBER',
            membership=self.membership,
            created_by_user=self.user
        )

        self.assertEqual(booking.status, 'CONFIRMED')
        self.assertIsNone(booking.waitlist_position)

        # Verify entitlement consumption
        self.entitlement.refresh_from_db()
        self.assertEqual(self.entitlement.consumed_units, Decimal('1.00'))
        self.assertEqual(self.entitlement.remaining_units, Decimal('9.00'))

        # Verify occurrence active count
        active_count = Booking.objects.filter(
            occurrence=self.occurrence,
            status__in=['CONFIRMED', 'RESERVED', 'COMPLETED']
        ).count()
        self.assertEqual(active_count, 1)

        # Verify audit & status history
        self.assertTrue(BookingStatusHistory.objects.filter(booking=booking, to_status='CONFIRMED').exists())

    def test_capacity_waitlist_overflow_and_auto_promotion(self):
        """When capacity is reached, subsequent booking joins waitlist. Cancellation auto-promotes top waitlisted booking."""
        # 1. Fill capacity with member 1
        booking1 = BookingWaitlistAttendanceService.create_booking(
            user_profile=self.member_profile,
            occurrence=self.occurrence,
            booking_type='MEMBER',
            membership=self.membership
        )
        self.assertEqual(booking1.status, 'CONFIRMED')

        # 2. Member 2 tries to book full class -> waitlisted at position 1
        booking2 = BookingWaitlistAttendanceService.create_booking(
            user_profile=self.member_profile2,
            occurrence=self.occurrence,
            booking_type='WALK_IN'
        )
        self.assertEqual(booking2.status, 'WAITLISTED')
        self.assertEqual(booking2.waitlist_position, 1)

        # Verify waitlist event logged
        self.assertTrue(BookingWaitlistEvent.objects.filter(booking=booking2, event_type='JOINED').exists())

        # 3. Member 1 cancels -> triggers auto-promotion of Member 2
        BookingWaitlistAttendanceService.cancel_booking(
            booking=booking1,
            reason_code='PERSONAL_EMERGENCY'
        )

        booking1.refresh_from_db()
        self.assertEqual(booking1.status, 'CANCELLED')

        booking2.refresh_from_db()
        self.assertEqual(booking2.status, 'CONFIRMED')
        self.assertIsNone(booking2.waitlist_position)

        # Occurrence capacity remains 1 (Member 2 now holds the spot)
        active_count = Booking.objects.filter(
            occurrence=self.occurrence,
            status__in=['CONFIRMED', 'RESERVED', 'COMPLETED']
        ).count()
        self.assertEqual(active_count, 1)

        # Promotion event logged
        self.assertTrue(BookingWaitlistEvent.objects.filter(booking=booking2, event_type='PROMOTED').exists())

    def test_booking_cancellation_policy_and_session_restore(self):
        """Early cancellation restores member session entitlement and writes snapshot record."""
        booking = BookingWaitlistAttendanceService.create_booking(
            user_profile=self.member_profile,
            occurrence=self.occurrence,
            booking_type='MEMBER',
            membership=self.membership
        )
        self.entitlement.refresh_from_db()
        self.assertEqual(self.entitlement.remaining_units, Decimal('9.00'))

        cancellation = BookingWaitlistAttendanceService.cancel_booking(
            booking=booking,
            reason_code='MEMBER_REQUEST',
            reason_text='Rescheduling to another day'
        )

        self.assertEqual(cancellation.session_action_applied, 'RESTORE')
        self.assertTrue(cancellation.within_cutoff)

        # Verify entitlement restored back to 10
        self.entitlement.refresh_from_db()
        self.assertEqual(self.entitlement.remaining_units, Decimal('10.00'))

        # Class capacity freed
        active_count = Booking.objects.filter(
            occurrence=self.occurrence,
            status__in=['CONFIRMED', 'RESERVED', 'COMPLETED']
        ).count()
        self.assertEqual(active_count, 0)

    def test_attendance_checkin_and_completion(self):
        """Recording attendance as PRESENT completes booking and creates attendance record."""
        booking = BookingWaitlistAttendanceService.create_booking(
            user_profile=self.member_profile,
            occurrence=self.occurrence,
            booking_type='MEMBER',
            membership=self.membership
        )

        att = BookingWaitlistAttendanceService.record_attendance(
            booking=booking,
            status='PRESENT',
            check_in_method='QR'
        )

        self.assertEqual(att.status, 'PRESENT')
        self.assertEqual(att.check_in_status, 'SUCCESSFUL')
        self.assertIsNotNone(att.check_in_at)

        booking.refresh_from_db()
        self.assertEqual(booking.status, 'COMPLETED')
        self.assertIsNotNone(booking.completed_at)

    def test_no_show_penalty_and_single_booking_restriction(self):
        """Consecutive no-shows reach restriction threshold, placing member into SINGLE_BOOKING restriction."""
        # Booking 1: No show
        booking1 = BookingWaitlistAttendanceService.create_booking(
            user_profile=self.member_profile,
            occurrence=self.occurrence,
            booking_type='MEMBER',
            membership=self.membership
        )
        BookingWaitlistAttendanceService.record_attendance(booking=booking1, status='NO_SHOW')

        state = MemberAttendanceState.objects.get(
            user_profile=self.member_profile,
            attendance_policy_set=self.att_policy
        )
        self.assertEqual(state.consecutive_no_show_count, 1)
        self.assertEqual(state.booking_mode, 'NORMAL')

        # Occurrence 2
        occ2 = ClassOccurrence.objects.create(
            class_template=self.cls_tmpl,
            branch=self.branch,
            start_at=self.start_time + timedelta(days=1),
            end_at=self.start_time + timedelta(days=1, minutes=45),
            capacity=10,
            status='SCHEDULED'
        )
        booking2 = BookingWaitlistAttendanceService.create_booking(
            user_profile=self.member_profile,
            occurrence=occ2,
            booking_type='MEMBER',
            membership=self.membership
        )
        BookingWaitlistAttendanceService.record_attendance(booking=booking2, status='NO_SHOW')

        # Now consecutive no-show is 2 -> reaches restriction_threshold (2)
        state.refresh_from_db()
        self.assertEqual(state.consecutive_no_show_count, 2)
        self.assertEqual(state.booking_mode, 'SINGLE_BOOKING')
        self.assertEqual(state.current_max_advance_bookings, 1)

        # Verify restriction event logged
        self.assertTrue(AttendancePolicyEvent.objects.filter(
            user_profile=self.member_profile,
            event_type='RESTRICTION_ACTIVATED'
        ).exists())

    def test_access_event_auto_checkin(self):
        """Access control entry event for member with today's booking auto checks-in attendance."""
        today_start = timezone.now() + timedelta(hours=1)
        occ_today = ClassOccurrence.objects.create(
            class_template=self.cls_tmpl,
            branch=self.branch,
            start_at=today_start,
            end_at=today_start + timedelta(minutes=45),
            capacity=10,
            status='SCHEDULED'
        )
        booking = BookingWaitlistAttendanceService.create_booking(
            user_profile=self.member_profile,
            occurrence=occ_today,
            booking_type='MEMBER',
            membership=self.membership
        )

        event = BookingWaitlistAttendanceService.log_access_event(
            user_profile=self.member_profile,
            branch=self.branch,
            event_type='ENTRY',
            device_reference='TURNSTILE-GATE-01'
        )

        self.assertEqual(event.event_type, 'ENTRY')

        # Verify booking status changed to COMPLETED via auto-check-in
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'COMPLETED')
        self.assertTrue(AttendanceRecord.objects.filter(booking=booking, check_in_method='ACCESS_DEVICE').exists())
