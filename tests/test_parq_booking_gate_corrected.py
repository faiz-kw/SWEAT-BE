import uuid
import os
import django
from decimal import Decimal
from datetime import timedelta, date, datetime

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.test import TestCase
from django.utils import timezone
from django.core.exceptions import ValidationError
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework import status

from apps.master.models import Tenant, TenantDataSource, ProductModule, TenantModule, SaasPlan, TenantSubscription
from config.routers import set_tenant_db_alias, get_tenant_db_alias
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_crm import (
    Lead, UserProfile, IntakeForm, IntakeQuestion, IntakeQuestionOption, IntakeSubmission, IntakeAnswer
)
from apps.tenant_core.models_catalog import (
    Program, Package, PackageVersion, PackagePrice, PackageEntitlementDefinition,
    TermsDocument, TermsDocumentVersion, TermsAcceptance
)
from apps.tenant_core.models_classes import ClassCategory, ClassTemplate, ClassOccurrence
from apps.tenant_core.models_commerce import Order, OrderItem
from apps.tenant_core.models_memberships import (
    Membership, MembershipEntitlement, MembershipEntitlementLedger
)
from apps.tenant_core.models_bookings import Booking
from apps.tenant_core.services_crm import CRMLeadService
from apps.tenant_core.services_memberships import MembershipLifecycleService
from apps.tenant_core.services_bookings import BookingWaitlistAttendanceService, ParqRequiredValidationError
from apps.tenant_core.views_memberships import MembershipViewSet
from apps.tenant_core.views_members import MemberViewSet

DB = 'tenant_test'


class ParqCorrectedWorkflowTests(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias(DB)

        # Master DB Tenant setup
        self.tenant = Tenant.objects.using('default').create(
            code=f"PARQ-{uuid.uuid4().hex[:4]}",
            name="Sweat PARQ Tenant",
            slug=f"sweat-parq-{uuid.uuid4().hex[:4]}",
            status="ACTIVE"
        )
        TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            db_name='test_fitness_tenant',
            database_name='test_fitness_tenant',
            status='ACTIVE',
            database_engine='POSTGRESQL'
        )

        # 1. Organization & Branch in tenant DB
        self.org = Organization.objects.using(DB).create(
            name="Test Sweat Gym",
            code=f"TSG-{uuid.uuid4().hex[:6]}",
            status="ACTIVE"
        )
        self.location = Location.objects.using(DB).create(
            organization=self.org,
            code=f"LOC-{uuid.uuid4().hex[:4]}",
            name="Downtown Location",
            status="ACTIVE"
        )
        self.branch = Branch.objects.using(DB).create(
            organization=self.org,
            location=self.location,
            name="Downtown Studio",
            code=f"DT-{uuid.uuid4().hex[:4]}",
            status="ACTIVE"
        )

        # 2. Staff user & Member user
        self.staff_user = TenantUser.objects.using(DB).create(
            organization=self.org,
            email=f"staff-{uuid.uuid4().hex[:6]}@test.com",
            username=f"staff-{uuid.uuid4().hex[:6]}",
            first_name="Staff",
            last_name="Coach",
            status="ACTIVE"
        )
        self.member_user = TenantUser.objects.using(DB).create(
            organization=self.org,
            email=f"member-{uuid.uuid4().hex[:6]}@test.com",
            username=f"member-{uuid.uuid4().hex[:6]}",
            first_name="Jane",
            last_name="Doe",
            status="ACTIVE"
        )
        self.other_member_user = TenantUser.objects.using(DB).create(
            organization=self.org,
            email=f"other-{uuid.uuid4().hex[:6]}@test.com",
            username=f"other-{uuid.uuid4().hex[:6]}",
            first_name="Other",
            last_name="Member",
            status="ACTIVE"
        )

        # Member UserProfile
        self.member_profile = UserProfile.objects.using(DB).create(
            user=self.member_user,
            member_status="ACTIVE",
            first_name_snapshot="Jane",
            last_name_snapshot="Doe",
        )
        self.other_member_profile = UserProfile.objects.using(DB).create(
            user=self.other_member_user,
            member_status="ACTIVE",
            first_name_snapshot="Other",
            last_name_snapshot="Member",
        )

        # 3. Program & Package
        self.program = Program.objects.using(DB).create(
            organization=self.org,
            name="Functional Fitness",
            code="FF",
            status="ACTIVE"
        )
        self.package = Package.objects.using(DB).create(
            organization=self.org,
            program=self.program,
            name="10-Class Pack",
            code="PKG-10",
            status="ACTIVE"
        )
        self.pv = PackageVersion.objects.using(DB).create(
            package=self.package,
            version_number=1,
            name_snapshot="10-Class Pack v1",
            duration_value=1,
            duration_unit="MONTH",
            effective_from=timezone.now(),
            created_by_user=self.staff_user,
            status="ACTIVE"
        )
        self.ped = PackageEntitlementDefinition.objects.using(DB).create(
            package_version=self.pv,
            entitlement_type="CLASS_CREDIT",
            allocated_units=Decimal("10.00"),
            is_unlimited=False
        )
        self.pp = PackagePrice.objects.using(DB).create(
            package_version=self.pv,
            currency="INR",
            base_price=Decimal("3000.00"),
            tax_percent=Decimal("0.000"),
            effective_from=timezone.now(),
            created_by_user=self.staff_user,
            status="ACTIVE"
        )

        # 4. Class Template & Occurrence
        self.cat = ClassCategory.objects.using(DB).create(
            organization=self.org,
            code="HIIT",
            name="HIIT",
            status="ACTIVE"
        )
        self.template = ClassTemplate.objects.using(DB).create(
            organization=self.org,
            category=self.cat,
            program=self.program,
            code="HIIT-MS",
            name="Morning Sweat",
            default_duration_minutes=60,
            status="ACTIVE"
        )
        start_time = timezone.now() + timedelta(days=1)
        self.occurrence = ClassOccurrence.objects.using(DB).create(
            class_template=self.template,
            branch=self.branch,
            occurrence_date=start_time.date(),
            start_at=start_time,
            end_at=start_time + timedelta(minutes=60),
            capacity=15,
            trial_capacity=3,
            status="SCHEDULED"
        )

        # 5. Configured PAR-Q Form with questions and agreement
        self.parq_form = IntakeForm.objects.using(DB).create(
            organization=self.org,
            name="PAR-Q General Form",
            form_type="PAR_Q",
            version_number=1,
            status="ACTIVE",
            agreement_title="Physical Activity Readiness & Assumption of Risk Agreement",
            agreement_text="I confirm I am in good health and accept all exercise risks.",
            is_required_for_purchase=True,
            requires_explicit_consent=True,
            is_default_for_all_programs=True
        )
        self.parq_form.assigned_programs.add(self.program)

        self.q1 = IntakeQuestion.objects.using(DB).create(
            intake_form=self.parq_form,
            question_text="Has your doctor ever said that you have a heart condition?",
            question_type="BOOLEAN",
            display_order=1,
            is_required=True
        )
        self.q2 = IntakeQuestion.objects.using(DB).create(
            intake_form=self.parq_form,
            question_text="Do you feel pain in your chest when you do physical activity?",
            question_type="BOOLEAN",
            display_order=2,
            is_required=True
        )
        self.q3 = IntakeQuestion.objects.using(DB).create(
            intake_form=self.parq_form,
            question_text="Do you lose your balance because of dizziness?",
            question_type="BOOLEAN",
            display_order=3,
            is_required=False
        )

    def test_workflow_complete(self):
        set_tenant_db_alias(DB)

        # -------------------------------------------------------------
        # Step 1: Purchase completes without any PAR-Q step
        # -------------------------------------------------------------
        # Order is created and paid without requiring PAR-Q
        order = Order.objects.using(DB).create(
            branch=self.branch,
            user_profile=self.member_profile,
            order_number=f"ORD-{uuid.uuid4().hex[:8]}",
            order_type="PACKAGE",
            status="PAID",
            currency="INR"
        )
        order_item = OrderItem.objects.using(DB).create(
            order=order,
            item_type="PACKAGE",
            package=self.package,
            package_version=self.pv,
            package_price=self.pp,
            unit_price_snapshot=Decimal("3000.00"),
            total_amount=Decimal("3000.00")
        )

        # -------------------------------------------------------------
        # Step 2: Paid membership activates with parq_status='PENDING'
        # -------------------------------------------------------------
        membership = MembershipLifecycleService.activate_membership_from_order(
            order=order,
            order_item=order_item,
            db_alias=DB,
            created_by_user=self.staff_user
        )
        self.assertEqual(membership.status, 'ACTIVE')
        self.assertEqual(membership.parq_status, 'PENDING')
        self.assertEqual(membership.parq_form.id, self.parq_form.id)
        self.assertIsNone(membership.parq_submission)

        # Check entitlement was initialized but must not be consumed yet
        ent = membership.entitlements.using(DB).first()
        self.assertEqual(ent.remaining_units, Decimal("10.00"))
        self.assertEqual(ent.consumed_units, Decimal("0.00"))

        # -------------------------------------------------------------
        # Step 3: First class booking is BLOCKED with 0 session changes
        # -------------------------------------------------------------
        with self.assertRaises(ParqRequiredValidationError) as ctx:
            BookingWaitlistAttendanceService.create_booking(
                user_profile=self.member_profile,
                occurrence=self.occurrence,
                booking_type='MEMBER',
                membership=membership,
                db_alias=DB
            )
        self.assertEqual(ctx.exception.code, 'PARQ_REQUIRED')

        # Verify zero bookings, reservations, or ledger deductions occurred
        self.assertEqual(Booking.objects.using(DB).filter(user_profile=self.member_profile).count(), 0)
        ent.refresh_from_db(using=DB)
        self.assertEqual(ent.consumed_units, Decimal("0.00"))
        self.assertEqual(ent.remaining_units, Decimal("10.00"))
        self.assertEqual(MembershipEntitlementLedger.objects.using(DB).filter(transaction_type='CONSUMPTION').count(), 0)

        # -------------------------------------------------------------
        # Step 4: Validation failures during signing are rejected
        # -------------------------------------------------------------
        factory = APIRequestFactory()
        view = MembershipViewSet.as_view({'get': 'parq_requirement', 'post': 'sign_parq'})

        # Helper to attach tenant db to request
        def make_req(req, user):
            req.user = user
            req._tenant_db_alias = DB
            req.organization = self.org
            force_authenticate(req, user=user)
            return req

        # 4a. Staff user cannot sign on member's behalf (HTTP 403)
        req_staff = make_req(factory.post(f"/api/v1/tenant/memberships/{membership.id}/sign-parq/", {
            'agreement_accepted': True,
            'signature_data': 'data:image/png;base64,' + 'A' * 200,
            'answers': [{'question_id': str(self.q1.id), 'value': False}, {'question_id': str(self.q2.id), 'value': False}]
        }, format='json'), self.staff_user)
        res_staff = view(req_staff, pk=str(membership.id))
        self.assertEqual(res_staff.status_code, status.HTTP_403_FORBIDDEN)

        # 4b. Another member cannot sign for this owner (HTTP 403)
        req_other = make_req(factory.post(f"/api/v1/tenant/memberships/{membership.id}/sign-parq/", {
            'agreement_accepted': True,
            'signature_data': 'data:image/png;base64,' + 'A' * 200,
            'answers': [{'question_id': str(self.q1.id), 'value': False}, {'question_id': str(self.q2.id), 'value': False}]
        }, format='json'), self.other_member_user)
        res_other = view(req_other, pk=str(membership.id))
        self.assertEqual(res_other.status_code, status.HTTP_403_FORBIDDEN)

        # 4c. Missing answer rejected (HTTP 400)
        req_missing = make_req(factory.post(f"/api/v1/tenant/memberships/{membership.id}/sign-parq/", {
            'agreement_accepted': True,
            'signature_data': 'data:image/png;base64,' + 'A' * 200,
            'answers': [{'question_id': str(self.q1.id), 'value': False}] # q2 missing
        }, format='json'), self.member_user)
        res_missing = view(req_missing, pk=str(membership.id))
        self.assertEqual(res_missing.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("required", str(res_missing.data))

        # 4d. Unchecked agreement consent rejected (HTTP 400)
        req_noconsent = make_req(factory.post(f"/api/v1/tenant/memberships/{membership.id}/sign-parq/", {
            'agreement_accepted': False,
            'signature_data': 'data:image/png;base64,' + 'A' * 200,
            'answers': [{'question_id': str(self.q1.id), 'value': False}, {'question_id': str(self.q2.id), 'value': False}]
        }, format='json'), self.member_user)
        res_noconsent = view(req_noconsent, pk=str(membership.id))
        self.assertEqual(res_noconsent.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("agreement", str(res_noconsent.data))

        # 4e. Blank or empty signature rejected (HTTP 400)
        req_nosig = make_req(factory.post(f"/api/v1/tenant/memberships/{membership.id}/sign-parq/", {
            'agreement_accepted': True,
            'signature_data': 'Jane Doe', # typed name, not drawn canvas
            'answers': [{'question_id': str(self.q1.id), 'value': False}, {'question_id': str(self.q2.id), 'value': False}]
        }, format='json'), self.member_user)
        res_nosig = view(req_nosig, pk=str(membership.id))
        self.assertEqual(res_nosig.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("signature", str(res_nosig.data).lower())

        # -------------------------------------------------------------
        # Step 5: Authenticated member signs and submits; answers & drawn signature saved
        # -------------------------------------------------------------
        valid_canvas_sig = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAJYAAABkCAYAAABg" + "A" * 150
        req_valid = make_req(factory.post(f"/api/v1/tenant/memberships/{membership.id}/sign-parq/", {
            'agreement_accepted': True,
            'signature_data': valid_canvas_sig,
            'answers': [
                {'question_id': str(self.q1.id), 'value': False}, # legitimate 'No' answer accepted!
                {'question_id': str(self.q2.id), 'value': False},
                {'question_id': str(self.q3.id), 'value': 'No dizziness'},
            ]
        }, format='json'), self.member_user)
        res_valid = view(req_valid, pk=str(membership.id))
        self.assertEqual(res_valid.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res_valid.data['parq_status'], 'COMPLETED')

        # Membership refresh confirms completed
        membership.refresh_from_db(using=DB)
        self.assertEqual(membership.parq_status, 'COMPLETED')
        self.assertIsNotNone(membership.parq_submission)
        self.assertEqual(membership.parq_submission.signature_data, valid_canvas_sig)
        self.assertEqual(membership.parq_submission.signer_identity, self.member_user.email)

        # -------------------------------------------------------------
        # Step 6: Member 360 displays exact completed record & signature
        # -------------------------------------------------------------
        m360_view = MemberViewSet.as_view({'get': 'member_360'}, permission_classes=[])
        req_360 = make_req(factory.get(f"/api/v1/tenant/members/{self.member_profile.id}/member_360/"), self.staff_user)
        res_360 = m360_view(req_360, pk=str(self.member_profile.id))
        self.assertEqual(res_360.status_code, status.HTTP_200_OK)

        parq_tab = res_360.data.get('par_q_form')
        self.assertIsNotNone(parq_tab)
        # Check requirements list has completed membership
        req_list = parq_tab.get('requirements', [])
        self.assertTrue(any(r['membership_id'] == str(membership.id) and r['parq_status'] == 'COMPLETED' for r in req_list))

        # Check submissions list has signature and answers
        sub_list = parq_tab.get('submissions', [])
        self.assertEqual(len(sub_list), 1)
        sub_record = sub_list[0]
        self.assertEqual(sub_record['signature_data'], valid_canvas_sig)
        self.assertEqual(sub_record['signer_identity'], self.member_user.email)
        self.assertEqual(len(sub_record['answers']), 3)

        # -------------------------------------------------------------
        # Step 7: Class booking succeeds afterward with proper session consumption
        # -------------------------------------------------------------
        booking = BookingWaitlistAttendanceService.create_booking(
            user_profile=self.member_profile,
            occurrence=self.occurrence,
            booking_type='MEMBER',
            membership=membership,
            db_alias=DB
        )
        self.assertEqual(booking.status, 'CONFIRMED')
        ent.refresh_from_db(using=DB)
        self.assertEqual(ent.consumed_units, Decimal("1.00"))
        self.assertEqual(ent.remaining_units, Decimal("9.00"))

        # -------------------------------------------------------------
        # Step 8 & 9: Renewal / new purchase creates a fresh PAR-Q requirement
        # Old signed submission must NOT satisfy new purchase!
        # -------------------------------------------------------------
        order2 = Order.objects.using(DB).create(
            branch=self.branch,
            user_profile=self.member_profile,
            order_number=f"ORD-{uuid.uuid4().hex[:8]}",
            order_type="PACKAGE",
            status="PAID",
            currency="INR"
        )
        order_item2 = OrderItem.objects.using(DB).create(
            order=order2,
            item_type="PACKAGE",
            package=self.package,
            package_version=self.pv,
            package_price=self.pp,
            unit_price_snapshot=Decimal("3000.00"),
            total_amount=Decimal("3000.00")
        )
        membership2 = MembershipLifecycleService.activate_membership_from_order(
            order=order2,
            order_item=order_item2,
            db_alias=DB,
            created_by_user=self.staff_user
        )
        # Even though member previously signed membership 1, membership 2 requires its OWN signature!
        self.assertEqual(membership2.parq_status, 'PENDING')
        self.assertIsNone(membership2.parq_submission)

        # Booking under membership2 is BLOCKED
        start_time2 = timezone.now() + timedelta(days=2)
        occ2 = ClassOccurrence.objects.using(DB).create(
            class_template=self.template,
            branch=self.branch,
            occurrence_date=start_time2.date(),
            start_at=start_time2,
            end_at=start_time2 + timedelta(minutes=60),
            capacity=15,
            trial_capacity=3,
            status="SCHEDULED"
        )
        with self.assertRaises(ParqRequiredValidationError):
            BookingWaitlistAttendanceService.create_booking(
                user_profile=self.member_profile,
                occurrence=occ2,
                booking_type='MEMBER',
                membership=membership2,
                db_alias=DB
            )

        # -------------------------------------------------------------
        # Step 11: Editing admin form does not alter historical submission snapshot
        # -------------------------------------------------------------
        self.q1.question_text = "MODIFIED: Have you ever had a heart attack?"
        self.q1.save(using=DB)
        self.parq_form.name = "MODIFIED FORM 2027"
        self.parq_form.save(using=DB)

        # Refresh Member 360: historical record must still display original question text!
        res_360_post_edit = m360_view(req_360, pk=str(self.member_profile.id))
        sub_historical = res_360_post_edit.data['par_q_form']['submissions'][0]
        self.assertEqual(sub_historical['form_title'], "PAR-Q General Form") # not "MODIFIED FORM 2027"
        q1_ans = [a for a in sub_historical['answers'] if a['question_id'] == str(self.q1.id)][0]
        self.assertEqual(q1_ans['question_text'], "Has your doctor ever said that you have a heart condition?") # unchanged!

        # -------------------------------------------------------------
        # Step 12: Duplicate payment callbacks / duplicate sign prevention
        # -------------------------------------------------------------
        # Duplicate activation returns existing without resetting
        existing_m2 = MembershipLifecycleService.activate_membership_from_order(
            order=order2,
            order_item=order_item2,
            db_alias=DB
        )
        self.assertEqual(existing_m2.id, membership2.id)

        # Duplicate submission on already signed membership is rejected
        req_dup = make_req(factory.post(f"/api/v1/tenant/memberships/{membership.id}/sign-parq/", {
            'agreement_accepted': True,
            'signature_data': valid_canvas_sig,
            'answers': [{'question_id': str(self.q1.id), 'value': False}]
        }, format='json'), self.member_user)
        res_dup = view(req_dup, pk=str(membership.id))
        self.assertEqual(res_dup.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already been completed", str(res_dup.data))

        print("\nAll 14 Verification Scenarios Passed Successfully!")

    def test_terms_acceptance_resolution_paths_and_isolation(self):
        """
        Regression Test for activate_membership_from_order terms acceptance resolution:
        1. Order-level acceptance exists -> recorded in contract_snapshot.terms_document_version_ids
        2. Fallback path (no order-level acceptance, but user_profile has prior acceptance) -> recorded
        3. Isolation: Unrelated users' acceptances are never selected
        4. Relationships: links correct member, order, accepted terms version, and contract snapshot
        """
        set_tenant_db_alias(DB)

        # 1. Setup Terms Document & Versions
        doc = TermsDocument.objects.using(DB).create(
            organization=self.org,
            code=f"TERMS-{uuid.uuid4().hex[:4]}",
            name="Studio Terms & Rules",
            document_type="MEMBERSHIP_TERMS",
            status="ACTIVE"
        )
        ver1 = TermsDocumentVersion.objects.using(DB).create(
            terms_document=doc,
            version_number=1,
            content_text="Terms version 1",
            effective_from=timezone.now(),
            status="ACTIVE",
            created_by_user=self.staff_user
        )
        ver2 = TermsDocumentVersion.objects.using(DB).create(
            terms_document=doc,
            version_number=2,
            content_text="Terms version 2",
            effective_from=timezone.now(),
            status="ACTIVE",
            created_by_user=self.staff_user
        )

        # Helper to create an order
        def create_test_order(profile):
            ord_obj = Order.objects.using(DB).create(
                branch=self.branch,
                user_profile=profile,
                order_number=f"ORD-{uuid.uuid4().hex[:8]}",
                order_type="PACKAGE",
                status="PAID",
                currency="INR"
            )
            item_obj = OrderItem.objects.using(DB).create(
                order=ord_obj,
                item_type="PACKAGE",
                package=self.package,
                package_version=self.pv,
                package_price=self.pp,
                unit_price_snapshot=Decimal("3000.00"),
                total_amount=Decimal("3000.00")
            )
            return ord_obj, item_obj

        # -------------------------------------------------------------
        # Scenario A: Order-level terms acceptance exists
        # -------------------------------------------------------------
        order_a, item_a = create_test_order(self.member_profile)
        TermsAcceptance.objects.using(DB).create(
            terms_document_version=ver1,
            order_id=order_a.id,
            user_profile=self.member_profile,
            accepted_via="WEB"
        )

        mem_a = MembershipLifecycleService.activate_membership_from_order(
            order=order_a,
            order_item=item_a,
            db_alias=DB,
            created_by_user=self.staff_user
        )
        self.assertIsNotNone(mem_a.contract_snapshot)
        self.assertEqual(mem_a.contract_snapshot.terms_document_version_ids, [str(ver1.id)])
        self.assertEqual(mem_a.user_profile.id, self.member_profile.id)
        self.assertEqual(mem_a.source_order.id, order_a.id)

        # -------------------------------------------------------------
        # Scenario B: Fallback path (no order-level acceptance, profile has acceptance)
        # Verify isolation: another unrelated member ALSO has a terms acceptance in DB!
        # -------------------------------------------------------------
        # Unrelated member accepts ver1
        TermsAcceptance.objects.using(DB).create(
            terms_document_version=ver1,
            order_id=None,
            user_profile=self.other_member_profile,
            accepted_via="WEB"
        )

        member_b_user = TenantUser.objects.using(DB).create(
            organization=self.org,
            email=f"member-b-{uuid.uuid4().hex[:6]}@test.com",
            first_name="Member",
            last_name="B",
            status="ACTIVE"
        )
        member_b_profile = UserProfile.objects.using(DB).create(
            user=member_b_user,
            member_number=f"MEM-B-{uuid.uuid4().hex[:4]}",
            first_name_snapshot="Member",
            last_name_snapshot="B",
            member_status="ACTIVE",
            preferred_branch=self.branch
        )

        # Member B accepts ver2 on their profile (e.g. at initial onboarding)
        TermsAcceptance.objects.using(DB).create(
            terms_document_version=ver2,
            order_id=None,
            user_profile=member_b_profile,
            accepted_via="WEB"
        )

        # Member B places order_b with NO order-level acceptance
        order_b, item_b = create_test_order(member_b_profile)

        mem_b = MembershipLifecycleService.activate_membership_from_order(
            order=order_b,
            order_item=item_b,
            db_alias=DB,
            created_by_user=self.staff_user
        )
        self.assertIsNotNone(mem_b.contract_snapshot)
        # Must resolve ver2 from member_b_profile, NOT ver1 from other_member_profile or member A!
        self.assertEqual(mem_b.contract_snapshot.terms_document_version_ids, [str(ver2.id)])
        self.assertNotIn(str(ver1.id), mem_b.contract_snapshot.terms_document_version_ids)
        self.assertEqual(mem_b.user_profile.id, member_b_profile.id)
        self.assertEqual(mem_b.source_order.id, order_b.id)

        # -------------------------------------------------------------
        # Scenario C: Brand new member with NO acceptances anywhere
        # -------------------------------------------------------------
        new_user = TenantUser.objects.using(DB).create(
            organization=self.org,
            email=f"brandnew-{uuid.uuid4().hex[:4]}@test.com",
            first_name="Brand",
            last_name="New",
            status="ACTIVE"
        )
        new_profile = UserProfile.objects.using(DB).create(
            user=new_user,
            member_number=f"MEM-BN-{uuid.uuid4().hex[:4]}",
            first_name_snapshot="Brand",
            last_name_snapshot="New",
            member_status="ACTIVE",
            preferred_branch=self.branch
        )
        order_c, item_c = create_test_order(new_profile)

        mem_c = MembershipLifecycleService.activate_membership_from_order(
            order=order_c,
            order_item=item_c,
            db_alias=DB,
            created_by_user=self.staff_user
        )
        self.assertIsNotNone(mem_c.contract_snapshot)
        # Must be empty list, and NOT contaminate with any other user's acceptances
        self.assertEqual(mem_c.contract_snapshot.terms_document_version_ids, [])
        self.assertEqual(mem_c.user_profile.id, new_profile.id)
        self.assertEqual(mem_c.source_order.id, order_c.id)
