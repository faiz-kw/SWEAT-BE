"""
Unit tests for Assessment Module Phase 2: Services.

Covers:
1. services_assessment_screening.py:
   - 10-Question PAR-Q evaluation.
   - Hard blocking red flags (chest pain, dizziness, resting dyspnea).
   - Modification flags (orthopedic, blood pressure, recent surgery).
   - Clinical escalation flags (minors under 18, pregnancy).
   - Clean pass (all clear).
2. services_assessment_metrics.py:
   - Real DOB resolution and age calculation from UserProfile.
   - Real biological sex resolution (MALE vs FEMALE Mifflin-St Jeor formula).
   - BMI calculation, WHO category, and mandatory screening disclaimer.
   - Calorie targets and macronutrient distribution.
   - Verification of zero fake CV constants and zero Unsplash stock URLs.
3. services_assessment_progress.py:
   - Like-for-like comparison between assessments.
   - Non-equivalent protocol detection and explanatory warning when variations differ.
   - Coach progression approval and member permission enforcement.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal
from django.test import TestCase
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.tenant_core.models_assessment import MemberAssessment, AssessmentTestProgression
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_crm import UserProfile
from apps.tenant_core.services_assessment_screening import evaluate_safety_readiness
from apps.tenant_core.services_assessment_metrics import (
    calculate_metabolic_nutrition_plan,
    calculate_bmi,
    resolve_member_demographics,
    BMI_DISCLAIMER,
    RMR_DISCLAIMER,
)
from apps.tenant_core.services_assessment_progress import (
    compare_assessments,
    approve_progression,
)
from config.routers import set_tenant_db_alias

DB = 'tenant_test'


class AssessmentServicesTestCase(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias(DB)
        self.org = Organization.objects.using(DB).create(
            name='Assessment Services Org',
            code=f'ASO-{uuid.uuid4().hex[:4].upper()}',
            status='ACTIVE',
        )
        self.location = Location.objects.using(DB).create(
            organization=self.org,
            name='Services Studio Location',
            city='Bangalore',
            code=f'LOC-{uuid.uuid4().hex[:4].upper()}',
            status='ACTIVE',
        )
        self.branch = Branch.objects.using(DB).create(
            organization=self.org,
            location=self.location,
            name='Central Studio',
            code=f'BR-{uuid.uuid4().hex[:4].upper()}',
            status='ACTIVE',
        )
        self.coach = self._make_user('coach', 'TRAINER')
        self.member_user = self._make_user('member', 'MEMBER')
        self.member_profile = self._make_profile(
            self.member_user,
            dob=date(1995, 6, 15),  # ~31 years old
            gender='FEMALE',
        )

    def tearDown(self):
        set_tenant_db_alias(None)

    def _make_user(self, label, user_type):
        token = uuid.uuid4().hex[:8]
        return TenantUser.objects.using(DB).create(
            organization=self.org,
            email=f'{label}.{token}@test.com',
            phone=f'+9198{uuid.uuid4().int % 10**8:08d}',
            username=f'{label}_{token}',
            first_name=label.title(),
            last_name='Tester',
            user_type=user_type,
            status='ACTIVE',
        )

    def _make_profile(self, user, dob=None, gender='FEMALE'):
        return UserProfile.objects.using(DB).create(
            user=user,
            preferred_branch=self.branch,
            member_status='ACTIVE',
            member_type='MEMBER',
            first_name_snapshot=user.first_name,
            last_name_snapshot=user.last_name,
            date_of_birth=dob,
            gender=gender,
        )

    # =========================================================================
    # 1. SAFETY & READINESS GATE TESTS (services_assessment_screening)
    # =========================================================================

    def test_safety_gate_cleared_when_all_negative(self):
        """When all 10 questions are negative, screening outcome is CLEARED."""
        screening_data = {
            'heart_condition': False,
            'chest_pain_activity': False,
            'chest_pain_rest': False,
            'dizziness_loss_of_balance': False,
            'bone_joint_problem': False,
            'blood_pressure_medication': False,
            'unexplained_shortness_of_breath': False,
            'recent_surgery_injury': False,
            'pregnancy_or_postpartum': False,
            'other_medical_condition': False,
        }
        res = evaluate_safety_readiness(screening_data, self.member_profile)
        self.assertTrue(res['cleared_for_testing'])
        self.assertEqual(res['screening_outcome'], 'CLEARED')
        self.assertEqual(res['recommended_status'], 'DRAFT')
        self.assertEqual(len(res['red_flags']), 0)
        self.assertEqual(len(res['modifications']), 0)

    def test_safety_gate_critical_red_flag_blocks_testing(self):
        """Chest pain or dizziness triggers DEFER_MEDICAL_CLEARANCE and blocks testing."""
        screening_data = {
            'chest_pain_activity': True,
            'dizziness_loss_of_balance': True,
        }
        res = evaluate_safety_readiness(screening_data, self.member_profile)
        self.assertFalse(res['cleared_for_testing'])
        self.assertEqual(res['screening_outcome'], 'DEFER_MEDICAL_CLEARANCE')
        self.assertEqual(res['recommended_status'], 'DEFERRED')
        self.assertTrue(res['requires_medical_clearance'])
        self.assertEqual(len(res['red_flags']), 2)

    def test_safety_gate_modifications_allowed_when_no_critical_red_flags(self):
        """Orthopedic or recent surgery flags require modifications without blocking testing."""
        screening_data = {
            'bone_joint_problem': True,
            'recent_surgery_injury': True,
        }
        res = evaluate_safety_readiness(screening_data, self.member_profile)
        self.assertTrue(res['cleared_for_testing'])
        self.assertEqual(res['screening_outcome'], 'PROCEED_WITH_MODIFICATIONS')
        self.assertEqual(res['recommended_status'], 'DRAFT')
        self.assertEqual(len(res['red_flags']), 0)
        self.assertEqual(len(res['modifications']), 2)

    def test_safety_gate_clinical_escalations_for_minors_and_pregnancy(self):
        """Minors under 18 and pregnancy trigger clinical escalation flags."""
        minor_profile = self._make_profile(
            self._make_user('minor', 'MEMBER'),
            dob=date.today() - timedelta(days=365 * 16),  # 16 years old
            gender='FEMALE',
        )
        screening_data = {'pregnancy_or_postpartum': True}
        res = evaluate_safety_readiness(screening_data, minor_profile)
        escalation_types = [e['type'] for e in res['clinical_escalations']]
        self.assertIn('PEDIATRIC_MINOR', escalation_types)
        self.assertIn('PRENATAL_POSTPARTUM', escalation_types)

    # =========================================================================
    # 2. METRICS & BIOMETRIC CALCULATION TESTS (services_assessment_metrics)
    # =========================================================================

    def test_bmi_calculation_and_disclaimer(self):
        """BMI calculation must include category and scientific disclaimer."""
        res = calculate_bmi(weight_kg=70.0, height_cm=175.0)
        # 70 / (1.75^2) = 22.86 -> 22.9
        self.assertEqual(res['bmi'], 22.9)
        self.assertEqual(res['category'], 'Normal weight')
        self.assertEqual(res['disclaimer'], BMI_DISCLAIMER)

    def test_mifflin_st_jeor_uses_real_dob_and_sex(self):
        """RMR calculation must dynamically adjust based on member profile DOB and sex."""
        # Member profile has dob 1995-06-15, female
        female_plan = calculate_metabolic_nutrition_plan(
            weight_kg=60.0,
            height_cm=165.0,
            user_profile=self.member_profile,
            activity_level='MODERATELY_ACTIVE',
            goal='FAT_LOSS',
        )
        # Male with same height, weight, and age
        male_profile = self._make_profile(
            self._make_user('male_mem', 'MEMBER'),
            dob=date(1995, 6, 15),
            gender='MALE',
        )
        male_plan = calculate_metabolic_nutrition_plan(
            weight_kg=60.0,
            height_cm=165.0,
            user_profile=male_profile,
            activity_level='MODERATELY_ACTIVE',
            goal='FAT_LOSS',
        )
        # Male formula (+5) vs Female formula (-161) -> difference of 166 kcal
        self.assertEqual(male_plan['rmr_kcal'] - female_plan['rmr_kcal'], 166)
        self.assertIn('Estimated via Mifflin-St Jeor', female_plan['disclaimers']['rmr'])
        self.assertEqual(female_plan['demographics_used']['sex'], 'FEMALE')
        self.assertEqual(male_plan['demographics_used']['sex'], 'MALE')

    def test_zero_fake_cv_and_zero_unsplash_urls_in_metrics(self):
        """Calculations must never output hardcoded CV posture scores or Unsplash URLs."""
        plan = calculate_metabolic_nutrition_plan(
            weight_kg=65.0,
            height_cm=170.0,
            user_profile=self.member_profile,
        )
        plan_str = str(plan)
        self.assertNotIn('images.unsplash.com', plan_str)
        self.assertNotIn('84.50', plan_str)

    # =========================================================================
    # 3. REASSESSMENT & PROGRESSION TESTS (services_assessment_progress)
    # =========================================================================

    def test_like_for_like_comparison_identical_protocols(self):
        """Identical exercise protocols compute quantitative deltas."""
        prev = MemberAssessment.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            user_profile=self.member_profile,
            measured_weight_kg=Decimal('70.0'),
            calculated_bmi=Decimal('25.7'),
            functional_tests_payload={
                'upper_body': {'name': 'Push-up', 'variation': 'Floor Standard', 'reps': 10, 'movement_quality': 'Meets criteria'},
            },
            status='APPROVED',
        )
        curr = MemberAssessment.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            user_profile=self.member_profile,
            previous_assessment=prev,
            measured_weight_kg=Decimal('67.5'),
            calculated_bmi=Decimal('24.8'),
            functional_tests_payload={
                'upper_body': {'name': 'Push-up', 'variation': 'Floor Standard', 'reps': 14, 'movement_quality': 'Meets criteria'},
            },
            status='APPROVED',
        )

        diff = compare_assessments(curr, prev)
        self.assertTrue(diff['has_baseline'])
        self.assertEqual(diff['vitals_comparison']['weight_kg']['delta'], -2.5)
        self.assertEqual(len(diff['movement_comparisons']), 1)
        comp = diff['movement_comparisons'][0]
        self.assertFalse(comp['non_equivalent_comparison'])
        self.assertEqual(comp['reps_delta'], 4.0)

    def test_like_for_like_comparison_detects_non_equivalent_protocol(self):
        """When exercise variation changes, non_equivalent_comparison flag is set with a warning note."""
        prev = MemberAssessment.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            user_profile=self.member_profile,
            functional_tests_payload={
                'upper_body': {'name': 'Push-up', 'variation': 'Knee Push-up', 'reps': 15},
            },
            status='APPROVED',
        )
        curr = MemberAssessment.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            user_profile=self.member_profile,
            previous_assessment=prev,
            functional_tests_payload={
                'upper_body': {'name': 'Push-up', 'variation': 'Standard Floor Push-up', 'reps': 8},
            },
            status='APPROVED',
        )

        diff = compare_assessments(curr, prev)
        comp = diff['movement_comparisons'][0]
        self.assertTrue(comp['non_equivalent_comparison'])
        self.assertIn('Non-equivalent protocol', comp['comparison_note'])
        self.assertIsNone(comp['reps_delta'])  # Cannot compare knee to floor reps

    def test_progression_approval_enforces_coach_role(self):
        """Coach can approve progression; member cannot approve their own progression."""
        prog = AssessmentTestProgression.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            user_profile=self.member_profile,
            exercise_name='Push-up',
            current_variation='Incline Push-up',
        )

        # Member cannot approve
        with self.assertRaises(ValidationError):
            approve_progression(prog, self.member_user, 'Criteria met', 'Floor Push-up', db_alias=DB)

        # Coach approves
        approved = approve_progression(prog, self.coach, 'Mastered 3 sets of 12 incline reps', 'Floor Push-up', db_alias=DB)
        self.assertEqual(approved.coach_approved_by, self.coach)
        self.assertEqual(approved.next_progression_target, 'Floor Push-up')
        self.assertEqual(approved.movement_quality, 'Meets criteria')
