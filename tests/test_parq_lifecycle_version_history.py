"""
backend/tests/test_parq_lifecycle_version_history.py

Regression Test Suite for SWEAT Members Module:
- Section 1: Member Portal — Completed PAR-Q Must Be View-Only (No silent overwrites in normal view)
- Section 2: Package Purchase, Upgrade & Renewal — 3-Option PAR-Q Decision Flow:
    - Option A: Reuse Existing PAR-Q (Preserves original, zero duplicate submission created)
    - Option B: Edit Existing PAR-Q (Creates revised version with new signature, preserves old)
    - Option C: Fill Out a New PAR-Q (Creates independent submission)
- Section 3: PAR-Q Version History (Retains version, timestamps, signatures, answers, links)
- Section 4: Admin Portal Members 360 Presentation (Distinguishes latest and previous versions)
- Section 5: Data Integrity, Member/Tenant Isolation & Booking Eligibility Enforcement
"""

import uuid
from decimal import Decimal
from datetime import datetime, date, timedelta
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework import status
from django.core.exceptions import ValidationError

from config.tenant_middleware import get_tenant_db_alias, set_tenant_db_alias
from apps.master.models import Tenant, TenantDataSource
from apps.tenant_core.models_org import Organization, Branch, Location
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_workforce import EmployeeProfile, TrainerProfile, UserProfile
from apps.tenant_core.models_crm import IntakeForm, IntakeQuestion, IntakeQuestionOption, IntakeSubmission, IntakeAnswer
from apps.tenant_core.models_catalog import Program
from apps.tenant_core.models_classes import (
    ClassCategory, ClassTemplate, ClassOccurrence, PackageClassAccessRule,
)
from apps.tenant_core.models_memberships import (
    Package, PackageVersion, PackagePrice, Membership, MembershipEntitlement,
)
from apps.tenant_core.views_mobile import (
    MobileOnboardingSurveyView, MobileOnboardingSubmitView, MobileScheduleView, MobileBookClassView,
)
from apps.tenant_core.views_memberships import MembershipViewSet
from apps.tenant_core.views_members import MemberViewSet

DB = 'tenant_test'


class ParqLifecycleAndVersionHistoryTests(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias(DB)
        self.factory = APIRequestFactory()

        # 1. Master Tenant
        self.tenant = Tenant.objects.using('default').create(
            code=f"SWT-{uuid.uuid4().hex[:4]}",
            name="Sweat UAT Gym",
            slug="sweat",
            status="ACTIVE",
        )
        TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            db_name='test_fitness_tenant',
            database_name='test_fitness_tenant',
            )

        # 2. Organization & Branch
        self.org = Organization.objects.using(DB).create(
            name="SWEAT Health & Fitness",
            code=f"SWEAT-{uuid.uuid4().hex[:4]}",
            )
        self.location = Location.objects.using(DB).create(
            organization=self.org,
            name="Mumbai Hub",
            code=f"MUM-{uuid.uuid4().hex[:4]}",
            city="Mumbai",
            state="Maharashtra",
            country="IN",
        )
        self.branch = Branch.objects.using(DB).create(
            organization=self.org,
            location=self.location,
            name="SWEAT Pilates Studio (Bandra)",
            code=f"BND-{uuid.uuid4().hex[:4]}",
            timezone="Asia/Kolkata",
            status="ACTIVE",
        )

        # 3. Users: Member 1 & Member 2
        self.user1 = TenantUser.objects.using(DB).create(
            organization=self.org,
            email=f"member1.{uuid.uuid4().hex[:6]}@sweat.test",
            first_name="Aarav",
            last_name="Patel",
            home_branch=self.branch,
            status="ACTIVE",
        )
        self.profile1 = UserProfile.objects.using(DB).create(
            user=self.user1,
            preferred_branch=self.branch,
            member_status="ACTIVE",
        )

        self.user2 = TenantUser.objects.using(DB).create(
            organization=self.org,
            email=f"member2.{uuid.uuid4().hex[:6]}@sweat.test",
            first_name="Rohan",
            last_name="Sharma",
            home_branch=self.branch,
            status="ACTIVE",
        )
        self.profile2 = UserProfile.objects.using(DB).create(
            user=self.user2,
            preferred_branch=self.branch,
            member_status="ACTIVE",
        )

        # 4. Staff Admin User
        self.staff_user = TenantUser.objects.using(DB).create(
            organization=self.org,
            email=f"admin.{uuid.uuid4().hex[:6]}@sweat.test",
            first_name="Admin",
            last_name="Staff",
            home_branch=self.branch,
            status="ACTIVE",
        )

        # 5. Program & Package
        self.program = Program.objects.using(DB).create(
            organization=self.org,
            name="SWEAT Reformer",
            code=f"PROG-{uuid.uuid4().hex[:4]}",
            status="ACTIVE",
        )
        self.package = Package.objects.using(DB).create(
            organization=self.org,
            program=self.program,
            name="10 Sessions Reformer Pack",
            code=f"PKG-{uuid.uuid4().hex[:4]}",
            status="ACTIVE",
        )
        self.package_ver = PackageVersion.objects.using(DB).create(
            package=self.package,
            version_number=1,
            status="ACTIVE",
            duration_value=3,
            duration_unit="MONTH",
            validity_days=90,
            effective_from=timezone.now(),
            name_snapshot="SWEAT Reformer v1",
            created_by_user=self.staff_user,
        )

        # 6. IntakeForm (PAR-Q) & Questions
        self.parq_form = IntakeForm.objects.using(DB).create(
            organization=self.org,
            name="Physical Activity Readiness Questionnaire (PAR-Q)",
            form_type="PAR_Q",
            status="ACTIVE",
            is_default_for_all_programs=True,
            version_number=1,
            agreement_title="Exercise Readiness & Assumption of Risk",
            agreement_text="I certify that I am in good physical condition and have disclosed all relevant medical history.",
            is_required_for_purchase=True,
        )
        self.q_heart = IntakeQuestion.objects.using(DB).create(
            intake_form=self.parq_form,
            question_text="Has your doctor ever said that you have a heart condition?",
            question_type="BOOLEAN",
            category="MEDICAL",
            is_sensitive=True,
            is_required=True,
            display_order=1,
            status="ACTIVE",
        )
        self.q_chest = IntakeQuestion.objects.using(DB).create(
            intake_form=self.parq_form,
            question_text="Do you feel pain in your chest when you engage in physical activity?",
            question_type="BOOLEAN",
            category="MEDICAL",
            is_sensitive=True,
            is_required=True,
            display_order=2,
            status="ACTIVE",
        )

        # Valid digital signature dummy
        self.dummy_sig = "data:image/png;base64," + "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==" * 5

        # 7. Initial Membership for Member 1 (Pending PAR-Q)
        self.membership1 = Membership.objects.using(DB).create(
            user_profile=self.profile1,
            package=self.package,
            package_version=self.package_ver,
            program=self.program,
            home_branch=self.branch,
            purchase_branch=self.branch,
            membership_number=f"MEM-001-{uuid.uuid4().hex[:4]}",
            start_date=timezone.now().date(),
            end_date=timezone.now().date() + timedelta(days=90),
            status="ACTIVE",
            parq_status="PENDING",
            parq_form=self.parq_form,
        )

    # -------------------------------------------------------------------------
    # 1. MEMBER PORTAL: COMPLETED PAR-Q MUST BE VIEW-ONLY
    # -------------------------------------------------------------------------
    def test_completed_parq_is_view_only_and_rejects_silent_overwrites(self):
        """A completed PAR-Q returns view-only details and normal resubmissions are rejected."""
        # A. First-time submission succeeds
        payload_initial = {
            'signature_data': self.dummy_sig,
            'agreement_accepted': True,
            'answers': {
                str(self.q_heart.id): False,
                str(self.q_chest.id): False,
            },
        }
        req_sub1 = self.factory.post('/api/v1/mobile/onboarding/submit/', data=payload_initial, format='json', HTTP_X_TENANT_SLUG='sweat')
        force_authenticate(req_sub1, user=self.user1)
        res_sub1 = MobileOnboardingSubmitView.as_view()(req_sub1)
        self.assertEqual(res_sub1.status_code, status.HTTP_200_OK)
        self.assertTrue(res_sub1.data['is_cleared'])
        sub_id_1 = res_sub1.data['submission_id']

        # Verify submission record in DB
        sub_1 = IntakeSubmission.objects.using(DB).get(id=sub_id_1)
        self.assertEqual(sub_1.user_profile, self.profile1)
        self.assertEqual(sub_1.answers.count(), 2)

        # B. Member calls GET onboarding-survey -> must be marked completed with view-only details
        req_get = self.factory.get('/api/v1/mobile/onboarding-survey/', HTTP_X_TENANT_SLUG='sweat')
        force_authenticate(req_get, user=self.user1)
        res_get = MobileOnboardingSurveyView.as_view()(req_get)
        self.assertEqual(res_get.status_code, status.HTTP_200_OK)
        self.assertTrue(res_get.data['has_completed_parq'])
        self.assertTrue(res_get.data['is_cleared'])
        self.assertEqual(res_get.data['status_badge'], 'PAR-Q Cleared')
        self.assertIsNotNone(res_get.data['submission'])
        self.assertEqual(res_get.data['submission']['id'], str(sub_id_1))
        self.assertIn('submission_history', res_get.data)
        self.assertEqual(len(res_get.data['submission_history']), 1)

        # C. Normal resubmission without decision workflow must be blocked (prevent silent overwrite)
        payload_overwrite = {
            'signature_data': self.dummy_sig,
            'agreement_accepted': True,
            'answers': {
                str(self.q_heart.id): True,
            },
        }
        req_sub2 = self.factory.post('/api/v1/mobile/onboarding/submit/', data=payload_overwrite, format='json', HTTP_X_TENANT_SLUG='sweat')
        force_authenticate(req_sub2, user=self.user1)
        res_sub2 = MobileOnboardingSubmitView.as_view()(req_sub2)
        self.assertEqual(res_sub2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_sub2.data.get('code'), 'PARQ_ALREADY_COMPLETED')
        self.assertIn('already on file', res_sub2.data.get('detail', ''))

        # Original answers remain unchanged in DB
        sub_1.refresh_from_db(using=DB)
        ans_heart = sub_1.answers.get(question=self.q_heart)
        self.assertEqual(ans_heart.boolean_value, False)

    # -------------------------------------------------------------------------
    # 2. OPTION A: REUSE EXISTING PAR-Q
    # -------------------------------------------------------------------------
    def test_package_workflow_option_a_reuse_preserves_original_and_creates_zero_duplicates(self):
        """Option A (Reuse) links existing submission to new membership without duplicate creation."""
        # Complete initial PAR-Q for Member 1
        sub_orig = IntakeSubmission.objects.using(DB).create(
            intake_form=self.parq_form,
            user_profile=self.profile1,
            submitted_by_user=self.user1,
            agreement_accepted=True,
            agreement_accepted_at=timezone.now(),
            signature_data=self.dummy_sig,
            signature_date=timezone.now(),
            signer_identity="Aarav Patel",
            status="COMPLETED",
            submitted_at=timezone.now() - timedelta(days=10),
        )
        IntakeAnswer.objects.using(DB).create(submission=sub_orig, question=self.q_heart, boolean_value=False)
        IntakeAnswer.objects.using(DB).create(submission=sub_orig, question=self.q_chest, boolean_value=False)

        subs_count_before = IntakeSubmission.objects.using(DB).filter(user_profile=self.profile1).count()
        self.assertEqual(subs_count_before, 1)

        # Member 1 signs membership with Option A: REUSE
        viewset = MembershipViewSet.as_view({'post': 'sign_parq', 'get': 'parq_requirement'})
        payload_reuse = {
            'parq_decision': 'REUSE',
            'existing_submission_id': str(sub_orig.id),
        }
        req_reuse = self.factory.post(
            f'/api/v1/tenant/memberships/{self.membership1.id}/sign-parq/',
            data=payload_reuse,
            format='json',
            HTTP_X_TENANT_SLUG='sweat',
        )
        force_authenticate(req_reuse, user=self.user1)
        res_reuse = viewset(req_reuse, pk=str(self.membership1.id))

        self.assertEqual(res_reuse.status_code, status.HTTP_200_OK)
        self.assertTrue(res_reuse.data.get('is_reused'))
        self.assertEqual(res_reuse.data.get('submission_id'), str(sub_orig.id))

        # DB Assertions:
        # 1. Total submissions in DB did NOT increase (Zero duplicate created)
        subs_count_after = IntakeSubmission.objects.using(DB).filter(user_profile=self.profile1).count()
        self.assertEqual(subs_count_after, 1)

        # 2. Membership is COMPLETED and references original submission
        self.membership1.refresh_from_db(using=DB)
        self.assertEqual(self.membership1.parq_status, 'COMPLETED')
        self.assertEqual(self.membership1.parq_submission, sub_orig)
        self.assertEqual(self.membership1.parq_completed_at, sub_orig.submitted_at)

    # -------------------------------------------------------------------------
    # 3. OPTION B: EDIT EXISTING PAR-Q (CREATES REVISED VERSION WITH NEW SIGNATURE)
    # -------------------------------------------------------------------------
    def test_package_workflow_option_b_edit_creates_audited_revision(self):
        """Option B creates a new version with new signature while permanently preserving old."""
        # Initial submission on record
        old_time = timezone.now() - timedelta(days=30)
        sub_old = IntakeSubmission.objects.using(DB).create(
            intake_form=self.parq_form,
            user_profile=self.profile1,
            submitted_by_user=self.user1,
            agreement_accepted=True,
            agreement_accepted_at=old_time,
            signature_data="data:image/png;base64,OLD_SIGNATURE_HASH_XYZ" * 5,
            signature_date=old_time,
            signer_identity="Aarav Patel",
            status="COMPLETED",
            submitted_at=old_time,
        )
        IntakeAnswer.objects.using(DB).create(submission=sub_old, question=self.q_heart, boolean_value=False)
        IntakeAnswer.objects.using(DB).create(submission=sub_old, question=self.q_chest, boolean_value=False)

        # Member 1 executes Option B: EDIT with a newly drawn signature
        new_sig = "data:image/png;base64,NEWLY_DRAWN_SIGNATURE_ABC" * 5
        payload_edit = {
            'parq_decision': 'EDIT',
            'signature_data': new_sig,
            'agreement_accepted': True,
            'answers': [
                {'question_id': str(self.q_heart.id), 'value': False},
                {'question_id': str(self.q_chest.id), 'value': True},  # Updated response
            ],
        }
        viewset = MembershipViewSet.as_view({'post': 'sign_parq'})
        req_edit = self.factory.post(
            f'/api/v1/tenant/memberships/{self.membership1.id}/sign-parq/',
            data=payload_edit,
            format='json',
            HTTP_X_TENANT_SLUG='sweat',
        )
        force_authenticate(req_edit, user=self.user1)
        res_edit = viewset(req_edit, pk=str(self.membership1.id))

        self.assertIn(res_edit.status_code, [status.HTTP_200_OK, status.HTTP_201_CREATED])
        new_sub_id = res_edit.data['submission_id']
        self.assertNotEqual(new_sub_id, str(sub_old.id))

        # DB Assertions:
        # 1. Total submissions is now 2
        all_subs = IntakeSubmission.objects.using(DB).filter(user_profile=self.profile1).order_by('-submitted_at')
        self.assertEqual(all_subs.count(), 2)

        # 2. Previous submission is permanently preserved and unchanged
        sub_old.refresh_from_db(using=DB)
        self.assertEqual(sub_old.answers.get(question=self.q_chest).boolean_value, False)
        self.assertIn("OLD_SIGNATURE", sub_old.signature_data)

        # 3. New submission has updated answer and new signature
        sub_new = IntakeSubmission.objects.using(DB).get(id=new_sub_id)
        self.assertEqual(sub_new.answers.get(question=self.q_chest).boolean_value, True)
        self.assertIn("NEWLY_DRAWN_SIGNATURE", sub_new.signature_data)
        self.assertTrue(sub_new.metadata.get('is_edit'))
        self.assertEqual(sub_new.metadata.get('submission_type'), 'REVISION')

    # -------------------------------------------------------------------------
    # 4. OPTION C: FILL OUT A NEW PAR-Q
    # -------------------------------------------------------------------------
    def test_package_workflow_option_c_new_independent_submission(self):
        """Option C creates an independent submission without linking as an edit revision."""
        # Initial submission
        sub_old = IntakeSubmission.objects.using(DB).create(
            intake_form=self.parq_form,
            user_profile=self.profile1,
            submitted_by_user=self.user1,
            agreement_accepted=True,
            agreement_accepted_at=timezone.now(),
            signature_data=self.dummy_sig,
            status="COMPLETED",
            submitted_at=timezone.now() - timedelta(days=40),
        )

        payload_new = {
            'parq_decision': 'NEW',
            'signature_data': self.dummy_sig,
            'agreement_accepted': True,
            'answers': [
                {'question_id': str(self.q_heart.id), 'value': False},
                {'question_id': str(self.q_chest.id), 'value': False},
            ],
        }
        viewset = MembershipViewSet.as_view({'post': 'sign_parq'})
        req_new = self.factory.post(
            f'/api/v1/tenant/memberships/{self.membership1.id}/sign-parq/',
            data=payload_new,
            format='json',
            HTTP_X_TENANT_SLUG='sweat',
        )
        force_authenticate(req_new, user=self.user1)
        res_new = viewset(req_new, pk=str(self.membership1.id))

        self.assertIn(res_new.status_code, [status.HTTP_200_OK, status.HTTP_201_CREATED])
        sub_created = IntakeSubmission.objects.using(DB).get(id=res_new.data['submission_id'])
        self.assertFalse(sub_created.metadata.get('is_edit'))
        self.assertEqual(sub_created.metadata.get('submission_type'), 'NEW_ASSESSMENT')

    # -------------------------------------------------------------------------
    # 5. ADMIN PORTAL: MEMBERS 360 PAR-Q PRESENTATION
    # -------------------------------------------------------------------------
    def test_members_360_parq_presentation_latest_and_history(self):
        """Members 360 displays latest submission and preserves historical versions with zero bleed-through."""
        # Create Version 1
        time1 = timezone.now() - timedelta(days=60)
        sub1 = IntakeSubmission.objects.using(DB).create(
            intake_form=self.parq_form,
            user_profile=self.profile1,
            submitted_by_user=self.user1,
            agreement_accepted=True,
            signature_data="data:image/png;base64,SIG_VERSION_1" * 5,
            signer_identity="Aarav Patel",
            status="COMPLETED",
            submitted_at=time1,
        )
        IntakeAnswer.objects.using(DB).create(submission=sub1, question=self.q_heart, boolean_value=False, text_value="No")
        IntakeAnswer.objects.using(DB).create(submission=sub1, question=self.q_chest, boolean_value=False, text_value="No")

        # Create Version 2 (Revision)
        time2 = timezone.now() - timedelta(days=10)
        sub2 = IntakeSubmission.objects.using(DB).create(
            intake_form=self.parq_form,
            user_profile=self.profile1,
            submitted_by_user=self.user1,
            agreement_accepted=True,
            signature_data="data:image/png;base64,SIG_VERSION_2" * 5,
            signer_identity="Aarav Patel",
            status="COMPLETED",
            submitted_at=time2,
            metadata={'is_edit': True, 'revision_number': 2, 'previous_submission_id': str(sub1.id)},
        )
        IntakeAnswer.objects.using(DB).create(submission=sub2, question=self.q_heart, boolean_value=True, text_value="Yes")  # Different answer
        IntakeAnswer.objects.using(DB).create(submission=sub2, question=self.q_chest, boolean_value=False, text_value="No")

        # Link membership to sub1 (Reuse scenario)
        self.membership1.parq_submission = sub1
        self.membership1.parq_status = "COMPLETED"
        self.membership1.save(using=DB, update_fields=['parq_submission', 'parq_status'])

        # Staff admin requests Member 360
        self.staff_user.is_staff = True
        viewset = MemberViewSet.as_view({'get': 'member_360'}, permission_classes=[])
        req_admin = self.factory.get(f'/api/v1/tenant/members/{self.profile1.id}/', HTTP_X_TENANT_SLUG='sweat')
        req_admin.user = self.staff_user
        req_admin._tenant_db_alias = DB
        req_admin.organization = self.org
        force_authenticate(req_admin, user=self.staff_user)
        res_admin = viewset(req_admin, pk=str(self.profile1.id))

        self.assertEqual(res_admin.status_code, status.HTTP_200_OK)
        parq_data = res_admin.data['par_q_form']
        subs_list = parq_data['submissions']
        self.assertEqual(len(subs_list), 2)

        # Latest submission checks (sub2)
        latest_sub = subs_list[0]
        self.assertEqual(latest_sub['id'], str(sub2.id))
        self.assertTrue(latest_sub['is_latest'])
        ans_map_latest = {a['question_id']: a['boolean_value'] for a in latest_sub['answers']}
        self.assertEqual(ans_map_latest[str(self.q_heart.id)], True)
        self.assertIn("SIG_VERSION_2", latest_sub['signature_data'])

        # Historical submission checks (sub1) - zero answer bleed-through
        older_sub = subs_list[1]
        self.assertEqual(older_sub['id'], str(sub1.id))
        self.assertFalse(older_sub['is_latest'])
        ans_map_older = {a['question_id']: a['boolean_value'] for a in older_sub['answers']}
        self.assertEqual(ans_map_older[str(self.q_heart.id)], False)  # Retains original False
        self.assertIn("SIG_VERSION_1", older_sub['signature_data'])

        # Verify linked_memberships shows membership1 on older_sub
        self.assertIn(self.membership1.membership_number, older_sub['linked_memberships'])

    # -------------------------------------------------------------------------
    # 6. MEMBER & TENANT ISOLATION
    # -------------------------------------------------------------------------
    def test_member_and_tenant_isolation(self):
        """Member 2 cannot access Member 1's requirement or reuse Member 1's submission."""
        sub1 = IntakeSubmission.objects.using(DB).create(
            intake_form=self.parq_form,
            user_profile=self.profile1,
            submitted_by_user=self.user1,
            agreement_accepted=True,
            signature_data=self.dummy_sig,
            status="COMPLETED",
            submitted_at=timezone.now(),
        )

        viewset = MembershipViewSet.as_view({'get': 'parq_requirement', 'post': 'sign_parq'})

        # Member 2 tries to GET Member 1's requirement -> 403 Forbidden
        req_get = self.factory.get(f'/api/v1/tenant/memberships/{self.membership1.id}/parq-requirement/', HTTP_X_TENANT_SLUG='sweat')
        force_authenticate(req_get, user=self.user2)
        res_get = viewset(req_get, pk=str(self.membership1.id))
        self.assertEqual(res_get.status_code, status.HTTP_403_FORBIDDEN)

        # Member 2 tries to reuse Member 1's submission -> fails / blocked
        mem2 = Membership.objects.using(DB).create(
            user_profile=self.profile2,
            package=self.package,
            package_version=self.package_ver,
            program=self.program,
            home_branch=self.branch,
            purchase_branch=self.branch,
            membership_number=f"MEM-002-{uuid.uuid4().hex[:4]}",
            start_date=timezone.now().date(),
            end_date=timezone.now().date() + timedelta(days=90),
            status="ACTIVE",
            parq_status="PENDING",
            parq_form=self.parq_form,
        )
        req_reuse = self.factory.post(
            f'/api/v1/tenant/memberships/{mem2.id}/sign-parq/',
            data={'parq_decision': 'REUSE', 'existing_submission_id': str(sub1.id)},
            format='json',
            HTTP_X_TENANT_SLUG='sweat',
        )
        force_authenticate(req_reuse, user=self.user2)
        res_reuse = viewset(req_reuse, pk=str(mem2.id))
        self.assertEqual(res_reuse.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("No existing completed PAR-Q submission found", res_reuse.data.get('detail', ''))

    # -------------------------------------------------------------------------
    # 7. BOOKING RESTRICTION PRESERVED
    # -------------------------------------------------------------------------
    def test_booking_gate_restriction_preserved(self):
        """Classes cannot be booked when PAR-Q is pending, but can be booked once cleared."""
        # Create an upcoming class slot
        cat = ClassCategory.objects.using(DB).create(organization=self.org, name="Reformer", code=f"REF-{uuid.uuid4().hex[:4]}")
        tpl = ClassTemplate.objects.using(DB).create(organization=self.org, category=cat, name="Sweat Reformer", default_capacity=10, default_duration_minutes=50)
        PackageClassAccessRule.objects.using(DB).create(package_version=self.package_ver, class_category=cat, access_type="INCLUDED", status="ACTIVE")

        future_dt = timezone.now() + timedelta(days=3)
        occ = ClassOccurrence.objects.using(DB).create(
            branch=self.branch,
            class_template=tpl,
            occurrence_date=future_dt.date(),
            start_at=future_dt,
            end_at=future_dt + timedelta(minutes=50),
            status="OPEN",
            capacity=10,
        )

        # Entitlement with 5 credits
        MembershipEntitlement.objects.using(DB).create(
            membership=self.membership1,
            entitlement_type="CLASS_CREDIT",
            allocated_units=Decimal("5.00"),
            consumed_units=Decimal("0.00"),
            valid_from=timezone.now(),
            valid_until=timezone.now() + timedelta(days=90),
            status="ACTIVE",
        )

        # While membership1 parq_status is PENDING -> booking blocked
        self.assertEqual(self.membership1.parq_status, 'PENDING')
        req_book = self.factory.post(f'/api/v1/mobile/schedule/{occ.id}/book/', data={'occurrence_id': str(occ.id)}, format='json', HTTP_X_TENANT_SLUG='sweat')
        force_authenticate(req_book, user=self.user1)
        res_book = MobileBookClassView.as_view()(req_book, occurrence_id=str(occ.id))
        self.assertEqual(res_book.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_book.data.get('code'), 'PARQ_REQUIRED')

        # Once membership1 parq_status is COMPLETED -> booking succeeds
        self.membership1.parq_status = 'COMPLETED'
        self.membership1.save(using=DB, update_fields=['parq_status'])

        req_book_ok = self.factory.post(f'/api/v1/mobile/schedule/{occ.id}/book/', data={'occurrence_id': str(occ.id)}, format='json', HTTP_X_TENANT_SLUG='sweat')
        force_authenticate(req_book_ok, user=self.user1)
        res_book_ok = MobileBookClassView.as_view()(req_book_ok, occurrence_id=str(occ.id))
        self.assertEqual(res_book_ok.status_code, status.HTTP_201_CREATED)
