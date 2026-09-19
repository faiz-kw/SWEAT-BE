import uuid
from decimal import Decimal
from datetime import timedelta, date
from django.test import TestCase
from django.utils import timezone
from django.core.exceptions import ValidationError
from rest_framework.test import APIRequestFactory, force_authenticate

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
    PackageEntitlementDefinition,
    ClassCategory,
    ClassTemplate,
    ClassOccurrence,
    PackageClassAccessRule,
    Membership,
    MembershipEntitlement,
    MembershipEntitlementLedger,
    BookingPolicySet,
    BookingCancellationRule,
    AttendancePolicySet,
    MemberAttendanceState,
    Booking,
    BookingStatusHistory,
    BookingReschedule,
    BookingCancellation,
    BookingWaitlistEvent,
    Role,
    RoleAssignment,
    RoleModuleAccess,
    RolePermissionSetItem,
    UserBranch,
)
from apps.tenant_core.services_bookings import BookingWaitlistAttendanceService
from apps.tenant_core.views_bookings import BookingViewSet


class BookingsModuleTargetedTests(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        self.db = 'tenant_test'
        set_tenant_db_alias(self.db)

        self.org = Organization.objects.using(self.db).create(
            name="Targeted Fitness Corp",
            code=f"TGT-{uuid.uuid4().hex[:6]}",
            status="ACTIVE"
        )
        self.loc = Location.objects.using(self.db).create(
            organization=self.org,
            code=f"LOC-{uuid.uuid4().hex[:4]}",
            name="Main Center",
            city="New York",
            status="ACTIVE"
        )
        self.branch1 = Branch.objects.using(self.db).create(
            organization=self.org,
            location=self.loc,
            name="Branch Alpha",
            code=f"BR1-{uuid.uuid4().hex[:4]}",
            timezone="UTC"
        )
        self.branch2 = Branch.objects.using(self.db).create(
            organization=self.org,
            location=self.loc,
            name="Branch Beta",
            code=f"BR2-{uuid.uuid4().hex[:4]}",
            timezone="UTC"
        )

        # Admin user
        self.admin_user = TenantUser.objects.using(self.db).create(
            organization=self.org,
            email="admin@targeted.local",
            first_name="Admin",
            last_name="User",
            status="ACTIVE"
        )
        self.admin_user._db_alias = self.db
        self.admin_user._tenant_id = str(uuid.uuid4())

        # Org Admin Role with ORG scope
        self.admin_role = Role.objects.using(self.db).create(
            organization=self.org,
            name="Org Admin",
            code="ORG_ADMIN",
            scope="ORG",
            is_active=True
        )
        RoleAssignment.objects.using(self.db).create(
            organization=self.org,
            user=self.admin_user,
            role=self.admin_role,
            is_active=True
        )

        # Catalog setup
        self.prog_cat = ProgramCategory.objects.using(self.db).create(
            organization=self.org,
            code=f"CAT-{uuid.uuid4().hex[:4]}",
            name="Strength",
            status="ACTIVE"
        )
        self.program = Program.objects.using(self.db).create(
            organization=self.org,
            category=self.prog_cat,
            code=f"PRG-{uuid.uuid4().hex[:4]}",
            name="Core Strength",
            status="ACTIVE"
        )
        self.package = Package.objects.using(self.db).create(
            organization=self.org,
            program=self.program,
            code=f"PKG-{uuid.uuid4().hex[:4]}",
            name="All-Access Package",
            status="ACTIVE"
        )
        self.pkg_v1 = PackageVersion.objects.using(self.db).create(
            package=self.package,
            version_number=1,
            name_snapshot="V1 Standard",
            duration_value=3,
            duration_unit='MONTH',
            effective_from=timezone.now(),
            status="ACTIVE",
            created_by_user=self.admin_user
        )
        self.pkg_price = PackagePrice.objects.using(self.db).create(
            package_version=self.pkg_v1,
            branch=self.branch1,
            base_price=Decimal('100.00'),
            effective_from=timezone.now(),
            status='ACTIVE',
            created_by_user=self.admin_user
        )

        # Class Template & Occurrences
        self.class_cat = ClassCategory.objects.using(self.db).create(
            organization=self.org,
            code=f"CC-{uuid.uuid4().hex[:4]}",
            name="Group Fitness",
            status="ACTIVE"
        )
        self.class_template1 = ClassTemplate.objects.using(self.db).create(
            organization=self.org,
            category=self.class_cat,
            code=f"TPL1-{uuid.uuid4().hex[:4]}",
            name="Power Yoga",
            default_capacity=2,
            status="ACTIVE"
        )
        self.class_template2 = ClassTemplate.objects.using(self.db).create(
            organization=self.org,
            category=self.class_cat,
            code=f"TPL2-{uuid.uuid4().hex[:4]}",
            name="HIIT Sprint",
            default_capacity=2,
            status="ACTIVE"
        )

        tomorrow = timezone.now().date() + timedelta(days=1)
        start1 = timezone.now() + timedelta(days=1, hours=2)
        end1 = start1 + timedelta(hours=1)

        self.occ1 = ClassOccurrence.objects.using(self.db).create(
            class_template=self.class_template1,
            branch=self.branch1,
            occurrence_date=tomorrow,
            start_at=start1,
            end_at=end1,
            capacity=1,  # 1-seat for testing capacity & waitlists
            status="SCHEDULED"
        )

        start2 = timezone.now() + timedelta(days=2, hours=2)
        end2 = start2 + timedelta(hours=1)
        self.occ2 = ClassOccurrence.objects.using(self.db).create(
            class_template=self.class_template2,
            branch=self.branch1,
            occurrence_date=tomorrow + timedelta(days=1),
            start_at=start2,
            end_at=end2,
            capacity=2,
            status="SCHEDULED"
        )

    def _create_member(self, email_prefix: str, active: bool = True, units: Decimal = Decimal('5.00'), pkg_ver=None):
        u = TenantUser.objects.using(self.db).create(
            organization=self.org,
            email=f"{email_prefix}-{uuid.uuid4().hex[:6]}@targeted.local",
            first_name="Test",
            last_name="Member",
            phone="+1234567890",
            status="ACTIVE" if active else "INACTIVE"
        )
        prof = UserProfile.objects.using(self.db).create(
            user=u,
            member_number=f"MEM-{uuid.uuid4().hex[:6]}",
            first_name_snapshot="Test",
            last_name_snapshot="Member",
            preferred_branch=self.branch1,
            member_status="ACTIVE" if active else "INACTIVE"
        )
        today = timezone.now().date()
        m = Membership.objects.using(self.db).create(
            user_profile=prof,
            program=self.program,
            package=self.package,
            package_version=pkg_ver or self.pkg_v1,
            package_price=self.pkg_price,
            purchase_branch=self.branch1,
            home_branch=self.branch1,
            membership_number=f"MB-{uuid.uuid4().hex[:6]}",
            start_date=today - timedelta(days=10),
            end_date=today + timedelta(days=50),
            status="ACTIVE" if active else "EXPIRED"
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

    def test_01_confirmed_booking(self):
        """Scenario 1: confirmed booking allocates seat and sets CONFIRMED status."""
        _, prof, m, _ = self._create_member("scen1")
        b = BookingWaitlistAttendanceService.create_booking(
            user_profile=prof,
            occurrence=self.occ1,
            booking_type='MEMBER',
            membership=m,
            db_alias=self.db
        )
        self.assertEqual(b.status, 'CONFIRMED')
        self.assertIsNone(b.waitlist_position)
        self.assertEqual(b.occurrence_id, self.occ1.id)

    def test_02_duplicate_booking_rejected(self):
        """Scenario 2: duplicate active booking for same occurrence raises ValidationError."""
        _, prof, m, _ = self._create_member("scen2")
        BookingWaitlistAttendanceService.create_booking(
            user_profile=prof,
            occurrence=self.occ1,
            membership=m,
            db_alias=self.db
        )
        with self.assertRaises(ValidationError):
            BookingWaitlistAttendanceService.create_booking(
                user_profile=prof,
                occurrence=self.occ1,
                membership=m,
                db_alias=self.db
            )

    def test_03_inactive_membership_rejected(self):
        """Scenario 3: inactive or expired membership cannot make member bookings."""
        _, prof, m, _ = self._create_member("scen3", active=False)
        m.status = 'EXPIRED'
        m.save(using=self.db)
        with self.assertRaises(ValidationError):
            BookingWaitlistAttendanceService.create_booking(
                user_profile=prof,
                occurrence=self.occ2,
                membership=m,
                db_alias=self.db
            )

    def test_04_package_version_class_access_denied(self):
        """Scenario 4: PackageClassAccessRule with EXCLUDED denies access."""
        _, prof, m, _ = self._create_member("scen4")
        PackageClassAccessRule.objects.using(self.db).create(
            package_version=self.pkg_v1,
            class_template=self.class_template1,
            access_type='EXCLUDED',
            status='ACTIVE'
        )
        with self.assertRaises(ValidationError):
            BookingWaitlistAttendanceService.create_booking(
                user_profile=prof,
                occurrence=self.occ1,
                membership=m,
                db_alias=self.db
            )

    def test_05_missing_insufficient_entitlement_rejected(self):
        """Scenario 5: 0 remaining entitlement units rejects booking."""
        _, prof, m, _ = self._create_member("scen5", units=Decimal('0.00'))
        with self.assertRaises(ValidationError):
            BookingWaitlistAttendanceService.create_booking(
                user_profile=prof,
                occurrence=self.occ2,
                membership=m,
                db_alias=self.db
            )

    def test_06_confirmed_booking_consumes_exactly_once(self):
        """Scenario 6: confirmed booking consumes exactly 1 unit and writes CONSUMPTION ledger."""
        _, prof, m, ent = self._create_member("scen6", units=Decimal('3.00'))
        BookingWaitlistAttendanceService.create_booking(
            user_profile=prof,
            occurrence=self.occ2,
            membership=m,
            db_alias=self.db
        )
        ent.refresh_from_db()
        self.assertEqual(ent.consumed_units, Decimal('1.00'))
        self.assertEqual(ent.remaining_units, Decimal('2.00'))
        ledger_count = MembershipEntitlementLedger.objects.using(self.db).filter(
            membership_entitlement=ent, transaction_type='CONSUMPTION'
        ).count()
        self.assertEqual(ledger_count, 1)

    def test_07_full_occurrence_waitlist(self):
        """Scenario 7: When capacity is full, subsequent booking joins waitlist."""
        _, prof1, m1, _ = self._create_member("scen7-1")
        _, prof2, m2, _ = self._create_member("scen7-2")

        b1 = BookingWaitlistAttendanceService.create_booking(
            user_profile=prof1, occurrence=self.occ1, membership=m1, db_alias=self.db
        )
        self.assertEqual(b1.status, 'CONFIRMED')

        b2 = BookingWaitlistAttendanceService.create_booking(
            user_profile=prof2, occurrence=self.occ1, membership=m2, db_alias=self.db
        )
        self.assertEqual(b2.status, 'WAITLISTED')
        self.assertEqual(b2.waitlist_position, 1)

    def test_08_waitlist_consumes_zero(self):
        """Scenario 8: Joining waitlist consumes ZERO entitlement."""
        _, prof1, m1, _ = self._create_member("scen8-1")
        _, prof2, m2, ent2 = self._create_member("scen8-2", units=Decimal('5.00'))

        BookingWaitlistAttendanceService.create_booking(
            user_profile=prof1, occurrence=self.occ1, membership=m1, db_alias=self.db
        )
        BookingWaitlistAttendanceService.create_booking(
            user_profile=prof2, occurrence=self.occ1, membership=m2, db_alias=self.db
        )
        ent2.refresh_from_db()
        self.assertEqual(ent2.consumed_units, Decimal('0.00'))
        self.assertEqual(ent2.remaining_units, Decimal('5.00'))

    def test_09_valid_cancellation_reversal(self):
        """Scenario 9: Cancellation appends REVERSAL ledger and never deletes CONSUMPTION."""
        _, prof, m, ent = self._create_member("scen9", units=Decimal('5.00'))
        b = BookingWaitlistAttendanceService.create_booking(
            user_profile=prof, occurrence=self.occ2, membership=m, db_alias=self.db
        )
        ent.refresh_from_db()
        self.assertEqual(ent.consumed_units, Decimal('1.00'))

        BookingCancellationRule.objects.using(self.db).create(
            organization=self.org,
            rule_name="Standard Cancel",
            cancellation_type="EARLY_CANCEL",
            session_action="RESTORE",
            priority=1,
            status="ACTIVE"
        )

        BookingWaitlistAttendanceService.cancel_booking(
            booking=b, reason_code="MEMBER_REQUEST", db_alias=self.db
        )
        ent.refresh_from_db()
        self.assertEqual(ent.consumed_units, Decimal('0.00'))
        self.assertEqual(ent.remaining_units, Decimal('5.00'))

        # Check ledgers: both CONSUMPTION and REVERSAL must exist
        ledgers = list(MembershipEntitlementLedger.objects.using(self.db).filter(membership_entitlement=ent))
        types = [l.transaction_type for l in ledgers]
        self.assertIn('CONSUMPTION', types)
        self.assertIn('REVERSAL', types)

    def test_10_waitlist_promotion_revalidation_and_atomic_consumption(self):
        """Scenario 10: Cancellation triggers waitlist promotion with revalidation and atomic entitlement consumption."""
        _, prof1, m1, _ = self._create_member("scen10-1")
        _, prof2, m2, ent2 = self._create_member("scen10-2", units=Decimal('4.00'))

        b1 = BookingWaitlistAttendanceService.create_booking(
            user_profile=prof1, occurrence=self.occ1, membership=m1, db_alias=self.db
        )
        b2 = BookingWaitlistAttendanceService.create_booking(
            user_profile=prof2, occurrence=self.occ1, membership=m2, db_alias=self.db
        )
        self.assertEqual(b2.status, 'WAITLISTED')

        # Cancel b1 -> b2 is promoted
        BookingWaitlistAttendanceService.cancel_booking(b1, db_alias=self.db)

        b2.refresh_from_db()
        self.assertEqual(b2.status, 'CONFIRMED')
        self.assertIsNone(b2.waitlist_position)

        # Verify entitlement was consumed for b2 upon promotion
        ent2.refresh_from_db()
        self.assertEqual(ent2.consumed_units, Decimal('1.00'))

    def test_11_final_seat_concurrency(self):
        """Scenario 11: 1-seat occurrence correctly caps at 1 CONFIRMED and 1 WAITLISTED."""
        _, p1, m1, _ = self._create_member("c1")
        _, p2, m2, _ = self._create_member("c2")

        b1 = BookingWaitlistAttendanceService.create_booking(user_profile=p1, occurrence=self.occ1, membership=m1, db_alias=self.db)
        b2 = BookingWaitlistAttendanceService.create_booking(user_profile=p2, occurrence=self.occ1, membership=m2, db_alias=self.db)

        confirmed_count = Booking.objects.using(self.db).filter(occurrence=self.occ1, status='CONFIRMED').count()
        waitlist_count = Booking.objects.using(self.db).filter(occurrence=self.occ1, status='WAITLISTED').count()

        self.assertEqual(confirmed_count, 1)
        self.assertEqual(waitlist_count, 1)

    def test_12_unauthorized_permission_403(self):
        """Scenario 12: User without required RBAC permissions receives 403."""
        unauth_user = TenantUser.objects.using(self.db).create(
            organization=self.org,
            email="unauth@targeted.local",
            first_name="No",
            last_name="Perms",
            status="ACTIVE"
        )
        unauth_user._db_alias = self.db
        unauth_user._auth_type = 'tenant'
        unauth_user._tenant_id = str(uuid.uuid4())

        factory = APIRequestFactory()
        request = factory.get('/api/v1/tenant/bookings/')
        request.user = unauth_user
        request._tenant_db_alias = self.db
        force_authenticate(request, user=unauth_user)

        view = BookingViewSet.as_view({'get': 'list'})
        response = view(request)
        self.assertEqual(response.status_code, 403)

    def test_13_effective_branch_scope(self):
        """Scenario 13: User restricted to branch2 cannot book branch1, and ORG role resolves all branches (Correction 1)."""
        branch_user = TenantUser.objects.using(self.db).create(
            organization=self.org,
            email="branch2user@targeted.local",
            first_name="Branch2",
            last_name="Staff",
            status="ACTIVE"
        )
        branch_user._db_alias = self.db
        branch_user._auth_type = 'tenant'
        branch_user._tenant_id = str(uuid.uuid4())

        branch_role = Role.objects.using(self.db).create(
            organization=self.org,
            name="Branch Staff",
            code="BR_STAFF",
            scope="BRANCH",
            is_active=True
        )
        RoleAssignment.objects.using(self.db).create(
            organization=self.org,
            user=branch_user,
            role=branch_role,
            branch=self.branch2,  # Only branch 2!
            is_active=True
        )

        from apps.tenant_core.views_bookings import get_user_effective_branch_ids
        # Verify derived effective branch IDs only contain branch 2
        effective_branches = get_user_effective_branch_ids(branch_user, self.db)
        self.assertEqual(effective_branches, {str(self.branch2.id)})
        self.assertNotIn(str(self.occ1.branch_id), effective_branches)

        # When assigned an ORG scope role, effective branches resolves to None (all branches permitted)
        org_role = Role.objects.using(self.db).create(
            organization=self.org,
            name="Org Manager",
            code="ORG_MGR",
            scope="ORG",
            is_active=True
        )
        RoleAssignment.objects.using(self.db).create(
            organization=self.org,
            user=branch_user,
            role=org_role,
            is_active=True
        )
        effective_after_org = get_user_effective_branch_ids(branch_user, self.db)
        self.assertIsNone(effective_after_org)

    def test_14_tenant_isolation(self):
        """Scenario 14: Booking rows exist strictly in tenant DB, never in default."""
        from django.db import ProgrammingError, DatabaseError
        _, prof, m, _ = self._create_member("scen14")
        b = BookingWaitlistAttendanceService.create_booking(
            user_profile=prof, occurrence=self.occ2, membership=m, db_alias=self.db
        )
        # Verify exists in self.db
        self.assertTrue(Booking.objects.using(self.db).filter(id=b.id).exists())
        # Verify does NOT exist in default (table is isolated from default)
        try:
            exists_in_default = Booking.objects.using('default').filter(id=b.id).exists()
            self.assertFalse(exists_in_default)
        except (ProgrammingError, DatabaseError):
            # Strict schema isolation: bookings table does not exist in master DB
            pass

    def test_15_no_show_restriction(self):
        """Scenario 15: Member in SINGLE_BOOKING mode exceeds allowed advance bookings -> denied (Correction 10)."""
        _, prof, m, _ = self._create_member("scen15")
        att_policy = AttendancePolicySet.objects.using(self.db).create(
            organization=self.org,
            name="No-Show Restriction Policy",
            status="ACTIVE"
        )
        MemberAttendanceState.objects.using(self.db).create(
            user_profile=prof,
            attendance_policy_set=att_policy,
            booking_mode="SINGLE_BOOKING",
            current_max_advance_bookings=1,
            consecutive_no_show_count=3
        )
        # Create 1 advance booking
        BookingWaitlistAttendanceService.create_booking(
            user_profile=prof, occurrence=self.occ2, membership=m, db_alias=self.db
        )
        # Attempting 2nd advance booking must be rejected
        with self.assertRaises(ValidationError):
            BookingWaitlistAttendanceService.create_booking(
                user_profile=prof, occurrence=self.occ1, membership=m, db_alias=self.db
            )

    def test_16_reschedule_success_without_double_consumption(self):
        """Scenario 16: Rescheduling moves booking to new occurrence without double consuming entitlement (Correction 6 & 7)."""
        _, prof, m, ent = self._create_member("scen16", units=Decimal('5.00'))
        b = BookingWaitlistAttendanceService.create_booking(
            user_profile=prof, occurrence=self.occ2, membership=m, db_alias=self.db
        )
        ent.refresh_from_db()
        self.assertEqual(ent.consumed_units, Decimal('1.00'))

        # Reschedule from occ2 to occ1
        rescheduled = BookingWaitlistAttendanceService.reschedule_booking(
            booking=b,
            to_occurrence=self.occ1,
            reason_code="SCHEDULE_CONFLICT",
            db_alias=self.db
        )
        self.assertEqual(rescheduled.occurrence_id, self.occ1.id)
        # Entitlement consumed units must STILL be 1.00 (NOT 2.00)
        ent.refresh_from_db()
        self.assertEqual(ent.consumed_units, Decimal('1.00'))

        # BookingReschedule record created
        reschedule_records = BookingReschedule.objects.using(self.db).filter(booking=b)
        self.assertEqual(reschedule_records.count(), 1)
        self.assertEqual(reschedule_records.first().to_occurrence_id, self.occ1.id)

    def test_17_failed_reschedule_leaves_original_booking_unchanged(self):
        """Scenario 17: Reschedule failure (e.g. target full) leaves original booking untouched (Correction 7)."""
        _, p1, m1, _ = self._create_member("scen17-1")
        _, p2, m2, _ = self._create_member("scen17-2")

        # occ1 capacity is 1: fill it with p1
        BookingWaitlistAttendanceService.create_booking(
            user_profile=p1, occurrence=self.occ1, membership=m1, db_alias=self.db
        )

        # p2 books occ2
        b2 = BookingWaitlistAttendanceService.create_booking(
            user_profile=p2, occurrence=self.occ2, membership=m2, db_alias=self.db
        )

        # p2 attempts reschedule to occ1 (which is full) -> must raise ValidationError
        with self.assertRaises(ValidationError):
            BookingWaitlistAttendanceService.reschedule_booking(
                booking=b2,
                to_occurrence=self.occ1,
                db_alias=self.db
            )

        # Original booking b2 is untouched and remains confirmed in occ2
        b2.refresh_from_db()
        self.assertEqual(b2.status, 'CONFIRMED')
        self.assertEqual(b2.occurrence_id, self.occ2.id)

    def test_18_v1_package_access_preserved_after_v2_catalog_change(self):
        """Scenario 18: Member on V1 continues receiving V1 class access even after V2 restricts it (Correction 4)."""
        # Package Version 1: allow class_template1
        PackageClassAccessRule.objects.using(self.db).create(
            package_version=self.pkg_v1,
            class_template=self.class_template1,
            access_type='INCLUDED',
            status='ACTIVE'
        )

        # Member purchased V1
        _, prof, m, _ = self._create_member("scen18", pkg_ver=self.pkg_v1)

        # Now catalog creates V2 with class_template1 EXCLUDED
        pkg_v2 = PackageVersion.objects.using(self.db).create(
            package=self.package,
            version_number=2,
            name_snapshot="V2 Restrictive",
            duration_value=3,
            duration_unit='MONTH',
            effective_from=timezone.now(),
            status="ACTIVE",
            created_by_user=self.admin_user
        )
        PackageClassAccessRule.objects.using(self.db).create(
            package_version=pkg_v2,
            class_template=self.class_template1,
            access_type='EXCLUDED',
            status='ACTIVE'
        )

        # Member on V1 CAN still book class_template1!
        b = BookingWaitlistAttendanceService.create_booking(
            user_profile=prof,
            occurrence=self.occ1,
            membership=m,
            db_alias=self.db
        )
        self.assertEqual(b.status, 'CONFIRMED')
