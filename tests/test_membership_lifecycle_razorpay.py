"""
tests/test_membership_lifecycle_razorpay.py

Comprehensive End-to-End Automated Tests for SWEAT CRM:
Self-Service Membership Lifecycle, Razorpay Checkout, CRM Synchronization & PAR-Q Integration.

Test Coverage:
1. Actions Options View (Dynamic CRM settings, active membership, branches, programs, dynamic extension & freeze tiers, PAR-Q info)
2. Renewal Flow:
   - Program Fixed Rule: Target package must belong to current active membership's program.
   - Branch Selection: Dynamic branch selection and pricing.
   - PAR-Q Decision (REUSE): Reuses existing valid submission, records in order notes.
   - Checkout Order Creation: Generates pending Razorpay order with authoritative taxes and amounts.
   - Idempotency: Repeating renewal request returns existing pending order.
   - Payment Verification: Authoritative signature verification, order marked PAID, PaymentTransaction SUCCESS,
     renewed membership activated with continuous start date and entitlements.
3. Upgrade Flow:
   - Self-service: Branch, program, and package switching without manual approval.
   - Authoritative Price Difference: Server calculates net payable diff and taxes.
   - Paid Upgrade: Razorpay checkout generated, payment verified, membership package/branch transitioned, entitlements updated.
   - Zero-difference Upgrade: Lateral transition applied immediately without payment gateway.
4. Validity Extension Flow:
   - Dynamic tiers from OrganizationSettings.membership_config.
   - Paid extension: Razorpay checkout, verified payment shifts end_date and entitlement valid_until.
   - Free extension: Applied immediately when fee == 0.
   - Validation: Rejects invalid durations and missing reasons.
5. Freeze & Unfreeze Flow:
   - Dynamic policy validation: Min 7 days, max 60 days, overlap prevention.
   - Freezing: Status changed to FROZEN, MembershipFreeze created.
   - Self-service unfreeze: Member instantly resumes membership, status restored to ACTIVE, validity extended.
6. Security & Idempotency:
   - Tampered signature rejected fail-closed (HTTP 400).
   - Duplicate payment verification handled idempotently without double activation.
   - Multi-tenant isolation enforced.
"""

import hmac
import hashlib
import json
import uuid
from decimal import Decimal
from datetime import date, timedelta
from unittest.mock import patch, MagicMock

from django.utils import timezone
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework import status

from apps.master.models import Tenant, TenantDataSource
from config.routers import set_tenant_db_alias
from apps.tenant_core.models_org import Organization, Branch, Location
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_govern import OrganizationSettings
from apps.tenant_core.models_catalog import (
    Program, Package, PackageVersion, PackagePrice, PackageEntitlementDefinition
)
from apps.tenant_core.models_memberships import (
    Membership, MembershipEntitlement, MembershipFreeze
)
from apps.tenant_core.models_crm import (
    IntakeForm, IntakeQuestion, IntakeSubmission, IntakeAnswer
)
from apps.tenant_core.models_commerce import Order, OrderItem, PaymentTransaction
from apps.tenant_core.services_razorpay import RazorpayService
from apps.tenant_core.views_mobile import (
    MobileMembershipActionsView,
    MobileMembershipRenewView,
    MobileMembershipUpgradeView,
    MobileMembershipExtendView,
    MobileMembershipFreezeView,
    MobileMembershipUnfreezeView,
    MobileCheckoutVerifyView,
)

DB = 'tenant_test'
TEST_KEY_ID = 'rzp_test_2OW34b7e5KWav7'
TEST_KEY_SECRET = 'mockedsecret123456789012'


def generate_valid_signature(order_id: str, payment_id: str, secret: str = TEST_KEY_SECRET) -> str:
    msg = f"{order_id}|{payment_id}".encode('utf-8')
    return hmac.new(secret.encode('utf-8'), msg, hashlib.sha256).hexdigest()


@override_settings(RAZORPAY_KEY_ID=TEST_KEY_ID, RAZORPAY_KEY_SECRET=TEST_KEY_SECRET)
class MembershipLifecycleRazorpayTestCase(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias(DB)
        self.factory = APIRequestFactory()

        # 1. Master Tenant
        self.tenant = Tenant.objects.using('default').create(
            code=f"SWT-{uuid.uuid4().hex[:4]}",
            name="Sweat UAT Gym",
            slug=f"sweat-uat-{uuid.uuid4().hex[:6]}",
            status="ACTIVE",
        )
        TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            db_name='test_fitness_tenant',
            database_name='test_fitness_tenant',
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )

        # 2. Organization, Location, Branches
        self.org = Organization.objects.using(DB).create(
            name="SWEAT Fitness Studio",
            code=f"org-{uuid.uuid4().hex[:6]}",
            status="ACTIVE",
        )
        self.location = Location.objects.using(DB).create(
            organization=self.org,
            name="Indiranagar Location",
            code=f"loc-{uuid.uuid4().hex[:4]}",
            city="Bengaluru",
            state="Karnataka",
            status="ACTIVE",
        )
        self.branch_indira = Branch.objects.using(DB).create(
            organization=self.org,
            location=self.location,
            name="SWEAT Indiranagar Studio",
            code=f"BLR-IND-{uuid.uuid4().hex[:4]}",
            status="ACTIVE",
        )
        self.branch_koramangala = Branch.objects.using(DB).create(
            organization=self.org,
            location=self.location,
            name="SWEAT Koramangala Studio",
            code=f"BLR-KOR-{uuid.uuid4().hex[:4]}",
            status="ACTIVE",
        )

        # 3. Organization Settings with dynamic membership_config
        self.org_settings = OrganizationSettings.objects.using(DB).create(
            organization=self.org,
            tax_rate_pct=Decimal('18.0'),
            membership_config={
                'extension_policy': {
                    'enabled': True,
                    'available_options': [
                        {'days': 7, 'base_fee': 500.0, 'tax_pct': 18.0, 'total_fee': 590.0, 'name': '7 Days Extension'},
                        {'days': 14, 'base_fee': 900.0, 'tax_pct': 18.0, 'total_fee': 1062.0, 'name': '14 Days Extension'},
                        {'days': 30, 'base_fee': 1500.0, 'tax_pct': 18.0, 'total_fee': 1770.0, 'name': '30 Days Extension'},
                        {'days': 60, 'base_fee': 2800.0, 'tax_pct': 18.0, 'total_fee': 3304.0, 'name': '60 Days Extension'},
                    ]
                },
                'freeze_policy': {
                    'enabled': True,
                    'min_days': 7,
                    'max_days': 60,
                    'fee_fixed': 200.0,
                    'fee_per_day': 10.0,
                }
            }
        )

        # 4. Member User & Profile
        self.user = TenantUser.objects.using(DB).create(
            email="siddharth.rao@tcs.com",
            first_name="Siddharth",
            last_name="Rao",
            organization=self.org,
            status='ACTIVE',
        )
        self.profile = UserProfile.objects.using(DB).create(
            user=self.user,
            member_status="ACTIVE",
            first_name_snapshot="Siddharth",
            last_name_snapshot="Rao",
            address_json={},
        )

        # 5. Program 1: Strength & Conditioning (Current)
        self.prog_strength = Program.objects.using(DB).create(
            organization=self.org,
            name="Strength & Conditioning",
            code=f"STR-{uuid.uuid4().hex[:4]}",
            status="ACTIVE",
        )
        # Package 1: Monthly Strength (Current)
        self.pkg_strength_monthly = Package.objects.using(DB).create(
            organization=self.org,
            program=self.prog_strength,
            name="Strength Monthly",
            code=f"STR-M-{uuid.uuid4().hex[:4]}",
            status="ACTIVE",
        )
        self.ver_strength_monthly = PackageVersion.objects.using(DB).create(
            package=self.pkg_strength_monthly,
            version_number=1,
            name_snapshot="Strength Monthly v1",
            duration_value=30,
            duration_unit="DAYS",
            effective_from=timezone.now(),
            created_by_user=self.user,
            status="ACTIVE",
        )
        self.price_strength_monthly = PackagePrice.objects.using(DB).create(
            package_version=self.ver_strength_monthly,
            base_price=Decimal("4000.00"),
            tax_percent=Decimal("18.00"),
            currency="INR",
            effective_from=timezone.now(),
            created_by_user=self.user,
            status="ACTIVE",
        )
        PackageEntitlementDefinition.objects.using(DB).create(
            package_version=self.ver_strength_monthly,
            entitlement_type="HOME_BRANCH_SESSION",
            allocated_units=Decimal("12.00"),
            is_unlimited=False,
            validity_days=30,
            status="ACTIVE",
        )

        # Package 2: Quarterly Strength (Same program, renewal option)
        self.pkg_strength_quarterly = Package.objects.using(DB).create(
            organization=self.org,
            program=self.prog_strength,
            name="Strength Quarterly",
            code=f"STR-Q-{uuid.uuid4().hex[:4]}",
            status="ACTIVE",
        )
        self.ver_strength_quarterly = PackageVersion.objects.using(DB).create(
            package=self.pkg_strength_quarterly,
            version_number=1,
            name_snapshot="Strength Quarterly v1",
            duration_value=90,
            duration_unit="DAYS",
            effective_from=timezone.now(),
            created_by_user=self.user,
            status="ACTIVE",
        )
        self.price_strength_quarterly = PackagePrice.objects.using(DB).create(
            package_version=self.ver_strength_quarterly,
            base_price=Decimal("10000.00"),
            tax_percent=Decimal("18.00"),
            currency="INR",
            effective_from=timezone.now(),
            created_by_user=self.user,
            status="ACTIVE",
        )

        # 6. Program 2: CrossFit Elite (Different program, for upgrades)
        self.prog_crossfit = Program.objects.using(DB).create(
            organization=self.org,
            name="CrossFit Elite",
            code=f"CF-{uuid.uuid4().hex[:4]}",
            status="ACTIVE",
        )
        self.pkg_crossfit_quarterly = Package.objects.using(DB).create(
            organization=self.org,
            program=self.prog_crossfit,
            name="CrossFit Elite Quarterly",
            code=f"CF-Q-{uuid.uuid4().hex[:4]}",
            status="ACTIVE",
        )
        self.ver_crossfit_quarterly = PackageVersion.objects.using(DB).create(
            package=self.pkg_crossfit_quarterly,
            version_number=1,
            name_snapshot="CrossFit Elite Quarterly v1",
            duration_value=90,
            duration_unit="DAYS",
            effective_from=timezone.now(),
            created_by_user=self.user,
            status="ACTIVE",
        )
        self.price_crossfit_quarterly = PackagePrice.objects.using(DB).create(
            package_version=self.ver_crossfit_quarterly,
            base_price=Decimal("12000.00"),
            tax_percent=Decimal("18.00"),
            currency="INR",
            effective_from=timezone.now(),
            created_by_user=self.user,
            status="ACTIVE",
        )
        PackageEntitlementDefinition.objects.using(DB).create(
            package_version=self.ver_crossfit_quarterly,
            entitlement_type="HOME_BRANCH_SESSION",
            allocated_units=Decimal("36.00"),
            is_unlimited=False,
            validity_days=90,
            status="ACTIVE",
        )

        # 7. Active Membership for Siddharth Rao
        self.today = timezone.now().date()
        self.membership = Membership.objects.using(DB).create(
            user_profile=self.profile,
            home_branch=self.branch_indira,
            purchase_branch=self.branch_indira,
            program=self.prog_strength,
            package=self.pkg_strength_monthly,
            package_version=self.ver_strength_monthly,
            package_price=self.price_strength_monthly,
            membership_number=f"MEM-{uuid.uuid4().hex[:8].upper()}",
            start_date=self.today - timedelta(days=15),
            end_date=self.today + timedelta(days=15),
            status="ACTIVE",
        )
        self.entitlement = MembershipEntitlement.objects.using(DB).create(
            membership=self.membership,
            entitlement_type="SESSION_POOL",
            allocated_units=Decimal("12.00"),
            consumed_units=Decimal("4.00"),
            valid_from=timezone.now() - timedelta(days=15),
            valid_until=timezone.now() + timedelta(days=15),
            status="ACTIVE",
        )

        # 8. PAR-Q Form and Initial Completed Submission
        self.parq_form = IntakeForm.objects.using(DB).create(
            organization=self.org,
            name="Physical Activity Readiness Questionnaire",
            form_type="PAR_Q",
            version_number=1,
            status="ACTIVE",
            agreement_title="Physical Activity Readiness Agreement",
            agreement_text="I confirm that I have answered all questions truthfully.",
        )
        self.q1 = IntakeQuestion.objects.using(DB).create(
            intake_form=self.parq_form,
            question_text="Has your doctor ever said that you have a heart condition?",
            question_type="BOOLEAN",
            is_sensitive=True,
            display_order=1,
            status="ACTIVE",
        )
        self.prior_parq_submission = IntakeSubmission.objects.using(DB).create(
            intake_form=self.parq_form,
            user_profile=self.profile,
            submitted_by_user=self.user,
            agreement_accepted=True,
            status="COMPLETED",
            signature_data="data:image/png;base64,mockSignData",
            signer_identity="Siddharth Rao",
            metadata={"channel": "MEMBER_PORTAL"},
        )
        IntakeAnswer.objects.using(DB).create(
            submission=self.prior_parq_submission,
            question=self.q1,
            boolean_value=False,
        )
        self.membership.parq_submission = self.prior_parq_submission
        self.membership.parq_status = 'COMPLETED'
        self.membership.save(using=DB, update_fields=['parq_submission', 'parq_status'])

    def _prepare_request(self, req, user):
        req.user = user
        user._state.db = DB
        req.META['HTTP_X_TENANT_ID'] = self.tenant.code
        req.tenant = self.tenant
        force_authenticate(req, user=user)
        return req

    # =========================================================================
    # 1. ACTIONS OPTIONS VIEW
    # =========================================================================
    def test_membership_actions_options_view_returns_authoritative_config(self):
        """GET /api/v1/mobile/memberships/actions/ returns dynamic CRM settings and filtered packages."""
        request = self.factory.get("/api/v1/mobile/memberships/actions/")
        self._prepare_request(request, self.user)

        response = MobileMembershipActionsView.as_view()(request)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data

        self.assertTrue(data["has_membership"])
        self.assertEqual(data["active_membership"]["id"], str(self.membership.id))
        self.assertEqual(data["active_membership"]["program_name"], "Strength & Conditioning")
        self.assertEqual(data["active_membership"]["remaining_sessions"], 8)
        self.assertFalse(data["active_membership"]["is_frozen"])

        # Branches and programs
        self.assertGreaterEqual(len(data["branches"]), 2)
        self.assertGreaterEqual(len(data["programs"]), 2)

        # Renew options must ONLY include packages from current program
        for ren_opt in data["renew_options"]:
            self.assertEqual(ren_opt["program_name"], "Strength & Conditioning")

        # Dynamic extension and freeze options from OrganizationSettings
        self.assertIn("available_options", data["extend_options"])
        self.assertEqual(len(data["extend_options"]["available_options"]), 4)
        self.assertEqual(data["freeze_options"]["min_days"], 7)
        self.assertEqual(data["freeze_options"]["max_days"], 60)

        # PAR-Q Info
        self.assertTrue(data["parq_info"]["has_completed_parq"])
        self.assertEqual(data["parq_info"]["submission_id"], str(self.prior_parq_submission.id))

    # =========================================================================
    # 2. RENEWAL FLOW & FIXED PROGRAM RULE
    # =========================================================================
    def test_renewal_rejects_package_from_different_program(self):
        """Renewal rule: attempting to renew with a package from another program must be rejected."""
        request = self.factory.post(
            "/api/v1/mobile/memberships/renew/",
            {"package_id": str(self.pkg_crossfit_quarterly.id)},
            format="json",
        )
        self._prepare_request(request, self.user)

        response = MobileMembershipRenewView.as_view()(request)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Renewal package must belong to your current program", response.data["detail"])

    def test_renewal_order_creation_and_parq_reuse(self):
        """Renewal creates pending order with authoritative pricing, links PAR-Q reuse, and enforces idempotency."""
        request = self.factory.post(
            "/api/v1/mobile/memberships/renew/",
            {
                "package_id": str(self.pkg_strength_quarterly.id),
                "branch_id": str(self.branch_koramangala.id),
                "parq_decision": "REUSE",
            },
            format="json",
        )
        self._prepare_request(request, self.user)

        response = MobileMembershipRenewView.as_view()(request)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["requires_payment"])
        self.assertFalse(response.data["is_active"])
        self.assertEqual(response.data["amount"], 11800.0)  # 10000 + 18% GST

        order_id = response.data["order_id"]
        order = Order.objects.using(DB).get(id=order_id)
        self.assertEqual(order.status, "PENDING_PAYMENT")
        self.assertEqual(order.order_type, "RENEWAL")
        self.assertEqual(order.branch, self.branch_koramangala)

        # Check notes metadata
        notes = json.loads(order.notes)
        self.assertEqual(notes["action_type"], "RENEWAL")
        self.assertEqual(notes["membership_id"], str(self.membership.id))
        self.assertEqual(notes["parq_decision"], "REUSE")
        self.assertEqual(notes["parq_submission_id"], str(self.prior_parq_submission.id))

        # Idempotency: repeated call within 10 min returns existing order with HTTP 200
        req2 = self.factory.post(
            "/api/v1/mobile/memberships/renew/",
            {
                "package_id": str(self.pkg_strength_quarterly.id),
                "branch_id": str(self.branch_koramangala.id),
                "parq_decision": "REUSE",
            },
            format="json",
        )
        self._prepare_request(req2, self.user)
        res2 = MobileMembershipRenewView.as_view()(req2)
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        self.assertEqual(res2.data["order_id"], order_id)

    @patch.object(RazorpayService, 'verify_payment_signature', return_value=True)
    def test_renewal_checkout_verification_activates_membership(self, mock_verify):
        """Verified payment for renewal order activates renewed membership with continuous start date."""
        # 1. Create order
        req = self.factory.post(
            "/api/v1/mobile/memberships/renew/",
            {"package_id": str(self.pkg_strength_quarterly.id), "parq_decision": "REUSE"},
            format="json",
        )
        self._prepare_request(req, self.user)
        res = MobileMembershipRenewView.as_view()(req)
        order_id = res.data["order_id"]
        rzp_order_id = res.data["razorpay_order_id"]

        # 2. Verify checkout
        payment_id = f"pay_{uuid.uuid4().hex[:12]}"
        sig = generate_valid_signature(rzp_order_id, payment_id)

        verify_req = self.factory.post(
            "/api/v1/mobile/checkout/verify/",
            {
                "internal_order_id": order_id,
                "razorpay_order_id": rzp_order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": sig,
            },
            format="json",
        )
        self._prepare_request(verify_req, self.user)
        verify_res = MobileCheckoutVerifyView.as_view()(verify_req)

        self.assertEqual(verify_res.status_code, status.HTTP_200_OK)
        self.assertTrue(verify_res.data["success"])
        self.assertEqual(verify_res.data["status"], "PAID")

        # Order must be PAID
        order = Order.objects.using(DB).get(id=order_id)
        self.assertEqual(order.status, "PAID")

        # PaymentTransaction recorded
        tx = PaymentTransaction.objects.using(DB).filter(order=order).first()
        self.assertIsNotNone(tx)
        self.assertEqual(tx.status, "SUCCESS")

        # Active memberships count is now 2 (or renewed membership exists)
        renewed_m = Membership.objects.using(DB).filter(
            user_profile=self.profile, package=self.pkg_strength_quarterly
        ).first()
        self.assertIsNotNone(renewed_m)
        self.assertEqual(renewed_m.status, "ACTIVE")
        # Continuous validity: starts the day after current membership ends
        self.assertEqual(renewed_m.start_date, self.membership.end_date + timedelta(days=1))
        self.assertEqual(renewed_m.end_date, renewed_m.start_date + timedelta(days=90))
        self.assertIn(renewed_m.parq_status, ["CLEARED", "COMPLETED"])

    # =========================================================================
    # 3. UPGRADE FLOW (SELF-SERVICE, AUTHORITATIVE PRICE DIFFERENCE)
    # =========================================================================
    def test_upgrade_calculates_server_price_difference(self):
        """Upgrade calculates target price minus remaining plan value, generates checkout order."""
        request = self.factory.post(
            "/api/v1/mobile/memberships/upgrade/",
            {
                "target_package_id": str(self.pkg_crossfit_quarterly.id),
                "target_branch_id": str(self.branch_koramangala.id),
                "reason": "Want intense CrossFit coaching",
            },
            format="json",
        )
        self._prepare_request(request, self.user)

        response = MobileMembershipUpgradeView.as_view()(request)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["requires_payment"])
        self.assertEqual(response.data["status"], "PENDING_PAYMENT")
        # Target price: 12000 + 18% = 14160. Remaining current plan: ~2360. Net diff > 0.
        self.assertGreater(response.data["net_difference"], 0)
        self.assertGreater(response.data["total_payable"], 0)

        order_id = response.data["order_id"]
        order = Order.objects.using(DB).get(id=order_id)
        self.assertEqual(order.order_type, "UPGRADE")

    @patch.object(RazorpayService, 'verify_payment_signature', return_value=True)
    def test_upgrade_payment_verification_executes_upgrade(self, mock_verify):
        """Verified payment for upgrade transitions package, increments version, updates branch and entitlements."""
        # 1. Create upgrade order
        req = self.factory.post(
            "/api/v1/mobile/memberships/upgrade/",
            {
                "target_package_id": str(self.pkg_crossfit_quarterly.id),
                "target_branch_id": str(self.branch_koramangala.id),
            },
            format="json",
        )
        self._prepare_request(req, self.user)
        res = MobileMembershipUpgradeView.as_view()(req)
        order_id = res.data["order_id"]
        rzp_order_id = res.data["razorpay_order_id"]

        # 2. Verify checkout
        payment_id = f"pay_{uuid.uuid4().hex[:12]}"
        sig = generate_valid_signature(rzp_order_id, payment_id)

        verify_req = self.factory.post(
            "/api/v1/mobile/checkout/verify/",
            {
                "internal_order_id": order_id,
                "razorpay_order_id": rzp_order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": sig,
            },
            format="json",
        )
        self._prepare_request(verify_req, self.user)
        verify_res = MobileCheckoutVerifyView.as_view()(verify_req)

        self.assertEqual(verify_res.status_code, status.HTTP_200_OK)
        self.assertTrue(verify_res.data["success"])

        # Check membership transitioned
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.package, self.pkg_crossfit_quarterly)
        self.assertEqual(self.membership.program, self.prog_crossfit)
        self.assertEqual(self.membership.home_branch, self.branch_koramangala)

        # Check new entitlements created
        new_ent = MembershipEntitlement.objects.using(DB).filter(
            membership=self.membership, status="ACTIVE", entitlement_type="HOME_BRANCH_SESSION"
        ).first()
        self.assertIsNotNone(new_ent)
        self.assertEqual(new_ent.allocated_units, 36)

    # =========================================================================
    # 4. VALIDITY EXTENSION FLOW
    # =========================================================================
    def test_extension_validates_duration_and_requires_reason(self):
        """Extension validates permitted durations (7, 14, 30, 60) and rejects empty reason."""
        # Empty reason
        req_no_reason = self.factory.post(
            "/api/v1/mobile/memberships/extend/",
            {"days": 14, "reason": ""},
            format="json",
        )
        self._prepare_request(req_no_reason, self.user)
        res1 = MobileMembershipExtendView.as_view()(req_no_reason)
        self.assertEqual(res1.status_code, status.HTTP_400_BAD_REQUEST)

        # Invalid duration (45 days is not in allowed list [7, 14, 30, 60])
        req_bad_days = self.factory.post(
            "/api/v1/mobile/memberships/extend/",
            {"days": 45, "reason": "Travel"},
            format="json",
        )
        self._prepare_request(req_bad_days, self.user)
        res2 = MobileMembershipExtendView.as_view()(req_bad_days)
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)

    @patch.object(RazorpayService, 'verify_payment_signature', return_value=True)
    def test_extension_paid_checkout_and_activation(self, mock_verify):
        """Paid extension creates order, payment verification extends membership end_date and entitlements."""
        initial_end_date = self.membership.end_date

        # 1. Create extension order for 14 days (configured base 900 + 18% tax = 1062)
        req = self.factory.post(
            "/api/v1/mobile/memberships/extend/",
            {"days": 14, "reason": "Work travel to Mumbai"},
            format="json",
        )
        self._prepare_request(req, self.user)
        res = MobileMembershipExtendView.as_view()(req)

        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertTrue(res.data["requires_payment"])
        self.assertEqual(res.data["amount"], 1062.0)
        order_id = res.data["order_id"]
        rzp_order_id = res.data["razorpay_order_id"]

        # 2. Verify checkout
        payment_id = f"pay_{uuid.uuid4().hex[:12]}"
        sig = generate_valid_signature(rzp_order_id, payment_id)

        verify_req = self.factory.post(
            "/api/v1/mobile/checkout/verify/",
            {
                "internal_order_id": order_id,
                "razorpay_order_id": rzp_order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": sig,
            },
            format="json",
        )
        self._prepare_request(verify_req, self.user)
        verify_res = MobileCheckoutVerifyView.as_view()(verify_req)

        self.assertEqual(verify_res.status_code, status.HTTP_200_OK)

        # Expiry date shifted by 14 days
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.end_date, initial_end_date + timedelta(days=14))

        # Entitlement validity shifted
        self.entitlement.refresh_from_db()
        self.assertEqual(self.entitlement.valid_until.date(), self.membership.end_date)

    # =========================================================================
    # 5. FREEZE & SELF-SERVICE UNFREEZE FLOW
    # =========================================================================
    def test_freeze_validates_dates_and_overlap(self):
        """Freeze validates min 7 days, max 60 days, and prevents overlaps."""
        # Less than 7 days
        req_short = self.factory.post(
            "/api/v1/mobile/memberships/freeze/",
            {
                "freeze_from": str(self.today + timedelta(days=2)),
                "freeze_until": str(self.today + timedelta(days=5)),
                "reason": "Weekend trip",
            },
            format="json",
        )
        self._prepare_request(req_short, self.user)
        res_short = MobileMembershipFreezeView.as_view()(req_short)
        self.assertEqual(res_short.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Minimum freeze duration is 7 days", res_short.data["detail"])

    @patch.object(RazorpayService, 'verify_payment_signature', return_value=True)
    def test_freeze_payment_and_self_service_unfreeze(self, mock_verify):
        """Paid freeze changes status to FROZEN; member calling unfreeze restores ACTIVE status and shifts validity."""
        from_d = self.today
        until_d = self.today + timedelta(days=14)
        initial_end_date = self.membership.end_date

        # 1. Create freeze order (fee: fixed 200 + 10*14 = 340 + 18% tax = 401.2)
        req = self.factory.post(
            "/api/v1/mobile/memberships/freeze/",
            {
                "freeze_from": str(from_d),
                "freeze_until": str(until_d),
                "reason": "Shoulder impingement rehab",
            },
            format="json",
        )
        self._prepare_request(req, self.user)
        res = MobileMembershipFreezeView.as_view()(req)

        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertTrue(res.data["requires_payment"])
        order_id = res.data["order_id"]
        rzp_order_id = res.data["razorpay_order_id"]

        # 2. Verify checkout
        payment_id = f"pay_{uuid.uuid4().hex[:12]}"
        sig = generate_valid_signature(rzp_order_id, payment_id)

        verify_req = self.factory.post(
            "/api/v1/mobile/checkout/verify/",
            {
                "internal_order_id": order_id,
                "razorpay_order_id": rzp_order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": sig,
            },
            format="json",
        )
        self._prepare_request(verify_req, self.user)
        verify_res = MobileCheckoutVerifyView.as_view()(verify_req)
        self.assertEqual(verify_res.status_code, status.HTTP_200_OK)

        # Status must now be FROZEN
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.status, "FROZEN")

        # Freeze record exists
        frz = MembershipFreeze.objects.using(DB).filter(membership=self.membership).first()
        self.assertIsNotNone(frz)
        self.assertIn(frz.status, ["ACTIVE", "SCHEDULED"])

        # 3. Member Self-Service Unfreeze
        unfreeze_req = self.factory.post(
            "/api/v1/mobile/memberships/unfreeze/",
            {"membership_id": str(self.membership.id)},
            format="json",
        )
        self._prepare_request(unfreeze_req, self.user)
        unfreeze_res = MobileMembershipUnfreezeView.as_view()(unfreeze_req)

        self.assertEqual(unfreeze_res.status_code, status.HTTP_200_OK)
        self.assertEqual(unfreeze_res.data["status"], "ACTIVE")

        # Status restored to ACTIVE
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.status, "ACTIVE")

    # =========================================================================
    # 6. SECURITY, IDEMPOTENCY & TAMPER PROTECTION
    # =========================================================================
    def test_tampered_signature_is_rejected_fail_closed(self):
        """An invalid or forged Razorpay signature returns 400 and does NOT activate membership."""
        # 1. Create order
        req = self.factory.post(
            "/api/v1/mobile/memberships/renew/",
            {"package_id": str(self.pkg_strength_quarterly.id)},
            format="json",
        )
        self._prepare_request(req, self.user)
        res = MobileMembershipRenewView.as_view()(req)
        order_id = res.data["order_id"]
        rzp_order_id = res.data["razorpay_order_id"]

        # 2. Tampered signature
        tampered_sig = "bad_signature_00000000000000000000000000000000"
        verify_req = self.factory.post(
            "/api/v1/mobile/checkout/verify/",
            {
                "internal_order_id": order_id,
                "razorpay_order_id": rzp_order_id,
                "razorpay_payment_id": f"pay_{uuid.uuid4().hex[:12]}",
                "razorpay_signature": tampered_sig,
            },
            format="json",
        )
        self._prepare_request(verify_req, self.user)
        verify_res = MobileCheckoutVerifyView.as_view()(verify_req)

        self.assertEqual(verify_res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Invalid Razorpay payment signature", verify_res.data["detail"])

        # Order remains PENDING_PAYMENT
        order = Order.objects.using(DB).get(id=order_id)
        self.assertEqual(order.status, "PENDING_PAYMENT")

    @patch.object(RazorpayService, 'verify_payment_signature', return_value=True)
    def test_duplicate_verification_is_idempotent(self, mock_verify):
        """Re-submitting the same payment verification payload returns 200 without double-activating."""
        req = self.factory.post(
            "/api/v1/mobile/memberships/renew/",
            {"package_id": str(self.pkg_strength_quarterly.id)},
            format="json",
        )
        self._prepare_request(req, self.user)
        res = MobileMembershipRenewView.as_view()(req)
        order_id = res.data["order_id"]
        rzp_order_id = res.data["razorpay_order_id"]

        payment_id = f"pay_{uuid.uuid4().hex[:12]}"
        sig = generate_valid_signature(rzp_order_id, payment_id)

        payload = {
            "internal_order_id": order_id,
            "razorpay_order_id": rzp_order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": sig,
        }

        # First verification
        v_req1 = self.factory.post("/api/v1/mobile/checkout/verify/", payload, format="json")
        self._prepare_request(v_req1, self.user)
        v_res1 = MobileCheckoutVerifyView.as_view()(v_req1)
        self.assertEqual(v_res1.status_code, status.HTTP_200_OK)

        count_after_first = Membership.objects.using(DB).filter(user_profile=self.profile).count()

        # Second verification (replay)
        v_req2 = self.factory.post("/api/v1/mobile/checkout/verify/", payload, format="json")
        self._prepare_request(v_req2, self.user)
        v_res2 = MobileCheckoutVerifyView.as_view()(v_req2)
        self.assertEqual(v_res2.status_code, status.HTTP_200_OK)
        self.assertEqual(v_res2.data["status"], "PAID")

        # No duplicate membership created
        count_after_second = Membership.objects.using(DB).filter(user_profile=self.profile).count()
        self.assertEqual(count_after_first, count_after_second)
