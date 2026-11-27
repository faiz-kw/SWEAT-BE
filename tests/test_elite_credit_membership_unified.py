"""
backend/tests/test_elite_credit_membership_unified.py

Comprehensive regression test suite for the Elite Credit-Based Membership Program
(CREDIT_WALLET) and configurable dynamic policies:
1. Variable & fractional (0.5) credit deduction, cancellation restore, waitlist auto-promotion, NO_SHOW reversal, & decimal preservation across APIs.
2. Configurable Branch Availability (CATALOG_BRANCHES, ALL_BRANCHES, HOME_ONLY).
3. Multi-pack resolution priority preserving Elite credits when a dedicated session pack matches.
4. Configurable Reschedule Credit Difference modes (ADJUST_DIFFERENCE, BLOCK_MISMATCH, KEEP_ORIGINAL).
5. Future renewal queue isolation in /mobile/credits/ (UPCOMING, no double-counting) & PAR-Q COMPLETED normalization.
6. Upgrade into CREDIT_WALLET expiring stale home/session entitlements + zero-difference upgrade immediate execution.
7. Mobile packages filtering (trials & hidden app packages excluded, taxes & credits) and unified checkout pricing (discount before tax, no hardcoded 499 fee).
8. Dynamic PackageVersion perks_config: Body Assessments, Guest Passes (+ monthly limits & cancel restore), PT via Credits (+ cancel restore), and Advance Booking Window override.
9. Automated overdue ACTIVE -> EXPIRED membership & entitlement expiry service task (idempotent).
"""

import hmac
import hashlib
import json
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.master.models import Tenant, TenantDataSource
from apps.tenant_core.context import set_tenant_db_alias
from apps.tenant_core.models import (
    AttendancePenaltyRule,
    AttendancePolicySet,
    Booking,
    BookingPolicyConfig,
    Branch,
    ClassCategory,
    ClassOccurrence,
    ClassTemplate,
    IntakeForm,
    IntakeSubmission,
    Location,
    Membership,
    MembershipEntitlement,
    MembershipEntitlementLedger,
    MembershipStatusHistory,
    Organization,
    OrganizationSettings,
    Package,
    PackageBranchAvailability,
    PackageEntitlementDefinition,
    PackagePrice,
    PackageVersion,
    Program,
    ProgramBranchAvailability,
    ProgramCategory,
    TenantUser,
    UserProfile,
)
from apps.tenant_core.models_commerce import Order, OrderItem
from apps.tenant_core.models_discounts import DiscountCampaign, DiscountCode
from apps.tenant_core.services_assessment import AssessmentEngineService
from apps.tenant_core.services_booking_policy import BookingPolicyService
from apps.tenant_core.services_bookings import BookingWaitlistAttendanceService
from apps.tenant_core.services_catalog import PackageCatalogService
from apps.tenant_core.services_memberships import MembershipLifecycleService
from apps.tenant_core.services_pricing import UnifiedPricingService
from apps.tenant_core.views_catalog import PackageVersionViewSet, PackageViewSet
from apps.tenant_core.views_invoice import MobileMembershipHistoryView
from apps.tenant_core.views_mobile import (
    MobileBookClassView,
    MobileCancelBookingView,
    MobileCancelPTAppointmentView,
    MobileCheckoutOrderView,
    MobileCheckoutVerifyView,
    MobileMeView,
    MobileMembershipActionsView,
    MobileMembershipFreezeView,
    MobileMembershipRenewView,
    MobileMembershipUpgradeView,
    MobileMyBookingsView,
    MobileMyCreditsView,
    MobilePTAppointmentsView,
    MobilePackagesView,
    MobileRescheduleBookingView,
    MobileScheduleView,
)

DB = "tenant_test"


class EliteCreditMembershipUnifiedTests(TestCase):
    databases = {"default", "tenant_test"}

    def setUp(self):
        set_tenant_db_alias(DB)
        self.factory = APIRequestFactory()
        self.today = timezone.now().date()

        self.tenant = Tenant.objects.using("default").create(
            code=f"ELT-{uuid.uuid4().hex[:6]}",
            name="Sweat Elite Test",
            slug="sweat",
            status="ACTIVE",
        )
        TenantDataSource.objects.using("default").create(
            tenant=self.tenant,
            db_name="test_fitness_tenant",
            database_name="test_fitness_tenant",
            status="ACTIVE",
        )

        self.org = Organization.objects.using(DB).create(
            name="SWEAT Studio",
            code=f"ORG-{uuid.uuid4().hex[:6]}",
            status="ACTIVE",
        )
        self.org_settings = OrganizationSettings.objects.using(DB).create(
            organization=self.org,
            allow_member_freeze=True,
            allow_guest_passes=True,
            max_freeze_days_per_year=60,
            tax_rate_pct=Decimal("18.00"),
            membership_config={},
        )
        self.loc = Location.objects.using(DB).create(
            organization=self.org,
            name="Mumbai Studio Hub",
            code=f"LOC-{uuid.uuid4().hex[:5]}",
            status="ACTIVE",
        )
        self.branch_alpha = Branch.objects.using(DB).create(
            organization=self.org,
            location=self.loc,
            name="Bandra Flagship",
            code=f"BR-A-{uuid.uuid4().hex[:4]}",
            timezone="Asia/Kolkata",
            status="ACTIVE",
        )
        self.branch_beta = Branch.objects.using(DB).create(
            organization=self.org,
            location=self.loc,
            name="Juhu Boutique",
            code=f"BR-B-{uuid.uuid4().hex[:4]}",
            timezone="Asia/Kolkata",
            status="ACTIVE",
        )

        self.admin_user = TenantUser.objects.using(DB).create(
            organization=self.org,
            email=f"admin.{uuid.uuid4().hex[:6]}@sweat.test",
            first_name="Studio",
            last_name="Admin",
            home_branch=self.branch_alpha,
            status="ACTIVE",
        )

        # Booking policy with generous windows for test scheduling
        self.policy = BookingPolicyConfig.objects.using(DB).create(
            organization=self.org,
            branch=None,
            cancellation_cutoff_hours=2,
            rescheduling_cutoff_hours=2,
            max_reschedules_per_booking=3,
            reschedule_credit_diff_mode="ADJUST_DIFFERENCE",
            late_cancellation_allowed=True,
            late_cancellation_session_action="CONSUME",
            max_upcoming_bookings=10,
            advance_booking_days_max=30,
        )

        # Program categories with distinct credit costs (0.5 Yoga, 1.0 Pilates, 3.0 Bootcamp Premium)
        self.prog_cat_yoga = ProgramCategory.objects.using(DB).create(
            organization=self.org,
            name="Mind & Mobility",
            code=f"YOG-{uuid.uuid4().hex[:4]}",
            credits=Decimal("0.50"),
            status="ACTIVE",
        )
        self.prog_cat_pilates = ProgramCategory.objects.using(DB).create(
            organization=self.org,
            name="Reformer Pilates",
            code=f"PIL-{uuid.uuid4().hex[:4]}",
            credits=Decimal("1.00"),
            status="ACTIVE",
        )
        self.prog_cat_premium = ProgramCategory.objects.using(DB).create(
            organization=self.org,
            name="Signature Bootcamp",
            code=f"BTC-{uuid.uuid4().hex[:4]}",
            credits=Decimal("3.00"),
            status="ACTIVE",
        )

        self.prog_yoga = Program.objects.using(DB).create(
            organization=self.org,
            category=self.prog_cat_yoga,
            name="Yoga Flow",
            code=f"P-YOG-{uuid.uuid4().hex[:4]}",
            status="ACTIVE",
        )
        self.prog_pilates = Program.objects.using(DB).create(
            organization=self.org,
            category=self.prog_cat_pilates,
            name="Pilates Core",
            code=f"P-PIL-{uuid.uuid4().hex[:4]}",
            status="ACTIVE",
        )
        self.prog_premium = Program.objects.using(DB).create(
            organization=self.org,
            category=self.prog_cat_premium,
            name="Bootcamp HIIT",
            code=f"P-BTC-{uuid.uuid4().hex[:4]}",
            status="ACTIVE",
        )
        self.prog_elite = Program.objects.using(DB).create(
            organization=self.org,
            name="SWEAT Elite All-Access",
            code=f"P-ELT-{uuid.uuid4().hex[:4]}",
            status="ACTIVE",
        )

        self.class_cat_yoga = ClassCategory.objects.using(DB).create(
            organization=self.org,
            name="Yoga Flow",
            code="YOGA",
            status="ACTIVE",
        )
        self.class_cat_pilates = ClassCategory.objects.using(DB).create(
            organization=self.org,
            name="Pilates",
            code="PILATES",
            status="ACTIVE",
        )
        self.class_cat_premium = ClassCategory.objects.using(DB).create(
            organization=self.org,
            name="Bootcamp",
            code="BOOTCAMP",
            status="ACTIVE",
        )

        self.tpl_yoga = ClassTemplate.objects.using(DB).create(
            organization=self.org,
            program=self.prog_yoga,
            category=self.class_cat_yoga,
            name="Stretch & Breathe (0.5 Cr)",
            code=f"TPL-Y-{uuid.uuid4().hex[:4]}",
            default_capacity=5,
            default_duration_minutes=50,
            status="ACTIVE",
        )
        self.tpl_pilates = ClassTemplate.objects.using(DB).create(
            organization=self.org,
            program=self.prog_pilates,
            category=self.class_cat_pilates,
            name="Signature Pilates (1 Cr)",
            code=f"TPL-P-{uuid.uuid4().hex[:4]}",
            default_capacity=5,
            default_duration_minutes=50,
            status="ACTIVE",
        )
        self.tpl_premium = ClassTemplate.objects.using(DB).create(
            organization=self.org,
            program=self.prog_premium,
            category=self.class_cat_premium,
            name="Elite HIIT Lab (3 Cr)",
            code=f"TPL-H-{uuid.uuid4().hex[:4]}",
            default_capacity=5,
            default_waitlist_capacity=5,
            allow_waitlist=True,
            default_duration_minutes=50,
            status="ACTIVE",
        )

        # Elite Package & Version
        self.pkg_elite = Package.objects.using(DB).create(
            organization=self.org,
            program=self.prog_elite,
            name="Elite 10 Credits",
            code=f"ELT10-{uuid.uuid4().hex[:4]}",
            status="ACTIVE",
        )
        self.ver_elite = PackageVersion.objects.using(DB).create(
            package=self.pkg_elite,
            version_number=1,
            name_snapshot="Elite 10 Credits v1",
            duration_value=30,
            duration_unit="DAYS",
            validity_days=30,
            total_days=30,
            effective_from=timezone.now() - timedelta(days=1),
            created_by_user=self.admin_user,
            status="ACTIVE",
            show_on_app=True,
            perks_config={},
        )
        self.price_elite = PackagePrice.objects.using(DB).create(
            package_version=self.ver_elite,
            base_price=Decimal("8000.00"),
            tax_percent=Decimal("18.00"),
            prices_include_tax=False,
            currency="INR",
            effective_from=timezone.now() - timedelta(days=1),
            created_by_user=self.admin_user,
            status="ACTIVE",
        )
        PackageEntitlementDefinition.objects.using(DB).create(
            package_version=self.ver_elite,
            entitlement_type="CREDIT_WALLET",
            allocated_units=Decimal("10.00"),
            validity_days=30,
            status="ACTIVE",
        )

    def _create_member(self, prefix: str = "elite"):
        user = TenantUser.objects.using(DB).create(
            organization=self.org,
            email=f"{prefix}.{uuid.uuid4().hex[:6]}@sweat.test",
            phone=f"+9198{uuid.uuid4().hex[:8].replace('a','1').replace('b','2').replace('c','3').replace('d','4').replace('e','5').replace('f','6')}",
            first_name="Elite",
            last_name="Member",
            home_branch=self.branch_alpha,
            user_type="MEMBER",
            status="ACTIVE",
        )
        user._state.db = DB
        profile = UserProfile.objects.using(DB).create(
            user=user,
            preferred_branch=self.branch_alpha,
            member_status="ACTIVE",
            member_type="MEMBER",
            member_number=f"SW-{uuid.uuid4().hex[:6].upper()}",
        )
        return user, profile

    def _grant_elite_membership(
        self,
        profile: UserProfile,
        credits: Decimal = Decimal("10.00"),
        start_offset_days: int = -1,
        end_offset_days: int = 30,
        perks: dict = None,
    ):
        if perks is not None:
            self.ver_elite.perks_config = perks
            self.ver_elite.save(using=DB, update_fields=["perks_config"])
        mem = Membership.objects.using(DB).create(
            user_profile=profile,
            home_branch=self.branch_alpha,
            purchase_branch=self.branch_alpha,
            program=self.prog_elite,
            package=self.pkg_elite,
            package_version=self.ver_elite,
            package_price=self.price_elite,
            membership_number=f"MEM-{uuid.uuid4().hex[:6].upper()}",
            start_date=self.today + timedelta(days=start_offset_days),
            end_date=self.today + timedelta(days=end_offset_days),
            status="ACTIVE",
            parq_status="COMPLETED",
        )
        ent = MembershipEntitlement.objects.using(DB).create(
            membership=mem,
            entitlement_type="CREDIT_WALLET",
            allocated_units=credits,
            consumed_units=Decimal("0.00"),
            valid_from=timezone.now() + timedelta(days=start_offset_days),
            valid_until=timezone.now() + timedelta(days=end_offset_days),
            status="ACTIVE",
        )
        return mem, ent

    def _create_occ(self, template: ClassTemplate, branch: Branch = None, days_ahead: int = 1, hours_offset: int = 10, capacity: int = 5):
        target_branch = branch or self.branch_alpha
        occ_date = self.today + timedelta(days=days_ahead)
        start_at = timezone.now() + timedelta(days=days_ahead, hours=hours_offset)
        end_at = start_at + timedelta(minutes=50)
        return ClassOccurrence.objects.using(DB).create(
            branch=target_branch,
            class_template=template,
            occurrence_date=occ_date,
            start_at=start_at,
            end_at=end_at,
            capacity=capacity,
            waitlist_capacity=5,
            status="OPEN",
        )

    def _prepare_request(self, req, user):
        req.user = user
        user._state.db = DB
        req.META["HTTP_X_TENANT_SLUG"] = "sweat"
        req.META["HTTP_X_TENANT_ID"] = self.tenant.code
        req.tenant = self.tenant
        force_authenticate(req, user=user)
        return req

    # -------------------------------------------------------------------------
    # 1. Fractional & Variable Credits + Waitlist + No-Show + Decimal Responses
    # -------------------------------------------------------------------------
    def test_01_fractional_variable_credits_waitlist_noshow_and_decimals(self):
        u1, p1 = self._create_member("u1")
        u2, p2 = self._create_member("u2")
        _, ent1 = self._grant_elite_membership(p1, credits=Decimal("10.00"))
        _, ent2 = self._grant_elite_membership(p2, credits=Decimal("10.00"))

        occ_yoga = self._create_occ(self.tpl_yoga, days_ahead=1, hours_offset=2, capacity=5)
        occ_full_premium = self._create_occ(self.tpl_premium, days_ahead=1, hours_offset=6, capacity=1)

        # Book 0.5 credit class via MobileBookClassView
        req_b = self._prepare_request(
            self.factory.post(f"/api/v1/mobile/schedule/{occ_yoga.id}/book/", {"occurrence_id": str(occ_yoga.id)}, format="json"),
            u1,
        )
        res_b = MobileBookClassView.as_view()(req_b, occurrence_id=str(occ_yoga.id))
        self.assertEqual(res_b.status_code, status.HTTP_201_CREATED)
        self.assertTrue(res_b.data["is_credit_based"])
        self.assertEqual(res_b.data["credits_deducted"], 0.5)
        self.assertIn("0.5 credits deducted", res_b.data["message"])

        ent1.refresh_from_db()
        self.assertEqual(ent1.remaining_units, Decimal("9.50"))

        # Check fractional decimals are preserved in /credits/, /me/, /memberships/actions/, and history
        res_cred = MobileMyCreditsView.as_view()(self._prepare_request(self.factory.get("/api/v1/mobile/credits/"), u1))
        self.assertEqual(res_cred.data["total_credits_available"], 9.5)
        self.assertTrue(res_cred.data["packs"][0]["is_credit_based"])

        res_me = MobileMeView.as_view()(self._prepare_request(self.factory.get("/api/v1/mobile/me/"), u1))
        self.assertEqual(res_me.data["credits"], 9.5)
        self.assertTrue(res_me.data["active_membership"]["is_credit_based"])

        res_act = MobileMembershipActionsView.as_view()(self._prepare_request(self.factory.get("/api/v1/mobile/memberships/actions/"), u1))
        self.assertEqual(res_act.data["active_membership"]["remaining_sessions"], 9.5)
        self.assertTrue(res_act.data["active_membership"]["is_credit_based"])

        res_hist = MobileMembershipHistoryView.as_view()(self._prepare_request(self.factory.get("/api/v1/mobile/memberships/history/"), u1))
        self.assertEqual(res_hist.data["history"][0]["sessions_remaining"], 9.5)

        # No-Show reversal (with restore rule configured) must restore exact 0.50 credits (not 1.00)
        att_pol = AttendancePolicySet.objects.using(DB).create(
            organization=self.org, name="Default Attendance", status="ACTIVE", created_by_user=self.admin_user
        )
        AttendancePenaltyRule.objects.using(DB).create(
            attendance_policy_set=att_pol, rule_code="FIRST_NO_SHOW", restore_booked_session=True, status="ACTIVE"
        )
        b_yoga = Booking.objects.using(DB).get(id=res_b.data["booking_id"])
        BookingWaitlistAttendanceService.record_attendance(
            booking=b_yoga, status="NO_SHOW", marked_by_user=self.admin_user, db_alias=DB
        )
        ent1.refresh_from_db()
        self.assertEqual(ent1.remaining_units, Decimal("10.00"))

        # Waitlist auto-promotion on 3.00-credit class deducts exact 3.00 credits from promoted member
        b_p1 = BookingWaitlistAttendanceService.create_booking(
            user_profile=p1, occurrence=occ_full_premium, created_by_user=u1, db_alias=DB
        )
        b_p2_wait = BookingWaitlistAttendanceService.create_booking(
            user_profile=p2, occurrence=occ_full_premium, created_by_user=u2, db_alias=DB
        )
        self.assertEqual(b_p2_wait.status, "WAITLISTED")
        ent1.refresh_from_db()
        self.assertEqual(ent1.remaining_units, Decimal("7.00"))

        # u1 cancels -> exact 3.00 restored to u1 AND exact 3.00 deducted from promoted u2
        res_c = MobileCancelBookingView.as_view()(
            self._prepare_request(self.factory.post(f"/api/v1/mobile/bookings/{b_p1.id}/cancel/", {}, format="json"), u1),
            booking_id=str(b_p1.id),
        )
        self.assertEqual(res_c.status_code, status.HTTP_200_OK)
        self.assertEqual(res_c.data["credits_refunded"], 3.0)
        self.assertIn("3 credits refunded", res_c.data["detail"])

        ent1.refresh_from_db()
        ent2.refresh_from_db()
        b_p2_wait.refresh_from_db()
        self.assertEqual(ent1.remaining_units, Decimal("10.00"))
        self.assertEqual(b_p2_wait.status, "CONFIRMED")
        self.assertEqual(ent2.remaining_units, Decimal("7.00"))

    # -------------------------------------------------------------------------
    # 2. Configurable Branch Availability (CATALOG_BRANCHES / ALL_BRANCHES / HOME_ONLY)
    # -------------------------------------------------------------------------
    def test_02_configurable_branch_access_for_elite(self):
        u, p = self._create_member("branch")
        mem, _ = self._grant_elite_membership(p, perks={"cross_branch_mode": "CATALOG_BRANCHES"})

        PackageBranchAvailability.objects.using(DB).create(package=self.pkg_elite, branch=self.branch_alpha, status="ENABLED")
        PackageBranchAvailability.objects.using(DB).create(package=self.pkg_elite, branch=self.branch_beta, status="DISABLED")

        occ_alpha = self._create_occ(self.tpl_pilates, branch=self.branch_alpha, days_ahead=1, hours_offset=3)
        occ_beta = self._create_occ(self.tpl_pilates, branch=self.branch_beta, days_ahead=1, hours_offset=5)

        # CATALOG_BRANCHES: alpha allowed, beta blocked
        res_sched = MobileScheduleView.as_view()(
            self._prepare_request(self.factory.get(f"/api/v1/mobile/schedule/?date={occ_beta.occurrence_date.isoformat()}&branch_id={self.branch_beta.id}"), u)
        )
        beta_slot = next(s for s in res_sched.data["slots"] if s["id"] == str(occ_beta.id))
        self.assertFalse(beta_slot["is_included_in_plan"])

        res_beta_fail = MobileBookClassView.as_view()(
            self._prepare_request(self.factory.post(f"/api/v1/mobile/schedule/{occ_beta.id}/book/", {}, format="json"), u),
            occurrence_id=str(occ_beta.id),
        )
        self.assertEqual(res_beta_fail.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_beta_fail.data["code"], "BRANCH_NOT_PERMITTED")

        # ALL_BRANCHES: dynamically unlocks branch_beta immediately without changing PackageBranchAvailability rows
        self.ver_elite.perks_config = {"cross_branch_mode": "ALL_BRANCHES"}
        self.ver_elite.save(using=DB, update_fields=["perks_config"])

        res_beta_ok = MobileBookClassView.as_view()(
            self._prepare_request(self.factory.post(f"/api/v1/mobile/schedule/{occ_beta.id}/book/", {}, format="json"), u),
            occurrence_id=str(occ_beta.id),
        )
        self.assertEqual(res_beta_ok.status_code, status.HTTP_201_CREATED)

        # HOME_ONLY: blocks branch_beta while permitting branch_alpha
        self.ver_elite.perks_config = {"cross_branch_mode": "HOME_ONLY"}
        self.ver_elite.save(using=DB, update_fields=["perks_config"])
        Booking.objects.using(DB).filter(id=res_beta_ok.data["booking_id"]).update(status="CANCELLED")

        ok_alpha, _ = BookingWaitlistAttendanceService.is_membership_branch_permitted(mem, occ_alpha, db_alias=DB, is_credit_based=True)
        ok_beta, _ = BookingWaitlistAttendanceService.is_membership_branch_permitted(mem, occ_beta, db_alias=DB, is_credit_based=True)
        self.assertTrue(ok_alpha)
        self.assertFalse(ok_beta)

    # -------------------------------------------------------------------------
    # 3. Multi-Pack Resolution Priority Preserves Elite Credits
    # -------------------------------------------------------------------------
    def test_03_multi_pack_priority_preserves_elite_credits(self):
        u, p = self._create_member("multipack")
        _, ent_elite = self._grant_elite_membership(p, credits=Decimal("50.00"))

        pkg_pilates = Package.objects.using(DB).create(organization=self.org, program=self.prog_pilates, name="Pilates 5", code=f"P5-{uuid.uuid4().hex[:4]}", status="ACTIVE")
        ver_pilates = PackageVersion.objects.using(DB).create(package=pkg_pilates, version_number=1, name_snapshot="Pilates 5 v1", duration_value=30, duration_unit="DAYS", effective_from=timezone.now(), created_by_user=self.admin_user, status="ACTIVE")
        mem_pilates = Membership.objects.using(DB).create(
            user_profile=p, home_branch=self.branch_alpha, purchase_branch=self.branch_alpha,
            program=self.prog_pilates, package=pkg_pilates, package_version=ver_pilates,
            membership_number=f"PIL-{uuid.uuid4().hex[:6]}", start_date=self.today, end_date=self.today + timedelta(days=30),
            status="ACTIVE", parq_status="COMPLETED",
        )
        ent_pilates = MembershipEntitlement.objects.using(DB).create(
            membership=mem_pilates,
            entitlement_type="HOME_BRANCH_SESSION",
            allocated_units=Decimal("5.00"),
            consumed_units=Decimal("0.00"),
            valid_from=timezone.now(),
            valid_until=timezone.now() + timedelta(days=30),
            status="ACTIVE",
        )

        occ_pilates = self._create_occ(self.tpl_pilates, days_ahead=1, hours_offset=2)
        occ_bootcamp = self._create_occ(self.tpl_premium, days_ahead=1, hours_offset=6)

        res1 = MobileBookClassView.as_view()(
            self._prepare_request(self.factory.post(f"/api/v1/mobile/schedule/{occ_pilates.id}/book/", {}, format="json"), u),
            occurrence_id=str(occ_pilates.id),
        )
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)
        self.assertFalse(res1.data["is_credit_based"])

        ent_pilates.refresh_from_db()
        ent_elite.refresh_from_db()
        self.assertEqual(ent_pilates.remaining_units, Decimal("4.00"))
        self.assertEqual(ent_elite.remaining_units, Decimal("50.00"))

        res2 = MobileBookClassView.as_view()(
            self._prepare_request(self.factory.post(f"/api/v1/mobile/schedule/{occ_bootcamp.id}/book/", {}, format="json"), u),
            occurrence_id=str(occ_bootcamp.id),
        )
        self.assertEqual(res2.status_code, status.HTTP_201_CREATED)
        self.assertTrue(res2.data["is_credit_based"])
        ent_elite.refresh_from_db()
        self.assertEqual(ent_elite.remaining_units, Decimal("47.00"))

    # -------------------------------------------------------------------------
    # 4. Configurable Reschedule Credit Difference Modes
    # -------------------------------------------------------------------------
    def test_04_configurable_reschedule_credit_diff_modes(self):
        u, p = self._create_member("resched")
        _, ent = self._grant_elite_membership(p, credits=Decimal("5.00"))

        occ_1cr = self._create_occ(self.tpl_pilates, days_ahead=2, hours_offset=2)
        occ_3cr_a = self._create_occ(self.tpl_premium, days_ahead=2, hours_offset=6)
        occ_3cr_b = self._create_occ(self.tpl_premium, days_ahead=3, hours_offset=6)

        b1 = BookingWaitlistAttendanceService.create_booking(user_profile=p, occurrence=occ_1cr, created_by_user=u, db_alias=DB)
        ent.refresh_from_db()
        self.assertEqual(ent.remaining_units, Decimal("4.00"))

        # ADJUST_DIFFERENCE: moving 1 -> 3 charges +2 (balance 2.00)
        res_adj_up = MobileRescheduleBookingView.as_view()(
            self._prepare_request(self.factory.post(f"/api/v1/mobile/bookings/{b1.id}/reschedule/", {"to_occurrence_id": str(occ_3cr_a.id)}, format="json"), u),
            booking_id=str(b1.id),
        )
        self.assertEqual(res_adj_up.status_code, status.HTTP_200_OK)
        ent.refresh_from_db()
        self.assertEqual(ent.remaining_units, Decimal("2.00"))

        # Moving 3 -> 1 refunds +2 (balance 4.00)
        b2_id = res_adj_up.data["booking_id"]
        res_adj_down = MobileRescheduleBookingView.as_view()(
            self._prepare_request(self.factory.post(f"/api/v1/mobile/bookings/{b2_id}/reschedule/", {"to_occurrence_id": str(occ_1cr.id)}, format="json"), u),
            booking_id=b2_id,
        )
        self.assertEqual(res_adj_down.status_code, status.HTTP_200_OK)
        ent.refresh_from_db()
        self.assertEqual(ent.remaining_units, Decimal("4.00"))

        # BLOCK_MISMATCH: reject 1 -> 3
        self.policy.reschedule_credit_diff_mode = "BLOCK_MISMATCH"
        self.policy.save(using=DB, update_fields=["reschedule_credit_diff_mode"])
        b3_id = res_adj_down.data["booking_id"]
        res_block = MobileRescheduleBookingView.as_view()(
            self._prepare_request(self.factory.post(f"/api/v1/mobile/bookings/{b3_id}/reschedule/", {"to_occurrence_id": str(occ_3cr_a.id)}, format="json"), u),
            booking_id=b3_id,
        )
        self.assertEqual(res_block.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_block.data["code"], "RESCHEDULE_CREDIT_MISMATCH")

        # KEEP_ORIGINAL: allow 1 -> 3 without changing balance (remains 4.00)
        self.policy.reschedule_credit_diff_mode = "KEEP_ORIGINAL"
        self.policy.save(using=DB, update_fields=["reschedule_credit_diff_mode"])
        res_keep = MobileRescheduleBookingView.as_view()(
            self._prepare_request(self.factory.post(f"/api/v1/mobile/bookings/{b3_id}/reschedule/", {"to_occurrence_id": str(occ_3cr_b.id)}, format="json"), u),
            booking_id=b3_id,
        )
        self.assertEqual(res_keep.status_code, status.HTTP_200_OK)
        ent.refresh_from_db()
        self.assertEqual(ent.remaining_units, Decimal("4.00"))

    # -------------------------------------------------------------------------
    # 5. Future Renewals Do Not Double-Count & PAR-Q Stays 'COMPLETED'
    # -------------------------------------------------------------------------
    @patch("apps.tenant_core.services_razorpay.RazorpayService.verify_payment_signature", return_value=True)
    @patch("apps.tenant_core.services_razorpay.RazorpayService.is_configured", return_value=False)
    def test_05_future_renewal_no_double_counting_and_parq_completed(self, _mock_cfg, _mock_sig):
        u, p = self._create_member("renew")
        self._grant_elite_membership(p, credits=Decimal("10.00"), start_offset_days=-2, end_offset_days=20)
        self._grant_elite_membership(p, credits=Decimal("50.00"), start_offset_days=21, end_offset_days=51)

        res_cred = MobileMyCreditsView.as_view()(self._prepare_request(self.factory.get("/api/v1/mobile/credits/"), u))
        self.assertEqual(res_cred.data["total_credits_available"], 10)
        statuses = {pk["status"] for pk in res_cred.data["packs"]}
        self.assertEqual(statuses, {"ACTIVE", "UPCOMING"})

        # Verify checkout renewal sets COMPLETED (not CLEARED)
        parq_form = IntakeForm.objects.using(DB).create(organization=self.org, name="PAR-Q", form_type="PAR_Q", status="ACTIVE", version_number=1)
        parq_sub = IntakeSubmission.objects.using(DB).create(intake_form=parq_form, user_profile=p, submitted_by_user=u, agreement_accepted=True, status="COMPLETED")
        ord_ren = Order.objects.using(DB).create(
            branch=self.branch_alpha, user_profile=p, order_number=f"REN-{uuid.uuid4().hex[:6]}",
            order_type="RENEWAL", status="PENDING_PAYMENT", subtotal=Decimal("8000.00"), tax_amount=Decimal("1440.00"), total_amount=Decimal("9440.00"),
            notes=json.dumps({
                "action_type": "RENEWAL", "branch_id": str(self.branch_alpha.id),
                "package_id": str(self.pkg_elite.id), "package_version_id": str(self.ver_elite.id),
                "parq_submission_id": str(parq_sub.id),
            }),
        )
        OrderItem.objects.using(DB).create(order=ord_ren, item_type="PACKAGE", package=self.pkg_elite, package_version=self.ver_elite, package_price=self.price_elite, item_name_snapshot="Elite", quantity=1, unit_price_snapshot=Decimal("8000.00"), tax_amount=Decimal("1440.00"), total_amount=Decimal("9440.00"))
        sig = hmac.new(b"test", f"ord_test|pay_test".encode(), hashlib.sha256).hexdigest()
        res_v = MobileCheckoutVerifyView.as_view()(
            self._prepare_request(self.factory.post("/api/v1/mobile/checkout/verify/", {
                "order_id": str(ord_ren.id), "razorpay_order_id": "ord_test", "razorpay_payment_id": "pay_test", "razorpay_signature": sig
            }, format="json"), u)
        )
        self.assertEqual(res_v.status_code, status.HTTP_200_OK)
        parq_sub.refresh_from_db()
        self.assertEqual(parq_sub.membership.parq_status, "COMPLETED")

    # -------------------------------------------------------------------------
    # 6. Upgrade Into Elite Expires Stale Session Entitlements + Zero-Diff Upgrade
    # -------------------------------------------------------------------------
    def test_06_upgrade_expires_stale_sessions_and_zero_diff_executes(self):
        u, p = self._create_member("upg")
        pkg_sess = Package.objects.using(DB).create(organization=self.org, program=self.prog_pilates, name="Standard 12", code=f"STD12-{uuid.uuid4().hex[:4]}", status="ACTIVE")
        ver_sess = PackageVersion.objects.using(DB).create(package=pkg_sess, version_number=1, name_snapshot="Std 12 v1", duration_value=30, duration_unit="DAYS", validity_days=30, effective_from=timezone.now(), created_by_user=self.admin_user, status="ACTIVE")
        price_sess = PackagePrice.objects.using(DB).create(package_version=ver_sess, base_price=Decimal("8000.00"), tax_percent=Decimal("18.00"), prices_include_tax=False, status="ACTIVE", effective_from=timezone.now(), created_by_user=self.admin_user)

        mem = Membership.objects.using(DB).create(
            user_profile=p, home_branch=self.branch_alpha, purchase_branch=self.branch_alpha,
            program=self.prog_pilates, package=pkg_sess, package_version=ver_sess, package_price=price_sess,
            membership_number=f"STD-{uuid.uuid4().hex[:6]}", start_date=self.today - timedelta(days=5), end_date=self.today + timedelta(days=25), status="ACTIVE",
        )
        old_ent = MembershipEntitlement.objects.using(DB).create(
            membership=mem,
            entitlement_type="HOME_BRANCH_SESSION",
            allocated_units=Decimal("12.00"),
            consumed_units=Decimal("2.00"),
            valid_from=timezone.now() - timedelta(days=5),
            valid_until=timezone.now() + timedelta(days=25),
            status="ACTIVE",
        )

        # Lateral ₹0 difference upgrade via MobileMembershipUpgradeView (same 8000+GST price) must execute immediately!
        res_upg = MobileMembershipUpgradeView.as_view()(
            self._prepare_request(self.factory.post("/api/v1/mobile/memberships/upgrade/", {
                "target_package_id": str(self.pkg_elite.id), "target_branch_id": str(self.branch_alpha.id), "parq_decision": "REUSE"
            }, format="json"), u)
        )
        self.assertEqual(res_upg.status_code, status.HTTP_200_OK)
        self.assertFalse(res_upg.data["requires_payment"])
        self.assertEqual(res_upg.data["amount"], 0.0)

        old_ent.refresh_from_db()
        self.assertEqual(old_ent.status, "EXPIRED")
        self.assertTrue(
            MembershipEntitlementLedger.objects.using(DB).filter(membership_entitlement=old_ent, transaction_type="EXPIRY").exists()
        )
        new_credit_ent = mem.entitlements.using(DB).filter(entitlement_type="CREDIT_WALLET", status="ACTIVE").first()
        self.assertIsNotNone(new_credit_ent)
        self.assertEqual(new_credit_ent.allocated_units, Decimal("10.00"))

    # -------------------------------------------------------------------------
    # 7. Unified Checkout Tax Order, Trial Filtering & Inclusive Price Packages
    # -------------------------------------------------------------------------
    @patch("apps.tenant_core.services_razorpay.RazorpayService.is_configured", return_value=False)
    def test_07_packages_filtering_inclusive_tax_and_unified_checkout(self, _mock_cfg):
        u, p = self._create_member("checkout")
        # Create hidden/trial package that MUST be excluded from MobilePackagesView, plus tax-inclusive Elite
        pkg_trial = Package.objects.using(DB).create(organization=self.org, program=self.prog_pilates, name="Free Trial", code=f"TRL-{uuid.uuid4().hex[:4]}", status="ACTIVE")
        ver_trial = PackageVersion.objects.using(DB).create(package=pkg_trial, version_number=1, name_snapshot="Trial", duration_value=7, duration_unit="DAYS", is_trial=True, show_on_app=True, effective_from=timezone.now(), created_by_user=self.admin_user, status="ACTIVE")
        PackagePrice.objects.using(DB).create(package_version=ver_trial, base_price=Decimal("0.00"), effective_from=timezone.now(), created_by_user=self.admin_user, status="ACTIVE")

        self.price_elite.prices_include_tax = True
        self.price_elite.save(using=DB, update_fields=["prices_include_tax"])

        res_pkgs = MobilePackagesView.as_view()(self._prepare_request(self.factory.get("/api/v1/mobile/packages/"), u))
        self.assertEqual(res_pkgs.status_code, status.HTTP_200_OK)
        names = [item["name"] for item in res_pkgs.data]
        self.assertNotIn("Free Trial", names)
        elite_item = next(item for item in res_pkgs.data if item["id"] == str(self.pkg_elite.id))
        self.assertTrue(elite_item["is_credit_based"])
        self.assertEqual(elite_item["credits"], 10.0)
        self.assertEqual(elite_item["final_price"], 8000.0)

        # Create 20% discount coupon & verify checkout matches UnifiedPricingService (no 499 fee)
        camp = DiscountCampaign.objects.using(DB).create(
            organization=self.org, name="20 Off", discount_type="PERCENTAGE", discount_value=Decimal("20.00"),
            valid_from=timezone.now() - timedelta(days=1), valid_until=timezone.now() + timedelta(days=10), status="ACTIVE",
            configuration={"audience": "ALL", "channels": ["MOBILE_APP", "WEB"], "applicable_order_types": ["NEW_MEMBERSHIP", "NEW_PACKAGE", "REJOIN"]},
        )
        DiscountCode.objects.using(DB).create(campaign=camp, code="SAVE20", status="ACTIVE")

        expected_quote = UnifiedPricingService.calculate_quote(
            order_type="NEW_MEMBERSHIP", package_id=str(self.pkg_elite.id), package_version_id=str(self.ver_elite.id),
            branch_id=str(self.branch_alpha.id), user_profile_id=str(p.id), coupon_code="SAVE20", db_alias=DB,
        )
        res_ord = MobileCheckoutOrderView.as_view()(
            self._prepare_request(self.factory.post("/api/v1/mobile/checkout/create-order/", {"package_id": str(self.pkg_elite.id), "coupon_code": "SAVE20"}, format="json"), u)
        )
        self.assertEqual(res_ord.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res_ord.data["activation_charge"], 0.0)
        self.assertEqual(res_ord.data["amount"], expected_quote["pricing"]["final_payable"])

    # -------------------------------------------------------------------------
    # 8. Dynamic Configurable Perks (Assessments, Guest Passes, PT Credits, Adv Window)
    # -------------------------------------------------------------------------
    def test_08_dynamic_perks_assessments_guests_pt_and_advance_window(self):
        u, p = self._create_member("perks")
        mem, ent = self._grant_elite_membership(p, credits=Decimal("10.00"), perks={})

        # Off by default -> Assessments, Guest Pass, PT all deny cleanly
        self.assertFalse(AssessmentEngineService.check_member_eligibility(p, db_alias=DB)["eligible"])
        occ_today = self._create_occ(self.tpl_pilates, days_ahead=1, hours_offset=2, capacity=5)
        res_g_denied = MobileBookClassView.as_view()(
            self._prepare_request(self.factory.post("/api/v1/mobile/bookings/book-guest/", {"occurrence_id": str(occ_today.id), "is_guest": True, "guest_name": "Rohan"}, format="json"), u)
        )
        self.assertEqual(res_g_denied.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_g_denied.data["code"], "GUEST_PASS_NOT_ENABLED")

        # Turn all perks on dynamically via PackageVersion.perks_config
        self.ver_elite.perks_config = {
            "include_assessments": True,
            "assessment_credits": 2,
            "guest_passes_per_month": 1,
            "pt_enabled": True,
            "pt_credits_per_session": 2.5,
            "advance_booking_days_override": 2,
            "cross_branch_mode": "ALL_BRANCHES",
        }
        self.ver_elite.save(using=DB, update_fields=["perks_config"])

        # Assessments unlocked!
        assess_elig = AssessmentEngineService.check_member_eligibility(p, db_alias=DB)
        self.assertTrue(assess_elig["eligible"])

        # Advance window override: 5 days ahead blocked (limit 2), 1 day ahead allowed
        occ_far = self._create_occ(self.tpl_pilates, days_ahead=5, hours_offset=2)
        with self.assertRaises(ValidationError) as ctx:
            BookingWaitlistAttendanceService.create_booking(user_profile=p, occurrence=occ_far, created_by_user=u, db_alias=DB)
        self.assertEqual(getattr(ctx.exception, "code", None), "ADVANCE_WINDOW_EXCEEDED")

        # Guest pass booking (1/month): 1st succeeds without deducting credits, 2nd fails limit, cancelling restores!
        res_g1 = MobileBookClassView.as_view()(
            self._prepare_request(self.factory.post("/api/v1/mobile/bookings/book-guest/", {"occurrence_id": str(occ_today.id), "is_guest": True, "guest_name": "Rohan Guest", "guest_phone": "+919900001111"}, format="json"), u)
        )
        self.assertEqual(res_g1.status_code, status.HTTP_201_CREATED)
        self.assertTrue(res_g1.data["is_guest_pass"])
        ent.refresh_from_db()
        self.assertEqual(ent.remaining_units, Decimal("10.00"))

        res_g2 = MobileBookClassView.as_view()(
            self._prepare_request(self.factory.post("/api/v1/mobile/bookings/book-guest/", {"occurrence_id": str(occ_today.id), "is_guest": True, "guest_name": "Second Friend"}, format="json"), u)
        )
        self.assertEqual(res_g2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_g2.data["code"], "GUEST_PASS_LIMIT_REACHED")

        # Cancel guest pass restores allowance
        res_gc = MobileCancelBookingView.as_view()(
            self._prepare_request(self.factory.post(f"/api/v1/mobile/bookings/{res_g1.data['booking_id']}/cancel/", {}, format="json"), u),
            booking_id=res_g1.data["booking_id"],
        )
        self.assertEqual(res_gc.status_code, status.HTTP_200_OK)
        self.assertEqual(BookingWaitlistAttendanceService.get_member_guest_pass_summary(mem, db_alias=DB)["remaining"], 1)

        # PT appointment booking & cancel deducts & restores exact 2.5 credits
        start_pt = (timezone.now() + timedelta(days=1)).isoformat()
        res_pt = MobilePTAppointmentsView.as_view()(
            self._prepare_request(self.factory.post("/api/v1/mobile/pt/appointments/", {"start_at": start_pt, "duration_minutes": 60}, format="json"), u)
        )
        self.assertEqual(res_pt.status_code, status.HTTP_201_CREATED)
        ent.refresh_from_db()
        self.assertEqual(ent.remaining_units, Decimal("7.50"))

        res_pt_c = MobileCancelPTAppointmentView.as_view()(
            self._prepare_request(self.factory.post(f"/api/v1/mobile/pt/appointments/{res_pt.data['appointment_id']}/cancel/", {}, format="json"), u),
            appointment_id=res_pt.data["appointment_id"],
        )
        self.assertEqual(res_pt_c.status_code, status.HTTP_200_OK)
        ent.refresh_from_db()
        self.assertEqual(ent.remaining_units, Decimal("10.00"))

    # -------------------------------------------------------------------------
    # 9. Automated Overdue Membership Expiry Task
    # -------------------------------------------------------------------------
    def test_09_automated_overdue_membership_expiry_task(self):
        _, p = self._create_member("expiry")
        mem, ent = self._grant_elite_membership(p, credits=Decimal("25.00"), start_offset_days=-32, end_offset_days=-2)

        res1 = MembershipLifecycleService.expire_overdue_memberships(db_alias=DB, as_of_date=self.today)
        self.assertEqual(res1["expired_memberships"], 1)
        self.assertEqual(res1["expired_entitlements"], 1)

        mem.refresh_from_db()
        ent.refresh_from_db()
        p.refresh_from_db()
        self.assertEqual(mem.status, "EXPIRED")
        self.assertEqual(ent.status, "EXPIRED")
        self.assertEqual(p.member_status, "EXPIRED")
        self.assertTrue(
            MembershipStatusHistory.objects.using(DB).filter(membership=mem, to_status="EXPIRED", reason_code="AUTO_EXPIRY").exists()
        )
        self.assertTrue(
            MembershipEntitlementLedger.objects.using(DB).filter(membership_entitlement=ent, transaction_type="EXPIRY", units=Decimal("-25.00")).exists()
        )

        # Second invocation is strictly idempotent
        res2 = MembershipLifecycleService.expire_overdue_memberships(db_alias=DB, as_of_date=self.today)
        self.assertEqual(res2["expired_memberships"], 0)

    # -------------------------------------------------------------------------
    # 10. H1 Regression: Guest Pass Reschedule & Display Zero-Credit Guarantees
    # -------------------------------------------------------------------------
    def test_10_h1_guest_pass_reschedule_and_display_zero_credit_impact(self):
        u, p = self._create_member("guestresched")
        perks = {
            "cross_branch_mode": "ALL_BRANCHES",
            "guest_passes_per_month": 2,
        }
        mem, ent = self._grant_elite_membership(p, credits=Decimal("10.00"), perks=perks)

        occ_1cr = self._create_occ(self.tpl_pilates, days_ahead=2, hours_offset=2, capacity=5)
        occ_3cr_a = self._create_occ(self.tpl_premium, days_ahead=2, hours_offset=5, capacity=5)
        occ_05cr = self._create_occ(self.tpl_yoga, days_ahead=3, hours_offset=2, capacity=5)
        occ_3cr_b = self._create_occ(self.tpl_premium, days_ahead=3, hours_offset=5, capacity=5)

        # Book member's own seat in 3-credit occ_3cr_a (wallet: 10.00 -> 7.00)
        member_bk = BookingWaitlistAttendanceService.create_booking(
            user_profile=p, occurrence=occ_3cr_a, created_by_user=u, db_alias=DB
        )
        ent.refresh_from_db()
        self.assertEqual(ent.remaining_units, Decimal("7.00"))

        # Book guest pass for Rohan on 1-credit occ_1cr (wallet remains 7.00)
        res_guest = MobileBookClassView.as_view()(
            self._prepare_request(
                self.factory.post(
                    "/api/v1/mobile/bookings/book-guest/",
                    {"occurrence_id": str(occ_1cr.id), "is_guest": True, "guest_name": "Rohan Guest", "guest_phone": "+919911223344"},
                    format="json",
                ),
                u,
            )
        )
        self.assertEqual(res_guest.status_code, status.HTTP_201_CREATED)
        guest_bk_id = res_guest.data["booking_id"]
        guest_bk = Booking.objects.using(DB).get(id=guest_bk_id)
        ent.refresh_from_db()
        self.assertEqual(ent.remaining_units, Decimal("7.00"))
        self.assertEqual(
            BookingWaitlistAttendanceService._get_booking_consumed_units(guest_bk, occ_1cr, db_alias=DB),
            Decimal("0.00"),
        )

        # /mobile/bookings/ must report credits_used=0.0 and is_credit_based=False for guest passes
        res_list = MobileMyBookingsView.as_view()(self._prepare_request(self.factory.get("/api/v1/mobile/bookings/"), u))
        self.assertEqual(res_list.status_code, status.HTTP_200_OK)
        upcoming_by_id = {b["id"]: b for b in res_list.data["upcoming"]}
        self.assertTrue(upcoming_by_id[guest_bk_id]["is_guest_pass"])
        self.assertEqual(upcoming_by_id[guest_bk_id]["guest_name"], "Rohan Guest")
        self.assertFalse(upcoming_by_id[guest_bk_id]["is_credit_based"])
        self.assertEqual(upcoming_by_id[guest_bk_id]["credits_used"], 0.0)
        self.assertTrue(upcoming_by_id[str(member_bk.id)]["is_credit_based"])
        self.assertEqual(upcoming_by_id[str(member_bk.id)]["credits_used"], 3.0)

        # ADJUST_DIFFERENCE: move 1-credit guest pass -> 3-credit occ_3cr_a (where member ALREADY has own seat)
        self.policy.reschedule_credit_diff_mode = "ADJUST_DIFFERENCE"
        self.policy.save(using=DB, update_fields=["reschedule_credit_diff_mode"])
        res_up = MobileRescheduleBookingView.as_view()(
            self._prepare_request(
                self.factory.post(f"/api/v1/mobile/bookings/{guest_bk_id}/reschedule/", {"to_occurrence_id": str(occ_3cr_a.id)}, format="json"),
                u,
            ),
            booking_id=guest_bk_id,
        )
        self.assertEqual(res_up.status_code, status.HTTP_200_OK)
        self.assertTrue(res_up.data["is_guest_pass"])
        self.assertEqual(res_up.data["guest_name"], "Rohan Guest")
        self.assertFalse(res_up.data["is_credit_based"])
        self.assertEqual(res_up.data["required_credits"], 0.0)
        ent.refresh_from_db()
        self.assertEqual(ent.remaining_units, Decimal("7.00"))

        # BLOCK_MISMATCH: moving guest pass from 3-credit -> 0.5-credit occ_05cr must succeed with 0 refund
        self.policy.reschedule_credit_diff_mode = "BLOCK_MISMATCH"
        self.policy.save(using=DB, update_fields=["reschedule_credit_diff_mode"])
        res_down = MobileRescheduleBookingView.as_view()(
            self._prepare_request(
                self.factory.post(f"/api/v1/mobile/bookings/{guest_bk_id}/reschedule/", {"to_occurrence_id": str(occ_05cr.id)}, format="json"),
                u,
            ),
            booking_id=guest_bk_id,
        )
        self.assertEqual(res_down.status_code, status.HTTP_200_OK)
        self.assertEqual(res_down.data["required_credits"], 0.0)
        ent.refresh_from_db()
        self.assertEqual(ent.remaining_units, Decimal("7.00"))

        # Same guest phone cannot be rescheduled onto an occurrence where that guest phone already has a guest booking
        guest2 = BookingWaitlistAttendanceService.create_booking(
            user_profile=p,
            occurrence=occ_1cr,
            booking_type="GUEST",
            membership=mem,
            guest_name="Rohan Copy",
            guest_phone="+919911223344",
            created_by_user=u,
            db_alias=DB,
        )
        res_dup = MobileRescheduleBookingView.as_view()(
            self._prepare_request(
                self.factory.post(f"/api/v1/mobile/bookings/{guest2.id}/reschedule/", {"to_occurrence_id": str(occ_05cr.id)}, format="json"),
                u,
            ),
            booking_id=str(guest2.id),
        )
        self.assertEqual(res_dup.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_dup.data["code"], "DUPLICATE_GUEST_BOOKING")

        # Member's own non-guest booking can reschedule onto occ_05cr even though a guest pass already exists there (under ADJUST_DIFFERENCE)
        self.policy.reschedule_credit_diff_mode = "ADJUST_DIFFERENCE"
        self.policy.save(using=DB, update_fields=["reschedule_credit_diff_mode"])
        res_mem_to_guest_slot = MobileRescheduleBookingView.as_view()(
            self._prepare_request(
                self.factory.post(f"/api/v1/mobile/bookings/{member_bk.id}/reschedule/", {"to_occurrence_id": str(occ_05cr.id)}, format="json"),
                u,
            ),
            booking_id=str(member_bk.id),
        )
        self.assertEqual(res_mem_to_guest_slot.status_code, status.HTTP_200_OK)
        # Moving member's own 3-credit booking -> 0.5-credit slot refunds +2.50 credits (7.00 -> 9.50)
        ent.refresh_from_db()
        self.assertEqual(ent.remaining_units, Decimal("9.50"))

        # Cancel guest pass records 0.00 session_units_applied and restores monthly quota
        canc_guest = BookingWaitlistAttendanceService.cancel_booking(
            booking=Booking.objects.using(DB).get(id=guest_bk_id),
            cancelled_by_user=u,
            db_alias=DB,
        )
        self.assertEqual(canc_guest.session_units_applied, Decimal("0.00"))
        ent.refresh_from_db()
        self.assertEqual(ent.remaining_units, Decimal("9.50"))

        # Unused helper occ_3cr_b branch-check for HOME_ONLY still enforced on guest reschedule
        self.ver_elite.perks_config = {"cross_branch_mode": "HOME_ONLY", "guest_passes_per_month": 2}
        self.ver_elite.save(using=DB, update_fields=["perks_config"])
        occ_beta = self._create_occ(self.tpl_pilates, branch=self.branch_beta, days_ahead=4, hours_offset=2)
        res_beta_block = MobileRescheduleBookingView.as_view()(
            self._prepare_request(
                self.factory.post(f"/api/v1/mobile/bookings/{guest2.id}/reschedule/", {"to_occurrence_id": str(occ_beta.id)}, format="json"),
                u,
            ),
            booking_id=str(guest2.id),
        )
        self.assertEqual(res_beta_block.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_beta_block.data["code"], "BRANCH_NOT_PERMITTED")
        # keep variable referenced cleanly
        self.assertIsNotNone(occ_3cr_b)

    # -------------------------------------------------------------------------
    # 11. H2 Regression: Catalog Reuse & Safe Clone Preserve perks_config
    # -------------------------------------------------------------------------
    def test_11_h2_catalog_reuse_and_clone_modify_preserve_perks_config(self):
        elite_perks = {
            "cross_branch_mode": "ALL_BRANCHES",
            "include_assessments": True,
            "assessment_credits": 3,
            "guest_passes_per_month": 2,
            "pt_enabled": True,
            "pt_credits_per_session": 2.5,
            "advance_booking_days_override": 21,
        }
        pkg = Package.objects.using(DB).create(
            organization=self.org,
            program=self.prog_elite,
            name="Elite Cloneable Plan",
            code=f"ELTC-{uuid.uuid4().hex[:4]}",
            status="ACTIVE",
        )
        v1 = PackageVersion.objects.using(DB).create(
            package=pkg,
            version_number=1,
            name_snapshot="Elite Cloneable Plan v1",
            duration_value=90,
            duration_unit="DAYS",
            validity_days=90,
            total_days=90,
            effective_from=timezone.now() - timedelta(days=1),
            created_by_user=self.admin_user,
            status="ACTIVE",
            show_on_web=True,
            show_on_app=True,
            perks_config=elite_perks,
        )
        PackagePrice.objects.using(DB).create(
            package_version=v1,
            base_price=Decimal("24000.00"),
            tax_percent=Decimal("18.00"),
            prices_include_tax=False,
            currency="INR",
            effective_from=timezone.now() - timedelta(days=1),
            created_by_user=self.admin_user,
            status="ACTIVE",
        )
        PackageEntitlementDefinition.objects.using(DB).create(
            package_version=v1,
            entitlement_type="CREDIT_WALLET",
            allocated_units=Decimal("24.00"),
            validity_days=90,
            status="ACTIVE",
        )

        # 1. modify_package_version_safely (without perks_config in modifications) preserves v1 perks cleanly
        v2 = PackageCatalogService.modify_package_version_safely(
            package_version_id=str(v1.id),
            actor=self.admin_user,
            modifications={"name_snapshot": "Elite Cloneable Plan v2"},
            db_alias=DB,
        )
        self.assertEqual(v2.status, "DRAFT")
        self.assertEqual(v2.version_number, 2)
        self.assertEqual(v2.perks_config, elite_perks)
        # Mutating cloned draft dict must not mutate source version (deepcopy isolation)
        v2.perks_config["guest_passes_per_month"] = 99
        v2.save(using=DB, update_fields=["perks_config"])
        v1.refresh_from_db()
        self.assertEqual(v1.perks_config["guest_passes_per_month"], 2)
        self.assertEqual(v2.entitlement_definitions.using(DB).filter(entitlement_type="CREDIT_WALLET", allocated_units=Decimal("24.00")).count(), 1)

        # 2. clone-modify-version endpoint with explicit perks override updates draft while leaving v1 untouched
        self.admin_user._auth_type = "tenant"
        self.admin_user.is_superuser = True
        override_perks = dict(elite_perks, guest_passes_per_month=4)
        req_cm = self._prepare_request(
            self.factory.post(
                f"/api/v1/tenant/packages/{pkg.id}/clone-modify-version/",
                {"package_version_id": str(v1.id), "modifications": {"perks_config": override_perks}},
                format="json",
            ),
            self.admin_user,
        )
        req_cm.organization = self.org
        res_cm = PackageViewSet.as_view({"post": "clone_modify_version"})(req_cm, pk=str(pkg.id))
        self.assertEqual(res_cm.status_code, status.HTTP_200_OK)
        self.assertEqual(res_cm.data["perks_config"], override_perks)

        # 3. Retire v1 and call /reuse/ API -> v4 draft preserves v1's exact perks_config & entitlements
        v1.status = "RETIRED"
        v1.save(using=DB, update_fields=["status"])
        req_reuse = self._prepare_request(
            self.factory.post(f"/api/v1/tenant/package-versions/{v1.id}/reuse/", {}, format="json"),
            self.admin_user,
        )
        req_reuse.organization = self.org
        res_reuse = PackageVersionViewSet.as_view({"post": "reuse"})(req_reuse, pk=str(v1.id))
        self.assertEqual(res_reuse.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res_reuse.data["status"], "DRAFT")
        self.assertEqual(res_reuse.data["perks_config"], elite_perks)
        v4 = PackageVersion.objects.using(DB).get(id=res_reuse.data["id"])
        self.assertEqual(v4.perks_config, elite_perks)
        self.assertEqual(v4.entitlement_definitions.using(DB).filter(entitlement_type="CREDIT_WALLET", allocated_units=Decimal("24.00")).count(), 1)

    # -------------------------------------------------------------------------
    # 13. H3: Body Assessment Eligibility, Quota Enforcement & TV Screen Pairing
    # -------------------------------------------------------------------------
    def test_13_h3_body_assessment_quota_enforcement_and_pairing_summary(self):
        u, p = self._create_member("h3_assess")
        # 1. Package configured with capped assessment limit = 2
        perks = {"include_assessments": True, "assessment_credits": 2}
        mem, _ = self._grant_elite_membership(p, credits=Decimal("10.00"), perks=perks)

        from apps.tenant_core.services_assessment import AssessmentEngineService
        from apps.tenant_core.models_assessment import MemberAssessment
        from apps.tenant_core.views_assessment import AssessmentSessionViewSet, StudioScreenPairingView

        elig1 = AssessmentEngineService.check_member_eligibility(p, db_alias=DB)
        self.assertTrue(elig1["eligible"])
        self.assertEqual(elig1["remaining_credits"], 2)

        # Coach creates 1st assessment via AssessmentSessionViewSet
        req1 = self._prepare_request(
            self.factory.post(
                "/api/v1/tenant/assessments/sessions/",
                {"user_profile_id": str(p.id), "assessment_type": "INITIAL", "notes": "Initial scan"},
                format="json",
            ),
            self.admin_user,
        )
        req1.organization = self.org
        res1 = AssessmentSessionViewSet.as_view({"post": "create"})(req1)
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res1.data["membership_id"], str(mem.id))

        a1 = MemberAssessment.objects.using(DB).get(id=res1.data["id"])
        self.assertEqual(a1.membership_id, mem.id)

        # 2nd assessment
        req2 = self._prepare_request(
            self.factory.post(
                "/api/v1/tenant/assessments/sessions/",
                {"user_profile_id": str(p.id), "assessment_type": "MONTHLY"},
                format="json",
            ),
            self.admin_user,
        )
        req2.organization = self.org
        res2 = AssessmentSessionViewSet.as_view({"post": "create"})(req2)
        self.assertEqual(res2.status_code, status.HTTP_201_CREATED)

        # Quota now exhausted (2/2 used)
        elig_full = AssessmentEngineService.check_member_eligibility(p, db_alias=DB)
        self.assertFalse(elig_full["eligible"])
        self.assertEqual(elig_full["code"], "ASSESSMENT_QUOTA_EXHAUSTED")
        self.assertEqual(elig_full["remaining_credits"], 0)

        # 3rd assessment rejected with HTTP 400
        req3 = self._prepare_request(
            self.factory.post(
                "/api/v1/tenant/assessments/sessions/",
                {"user_profile_id": str(p.id), "assessment_type": "QUARTERLY"},
                format="json",
            ),
            self.admin_user,
        )
        req3.organization = self.org
        res3 = AssessmentSessionViewSet.as_view({"post": "create"})(req3)
        self.assertEqual(res3.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("all 2 included assessment(s)", res3.data["detail"].lower())

        # StudioScreenPairingView._approved_summary includes CREDIT_WALLET credits
        a1.recommended_package = self.pkg_elite
        a1.recommended_package_version = self.ver_elite
        a1.save(using=DB, update_fields=["recommended_package", "recommended_package_version"])
        summary = StudioScreenPairingView._approved_summary(a1)
        self.assertTrue(summary["recommended_package"]["is_credit_based"])
        self.assertEqual(summary["recommended_package"]["session_units"], 10)

    # -------------------------------------------------------------------------
    # 14. H4: Contract Perks Freezing and NEW_MEMBERSHIP Checkout PAR-Q Handling
    # -------------------------------------------------------------------------
    @patch("apps.tenant_core.services_razorpay.RazorpayService.verify_payment_signature", return_value=True)
    def test_14_h4_contract_snapshot_perks_frozen_and_checkout_parq(self, _mock_sig):
        u, p = self._create_member("h4_freeze")
        original_perks = {
            "cross_branch_mode": "ALL_BRANCHES",
            "guest_passes_per_month": 2,
            "include_assessments": True,
            "assessment_credits": 3,
        }
        self.ver_elite.perks_config = original_perks
        self.ver_elite.save(using=DB, update_fields=["perks_config"])

        # Create intake form
        form = IntakeForm.objects.using(DB).create(
            organization=self.org,
            name="PAR-Q Health Form",
            form_type="PAR_Q",
            version_number=1,
            status="ACTIVE",
        )

        # Checkout NEW_MEMBERSHIP passing PAR-Q signature
        req_ord = self._prepare_request(
            self.factory.post(
                "/api/v1/mobile/checkout/create-order/",
                {
                    "package_id": str(self.pkg_elite.id),
                    "parq_decision": "NEW",
                    "signature_data": "data:image/png;base64," + "MOCK_SIGNATURE" * 10,
                },
                format="json",
            ),
            u,
        )
        res_ord = MobileCheckoutOrderView.as_view()(req_ord)
        self.assertEqual(res_ord.status_code, status.HTTP_201_CREATED)
        order_id = res_ord.data["order_id"]

        # Verify checkout
        req_ver = self._prepare_request(
            self.factory.post(
                "/api/v1/mobile/checkout/verify/",
                {
                    "internal_order_id": order_id,
                    "payment_id": "pay_test_h4_ok",
                    "razorpay_order_id": res_ord.data["razorpay_order_id"],
                    "razorpay_signature": "test_signature",
                },
                format="json",
            ),
            u,
        )
        res_ver = MobileCheckoutVerifyView.as_view()(req_ver)
        self.assertEqual(res_ver.status_code, status.HTTP_200_OK)

        m = Membership.objects.using(DB).get(id=res_ver.data["membership_id"])
        self.assertEqual(m.parq_status, "COMPLETED")
        self.assertIsNotNone(m.parq_submission)

        # Verify contract snapshot has perks_config permanently frozen
        snap = m.contract_snapshot
        self.assertIsNotNone(snap)
        self.assertEqual(snap.perks_config, original_perks)

        # Admin subsequently edits package version perks to 0/empty
        self.ver_elite.perks_config = {}
        self.ver_elite.save(using=DB, update_fields=["perks_config"])

        # Effective perks for existing member MUST come from contract snapshot, not mutated package version
        eff_perks = BookingWaitlistAttendanceService.get_membership_effective_perks(m, db_alias=DB)
        self.assertEqual(eff_perks["guest_passes_per_month"], 2)
        self.assertEqual(eff_perks["assessment_credits"], 3)
        self.assertEqual(eff_perks["cross_branch_mode"], "ALL_BRANCHES")

    # -------------------------------------------------------------------------
    # 15. H5: Renew/Upgrade Unified Pricing, Coin Redemption, and Window Rule
    # -------------------------------------------------------------------------
    def test_15_h5_renewal_upgrade_pricing_and_window_policy(self):
        u, p = self._create_member("h5_pricing")
        mem, _ = self._grant_elite_membership(p, credits=Decimal("10.00"), perks={})

        # Credit 500 reward coins to user profile
        from apps.tenant_core.services_rewards import ReferralRewardService
        ReferralRewardService.earn_rewards(
            user_profile=p,
            reward_type="POINTS",
            quantity=Decimal("500.00"),
            reason="Test coins",
            db_alias=DB,
        )

        # 1. Renewal order calculates coupon and coins cleanly
        req_ren = self._prepare_request(
            self.factory.post(
                "/api/v1/mobile/memberships/renew/",
                {
                    "package_id": str(self.pkg_elite.id),
                    "points_to_redeem": 100,
                },
                format="json",
            ),
            u,
        )
        res_ren = MobileMembershipRenewView.as_view()(req_ren)
        self.assertEqual(res_ren.status_code, status.HTTP_201_CREATED)
        self.assertTrue(res_ren.data["requires_payment"])
        # Original: 8000 + 18% GST = 9440. 100 coins = ₹100 discount -> 9340
        self.assertEqual(res_ren.data["amount"], 9340.0)

        # 2. Upgrade window enforcement: set membership start date beyond 15 days
        pol_cfg = MembershipLifecycleService.get_policy_config(self.org, db_alias=DB)
        pol_cfg.upgrade_allowed_window_days = 15
        pol_cfg.save(using=DB, update_fields=["upgrade_allowed_window_days"])

        mem.start_date = self.today - timedelta(days=20)
        mem.save(using=DB, update_fields=["start_date"])

        # Target package with higher tier price in the SAME program
        pkg_super = Package.objects.using(DB).create(
            organization=self.org, program=mem.program or self.prog_elite, name="Super Elite Plan", code=f"SUP-{uuid.uuid4().hex[:4]}", status="ACTIVE"
        )
        ver_super = PackageVersion.objects.using(DB).create(
            package=pkg_super, version_number=1, name_snapshot="Super Elite Plan", duration_value=90, duration_unit="DAYS", effective_from=timezone.now(), created_by_user=self.admin_user, status="ACTIVE"
        )
        PackagePrice.objects.using(DB).create(
            package_version=ver_super, base_price=Decimal("15000.00"), tax_percent=Decimal("18.00"), effective_from=timezone.now(), created_by_user=self.admin_user, status="ACTIVE"
        )
        PackageEntitlementDefinition.objects.using(DB).create(
            package_version=ver_super, entitlement_type="CREDIT_WALLET", allocated_units=Decimal("30.00"), validity_days=90, status="ACTIVE"
        )

        req_upg_expired = self._prepare_request(
            self.factory.post(
                "/api/v1/mobile/memberships/upgrade/",
                {"target_package_id": str(pkg_super.id)},
                format="json",
            ),
            u,
        )
        res_upg_expired = MobileMembershipUpgradeView.as_view()(req_upg_expired)
        self.assertEqual(res_upg_expired.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_upg_expired.data["code"], "UPGRADE_WINDOW_EXPIRED")

        # Reset within window (10 days elapsed)
        mem.start_date = self.today - timedelta(days=10)
        mem.created_at = timezone.now() - timedelta(days=10)
        mem.save(using=DB, update_fields=["start_date", "created_at"])

        # 3. Staff upgrade options endpoint (views_members.py) correctly reports CREDIT_WALLET candidate
        from apps.tenant_core.views_members import MemberViewSet
        req_staff = self._prepare_request(
            self.factory.get(f"/api/v1/tenant/members/{p.id}/upgrade-options/?membership_id={mem.id}"),
            self.admin_user,
        )
        req_staff.organization = self.org
        self.admin_user._auth_type = "tenant"
        self.admin_user.is_superuser = True
        res_staff = MemberViewSet.as_view({"get": "upgrade_options"})(req_staff, pk=str(p.id))
        self.assertEqual(res_staff.status_code, status.HTTP_200_OK)
        cand_names = [pkg["name"] for pkg in res_staff.data["packages"]]
        self.assertIn("Super Elite Plan", cand_names)
        cand_super = next(pkg for pkg in res_staff.data["packages"] if pkg["name"] == "Super Elite Plan")
        self.assertTrue(cand_super["is_credit_based"])
        self.assertEqual(cand_super["credits"], 30.0)
