"""
Regression tests for three Assessment module security fixes:
1. /media/ is no longer a public static route. Files need a valid signed URL or an authenticated
   user who owns the assessment (staff in the organization, or the member for an APPROVED assessment).
2. Studio TV pairing: POST only, expiring single-use code, no database write on GET,
   and the approved summary excludes vitals and prescriptions.
3. Members can retrieve only their own APPROVED assessments. Staff keep full access.
"""
import os
import tempfile
import uuid
from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import resolve
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.tenant_core.models_assessment import MemberAssessment
from apps.tenant_core.models_org import Organization
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.services_media import build_signed_media_url
from apps.tenant_core.views_assessment import AssessmentSessionViewSet, StudioScreenPairingView
from apps.tenant_core.views_media import ProtectedMediaView
from config.routers import set_tenant_db_alias

DB = 'tenant_test'
PHOTO_NAME = 'assessments/baseline/2026/11/front.jpg'


class AssessmentSecurityFixesTestCase(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        cache.clear()  # reset throttle counters
        set_tenant_db_alias(DB)
        self.factory = APIRequestFactory()
        self.org = Organization.objects.using(DB).create(
            name='Assessment Security Org',
            code=f'ASO-{uuid.uuid4().hex[:4].upper()}',
            status='ACTIVE',
        )
        self.staff = self._make_user('staff', 'TRAINER')
        self.member = self._make_user('member', 'MEMBER')
        self.other_member = self._make_user('other', 'MEMBER')
        self.member_profile = self._make_profile(self.member)
        self.other_profile = self._make_profile(self.other_member)

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

    def _make_assessment(self, profile=None, status_value='DRAFT', **extra):
        return MemberAssessment.objects.using(DB).create(
            organization=self.org,
            user_profile=profile,
            member_name='Alice Member',
            status=status_value,
            **extra,
        )

    def _media_get(self, user, path):
        set_tenant_db_alias(DB)  # the test client's middleware resets the alias on every request
        request = self.factory.get(f'/media/{path}')
        force_authenticate(request, user=user)
        return ProtectedMediaView.as_view()(request, path=path)

    # ------------------------------------------------------------------
    # Fix 1: /media/ is protected
    # ------------------------------------------------------------------
    def test_media_route_requires_signature_or_authorized_user(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            full_path = os.path.join(media_root, PHOTO_NAME)
            os.makedirs(os.path.dirname(full_path))
            with open(full_path, 'wb') as fh:
                fh.write(b'jpeg-bytes')

            # The URL resolves to the protected view, not django.views.static.serve.
            self.assertIs(resolve(f'/media/{PHOTO_NAME}').func.view_class, ProtectedMediaView)

            approved = self._make_assessment(self.member_profile, 'APPROVED', photo_front=PHOTO_NAME)

            # Anonymous, no signature: rejected.
            self.assertEqual(self.client.get(f'/media/{PHOTO_NAME}').status_code, 401)
            # Tampered signature: rejected.
            tampered = build_signed_media_url(PHOTO_NAME)[:-3] + 'xyz'
            self.assertEqual(self.client.get(tampered).status_code, 401)
            # Signature issued for another file: rejected.
            other_file_url = build_signed_media_url('assessments/baseline/2026/11/other.jpg')
            token = other_file_url.split('?sig=')[1]
            self.assertEqual(self.client.get(f'/media/{PHOTO_NAME}?sig={token}').status_code, 401)
            # Valid signed URL: served.
            signed = self.client.get(build_signed_media_url(PHOTO_NAME))
            self.assertEqual(signed.status_code, 200)
            self.assertEqual(b''.join(signed.streaming_content), b'jpeg-bytes')

            # Authenticated ownership rules.
            self.assertEqual(self._media_get(self.staff, PHOTO_NAME).status_code, 200)
            self.assertEqual(self._media_get(self.member, PHOTO_NAME).status_code, 200)
            self.assertEqual(self._media_get(self.other_member, PHOTO_NAME).status_code, 404)
            MemberAssessment.objects.using(DB).filter(pk=approved.pk).update(status='PENDING_REVIEW')
            self.assertEqual(self._media_get(self.member, PHOTO_NAME).status_code, 404)

            # Path traversal is rejected even for staff.
            self.assertEqual(self._media_get(self.staff, '../secret.txt').status_code, 404)

    # ------------------------------------------------------------------
    # Fix 2: studio screen pairing
    # ------------------------------------------------------------------
    def _pair(self, code):
        request = self.factory.post('/api/v1/tenant/assessments/pair/', {'code': code}, format='json')
        return StudioScreenPairingView.as_view()(request)

    def test_pairing_is_post_only_expiring_single_use_and_approved_summary_only(self):
        assessment = self._make_assessment(
            self.member_profile,
            'TESTING',
            workout_prescription='SECRET WORKOUT',
            diet_prescription='SECRET DIET',
            measured_weight_kg=70,
            resting_heart_rate_bpm=61,
        )
        code = assessment.pairing_code

        # GET is not allowed and writes nothing.
        get_request = self.factory.get('/api/v1/tenant/assessments/pair/', {'code': code})
        self.assertEqual(StudioScreenPairingView.as_view()(get_request).status_code, 405)
        assessment.refresh_from_db()
        self.assertFalse(assessment.display_screen_connected)

        # Not approved yet: standby response, code stays usable, no clinical data.
        standby = self._pair(code)
        self.assertEqual(standby.status_code, 200)
        self.assertFalse(standby.data['is_approved'])
        self.assertNotIn('vitals', standby.data)
        assessment.refresh_from_db()
        self.assertTrue(assessment.display_screen_connected)
        self.assertIsNone(assessment.pairing_code_used_at)

        # Approved: summary returned once, without vitals or prescriptions.
        MemberAssessment.objects.using(DB).filter(pk=assessment.pk).update(status='APPROVED')
        revealed = self._pair(code)
        self.assertEqual(revealed.status_code, 200)
        self.assertTrue(revealed.data['is_approved'])
        for forbidden in ('vitals', 'workout_prescription', 'diet_prescription'):
            self.assertNotIn(forbidden, revealed.data)
        self.assertNotIn('SECRET', str(revealed.data))
        self.assertIn('recommended_package', revealed.data)
        assessment.refresh_from_db()
        self.assertIsNotNone(assessment.pairing_code_used_at)

        # Single use: the same code no longer works.
        self.assertEqual(self._pair(code).status_code, 404)

        # Expired code is rejected.
        expired = self._make_assessment(
            self.other_profile, 'APPROVED', pairing_code_expires_at=timezone.now() - timedelta(minutes=1)
        )
        self.assertEqual(self._pair(expired.pairing_code).status_code, 404)

    # ------------------------------------------------------------------
    # Fix 3: members see only APPROVED assessments
    # ------------------------------------------------------------------
    def _retrieve(self, user, assessment):
        request = self.factory.get(f'/api/v1/mobile/assessment/{assessment.id}/')
        force_authenticate(request, user=user)
        return AssessmentSessionViewSet.as_view({'get': 'retrieve'})(request, pk=str(assessment.id))

    def test_member_retrieves_only_own_approved_assessments_and_staff_see_all(self):
        draft = self._make_assessment(self.member_profile, 'DRAFT')
        approved = self._make_assessment(self.member_profile, 'APPROVED')
        others = self._make_assessment(self.other_profile, 'APPROVED')

        self.assertEqual(self._retrieve(self.member, draft).status_code, 404)
        self.assertEqual(self._retrieve(self.member, others).status_code, 404)
        ok = self._retrieve(self.member, approved)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.data['status'], 'APPROVED')

        self.assertEqual(self._retrieve(self.staff, draft).status_code, 200)
        self.assertEqual(self._retrieve(self.staff, approved).status_code, 200)
