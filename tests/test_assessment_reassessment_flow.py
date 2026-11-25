"""
End-to-End Functional & Integration Tests for Assessment Module Phase 4.

Covers:
1. Full end-to-end initial assessment -> like-for-like reassessment comparison.
   - Identical protocol variations produce exact numerical deltas.
   - Non-equivalent protocol progressions flag non_equivalent_comparison=True.
2. Safety-Stop Deferral flow:
   - PAR-Q red flags (chest pain, dizziness) trigger DEFERRED / CANCELLED status.
3. Member-Only Approved Visibility:
   - DRAFT, PENDING_REVIEW, and DEFERRED assessments are strictly hidden from members.
   - Once APPROVED by coach, the member sees only their own assessment record.
   - Cross-member access is strictly prevented.
4. Studio TV Pairing Mirror flow:
   - 6-digit pairing code redemption.
   - Unapproved assessment displays waiting state without consuming code.
   - Approved assessment reveals goal and matrix, hides private vitals/prescriptions,
     and consumes the code (single-use enforcement).
   - Expired or reused codes are rejected with HTTP 404.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.tenant_core.models_assessment import MemberAssessment, AssessmentTestProgression
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_crm import UserProfile
from apps.tenant_core.views_assessment import AssessmentSessionViewSet, StudioScreenPairingView
from config.routers import set_tenant_db_alias

DB = 'tenant_test'


class AssessmentReassessmentFlowTestCase(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias(DB)
        self.factory = APIRequestFactory()
        self.org = Organization.objects.using(DB).create(
            name='E2E Assessment Org',
            code=f'E2E-{uuid.uuid4().hex[:4].upper()}',
            status='ACTIVE',
        )
        self.location = Location.objects.using(DB).create(
            organization=self.org,
            name='Bandra Studio',
            city='Mumbai',
            code=f'LOC-{uuid.uuid4().hex[:4].upper()}',
            status='ACTIVE',
        )
        self.branch = Branch.objects.using(DB).create(
            organization=self.org,
            location=self.location,
            name='Bandra West',
            code=f'BR-{uuid.uuid4().hex[:4].upper()}',
            status='ACTIVE',
        )
        self.coach = self._make_user('lead_coach', 'TRAINER')
        self.member_user = self._make_user('jane_doe', 'MEMBER')
        self.other_member_user = self._make_user('john_smith', 'MEMBER')

        self.member_profile = UserProfile.objects.using(DB).create(
            user=self.member_user,
            preferred_branch=self.branch,
            member_status='ACTIVE',
            member_type='MEMBER',
            first_name_snapshot='Jane',
            last_name_snapshot='Doe',
            date_of_birth=date(1995, 6, 15),
            gender='FEMALE',
        )
        self.other_member_profile = UserProfile.objects.using(DB).create(
            user=self.other_member_user,
            preferred_branch=self.branch,
            member_status='ACTIVE',
            member_type='MEMBER',
            first_name_snapshot='John',
            last_name_snapshot='Smith',
            date_of_birth=date(1992, 3, 20),
            gender='MALE',
        )

    def tearDown(self):
        set_tenant_db_alias(None)

    def _make_user(self, label, user_type):
        token = uuid.uuid4().hex[:8]
        return TenantUser.objects.using(DB).create(
            organization=self.org,
            email=f'{label}.{token}@test.com',
            phone=f'+9197{uuid.uuid4().int % 10**8:08d}',
            username=f'{label}_{token}',
            first_name=label.title(),
            last_name='Tester',
            user_type=user_type,
            status='ACTIVE',
        )

    def test_e2e_reassessment_comparison_like_for_like_and_progression_warning(self):
        """
        Verify end-to-end longitudinal comparison:
        - Baseline assessment has Squat (Box Squat, 12 reps) and Push-Up (Incline Push-Up, 10 reps).
        - Reassessment has Squat (Box Squat, 16 reps -> +4 reps like-for-like)
          and Push-Up (Standard Floor Push-Up, 8 reps -> non-equivalent progression warning).
        """
        baseline = MemberAssessment.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            user_profile=self.member_profile,
            assessment_type='INITIAL',
            measured_weight_kg=Decimal('62.5'),
            waist_circumference_cm=Decimal('76.0'),
            resting_heart_rate_bpm=74,
            functional_tests_payload={
                'lower_body': {
                    'test_title': 'Squat Pattern',
                    'variation': 'Box Squat',
                    'reps': 12,
                    'movement_quality': 'Acceptable',
                },
                'upper_body': {
                    'test_title': 'Push Pattern',
                    'variation': 'Incline Push-Up',
                    'reps': 10,
                    'movement_quality': 'Good',
                },
            },
            status='APPROVED',
        )

        reassessment = MemberAssessment.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            user_profile=self.member_profile,
            previous_assessment=baseline,
            assessment_type='REASSESSMENT',
            measured_weight_kg=Decimal('60.5'),
            waist_circumference_cm=Decimal('73.5'),
            resting_heart_rate_bpm=68,
            functional_tests_payload={
                'lower_body': {
                    'test_title': 'Squat Pattern',
                    'variation': 'Box Squat',
                    'reps': 16,
                    'movement_quality': 'Pristine',
                },
                'upper_body': {
                    'test_title': 'Push Pattern',
                    'variation': 'Standard Floor Push-Up',
                    'reps': 8,
                    'movement_quality': 'Good',
                },
            },
            status='APPROVED',
        )

        view = AssessmentSessionViewSet.as_view({'get': 'comparison'})
        req = self.factory.get(f'/api/v1/tenant/assessments/{reassessment.id}/comparison/')
        force_authenticate(req, user=self.coach)
        resp = view(req, pk=str(reassessment.id))

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        comp = resp.data
        self.assertTrue(comp['has_baseline'])
        self.assertEqual(comp['previous_assessment_id'], str(baseline.id))

        # Vitals deltas: weight -2.0 kg, waist -2.5 cm, resting HR -6 bpm
        self.assertEqual(comp['vitals_comparison']['weight_kg']['delta'], -2.0)
        self.assertEqual(comp['vitals_comparison']['waist_cm']['delta'], -2.5)
        self.assertEqual(comp['vitals_comparison']['resting_heart_rate_bpm']['delta'], -6)

        # Movement tests comparison
        mov_comps = {m['domain']: m for m in comp['movement_comparisons']}

        # Lower body: Like-for-like comparison (both Box Squat)
        lower = mov_comps['lower_body']
        self.assertFalse(lower['non_equivalent_comparison'])
        self.assertEqual(lower['reps_delta'], 4)
        self.assertEqual(lower['current_reps'], 16)
        self.assertEqual(lower['previous_reps'], 12)

        # Upper body: Non-equivalent progression (Incline -> Standard Floor)
        upper = mov_comps['upper_body']
        self.assertTrue(upper['non_equivalent_comparison'])
        self.assertIsNone(upper['reps_delta'])  # Reps delta must be suppressed
        self.assertIn('Non-equivalent protocol', upper['comparison_note'])

    def test_safety_stop_deferral_workflow(self):
        """
        Verify that safety-stop conditions (chest pain) automatically set DEFERRED
        and record screening outcome.
        """
        payload = {
            'user_profile_id': str(self.member_profile.id),
            'branch_id': str(self.branch.id),
            'health_screening': {
                'heart_condition': False,
                'chest_pain_activity': True,  # Critical Red Flag
                'chest_pain_rest': False,
                'loss_of_consciousness': False,
            },
        }
        view = AssessmentSessionViewSet.as_view({'post': 'create'})
        req = self.factory.post('/api/v1/tenant/assessments/', payload, format='json')
        force_authenticate(req, user=self.coach)
        resp = view(req)

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data['status'], 'DEFERRED')
        self.assertEqual(resp.data['screening_outcome'], 'DEFER_MEDICAL_CLEARANCE')

    def test_member_only_approved_visibility_isolation(self):
        """
        Verify that:
        1. Member querying GET /assessments/ receives 0 results when assessment is DRAFT/PENDING_REVIEW/DEFERRED.
        2. Once APPROVED, the member sees their own assessment.
        3. Other members cannot see this assessment.
        """
        assessment = MemberAssessment.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            user_profile=self.member_profile,
            status='DRAFT',
            target_goal='FAT_LOSS',
        )

        view = AssessmentSessionViewSet.as_view({'get': 'list'})

        def _get_items(response):
            if isinstance(response.data, dict) and 'results' in response.data:
                return response.data['results']
            return response.data

        # Member sees empty list while assessment is DRAFT
        req1 = self.factory.get('/api/v1/tenant/assessments/')
        force_authenticate(req1, user=self.member_user)
        resp1 = view(req1)
        self.assertEqual(resp1.status_code, status.HTTP_200_OK)
        self.assertEqual(len(_get_items(resp1)), 0)

        # Transition status to PENDING_REVIEW
        assessment.status = 'PENDING_REVIEW'
        assessment.save(using=DB)

        req2 = self.factory.get('/api/v1/tenant/assessments/')
        force_authenticate(req2, user=self.member_user)
        resp2 = view(req2)
        self.assertEqual(len(_get_items(resp2)), 0)

        # Coach approves assessment
        approve_view = AssessmentSessionViewSet.as_view({'post': 'approve_and_send'})
        req_appr = self.factory.post(
            f'/api/v1/tenant/assessments/{assessment.id}/approve-and-send/',
            {'coach_notes': 'Verified and approved for onboarding.'},
            format='json',
        )
        force_authenticate(req_appr, user=self.coach)
        resp_appr = approve_view(req_appr, pk=str(assessment.id))
        self.assertEqual(resp_appr.status_code, status.HTTP_200_OK)

        # Now member sees their approved assessment
        req3 = self.factory.get('/api/v1/tenant/assessments/')
        force_authenticate(req3, user=self.member_user)
        resp3 = view(req3)
        self.assertEqual(resp3.status_code, status.HTTP_200_OK)
        items3 = _get_items(resp3)
        self.assertEqual(len(items3), 1)
        self.assertEqual(items3[0]['id'], str(assessment.id))
        self.assertEqual(items3[0]['status'], 'APPROVED')

        # Other member sees 0 results (strict isolation)
        req4 = self.factory.get('/api/v1/tenant/assessments/')
        force_authenticate(req4, user=self.other_member_user)
        resp4 = view(req4)
        self.assertEqual(resp4.status_code, status.HTTP_200_OK)
        self.assertEqual(len(_get_items(resp4)), 0)

    def test_studio_tv_pairing_flow_and_privacy(self):
        """
        Verify Studio TV Pairing mirror:
        1. Valid pairing code connects unapproved assessment in waiting state.
        2. Once approved, pairing code reveals summary without leaking private vitals/prescriptions.
        3. Code is single-use and cannot be redeemed again.
        4. Expired pairing code returns 404.
        """
        code = '987654'
        now = timezone.now()
        assessment = MemberAssessment.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            user_profile=self.member_profile,
            member_name='Jane Doe',
            pairing_code=code,
            pairing_code_expires_at=now + timedelta(hours=4),
            measured_weight_kg=Decimal('62.5'),
            measured_height_cm=Decimal('168.0'),
            resting_blood_pressure='120/80',
            workout_prescription='Strict PT 3x weekly',
            status='PENDING_REVIEW',
        )

        view = StudioScreenPairingView.as_view()

        # Step 1: Pair while unapproved -> connects but is_approved is False
        req1 = self.factory.post('/api/v1/tenant/assessments/pair/', {'code': code}, format='json')
        resp1 = view(req1)
        self.assertEqual(resp1.status_code, status.HTTP_200_OK)
        self.assertTrue(resp1.data['connected'])
        self.assertFalse(resp1.data['is_approved'])

        # Step 2: Approve the assessment
        assessment.status = 'APPROVED'
        assessment.save(using=DB)

        # Step 3: Pair again -> reveals approved summary
        req2 = self.factory.post('/api/v1/tenant/assessments/pair/', {'code': code}, format='json')
        resp2 = view(req2)
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        data = resp2.data
        self.assertTrue(data['is_approved'])
        self.assertEqual(data['member_name'], 'Jane Doe')

        # Privacy check: Private vitals and medical prescriptions MUST NOT be on the public TV display
        self.assertNotIn('measured_weight_kg', data)
        self.assertNotIn('measured_height_cm', data)
        self.assertNotIn('resting_blood_pressure', data)
        self.assertNotIn('workout_prescription', data)

        # Step 4: Code was consumed on approved reveal -> second attempt returns 404
        req3 = self.factory.post('/api/v1/tenant/assessments/pair/', {'code': code}, format='json')
        resp3 = view(req3)
        self.assertEqual(resp3.status_code, status.HTTP_404_NOT_FOUND)

    def test_expired_pairing_code_returns_404(self):
        """Expired pairing codes (> 4 hours old) are rejected."""
        code = '123456'
        now = timezone.now()
        MemberAssessment.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            user_profile=self.member_profile,
            pairing_code=code,
            pairing_code_expires_at=now - timedelta(minutes=10),  # Expired
            status='APPROVED',
        )

        view = StudioScreenPairingView.as_view()
        req = self.factory.post('/api/v1/tenant/assessments/pair/', {'code': code}, format='json')
        resp = view(req)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
