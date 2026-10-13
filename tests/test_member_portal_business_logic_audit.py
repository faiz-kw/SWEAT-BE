"""
backend/tests/test_member_portal_business_logic_audit.py

Regression Test Suite for SWEAT Member Portal Business Logic Audit:
- Issue 1: Real Branch Options & Tenant/Org Scoping (Exclusion of dormant seed 'Main Branch')
- Issue 2: Trainer Schedule Overlap Prevention (Same-branch, cross-branch, buffer, non-overlapping)
- Issue 3: PAR-Q Self-Service Resolution, Digital Signing & Health Clearance
- Issue 4: Package Eligibility, Credit Deductions, and Structured Ineligibility Rejection
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
from apps.tenant_core.models_crm import IntakeForm, IntakeQuestion, IntakeQuestionOption
from apps.tenant_core.models_catalog import Program
from apps.tenant_core.models_classes import (
    ClassCategory, ClassTemplate, ClassOccurrence, ClassOccurrenceTrainer,
    PackageClassAccessRule,
)
from apps.tenant_core.models_memberships import (
    Package, PackageVersion, Membership, MembershipEntitlement,
)
from apps.tenant_core.views_mobile import (
    MobileBranchesView, MobileScheduleView, MobileBookClassView,
)
from apps.tenant_core.views_memberships import MembershipViewSet
from apps.tenant_core.services_classes import ClassSchedulingService

DB = 'tenant_test'


class MemberPortalBusinessLogicAuditTests(TestCase):
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
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )

        # 2. Real Active Gym Organization
        self.org_real = Organization.objects.using(DB).create(
            name="SWEAT UAT Organization",
            code="SWEAT_UAT_ORG",
            status="ACTIVE",
        )
        self.location_real = Location.objects.using(DB).create(
            organization=self.org_real,
            name="Mumbai Western Suburbs",
            code="LOC-MUM-WEST",
            status="ACTIVE",
        )
        self.branch_andheri = Branch.objects.using(DB).create(
            organization=self.org_real,
            location=self.location_real,
            name="SWEAT Pilates & Studio",
            code="SWEAT_ANDHERI",
            status="ACTIVE",
        )
        self.branch_goregaon = Branch.objects.using(DB).create(
            organization=self.org_real,
            location=self.location_real,
            name="SWEAT Bootcamp & Pilates",
            code="SWEAT_GOREGAON",
            status="ACTIVE",
        )

        # 3. Legacy/Seed Organization with dormant "Main Branch"
        self.org_legacy = Organization.objects.using(DB).create(
            name="SWEAT International",
            code="ORG-SWEAT-INTL",
            status="ACTIVE",
        )
        self.location_legacy = Location.objects.using(DB).create(
            organization=self.org_legacy,
            name="Flagship Center",
            code="LOC-FLAGSHIP",
            status="ACTIVE",
        )
        self.branch_main = Branch.objects.using(DB).create(
            organization=self.org_legacy,
            location=self.location_legacy,
            name="Main Branch",
            code="",
            status="ACTIVE",
        )

        # 4. Member User
        self.member_user = TenantUser.objects.using(DB).create(
            email="siddharth.rao@tcs.com",
            first_name="Siddharth",
            last_name="Rao",
            home_branch=self.branch_andheri,
            organization=self.org_real,
            status="ACTIVE",
        )
        self.member_profile = UserProfile.objects.using(DB).create(
            user=self.member_user,
            preferred_branch=self.branch_andheri,
            member_status="ACTIVE",
        )

        # 5. Trainer User & Profile
        self.trainer_user = TenantUser.objects.using(DB).create(
            email="tom.trainer@sweat.com",
            first_name="Tom",
            last_name="Trainer",
            home_branch=self.branch_andheri,
            organization=self.org_real,
            status="ACTIVE",
        )
        self.trainer_uprofile = UserProfile.objects.using(DB).create(
            user=self.trainer_user,
            preferred_branch=self.branch_andheri,
        )
        self.emp_profile = EmployeeProfile.objects.using(DB).create(
            user_profile=self.trainer_uprofile,
            organization=self.org_real,
            employee_code="EMP-TOM01",
            employment_status="ACTIVE",
        )
        self.trainer_profile = TrainerProfile.objects.using(DB).create(
            employee_profile=self.emp_profile,
            trainer_code="TRN-TOM01",
            trainer_status="ACTIVE",
            minimum_schedule_buffer_minutes=15,
            can_teach_all_specialties=True,
        )

        # 6. Class Templates & Categories
        self.cat_pilates = ClassCategory.objects.using(DB).create(
            name="Reformer Pilates",
            code="PILATES",
            organization=self.org_real,
            status="ACTIVE",
        )
        self.cat_bootcamp = ClassCategory.objects.using(DB).create(
            name="Bootcamp HIIT",
            code="BOOTCAMP",
            organization=self.org_real,
            status="ACTIVE",
        )

        self.tpl_stretch = ClassTemplate.objects.using(DB).create(
            organization=self.org_real,
            category=self.cat_pilates,
            name="Sweat Stretch",
            code="TPL-STRETCH",
            default_duration_minutes=50,
            default_capacity=10,
            status="ACTIVE",
        )
        self.tpl_glutes = ClassTemplate.objects.using(DB).create(
            organization=self.org_real,
            category=self.cat_bootcamp,
            name="Sweat Glutes (Abs & Ass)",
            code="TPL-GLUTES",
            default_duration_minutes=50,
            default_capacity=10,
            status="ACTIVE",
        )

        # 7. Package and Version (Pilates Only)
        self.program = Program.objects.using(DB).create(
            organization=self.org_real,
            name="Pilates Program",
            code=f"PROG-{uuid.uuid4().hex[:4]}",
            status="ACTIVE",
        )
        self.package = Package.objects.using(DB).create(
            organization=self.org_real,
            program=self.program,
            name="Sweat Pilates - 1 Session Drop-In / Trial",
            code=f"PKG-{uuid.uuid4().hex[:4]}",
            status="ACTIVE",
        )
        self.package_version = PackageVersion.objects.using(DB).create(
            package=self.package,
            version_number=1,
            name_snapshot="Sweat Pilates v1",
            duration_value=30,
            duration_unit="DAYS",
            validity_days=30,
            effective_from=timezone.now(),
            created_by_user=self.member_user,
            status="ACTIVE",
        )
        # Access rule: Only Sweat Stretch is included
        PackageClassAccessRule.objects.using(DB).create(
            package_version=self.package_version,
            class_template=self.tpl_stretch,
            access_type="INCLUDED",
            status="ACTIVE",
        )

        # 8. Active Membership for Siddharth
        self.membership = Membership.objects.using(DB).create(
            user_profile=self.member_profile,
            program=self.program,
            package=self.package,
            package_version=self.package_version,
            purchase_branch=self.branch_andheri,
            home_branch=self.branch_andheri,
            status="ACTIVE",
            start_date=timezone.now().date(),
            end_date=timezone.now().date() + timedelta(days=14),
            membership_number="MEM-NUM-001",
            parq_status="PENDING",
        )
        self.entitlement = MembershipEntitlement.objects.using(DB).create(
            membership=self.membership,
            entitlement_type="CLASS_SESSION",
            allocated_units=Decimal("1.00"),
            consumed_units=Decimal("0.00"),
            status="ACTIVE",
            valid_from=timezone.now(),
            valid_until=timezone.now() + timedelta(days=14),
        )

    # -------------------------------------------------------------------------
    # ISSUE 1: BRANCH DROPDOWN
    # -------------------------------------------------------------------------
    def test_branch_dropdown_excludes_dormant_main_branch(self):
        """Branch API must return only active branches under the active organization."""
        req = self.factory.get('/api/v1/mobile/branches/', HTTP_X_TENANT_SLUG='sweat')
        force_authenticate(req, user=self.member_user)

        res = MobileBranchesView.as_view()(req)
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        branch_names = [b['name'] for b in res.data]
        self.assertIn("SWEAT Pilates & Studio", branch_names)
        self.assertIn("SWEAT Bootcamp & Pilates", branch_names)
        # 'Main Branch' must be excluded
        self.assertNotIn("Main Branch", branch_names)

    # -------------------------------------------------------------------------
    # ISSUE 2: TRAINER SCHEDULING OVERLAP
    # -------------------------------------------------------------------------
    def test_trainer_same_branch_overlap_prevented(self):
        """Prevent assigning a trainer to overlapping occurrences at the same branch."""
        now = timezone.now().replace(minute=0, second=0, microsecond=0) + timedelta(days=1)
        occ1 = ClassOccurrence.objects.using(DB).create(
            branch=self.branch_andheri,
            class_template=self.tpl_stretch,
            occurrence_date=now.date(),
            start_at=now,
            end_at=now + timedelta(minutes=50),
            status="OPEN",
            capacity=10,
        )
        occ2 = ClassOccurrence.objects.using(DB).create(
            branch=self.branch_andheri,
            class_template=self.tpl_stretch,
            occurrence_date=now.date(),
            start_at=now + timedelta(minutes=30),  # Overlaps occ1!
            end_at=now + timedelta(minutes=80),
            status="OPEN",
            capacity=10,
        )

        # First assignment succeeds
        ClassOccurrenceTrainer.objects.using(DB).create(
            occurrence=occ1,
            trainer_profile=self.trainer_profile,
            trainer_role="LEAD",
            status="CONFIRMED",
        )

        # Overlapping assignment must raise ValidationError
        with self.assertRaises(ValidationError):
            ClassOccurrenceTrainer.objects.using(DB).create(
                occurrence=occ2,
                trainer_profile=self.trainer_profile,
                trainer_role="LEAD",
                status="CONFIRMED",
            )

    def test_trainer_cross_branch_overlap_prevented(self):
        """Prevent assigning a trainer to overlapping occurrences at different branches."""
        now = timezone.now().replace(minute=0, second=0, microsecond=0) + timedelta(days=2)
        occ1 = ClassOccurrence.objects.using(DB).create(
            branch=self.branch_andheri,
            class_template=self.tpl_stretch,
            occurrence_date=now.date(),
            start_at=now,
            end_at=now + timedelta(minutes=50),
            status="OPEN",
            capacity=10,
        )
        occ_goregaon = ClassOccurrence.objects.using(DB).create(
            branch=self.branch_goregaon,  # Different location!
            class_template=self.tpl_glutes,
            occurrence_date=now.date(),
            start_at=now,  # Exact same time!
            end_at=now + timedelta(minutes=50),
            status="OPEN",
            capacity=10,
        )

        ClassOccurrenceTrainer.objects.using(DB).create(
            occurrence=occ1,
            trainer_profile=self.trainer_profile,
            trainer_role="LEAD",
            status="CONFIRMED",
        )

        with self.assertRaises(ValidationError):
            ClassOccurrenceTrainer.objects.using(DB).create(
                occurrence=occ_goregaon,
                trainer_profile=self.trainer_profile,
                trainer_role="LEAD",
                status="CONFIRMED",
            )

    def test_trainer_non_overlapping_allowed(self):
        """Non-overlapping occurrences respecting buffer time can be assigned cleanly."""
        now = timezone.now().replace(minute=0, second=0, microsecond=0) + timedelta(days=3)
        occ1 = ClassOccurrence.objects.using(DB).create(
            branch=self.branch_andheri,
            class_template=self.tpl_stretch,
            occurrence_date=now.date(),
            start_at=now,
            end_at=now + timedelta(minutes=50),
            status="OPEN",
            capacity=10,
        )
        # Class 2 starts 70 minutes later (50 min class + 20 min gap > 15 min buffer)
        occ2 = ClassOccurrence.objects.using(DB).create(
            branch=self.branch_andheri,
            class_template=self.tpl_stretch,
            occurrence_date=now.date(),
            start_at=now + timedelta(minutes=70),
            end_at=now + timedelta(minutes=120),
            status="OPEN",
            capacity=10,
        )

        a1 = ClassOccurrenceTrainer.objects.using(DB).create(
            occurrence=occ1,
            trainer_profile=self.trainer_profile,
            trainer_role="LEAD",
            status="CONFIRMED",
        )
        a2 = ClassOccurrenceTrainer.objects.using(DB).create(
            occurrence=occ2,
            trainer_profile=self.trainer_profile,
            trainer_role="LEAD",
            status="CONFIRMED",
        )
        self.assertIsNotNone(a1.id)
        self.assertIsNotNone(a2.id)

    # -------------------------------------------------------------------------
    # ISSUE 3: PAR-Q SELF-SERVICE RESOLUTION & SIGNING
    # -------------------------------------------------------------------------
    def test_parq_resolution_fallback_and_signing(self):
        """Authoritative PAR-Q form is resolved via fallback and can be digitally signed."""
        # Create an active tenant-level PAR-Q form under legacy org to test fallback
        form = IntakeForm.objects.using(DB).create(
            organization=self.org_legacy,
            name="SWEAT PAR-Q Form",
            form_type="PAR_Q",
            status="ACTIVE",
            is_default_for_all_programs=True,
            agreement_title="Exercise Readiness Agreement",
            agreement_text="I confirm I am physically fit to participate in studio training.",
            version_number=1,
        )
        q1 = IntakeQuestion.objects.using(DB).create(
            intake_form=form,
            question_text="Has your doctor ever told you that you have a heart condition?",
            question_type="BOOLEAN",
            display_order=1,
            is_required=True,
            status="ACTIVE",
        )

        # 1. Fetch requirement
        viewset = MembershipViewSet.as_view({'get': 'parq_requirement', 'post': 'sign_parq'})
        req_get = self.factory.get(f'/api/v1/tenant/memberships/{self.membership.id}/parq-requirement/', HTTP_X_TENANT_SLUG='sweat')
        force_authenticate(req_get, user=self.member_user)

        res_get = viewset(req_get, pk=str(self.membership.id))
        self.assertEqual(res_get.status_code, status.HTTP_200_OK)
        self.assertTrue(res_get.data['is_configured'])
        self.assertEqual(res_get.data['form']['id'], str(form.id))

        # 2. Digitally Sign PAR-Q
        sig_data = "data:image/png;base64," + "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==" * 5
        payload = {
            'agreement_accepted': True,
            'signature_data': sig_data,
            'answers': [
                {'question_id': str(q1.id), 'value': False},
            ],
        }
        req_post = self.factory.post(
            f'/api/v1/tenant/memberships/{self.membership.id}/sign-parq/',
            data=payload,
            format='json',
            HTTP_X_TENANT_SLUG='sweat',
        )
        force_authenticate(req_post, user=self.member_user)

        res_post = viewset(req_post, pk=str(self.membership.id))
        self.assertIn(res_post.status_code, [status.HTTP_200_OK, status.HTTP_201_CREATED])
        self.assertEqual(res_post.data['parq_status'], 'COMPLETED')

        # Verify DB status
        self.membership.refresh_from_db(using=DB)
        self.assertEqual(self.membership.parq_status, 'COMPLETED')
        self.assertIsNotNone(self.membership.parq_completed_at)

    # -------------------------------------------------------------------------
    # ISSUE 4: SCHEDULE PACKAGE ELIGIBILITY & REJECTION
    # -------------------------------------------------------------------------
    def test_schedule_package_eligibility_and_booking_rejection(self):
        """Schedule returns accurate is_included_in_plan, and ineligible bookings fail with 400."""
        target_date = timezone.now().date() + timedelta(days=4)
        occ_included = ClassOccurrence.objects.using(DB).create(
            branch=self.branch_andheri,
            class_template=self.tpl_stretch,
            occurrence_date=target_date,
            start_at=timezone.make_aware(datetime.combine(target_date, datetime.min.time())),
            end_at=timezone.make_aware(datetime.combine(target_date, datetime.min.time())) + timedelta(minutes=50),
            status="OPEN",
            capacity=10,
        )
        occ_excluded = ClassOccurrence.objects.using(DB).create(
            branch=self.branch_andheri,
            class_template=self.tpl_glutes,
            occurrence_date=target_date,
            start_at=timezone.make_aware(datetime.combine(target_date, datetime.min.time())) + timedelta(hours=2),
            end_at=timezone.make_aware(datetime.combine(target_date, datetime.min.time())) + timedelta(hours=2, minutes=50),
            status="OPEN",
            capacity=10,
        )

        # 1. Fetch Schedule
        req_sched = self.factory.get(f'/api/v1/mobile/schedule/?date={target_date.isoformat()}', HTTP_X_TENANT_SLUG='sweat')
        force_authenticate(req_sched, user=self.member_user)

        res_sched = MobileScheduleView.as_view()(req_sched)
        self.assertEqual(res_sched.status_code, status.HTTP_200_OK)
        slots = {s['class_name']: s for s in res_sched.data['slots']}

        self.assertTrue(slots['Sweat Stretch']['is_included_in_plan'])
        self.assertIsNone(slots['Sweat Stretch']['ineligible_reason'])

        self.assertFalse(slots['Sweat Glutes (Abs & Ass)']['is_included_in_plan'])
        self.assertIn("not included in package version 1", slots['Sweat Glutes (Abs & Ass)']['ineligible_reason'])

        # 2. Attempt Booking Ineligible Class -> Must return 400 with PACKAGE_CLASS_NOT_INCLUDED
        self.membership.parq_status = "COMPLETED"
        self.membership.save(using=DB, update_fields=['parq_status'])

        req_book_ineligible = self.factory.post(
            f'/api/v1/mobile/schedule/{occ_excluded.id}/book/',
            data={'occurrence_id': str(occ_excluded.id)},
            format='json',
            HTTP_X_TENANT_SLUG='sweat',
        )
        force_authenticate(req_book_ineligible, user=self.member_user)

        res_book_ineligible = MobileBookClassView.as_view()(req_book_ineligible, occurrence_id=str(occ_excluded.id))
        self.assertEqual(res_book_ineligible.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_book_ineligible.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_book_ineligible.data.get('code'), 'PACKAGE_CLASS_NOT_INCLUDED')

        # 3. Book Eligible Class -> Must succeed and deduct credit
        self.membership.parq_status = "COMPLETED"
        self.membership.save(using=DB, update_fields=['parq_status'])

        req_book_eligible = self.factory.post(
            f'/api/v1/mobile/schedule/{occ_included.id}/book/',
            data={'occurrence_id': str(occ_included.id)},
            format='json',
            HTTP_X_TENANT_SLUG='sweat',
        )
        force_authenticate(req_book_eligible, user=self.member_user)

        res_book_eligible = MobileBookClassView.as_view()(req_book_eligible, occurrence_id=str(occ_included.id))
        if res_book_eligible.status_code != 201: print('DEBUG BOOK ELIGIBLE ERROR:', res_book_eligible.data)
        self.assertEqual(res_book_eligible.status_code, status.HTTP_201_CREATED)

        # Verify entitlement consumed
        self.entitlement.refresh_from_db(using=DB)
        self.assertEqual(self.entitlement.consumed_units, Decimal("1.00"))

    # -------------------------------------------------------------------------
    # ISSUE 5: PAST & IN-PROGRESS OCCURRENCES BOOKING DISALLOWANCE
    # -------------------------------------------------------------------------
    def test_past_and_in_progress_classes_cannot_be_booked(self):
        """Past and in-progress occurrences must be unbookable on schedule and rejected on booking."""
        from apps.tenant_core.services_bookings import BookingWaitlistAttendanceService

        now = timezone.now()
        today = now.date()

        # 1. Past occurrence (Ended 1 hour ago)
        occ_past = ClassOccurrence.objects.using(DB).create(
            branch=self.branch_andheri,
            class_template=self.tpl_stretch,
            occurrence_date=today,
            start_at=now - timedelta(hours=2),
            end_at=now - timedelta(hours=1, minutes=10),
            status="OPEN",
            capacity=10,
        )

        # 2. In-progress occurrence (Started 20 min ago, ends in 30 min)
        occ_in_progress = ClassOccurrence.objects.using(DB).create(
            branch=self.branch_andheri,
            class_template=self.tpl_stretch,
            occurrence_date=today,
            start_at=now - timedelta(minutes=20),
            end_at=now + timedelta(minutes=30),
            status="OPEN",
            capacity=10,
        )

        # 3. Upcoming occurrence (Starts in 2 hours)
        occ_upcoming = ClassOccurrence.objects.using(DB).create(
            branch=self.branch_andheri,
            class_template=self.tpl_stretch,
            occurrence_date=today,
            start_at=now + timedelta(hours=2),
            end_at=now + timedelta(hours=2, minutes=50),
            status="OPEN",
            capacity=10,
        )

        # A. Verify MobileScheduleView flags
        req_sched = self.factory.get(f'/api/v1/mobile/schedule/?date={today.isoformat()}', HTTP_X_TENANT_SLUG='sweat')
        force_authenticate(req_sched, user=self.member_user)
        res_sched = MobileScheduleView.as_view()(req_sched)
        self.assertEqual(res_sched.status_code, status.HTTP_200_OK)

        slot_map = {s['id']: s for s in res_sched.data['slots']}

        # Past slot checks
        s_past = slot_map[str(occ_past.id)]
        self.assertTrue(s_past['has_ended'])
        self.assertTrue(s_past['has_started'])
        self.assertFalse(s_past['is_in_progress'])
        self.assertFalse(s_past['is_bookable'])
        self.assertFalse(s_past['can_book'])
        self.assertFalse(s_past['can_waitlist'])
        self.assertEqual(s_past['booking_state'], 'ENDED')

        # In-progress slot checks
        s_prog = slot_map[str(occ_in_progress.id)]
        self.assertFalse(s_prog['has_ended'])
        self.assertTrue(s_prog['has_started'])
        self.assertTrue(s_prog['is_in_progress'])
        self.assertFalse(s_prog['is_bookable'])
        self.assertFalse(s_prog['can_book'])
        self.assertFalse(s_prog['can_waitlist'])
        self.assertEqual(s_prog['booking_state'], 'IN_PROGRESS')

        # Upcoming slot checks
        s_up = slot_map[str(occ_upcoming.id)]
        self.assertFalse(s_up['has_ended'])
        self.assertFalse(s_up['has_started'])
        self.assertFalse(s_up['is_in_progress'])
        self.assertTrue(s_up['is_bookable'])
        self.assertTrue(s_up['can_book'])
        self.assertEqual(s_up['booking_state'], 'AVAILABLE')

        # B. Direct API booking attempt on Past Occurrence -> 400 CLASS_ALREADY_ENDED
        self.membership.parq_status = "COMPLETED"
        self.membership.save(using=DB, update_fields=['parq_status'])
        self.entitlement.allocated_units = Decimal("5.00")
        self.entitlement.consumed_units = Decimal("0.00")
        self.entitlement.save(using=DB, update_fields=['allocated_units', 'consumed_units'])

        req_book_past = self.factory.post(
            f'/api/v1/mobile/schedule/{occ_past.id}/book/',
            data={'occurrence_id': str(occ_past.id)},
            format='json',
            HTTP_X_TENANT_SLUG='sweat',
        )
        force_authenticate(req_book_past, user=self.member_user)
        res_book_past = MobileBookClassView.as_view()(req_book_past, occurrence_id=str(occ_past.id))
        self.assertEqual(res_book_past.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_book_past.data.get('code'), 'CLASS_ALREADY_ENDED')

        # C. Direct API booking attempt on In-Progress Occurrence -> 400 CLASS_ALREADY_STARTED
        req_book_prog = self.factory.post(
            f'/api/v1/mobile/schedule/{occ_in_progress.id}/book/',
            data={'occurrence_id': str(occ_in_progress.id)},
            format='json',
            HTTP_X_TENANT_SLUG='sweat',
        )
        force_authenticate(req_book_prog, user=self.member_user)
        res_book_prog = MobileBookClassView.as_view()(req_book_prog, occurrence_id=str(occ_in_progress.id))
        self.assertEqual(res_book_prog.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_book_prog.data.get('code'), 'CLASS_ALREADY_STARTED')

        # D. Backend service validation enforcement directly in BookingWaitlistAttendanceService
        with self.assertRaises(ValidationError) as cm_past:
            BookingWaitlistAttendanceService.create_booking(
                user_profile=self.member_profile,
                occurrence=occ_past,
                membership=self.membership,
                db_alias=DB,
            )
        self.assertIn("already ended", str(cm_past.exception))

        with self.assertRaises(ValidationError) as cm_prog:
            BookingWaitlistAttendanceService.create_booking(
                user_profile=self.member_profile,
                occurrence=occ_in_progress,
                membership=self.membership,
                db_alias=DB,
            )
        self.assertIn("already started", str(cm_prog.exception))

        # E. Upcoming slot booking succeeds
        req_book_up = self.factory.post(
            f'/api/v1/mobile/schedule/{occ_upcoming.id}/book/',
            data={'occurrence_id': str(occ_upcoming.id)},
            format='json',
            HTTP_X_TENANT_SLUG='sweat',
        )
        force_authenticate(req_book_up, user=self.member_user)
        res_book_up = MobileBookClassView.as_view()(req_book_up, occurrence_id=str(occ_upcoming.id))
        self.assertEqual(res_book_up.status_code, status.HTTP_201_CREATED)

    # -------------------------------------------------------------------------
    # ISSUE 6: CROSS-DAY MIDNIGHT TIMEZONE BOUNDARY SCHEDULE SCOPING
    # -------------------------------------------------------------------------
    def test_cross_day_timezone_boundary_schedule_scoping(self):
        """Schedule query must use studio-local calendar day window, correctly assigning midnight/1AM classes to their true local date."""
        import zoneinfo
        kolkata = zoneinfo.ZoneInfo('Asia/Kolkata')
        utc = zoneinfo.ZoneInfo('UTC')

        # Oct 13 11:00 PM IST = Oct 13 17:30 UTC
        start_13_night = datetime(2026, 10, 13, 17, 30, tzinfo=utc)
        occ_13_evening = ClassOccurrence.objects.using(DB).create(
            branch=self.branch_andheri,
            class_template=self.tpl_stretch,
            occurrence_date=date(2026, 10, 13),
            start_at=start_13_night,
            end_at=start_13_night + timedelta(minutes=50),
            status="OPEN",
            capacity=10,
        )

        # Oct 14 12:00 AM IST (Midnight) = Oct 13 18:30 UTC. Note DB occurrence_date stored as 2026-10-13 (UTC date)
        start_14_mid = datetime(2026, 10, 13, 18, 30, tzinfo=utc)
        occ_14_midnight = ClassOccurrence.objects.using(DB).create(
            branch=self.branch_andheri,
            class_template=self.tpl_stretch,
            occurrence_date=date(2026, 10, 13),  # Legacy/UTC date in DB
            start_at=start_14_mid,
            end_at=start_14_mid + timedelta(minutes=50),
            status="OPEN",
            capacity=10,
        )

        # Oct 14 01:00 AM IST = Oct 13 19:30 UTC
        start_14_1am = datetime(2026, 10, 13, 19, 30, tzinfo=utc)
        occ_14_1am = ClassOccurrence.objects.using(DB).create(
            branch=self.branch_andheri,
            class_template=self.tpl_stretch,
            occurrence_date=date(2026, 10, 13),  # Legacy/UTC date in DB
            start_at=start_14_1am,
            end_at=start_14_1am + timedelta(minutes=50),
            status="OPEN",
            capacity=10,
        )

        # Oct 14 12:30 PM IST = Oct 14 07:00 UTC
        start_14_noon = datetime(2026, 10, 14, 7, 0, tzinfo=utc)
        occ_14_noon = ClassOccurrence.objects.using(DB).create(
            branch=self.branch_andheri,
            class_template=self.tpl_stretch,
            occurrence_date=date(2026, 10, 14),
            start_at=start_14_noon,
            end_at=start_14_noon + timedelta(minutes=50),
            status="OPEN",
            capacity=10,
        )

        # 1. Fetch Schedule for 2026-10-13:
        # Must include 11:00 PM IST, and EXCLUDE 12:00 AM and 01:00 AM IST (which belong to Oct 14)
        req_13 = self.factory.get('/api/v1/mobile/schedule/?date=2026-10-13', HTTP_X_TENANT_SLUG='sweat')
        force_authenticate(req_13, user=self.member_user)
        res_13 = MobileScheduleView.as_view()(req_13)
        self.assertEqual(res_13.status_code, status.HTTP_200_OK)

        slot_ids_13 = {s['id'] for s in res_13.data['slots']}
        self.assertIn(str(occ_13_evening.id), slot_ids_13)
        self.assertNotIn(str(occ_14_midnight.id), slot_ids_13, "Midnight class on Oct 14 IST must NOT appear on Oct 13")
        self.assertNotIn(str(occ_14_1am.id), slot_ids_13, "1:00 AM class on Oct 14 IST must NOT appear on Oct 13")
        self.assertNotIn(str(occ_14_noon.id), slot_ids_13)

        # 2. Fetch Schedule for 2026-10-14:
        # Must INCLUDE 12:00 AM, 01:00 AM, and 12:30 PM IST, and EXCLUDE 11:00 PM on Oct 13
        req_14 = self.factory.get('/api/v1/mobile/schedule/?date=2026-10-14', HTTP_X_TENANT_SLUG='sweat')
        force_authenticate(req_14, user=self.member_user)
        res_14 = MobileScheduleView.as_view()(req_14)
        self.assertEqual(res_14.status_code, status.HTTP_200_OK)

        slot_ids_14 = {s['id'] for s in res_14.data['slots']}
        self.assertIn(str(occ_14_midnight.id), slot_ids_14, "Midnight class on Oct 14 IST must appear under Oct 14")
        self.assertIn(str(occ_14_1am.id), slot_ids_14, "1:00 AM class on Oct 14 IST must appear under Oct 14")
        self.assertIn(str(occ_14_noon.id), slot_ids_14)
        self.assertNotIn(str(occ_13_evening.id), slot_ids_14)

        # Verify serialized date matches the studio-local calendar date
        midnight_slot = next(s for s in res_14.data['slots'] if s['id'] == str(occ_14_midnight.id))
        self.assertEqual(midnight_slot['date'], '2026-10-14')
        self.assertTrue(midnight_slot['can_book'])
        self.assertEqual(midnight_slot['booking_state'], 'AVAILABLE')
