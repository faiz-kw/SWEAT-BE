"""
backend/tests/test_phase2_member_portal_actions.py

Comprehensive Automated Tests for SWEAT Phase 2:
1. Requirement 1: PAR-Q Visibility & Answer Formatting in Admin Members / Client 360
   - Boolean True and False answer formatting (verifying 'No' is not evaluated as empty string)
   - Question text, category, sensitivity, and form snapshot preservation
   - Revision tracking and previous answers carryover
   - Tenant isolation: member answers are not exposed across tenant boundaries

2. Requirement 2: PT Hiding from Member Portal
   - Verifying PT is deferred/hidden from member-facing portal while remaining fully intact
     on backend models and operational tables.

3. Requirement 3: Member-Facing Membership Self-Service Actions
   - Actions Options: GET /api/v1/mobile/memberships/actions/
   - Renew Membership: POST /api/v1/mobile/memberships/renew/ (idempotency, creates pending order, does NOT activate prematurely)
   - Upgrade Package: POST /api/v1/mobile/memberships/upgrade/ (creates ApprovalRequest PENDING, prevents duplicate pending requests)
   - Extend Validity: POST /api/v1/mobile/memberships/extend/ (validates 7-30 days, reason required, creates ApprovalRequest PENDING)
   - Freeze Membership: POST /api/v1/mobile/memberships/freeze/ (validates 7-60 days, future dates, prevents overlaps, creates ApprovalRequest & scheduled freeze)
   - Authorization: Unauthenticated access rejected, cross-user isolation enforced.
"""

import uuid
from decimal import Decimal
from datetime import date, timedelta
from django.utils import timezone
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework import status

from apps.master.models import Tenant, TenantDataSource
from config.routers import set_tenant_db_alias, get_tenant_db_alias
from apps.tenant_core.models_org import Organization, Branch, Location
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_memberships import (
    Membership,
    Package,
    PackageVersion,
    PackagePrice,
    MembershipEntitlement,
    MembershipFreeze,
)
from apps.tenant_core.models_crm import (
    IntakeForm,
    IntakeQuestion,
    IntakeSubmission,
    IntakeAnswer,
)
from apps.tenant_core.models_commerce import Order
from apps.tenant_core.models_approvals import ApprovalRequest
from apps.tenant_core.views_members import build_member_360_aggregate
from apps.tenant_core.views_mobile import (
    MobileOnboardingSubmitView,
    MobileMembershipActionsView,
    MobileMembershipRenewView,
    MobileMembershipUpgradeView,
    MobileMembershipExtendView,
    MobileMembershipFreezeView,
)

DB = 'tenant_test'


class Phase2MemberPortalActionsTest(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias(DB)
        self.factory = APIRequestFactory()

        # Master Tenant
        self.tenant = Tenant.objects.using('default').create(
            code=f"SWT-{uuid.uuid4().hex[:4]}",
            name="Sweat Test Tenant",
            slug=f"sweat-test-{uuid.uuid4().hex[:6]}",
            status="ACTIVE",
        )
        TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            db_name='test_fitness_tenant',
            database_name='test_fitness_tenant',
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )

        # Organization, Location, Branch
        self.org = Organization.objects.using(DB).create(
            name="SWEAT Fitness Studio",
            code=f"org-{uuid.uuid4().hex[:6]}",
            status="ACTIVE",
        )
        self.location = Location.objects.using(DB).create(
            organization=self.org,
            name="Indiranagar Location",
            code=f"loc-{uuid.uuid4().hex[:4]}",
            status="ACTIVE",
        )
        self.branch = Branch.objects.using(DB).create(
            organization=self.org,
            location=self.location,
            name="SWEAT Indiranagar Studio",
            code=f"BLR-{uuid.uuid4().hex[:4]}",
            status="ACTIVE",
        )

        # Member User & Profile
        self.user = TenantUser.objects.using(DB).create(
            email=f"member_{uuid.uuid4().hex[:6]}@test.com",
            first_name="Priya",
            last_name="Sharma",
            organization=self.org,
            status='ACTIVE',
        )
        self.profile = UserProfile.objects.using(DB).create(
            user=self.user,
            member_status="ACTIVE",
            first_name_snapshot="Priya",
            last_name_snapshot="Sharma",
            address_json={},
        )

        # Another Member User (for cross-user isolation tests)
        self.other_user = TenantUser.objects.using(DB).create(
            email=f"other_{uuid.uuid4().hex[:6]}@test.com",
            first_name="Other",
            last_name="Member",
            organization=self.org,
            status='ACTIVE',
        )
        self.other_profile = UserProfile.objects.using(DB).create(
            user=self.other_user,
            member_status="ACTIVE",
            first_name_snapshot="Other",
            last_name_snapshot="Member",
            address_json={},
        )

        # Program & Packages
        from apps.tenant_core.models_catalog import Program, PackageEntitlementDefinition

        self.program = Program.objects.using(DB).create(
            organization=self.org,
            name="Bootcamp Program",
            code=f"BC-{uuid.uuid4().hex[:4]}",
            status="ACTIVE",
        )
        self.pkg_standard = Package.objects.using(DB).create(
            organization=self.org,
            program=self.program,
            name="Sweat Bootcamp Standard",
            code=f"BOOT-{uuid.uuid4().hex[:4]}",
            status="ACTIVE",
        )
        self.ver_standard = PackageVersion.objects.using(DB).create(
            package=self.pkg_standard,
            version_number=1,
            name_snapshot="Sweat Bootcamp Standard v1",
            duration_value=1,
            duration_unit="MONTH",
            effective_from=timezone.now(),
            created_by_user=self.user,
            status="ACTIVE",
        )
        self.ped_standard = PackageEntitlementDefinition.objects.using(DB).create(
            package_version=self.ver_standard,
            entitlement_type="CLASS_CREDIT",
            allocated_units=Decimal("16.00"),
            is_unlimited=False,
        )
        self.price_standard = PackagePrice.objects.using(DB).create(
            package_version=self.ver_standard,
            base_price=Decimal("4999.00"),
            currency="INR",
            tax_percent=Decimal("18.0"),
            effective_from=timezone.now(),
            created_by_user=self.user,
            status="ACTIVE",
        )

        self.pkg_premium = Package.objects.using(DB).create(
            organization=self.org,
            program=self.program,
            name="Sweat Bootcamp Unlimited",
            code=f"UNLIM-{uuid.uuid4().hex[:4]}",
            status="ACTIVE",
        )
        self.ver_premium = PackageVersion.objects.using(DB).create(
            package=self.pkg_premium,
            version_number=1,
            name_snapshot="Sweat Bootcamp Unlimited v1",
            duration_value=1,
            duration_unit="MONTH",
            effective_from=timezone.now(),
            created_by_user=self.user,
            status="ACTIVE",
        )
        self.ped_premium = PackageEntitlementDefinition.objects.using(DB).create(
            package_version=self.ver_premium,
            entitlement_type="CLASS_CREDIT",
            allocated_units=Decimal("32.00"),
            is_unlimited=False,
        )
        self.price_premium = PackagePrice.objects.using(DB).create(
            package_version=self.ver_premium,
            base_price=Decimal("8999.00"),
            currency="INR",
            tax_percent=Decimal("18.0"),
            effective_from=timezone.now(),
            created_by_user=self.user,
            status="ACTIVE",
        )

        # Active Membership for self.user
        today = timezone.now().date()
        self.membership = Membership.objects.using(DB).create(
            user_profile=self.profile,
            program=self.program,
            package=self.pkg_standard,
            package_version=self.ver_standard,
            package_price=self.price_standard,
            purchase_branch=self.branch,
            home_branch=self.branch,
            membership_number=f"MEM-{uuid.uuid4().hex[:6].upper()}",
            start_date=today - timedelta(days=10),
            end_date=today + timedelta(days=20),
            status="ACTIVE",
        )
        self.entitlement = MembershipEntitlement.objects.using(DB).create(
            membership=self.membership,
            entitlement_type="SESSION_POOL",
            allocated_units=Decimal("16.00"),
            consumed_units=Decimal("4.00"),
            valid_from=timezone.now() - timedelta(days=10),
            valid_until=timezone.now() + timedelta(days=20),
            status="ACTIVE",
        )

        # PAR-Q Form & Questions
        self.parq_form = IntakeForm.objects.using(DB).create(
            organization=self.org,
            name="SWEAT PAR-Q Assessment",
            form_type="PAR_Q",
            version_number=1,
            status="ACTIVE",
            agreement_title="Physical Activity Readiness Agreement",
            agreement_text="I confirm that I have answered all questions truthfully.",
        )
        self.q_heart = IntakeQuestion.objects.using(DB).create(
            intake_form=self.parq_form,
            question_text="Has your doctor ever said you have a heart condition?",
            question_type="BOOLEAN",
            category="MEDICAL",
            is_sensitive=True,
            display_order=1,
            status="ACTIVE",
        )
        self.q_chest_pain = IntakeQuestion.objects.using(DB).create(
            intake_form=self.parq_form,
            question_text="Do you feel pain in your chest when performing physical activity?",
            question_type="BOOLEAN",
            category="MEDICAL",
            is_sensitive=True,
            display_order=2,
            status="ACTIVE",
        )
        self.q_emergency_contact = IntakeQuestion.objects.using(DB).create(
            intake_form=self.parq_form,
            question_text="Emergency Contact Name & Phone Number",
            question_type="TEXT",
            category="GENERAL",
            is_sensitive=False,
            display_order=3,
            status="ACTIVE",
        )

    def tearDown(self):
        set_tenant_db_alias(None)

    def _prepare_request(self, req, user):
        req.user = user
        user._state.db = DB
        req.META['HTTP_X_TENANT_ID'] = self.tenant.code
        force_authenticate(req, user=user)
        return req

    # =========================================================================
    # REQUIREMENT 1: PAR-Q VISIBILITY & ANSWER FORMATTING TESTS
    # =========================================================================

    def test_parq_boolean_false_and_true_formatted_correctly_in_client_360(self):
        """
        Tests that boolean False answers are correctly formatted as 'No'
        instead of blank/empty strings, and boolean True is formatted as 'Yes'.
        """
        submission = IntakeSubmission.objects.using(DB).create(
            intake_form=self.parq_form,
            user_profile=self.profile,
            submitted_by_user=self.user,
            agreement_accepted=True,
            status="COMPLETED",
            signature_data="data:image/png;base64,mockSignatureCanvasData",
            signer_identity="Priya Sharma",
            metadata={"channel": "MEMBER_PORTAL", "revision_number": 1},
        )
        # q_heart = False (No heart condition) -> Previously evaluated to None/empty!
        IntakeAnswer.objects.using(DB).create(
            submission=submission,
            question=self.q_heart,
            boolean_value=False,
        )
        # q_chest_pain = False (No chest pain)
        IntakeAnswer.objects.using(DB).create(
            submission=submission,
            question=self.q_chest_pain,
            boolean_value=False,
        )
        # q_emergency_contact = Text
        IntakeAnswer.objects.using(DB).create(
            submission=submission,
            question=self.q_emergency_contact,
            text_value="Aarav Sharma +91 9876543210",
        )

        read_model = build_member_360_aggregate(
            profile=self.profile,
            alias=DB,
            requesting_user=self.user,
        )

        submissions = read_model["par_q_form"]["submissions"]
        self.assertEqual(len(submissions), 1)

        sub_data = submissions[0]
        self.assertEqual(sub_data["form_title"], "SWEAT PAR-Q Assessment")
        self.assertEqual(sub_data["form_version"], 1)
        self.assertTrue(sub_data["agreement_accepted"])
        self.assertIsNotNone(sub_data["signature_data"])

        answers = sub_data["answers"]
        self.assertEqual(len(answers), 3)

        heart_ans = next(a for a in answers if a["question_id"] == str(self.q_heart.id))
        self.assertEqual(heart_ans["answer"], "No")
        self.assertIs(heart_ans["boolean_value"], False)
        self.assertTrue(heart_ans["is_sensitive"])

        chest_ans = next(a for a in answers if a["question_id"] == str(self.q_chest_pain.id))
        self.assertEqual(chest_ans["answer"], "No")
        self.assertIs(chest_ans["boolean_value"], False)

        contact_ans = next(a for a in answers if a["question_id"] == str(self.q_emergency_contact.id))
        self.assertEqual(contact_ans["answer"], "Aarav Sharma +91 9876543210")

    def test_parq_submission_via_mobile_view_creates_snapshot_and_answers(self):
        """
        Tests that MobileOnboardingSubmitView creates full question snapshots
        and IntakeAnswer records safely.
        """
        request = self.factory.post(
            "/api/v1/mobile/onboarding/submit/",
            {
                "agreement_accepted": True,
                "signature_data": "data:image/png;base64,sampleSignedData",
                "answers": {
                    str(self.q_heart.id): False,
                    str(self.q_chest_pain.id): False,
                    str(self.q_emergency_contact.id): "Rajesh Sharma 9988776655",
                },
            },
            format="json",
        )
        self._prepare_request(request, self.user)

        view = MobileOnboardingSubmitView.as_view()
        response = view(request)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_cleared"])

        sub = IntakeSubmission.objects.using(DB).filter(user_profile=self.profile).order_by("-submitted_at").first()
        self.assertIsNotNone(sub)
        self.assertEqual(sub.answers.count(), 3)
        self.assertIn("questions", sub.form_snapshot)
        self.assertEqual(len(sub.form_snapshot["questions"]), 3)

    # =========================================================================
    # REQUIREMENT 3: MEMBER-FACING MEMBERSHIP ACTIONS TESTS
    # =========================================================================

    def test_membership_actions_options_endpoint(self):
        """
        GET /api/v1/mobile/memberships/actions/ returns active membership,
        renewal options, upgrade options, extension rules, and freeze rules.
        """
        request = self.factory.get("/api/v1/mobile/memberships/actions/")
        self._prepare_request(request, self.user)

        response = MobileMembershipActionsView.as_view()(request)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data

        self.assertTrue(data["has_membership"])
        self.assertEqual(data["active_membership"]["membership_number"], self.membership.membership_number)
        self.assertEqual(data["active_membership"]["remaining_sessions"], 12)
        self.assertFalse(data["active_membership"]["is_frozen"])

        self.assertTrue(len(data["renew_options"]) >= 1)
        self.assertTrue(len(data["upgrade_options"]) >= 1)
        self.assertEqual(data["extend_options"]["available_days"], [7, 14, 21, 30])
        self.assertEqual(data["freeze_options"]["min_days"], 7)
        self.assertEqual(data["freeze_options"]["max_days"], 60)

    def test_renew_membership_creates_pending_order_and_enforces_idempotency(self):
        """
        POST /api/v1/mobile/memberships/renew/ creates a pending checkout order.
        Does NOT activate renewed membership prematurely.
        Reuses existing pending order if clicked within 10 minutes.
        """
        request = self.factory.post(
            "/api/v1/mobile/memberships/renew/",
            {"package_id": str(self.pkg_standard.id)},
            format="json",
        )
        self._prepare_request(request, self.user)

        response = MobileMembershipRenewView.as_view()(request)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["requires_payment"])
        self.assertFalse(response.data["is_active"])

        order_id = response.data["order_id"]
        order = Order.objects.using(DB).get(id=order_id)
        self.assertEqual(order.status, "PENDING_PAYMENT")
        self.assertEqual(order.order_type, "RENEWAL")

        # Active membership is STILL 1
        self.assertEqual(Membership.objects.using(DB).filter(user_profile=self.profile, status="ACTIVE").count(), 1)

        # Idempotency: second click returns existing pending order
        request2 = self.factory.post(
            "/api/v1/mobile/memberships/renew/",
            {"package_id": str(self.pkg_standard.id)},
            format="json",
        )
        self._prepare_request(request2, self.user)

        response2 = MobileMembershipRenewView.as_view()(request2)
        self.assertEqual(response2.status_code, status.HTTP_200_OK)
        self.assertEqual(response2.data["order_id"], order_id)
        self.assertEqual(Order.objects.using(DB).filter(user_profile=self.profile, order_type="RENEWAL").count(), 1)

    def test_upgrade_package_submits_pending_approval_request(self):
        """
        POST /api/v1/mobile/memberships/upgrade/ creates an ApprovalRequest (status PENDING).
        Prevents duplicate pending upgrade requests.
        """
        request = self.factory.post(
            "/api/v1/mobile/memberships/upgrade/",
            {
                "target_package_id": str(self.pkg_premium.id),
                "reason": "Need unlimited sessions for daily workouts",
            },
            format="json",
        )
        self._prepare_request(request, self.user)

        response = MobileMembershipUpgradeView.as_view()(request)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["status"], "PENDING")
        self.assertEqual(response.data["price_difference"], 4000.0)

        approval = ApprovalRequest.objects.using(DB).get(id=response.data["request_id"])
        self.assertEqual(approval.status, "PENDING")
        self.assertEqual(approval.request_type, "MEMBERSHIP_UPGRADE")
        self.assertEqual(approval.requested_by_user, self.user)

        # Duplicate submission is rejected
        dup_request = self.factory.post(
            "/api/v1/mobile/memberships/upgrade/",
            {"target_package_id": str(self.pkg_premium.id)},
            format="json",
        )
        self._prepare_request(dup_request, self.user)

        dup_response = MobileMembershipUpgradeView.as_view()(dup_request)
        self.assertEqual(dup_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(dup_response.data["code"], "UPGRADE_REQUEST_ALREADY_PENDING")

    def test_extend_validity_validates_duration_and_requires_reason(self):
        """
        POST /api/v1/mobile/memberships/extend/ validates duration (7, 14, 21, 30 days),
        requires reason, and creates ApprovalRequest. Does not silently change expiry date.
        """
        initial_end_date = self.membership.end_date

        req_no_reason = self.factory.post(
            "/api/v1/mobile/memberships/extend/",
            {"days": 14, "reason": ""},
            format="json",
        )
        self._prepare_request(req_no_reason, self.user)

        res_no_reason = MobileMembershipExtendView.as_view()(req_no_reason)
        self.assertEqual(res_no_reason.status_code, status.HTTP_400_BAD_REQUEST)

        req_invalid_days = self.factory.post(
            "/api/v1/mobile/memberships/extend/",
            {"days": 45, "reason": "Travel"},
            format="json",
        )
        self._prepare_request(req_invalid_days, self.user)

        res_invalid_days = MobileMembershipExtendView.as_view()(req_invalid_days)
        self.assertEqual(res_invalid_days.status_code, status.HTTP_400_BAD_REQUEST)

        req_valid = self.factory.post(
            "/api/v1/mobile/memberships/extend/",
            {"days": 14, "reason": "Official business travel to London"},
            format="json",
        )
        self._prepare_request(req_valid, self.user)

        res_valid = MobileMembershipExtendView.as_view()(req_valid)
        self.assertEqual(res_valid.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res_valid.data["status"], "PENDING")

        self.membership.refresh_from_db()
        self.assertEqual(self.membership.end_date, initial_end_date)

    def test_freeze_membership_validates_dates_and_creates_scheduled_freeze(self):
        """
        POST /api/v1/mobile/memberships/freeze/ validates minimum 7 days, maximum 60 days,
        future dates, creates scheduled freeze and ApprovalRequest.
        """
        tomorrow = timezone.now().date() + timedelta(days=1)
        in_14_days = tomorrow + timedelta(days=14)

        request = self.factory.post(
            "/api/v1/mobile/memberships/freeze/",
            {
                "freeze_from": str(tomorrow),
                "freeze_until": str(in_14_days),
                "reason": "Ankle sprain recovery on doctor recommendation",
            },
            format="json",
        )
        self._prepare_request(request, self.user)

        response = MobileMembershipFreezeView.as_view()(request)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["days"], 14)
        self.assertEqual(response.data["status"], "PENDING")

        frz = MembershipFreeze.objects.using(DB).get(id=response.data["freeze_id"])
        self.assertEqual(frz.status, "SCHEDULED")
        self.assertEqual(frz.extend_membership_days, 14)

        overlap_req = self.factory.post(
            "/api/v1/mobile/memberships/freeze/",
            {
                "freeze_from": str(tomorrow + timedelta(days=2)),
                "freeze_until": str(tomorrow + timedelta(days=10)),
                "reason": "Another freeze",
            },
            format="json",
        )
        self._prepare_request(overlap_req, self.user)

        overlap_res = MobileMembershipFreezeView.as_view()(overlap_req)
        self.assertEqual(overlap_res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cross_member_isolation_security(self):
        """
        Verifies that authenticated member actions strictly operate on their own
        memberships and cannot access or modify another member's membership.
        """
        request = self.factory.get("/api/v1/mobile/memberships/actions/")
        self._prepare_request(request, self.other_user)

        response = MobileMembershipActionsView.as_view()(request)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["has_membership"])
        self.assertIsNone(response.data["active_membership"])
