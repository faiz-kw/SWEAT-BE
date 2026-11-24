"""
Unit tests for Assessment Module Phase 1: Models, Status Lifecycle, Consents, and Progressions.

Covers:
1. MemberAssessment status choices and transitions including DEFERRED and CANCELLED.
2. Consent fields defaulting to False (opt-in compliance).
3. Sign-off and governance fields (coach_signed_off_by, coach_signed_off_at, member_acknowledged_at, consent_notice_version, reassessment_due_date).
4. Self-referential previous_assessment relationship for like-for-like tracking.
5. Decoupled symptoms_and_safety_events persistence.
6. AssessmentTestProgression model creation, relationships, and queries.
"""
import uuid
from decimal import Decimal
from datetime import date, timedelta
from django.test import TestCase
from django.utils import timezone

from apps.tenant_core.models_assessment import MemberAssessment, AssessmentTestProgression
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_workforce import UserProfile
from config.routers import set_tenant_db_alias

DB = 'tenant_test'


class AssessmentModelsTestCase(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias(DB)
        self.org = Organization.objects.using(DB).create(
            name='Assessment Model Test Org',
            code=f'AMO-{uuid.uuid4().hex[:4].upper()}',
            status='ACTIVE',
        )
        self.location = Location.objects.using(DB).create(
            organization=self.org,
            name='Test City Location',
            city='Mumbai',
            code=f'LOC-{uuid.uuid4().hex[:4].upper()}',
            status='ACTIVE',
        )
        self.branch = Branch.objects.using(DB).create(
            organization=self.org,
            location=self.location,
            name='Main Studio Branch',
            code=f'BR-{uuid.uuid4().hex[:4].upper()}',
            status='ACTIVE',
        )
        self.coach = self._make_user('coach', 'TRAINER')
        self.member_user = self._make_user('member', 'MEMBER')
        self.member_profile = self._make_profile(self.member_user)

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
            is_login_allowed=True,
        )

    def _make_profile(self, user):
        return UserProfile.objects.using(DB).create(
            user=user,
            member_status='ACTIVE',
            member_type='MEMBER',
            first_name_snapshot=user.first_name,
            last_name_snapshot=user.last_name,
        )

    def test_default_consents_are_false(self):
        """All consent fields must default to False for opt-in compliance."""
        assessment = MemberAssessment.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            user_profile=self.member_profile,
            member_name='Test Member',
            status='DRAFT',
        )
        self.assertFalse(assessment.consent_body_measurements)
        self.assertFalse(assessment.consent_movement_photos)
        self.assertFalse(assessment.consent_ai_analysis)
        self.assertFalse(assessment.consent_secondary_use)
        self.assertEqual(assessment.consent_notice_version, '')

    def test_status_choices_include_deferred_and_cancelled(self):
        """Lifecycle must support DEFERRED and CANCELLED statuses."""
        deferred = MemberAssessment.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            user_profile=self.member_profile,
            member_name='Deferred Member',
            status='DEFERRED',
            screening_outcome='DEFER_MEDICAL_CLEARANCE',
            screening_coach_notes='Awaiting clearance from cardiologist.',
        )
        self.assertEqual(deferred.status, 'DEFERRED')
        self.assertIn('Deferred', deferred.get_status_display())

        cancelled = MemberAssessment.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            user_profile=self.member_profile,
            member_name='Cancelled Member',
            status='CANCELLED',
            screening_outcome='STOPPED_SAFETY_CONCERNS',
            screening_coach_notes='Member experienced sudden dizziness during warm-up.',
        )
        self.assertEqual(cancelled.status, 'CANCELLED')
        self.assertIn('Cancelled', cancelled.get_status_display())

    def test_sign_off_and_governance_fields(self):
        """Sign-off and governance fields must persist and retrieve correctly."""
        now = timezone.now()
        due_date = (now + timedelta(days=42)).date()

        assessment = MemberAssessment.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            user_profile=self.member_profile,
            member_name='Sign-off Member',
            status='APPROVED',
            coach_signed_off_by=self.coach,
            coach_signed_off_at=now,
            member_acknowledged_at=now,
            consent_notice_version='v1.0-2026-11',
            reassessment_due_date=due_date,
            consent_body_measurements=True,
            consent_movement_photos=True,
        )

        fetched = MemberAssessment.objects.using(DB).get(pk=assessment.pk)
        self.assertEqual(fetched.coach_signed_off_by, self.coach)
        self.assertEqual(fetched.coach_signed_off_at, now)
        self.assertEqual(fetched.member_acknowledged_at, now)
        self.assertEqual(fetched.consent_notice_version, 'v1.0-2026-11')
        self.assertEqual(fetched.reassessment_due_date, due_date)
        self.assertTrue(fetched.consent_body_measurements)
        self.assertTrue(fetched.consent_movement_photos)
        self.assertFalse(fetched.consent_ai_analysis)

    def test_previous_assessment_self_reference(self):
        """Assessments must link to previous assessments for like-for-like tracking."""
        baseline = MemberAssessment.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            user_profile=self.member_profile,
            member_name='Longitudinal Member',
            assessment_type='INITIAL',
            status='APPROVED',
        )

        follow_up = MemberAssessment.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            user_profile=self.member_profile,
            member_name='Longitudinal Member',
            assessment_type='MONTHLY',
            status='DRAFT',
            previous_assessment=baseline,
        )

        self.assertEqual(follow_up.previous_assessment, baseline)
        self.assertIn(follow_up, baseline.follow_up_assessments.all())

    def test_decoupled_symptoms_and_safety_events(self):
        """Symptoms and safety events must be stored in a dedicated decoupled structure."""
        symptoms_payload = {
            'adverse_events': [
                {
                    'test_name': 'Treadmill Walk',
                    'symptom': 'Unusual breathlessness',
                    'severity': 'Mild',
                    'action_taken': 'Speed reduced and test terminated early',
                    'logged_at': '2026-11-24T10:00:00Z',
                }
            ],
            'pain_reports': [
                {
                    'location': 'Right knee',
                    'severity_score': 4,
                    'movement': 'Squat pattern',
                }
            ],
            'stopping_reason': 'Technique limit reached',
        }

        assessment = MemberAssessment.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            user_profile=self.member_profile,
            member_name='Symptom Member',
            status='TESTING',
            symptoms_and_safety_events=symptoms_payload,
        )

        fetched = MemberAssessment.objects.using(DB).get(pk=assessment.pk)
        self.assertEqual(len(fetched.symptoms_and_safety_events['adverse_events']), 1)
        self.assertEqual(
            fetched.symptoms_and_safety_events['adverse_events'][0]['symptom'],
            'Unusual breathlessness'
        )
        self.assertEqual(
            fetched.symptoms_and_safety_events['pain_reports'][0]['location'],
            'Right knee'
        )

    def test_assessment_test_progression_creation(self):
        """AssessmentTestProgression must persist exercise-specific progression criteria and approval."""
        assessment = MemberAssessment.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            user_profile=self.member_profile,
            member_name='Progression Member',
            status='APPROVED',
        )

        review_dt = date(2026, 12, 15)
        progression = AssessmentTestProgression.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            user_profile=self.member_profile,
            assessment=assessment,
            exercise_name='Push-up',
            current_variation='Wall push-up',
            current_load_kg=Decimal('0.00'),
            current_assistance='Wall support (arms extended)',
            current_reps_or_duration='15 valid reps',
            movement_quality='Meets criteria',
            reported_symptoms='None',
            next_progression_target='High-incline push-up (box/bench)',
            progression_criteria='Perform 3 sets of 15 wall push-ups with neutral spine before advancing.',
            review_date=review_dt,
            coach_approved_by=self.coach,
        )

        self.assertIsNotNone(progression.id)
        self.assertEqual(progression.exercise_name, 'Push-up')
        self.assertEqual(progression.current_variation, 'Wall push-up')
        self.assertEqual(progression.coach_approved_by, self.coach)
        self.assertEqual(progression.review_date, review_dt)
        self.assertIn(progression, assessment.progressions.all())
        self.assertIn(progression, self.member_profile.assessment_progressions.all())
