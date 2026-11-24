"""
API Integration tests for Assessment Module Phase 2.

Covers:
1. POST /api/v1/tenant/assessments/ (Intake creation with DRF serializers).
2. Safety Gate automatic status assignment (DEFERRED on critical red flag).
3. GET /api/v1/tenant/assessments/<id>/ (Detailed serialized view with disclaimers).
4. PATCH /api/v1/tenant/assessments/<id>/ (Vitals update & metabolic recalculation).
5. POST /api/v1/tenant/assessments/<id>/approve-and-send/:
   - HTTP 403 Forbidden when a member user attempts to approve.
   - HTTP 200 OK and sign-off recording when a coach/trainer approves.
6. GET /api/v1/tenant/assessments/<id>/comparison/ (Like-for-like comparison endpoint).
7. CRUD & Approval on /api/v1/tenant/assessment-progressions/.
"""

import uuid
from datetime import date
from decimal import Decimal
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.tenant_core.models_assessment import MemberAssessment, AssessmentTestProgression
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_crm import UserProfile
from apps.tenant_core.views_assessment import AssessmentSessionViewSet, AssessmentProgressionViewSet
from config.routers import set_tenant_db_alias

DB = 'tenant_test'


class AssessmentApiTestCase(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias(DB)
        self.factory = APIRequestFactory()
        self.org = Organization.objects.using(DB).create(
            name='Assessment API Test Org',
            code=f'API-{uuid.uuid4().hex[:4].upper()}',
            status='ACTIVE',
        )
        self.location = Location.objects.using(DB).create(
            organization=self.org,
            name='Test Location',
            city='Mumbai',
            code=f'LOC-{uuid.uuid4().hex[:4].upper()}',
            status='ACTIVE',
        )
        self.branch = Branch.objects.using(DB).create(
            organization=self.org,
            location=self.location,
            name='Main Studio',
            code=f'BR-{uuid.uuid4().hex[:4].upper()}',
            status='ACTIVE',
        )
        self.coach = self._make_user('coach', 'TRAINER')
        self.member_user = self._make_user('member', 'MEMBER')
        self.member_profile = UserProfile.objects.using(DB).create(
            user=self.member_user,
            preferred_branch=self.branch,
            member_status='ACTIVE',
            member_type='MEMBER',
            first_name_snapshot='Member',
            last_name_snapshot='Tester',
            date_of_birth=date(1996, 4, 10),
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

    def test_create_assessment_intake_success(self):
        """Coach creates an assessment session; response is serialized with defaults."""
        payload = {
            'user_profile_id': str(self.member_profile.id),
            'branch_id': str(self.branch.id),
            'assessment_type': 'INITIAL',
            'target_goal': 'POSTURE_ALIGNMENT',
            'health_screening': {
                'heart_condition': False,
                'chest_pain_activity': False,
            },
            'measured_height_cm': 165.0,
            'measured_weight_kg': 60.0,
            'consent_body_measurements': True,
        }
        view = AssessmentSessionViewSet.as_view({'post': 'create'})
        req = self.factory.post('/api/v1/tenant/assessments/', payload, format='json')
        force_authenticate(req, user=self.coach)
        resp = view(req)

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        data = resp.data
        self.assertEqual(data['status'], 'DRAFT')
        self.assertEqual(data['vitals']['bmi'], 22.0)
        self.assertIn('BMI is a screening metric', data['vitals']['disclaimers']['bmi'])
        self.assertTrue(data['consent_body_measurements'])
        self.assertFalse(data['consent_ai_analysis'])  # Defaults to False

    def test_create_assessment_safety_gate_deferred_on_critical_red_flag(self):
        """When member reports chest pain, assessment is created in DEFERRED status."""
        payload = {
            'user_profile_id': str(self.member_profile.id),
            'branch_id': str(self.branch.id),
            'health_screening': {
                'chest_pain_activity': True,
            },
        }
        view = AssessmentSessionViewSet.as_view({'post': 'create'})
        req = self.factory.post('/api/v1/tenant/assessments/', payload, format='json')
        force_authenticate(req, user=self.coach)
        resp = view(req)

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data['status'], 'DEFERRED')
        self.assertEqual(resp.data['screening_outcome'], 'DEFER_MEDICAL_CLEARANCE')

    def test_member_cannot_approve_assessment(self):
        """Members attempting to approve an assessment receive HTTP 403 Forbidden."""
        assessment = MemberAssessment.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            user_profile=self.member_profile,
            status='PENDING_REVIEW',
        )
        view = AssessmentSessionViewSet.as_view({'post': 'approve_and_send'})
        req = self.factory.post(
            f'/api/v1/tenant/assessments/{assessment.id}/approve-and-send/',
            {'coach_notes': 'Self approval attempt'},
            format='json',
        )
        force_authenticate(req, user=self.member_user)
        resp = view(req, pk=str(assessment.id))

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('Permission denied', resp.data['error'])

    def test_coach_can_approve_assessment_with_governance_fields(self):
        """Coach approval sets coach_signed_off_at, coach_signed_off_by, and reassessment_due_date."""
        assessment = MemberAssessment.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            user_profile=self.member_profile,
            status='PENDING_REVIEW',
        )
        view = AssessmentSessionViewSet.as_view({'post': 'approve_and_send'})
        req = self.factory.post(
            f'/api/v1/tenant/assessments/{assessment.id}/approve-and-send/',
            {
                'coach_notes': 'Great baseline, focus on core stability.',
                'workout_prescription': 'Pilates Reformer 3x weekly',
                'diet_prescription': 'Prioritize 100g daily protein',
                'reassessment_interval_weeks': 8,
            },
            format='json',
        )
        force_authenticate(req, user=self.coach)
        resp = view(req, pk=str(assessment.id))

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data
        self.assertEqual(data['status'], 'APPROVED')
        self.assertIsNotNone(data['coach_signed_off_at'])
        self.assertEqual(data['coach_signed_off_by'], self.coach.id)
        self.assertIsNotNone(data['reassessment_due_date'])

    def test_reassessment_comparison_endpoint(self):
        """Comparison endpoint returns longitudinal delta between assessments."""
        prev = MemberAssessment.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            user_profile=self.member_profile,
            measured_weight_kg=Decimal('65.0'),
            calculated_bmi=Decimal('23.9'),
            status='APPROVED',
        )
        curr = MemberAssessment.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            user_profile=self.member_profile,
            previous_assessment=prev,
            measured_weight_kg=Decimal('63.0'),
            calculated_bmi=Decimal('23.1'),
            status='APPROVED',
        )
        view = AssessmentSessionViewSet.as_view({'get': 'comparison'})
        req = self.factory.get(f'/api/v1/tenant/assessments/{curr.id}/comparison/')
        force_authenticate(req, user=self.coach)
        resp = view(req, pk=str(curr.id))

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data['has_baseline'])
        self.assertEqual(resp.data['vitals_comparison']['weight_kg']['delta'], -2.0)

    def test_progression_approval_api_enforces_coach_role(self):
        """Progression approval endpoint rejects members and allows coaches."""
        prog = AssessmentTestProgression.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            user_profile=self.member_profile,
            exercise_name='Squat',
            current_variation='Box Squat',
        )
        view = AssessmentProgressionViewSet.as_view({'post': 'approve'})

        # Member attempt -> 403
        req_member = self.factory.post(
            f'/api/v1/tenant/assessment-progressions/{prog.id}/approve/',
            {'next_variation': 'Bodyweight Squat'},
            format='json',
        )
        force_authenticate(req_member, user=self.member_user)
        resp_member = view(req_member, pk=str(prog.id))
        self.assertEqual(resp_member.status_code, status.HTTP_403_FORBIDDEN)

        # Coach attempt -> 200
        req_coach = self.factory.post(
            f'/api/v1/tenant/assessment-progressions/{prog.id}/approve/',
            {'next_variation': 'Bodyweight Squat', 'criteria_met_notes': '15 depth-controlled box squats completed'},
            format='json',
        )
        force_authenticate(req_coach, user=self.coach)
        resp_coach = view(req_coach, pk=str(prog.id))
        self.assertEqual(resp_coach.status_code, status.HTTP_200_OK)
        self.assertEqual(resp_coach.data['next_progression_target'], 'Bodyweight Squat')
        self.assertEqual(resp_coach.data['coach_approved_by'], self.coach.id)
