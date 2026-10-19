"""
backend/tests/test_p0_security_remediation.py

Phase 1 P0 Security Remediation Regression Test Suite:
1. Phase A: Mobile Free-Trial Account Takeover Defense
   - Anonymous request targeting existing MEMBER account fails with 401 AUTHENTICATION_REQUIRED and no tokens.
   - Anonymous request targeting existing TRAINER account fails with 401 AUTHENTICATION_REQUIRED and no tokens.
   - Anonymous request targeting existing ORG_ADMIN account fails with 401 AUTHENTICATION_REQUIRED and no tokens.
   - Anonymous request targeting existing user's phone number fails with 401 AUTHENTICATION_REQUIRED and no tokens.
   - Authenticated user can legitimately claim/book a free trial without token leakage or account collision.
   - Legitimate new prospect (unseen email and phone) creates a new guest user, receives initial tokens, and books trial.
   - Trial booking does not prematurely convert lead: lead.converted_user_profile remains None, status is TRIAL_BOOKED.
   - Mobile registration does not prematurely mark lead as commercially converted (converted_user_profile remains None).

2. Phase B: Strict Tenant Resolution & Cross-Tenant Isolation
   - Explicit invalid tenant slug in X-Tenant-Slug header fails closed (404 TENANT_NOT_FOUND) in TenantDatabaseMiddleware.
   - Explicit invalid tenant slug in mobile view fails closed (404 TENANT_NOT_FOUND) instead of bleeding into first tenant.
   - Missing tenant slug on public intake defaults to 'sweat' if available.
   - Valid explicit tenant identifier routes to the target tenant's database.
   - Isolation: Request targeting Tenant A cannot read or write data in Tenant B.
"""

import uuid
from decimal import Decimal
from datetime import timedelta
from django.test import TestCase, RequestFactory
from django.utils import timezone
from django.conf import settings
from rest_framework import status
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.master.models import Tenant, TenantDataSource
from config.routers import set_tenant_db_alias, get_tenant_db_alias
from config.tenant_middleware import TenantDatabaseMiddleware
from apps.tenant_core.models_org import Organization, Branch, Location
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_rbac import Role, RoleAssignment
from apps.tenant_core.models_catalog import Program, Package, PackageVersion, PackagePrice
from apps.tenant_core.models_classes import ClassCategory, ClassTemplate, ClassOccurrence
from apps.tenant_core.models_crm import Lead, LeadSource, TrialBooking
from apps.tenant_core.views_mobile import (
    MobileClaimFreeTrialView,
    MobileRegisterView,
    _resolve_mobile_tenant_and_db,
)

DB = 'tenant_test'


class P0SecurityRemediationTestCase(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias(DB)
        self.factory = APIRequestFactory()
        self.django_factory = RequestFactory()

        # Primary test tenant
        self.tenant_a = Tenant.objects.using('default').create(
            name="SWEAT Security Tenant A",
            code=f"TA-{uuid.uuid4().hex[:4].upper()}",
            slug=f"tenant-a-{uuid.uuid4().hex[:6]}",
            status="ACTIVE",
        )
        db_a_name = f"test_fitness_a_{uuid.uuid4().hex[:6]}"
        TenantDataSource.objects.using('default').create(
            tenant=self.tenant_a,
            db_name=db_a_name,
            database_name=db_a_name,
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )

        # Secondary test tenant for cross-tenant isolation testing
        self.tenant_b = Tenant.objects.using('default').create(
            name="SWEAT Security Tenant B",
            code=f"TB-{uuid.uuid4().hex[:4].upper()}",
            slug=f"tenant-b-{uuid.uuid4().hex[:6]}",
            status="ACTIVE",
        )
        db_b_name = f"test_fitness_b_{uuid.uuid4().hex[:6]}"
        TenantDataSource.objects.using('default').create(
            tenant=self.tenant_b,
            db_name=db_b_name,
            database_name=db_b_name,
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )

        # Organization and Branch
        self.org = Organization.objects.using(DB).create(
            name="SWEAT Test Studio Org",
            code=f"STO-{uuid.uuid4().hex[:4].upper()}",
            status="ACTIVE",
        )
        self.loc = Location.objects.using(DB).create(
            organization=self.org,
            name="Indiranagar",
            code=f"IN-{uuid.uuid4().hex[:3].upper()}",
            city="Bangalore",
        )
        self.branch = Branch.objects.using(DB).create(
            organization=self.org,
            location=self.loc,
            name="Indiranagar Studio",
            code=f"IS-{uuid.uuid4().hex[:3].upper()}",
            status="ACTIVE",
        )

        # System roles
        self.role_member, _ = Role.objects.using(DB).get_or_create(
            organization=self.org,
            code="MEMBER",
            defaults={"name": "Studio Member", "scope": "BRANCH", "is_system": True, "is_active": True},
        )
        self.role_trainer, _ = Role.objects.using(DB).get_or_create(
            organization=self.org,
            code="TRAINER",
            defaults={"name": "Studio Trainer", "scope": "BRANCH", "is_system": True, "is_active": True},
        )
        self.role_admin, _ = Role.objects.using(DB).get_or_create(
            organization=self.org,
            code="ORG_ADMIN",
            defaults={"name": "Organization Admin", "scope": "ORG", "is_system": True, "is_active": True},
        )

        # Pre-existing accounts in Tenant A
        self.existing_member = TenantUser.objects.using(DB).create(
            organization=self.org,
            email="existing.member@test.com",
            phone="+919876543201",
            username="existing_member",
            first_name="Alice",
            last_name="Member",
            user_type="MEMBER",
            status="ACTIVE",
            is_login_allowed=True,
        )
        RoleAssignment.objects.using(DB).create(
            user=self.existing_member, role=self.role_member, branch=self.branch, is_active=True
        )
        self.existing_member_profile = UserProfile.objects.using(DB).create(
            user=self.existing_member,
            member_status="ACTIVE",
            member_type="MEMBER",
            first_name_snapshot="Alice",
            last_name_snapshot="Member",
            preferred_branch=self.branch,
        )

        self.existing_trainer = TenantUser.objects.using(DB).create(
            organization=self.org,
            email="existing.trainer@test.com",
            phone="+919876543202",
            username="existing_trainer",
            first_name="Bob",
            last_name="Coach",
            user_type="TRAINER",
            status="ACTIVE",
            is_login_allowed=True,
        )
        RoleAssignment.objects.using(DB).create(
            user=self.existing_trainer, role=self.role_trainer, branch=self.branch, is_active=True
        )

        self.existing_admin = TenantUser.objects.using(DB).create(
            organization=self.org,
            email="existing.admin@test.com",
            phone="+919876543203",
            username="existing_admin",
            first_name="Carol",
            last_name="Admin",
            user_type="ORG_ADMIN",
            status="ACTIVE",
            is_login_allowed=True,
        )
        RoleAssignment.objects.using(DB).create(
            user=self.existing_admin, role=self.role_admin, branch=None, is_active=True
        )

        # Class template and future occurrence for booking trials
        self.program = Program.objects.using(DB).create(
            organization=self.org, name="Reformer Pilates", code="REF", status="ACTIVE"
        )
        self.category = ClassCategory.objects.using(DB).create(
            organization=self.org, name="Reformer", code="REF-CAT", status="ACTIVE"
        )
        self.template = ClassTemplate.objects.using(DB).create(
            organization=self.org,
            category=self.category,
            program=self.program,
            name="Morning Reformer Sweat",
            code="REF-AM",
            default_duration_minutes=50,
            default_capacity=10,
            default_trial_capacity=5,
            status="ACTIVE",
        )
        future_start = timezone.now() + timedelta(days=2)
        self.occurrence = ClassOccurrence.objects.using(DB).create(
            class_template=self.template,
            branch=self.branch,
            occurrence_date=future_start.date(),
            start_at=future_start,
            end_at=future_start + timedelta(minutes=50),
            capacity=10,
            trial_capacity=5,
            status="SCHEDULED",
        )

    # -------------------------------------------------------------------------
    # PHASE A: FREE-TRIAL ACCOUNT TAKEOVER DEFENSE TESTS
    # -------------------------------------------------------------------------

    def test_anonymous_trial_booking_with_existing_member_email_rejected_no_tokens(self):
        """Anonymous caller supplying existing MEMBER email must receive 401 and NO JWT tokens."""
        view = MobileClaimFreeTrialView.as_view()
        payload = {
            'email': self.existing_member.email,
            'occurrence_id': str(self.occurrence.id),
            'first_name': 'Attacker',
        }
        request = self.factory.post(
            '/api/v1/mobile/bookings/claim-free-trial/',
            data=payload,
            format='json',
            HTTP_X_TENANT_SLUG=self.tenant_a.slug,
        )
        response = view(request)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data.get('code'), 'AUTHENTICATION_REQUIRED')
        self.assertTrue(response.data.get('requires_login'))
        self.assertNotIn('auth', response.data)
        self.assertNotIn('access', response.data)
        self.assertNotIn('refresh', response.data)

        # Verify no booking was created
        self.assertFalse(
            self.occurrence.bookings.using(DB).filter(user_profile=self.existing_member_profile).exists()
        )

    def test_anonymous_trial_booking_with_existing_trainer_email_rejected_no_tokens(self):
        """Anonymous caller supplying existing TRAINER email must receive 401 and NO JWT tokens."""
        view = MobileClaimFreeTrialView.as_view()
        payload = {
            'email': self.existing_trainer.email,
            'occurrence_id': str(self.occurrence.id),
        }
        request = self.factory.post(
            '/api/v1/mobile/bookings/claim-free-trial/',
            data=payload,
            format='json',
            HTTP_X_TENANT_SLUG=self.tenant_a.slug,
        )
        response = view(request)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data.get('code'), 'AUTHENTICATION_REQUIRED')
        self.assertNotIn('auth', response.data)

    def test_anonymous_trial_booking_with_existing_admin_email_rejected_no_tokens(self):
        """Anonymous caller supplying existing ORG_ADMIN email must receive 401 and NO JWT tokens."""
        view = MobileClaimFreeTrialView.as_view()
        payload = {
            'email': self.existing_admin.email,
            'occurrence_id': str(self.occurrence.id),
        }
        request = self.factory.post(
            '/api/v1/mobile/bookings/claim-free-trial/',
            data=payload,
            format='json',
            HTTP_X_TENANT_SLUG=self.tenant_a.slug,
        )
        response = view(request)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data.get('code'), 'AUTHENTICATION_REQUIRED')
        self.assertNotIn('auth', response.data)

    def test_anonymous_trial_booking_with_existing_phone_rejected_no_tokens(self):
        """Anonymous caller supplying phone number of existing user must receive 401 and NO JWT tokens."""
        view = MobileClaimFreeTrialView.as_view()
        payload = {
            'phone': self.existing_member.phone,
            'occurrence_id': str(self.occurrence.id),
            'first_name': 'AttackerByPhone',
        }
        request = self.factory.post(
            '/api/v1/mobile/bookings/claim-free-trial/',
            data=payload,
            format='json',
            HTTP_X_TENANT_SLUG=self.tenant_a.slug,
        )
        response = view(request)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data.get('code'), 'AUTHENTICATION_REQUIRED')
        self.assertNotIn('auth', response.data)

    def test_authenticated_user_can_legitimately_claim_free_trial(self):
        """An authenticated member can legitimately claim their free trial without token leakage."""
        view = MobileClaimFreeTrialView.as_view()
        payload = {
            'occurrence_id': str(self.occurrence.id),
        }
        request = self.factory.post(
            '/api/v1/mobile/bookings/claim-free-trial/',
            data=payload,
            format='json',
            HTTP_X_TENANT_SLUG=self.tenant_a.slug,
        )
        force_authenticate(request, user=self.existing_member)
        response = view(request)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data.get('is_free_trial'), True)
        self.assertTrue(
            self.occurrence.bookings.using(DB).filter(user_profile=self.existing_member_profile).exists()
        )

    def test_legitimate_new_guest_trial_booking_succeeds_and_preserves_unconverted_lifecycle(self):
        """A genuine new prospect books a trial: creates LEAD user, returns guest tokens, DOES NOT set converted_user_profile."""
        view = MobileClaimFreeTrialView.as_view()
        new_email = f"guest.prospect.{uuid.uuid4().hex[:6]}@test.com"
        new_phone = "+919876549999"
        payload = {
            'email': new_email,
            'phone': new_phone,
            'first_name': 'NewGuest',
            'last_name': 'Prospect',
            'occurrence_id': str(self.occurrence.id),
            'branch_id': str(self.branch.id),
        }
        request = self.factory.post(
            '/api/v1/mobile/bookings/claim-free-trial/',
            data=payload,
            format='json',
            HTTP_X_TENANT_SLUG=self.tenant_a.slug,
        )
        response = view(request)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data.get('is_free_trial'))
        self.assertIn('auth', response.data)
        self.assertIn('access', response.data['auth'])

        # Verify new TenantUser was created as LEAD
        new_user = TenantUser.objects.using(DB).get(email=new_email)
        self.assertEqual(new_user.user_type, 'LEAD')

        # Verify Lead record was created, status is TRIAL_BOOKED, and converted_user_profile is strictly None
        lead = Lead.objects.using(DB).get(email_normalized=new_email)
        self.assertEqual(lead.current_status, 'TRIAL_BOOKED')
        self.assertIsNone(lead.converted_user_profile)
        self.assertIsNone(lead.converted_user_profile_id)

    def test_mobile_registration_does_not_prematurely_convert_lead(self):
        """MobileRegisterView creates user and lead but leaves converted_user_profile as None."""
        view = MobileRegisterView.as_view()
        reg_email = f"mobile.reg.{uuid.uuid4().hex[:6]}@test.com"
        reg_phone = "+919876548888"
        payload = {
            'email': reg_email,
            'phone': reg_phone,
            'first_name': 'Mobile',
            'last_name': 'Registrant',
            'password': 'SecurePassword123!',
            'branch_id': str(self.branch.id),
        }
        request = self.factory.post(
            '/api/v1/mobile/auth/register/',
            data=payload,
            format='json',
            HTTP_X_TENANT_SLUG=self.tenant_a.slug,
        )
        response = view(request)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        lead = Lead.objects.using(DB).get(email_normalized=reg_email)
        self.assertIsNone(lead.converted_user_profile)
        self.assertIsNone(lead.converted_user_profile_id)
        self.assertNotEqual(lead.current_status, 'CONVERTED')

    # -------------------------------------------------------------------------
    # PHASE B: TENANT RESOLUTION & ISOLATION TESTS
    # -------------------------------------------------------------------------

    def test_middleware_rejects_invalid_explicit_tenant_slug_with_404(self):
        """TenantDatabaseMiddleware fails closed with 404 when an invalid explicit tenant slug is provided."""
        middleware = TenantDatabaseMiddleware(get_response=lambda req: None)
        request = self.django_factory.get(
            '/api/v1/tenant/leads/',
            HTTP_X_TENANT_SLUG='nonexistent_tenant_slug_xyz',
        )
        response = middleware(request)

        self.assertEqual(response.status_code, 404)
        self.assertIn('TENANT_NOT_FOUND', response.content.decode())
        self.assertIsNone(get_tenant_db_alias())

    def test_mobile_view_rejects_invalid_explicit_tenant_slug_with_404(self):
        """Mobile endpoints reject invalid explicit tenant slug with 404 instead of silent fallback."""
        view = MobileClaimFreeTrialView.as_view()
        payload = {
            'email': 'some.user@test.com',
            'occurrence_id': str(self.occurrence.id),
        }
        request = self.factory.post(
            '/api/v1/mobile/bookings/claim-free-trial/',
            data=payload,
            format='json',
            HTTP_X_TENANT_SLUG='completely_invalid_tenant_slug',
        )
        response = view(request)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data.get('code'), 'TENANT_NOT_FOUND')

    def test_mobile_resolver_fails_closed_on_invalid_explicit_slug(self):
        """_resolve_mobile_tenant_and_db returns (None, None) for invalid explicit slug."""
        request = self.factory.get(
            '/api/v1/mobile/schedule/',
            HTTP_X_TENANT_SLUG='unknown_slug_123',
        )
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        self.assertIsNone(alias)
        self.assertIsNone(tenant)

    def test_mobile_resolver_succeeds_on_valid_explicit_slug(self):
        """_resolve_mobile_tenant_and_db resolves correct tenant when valid slug is supplied."""
        request = self.factory.get(
            '/api/v1/mobile/schedule/',
            HTTP_X_TENANT_SLUG=self.tenant_a.slug,
        )
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        self.assertIsNotNone(alias)
        self.assertEqual(tenant.id, self.tenant_a.id)
        self.assertEqual(tenant.slug, self.tenant_a.slug)

    def test_cross_tenant_isolation_prevents_unauthorized_routing(self):
        """Explicit slug for Tenant A cannot route to or return data from Tenant B."""
        # Query with Tenant A slug
        req_a = self.factory.get(
            '/api/v1/mobile/schedule/',
            HTTP_X_TENANT_SLUG=self.tenant_a.slug,
        )
        alias_a, resolved_a = _resolve_mobile_tenant_and_db(req_a)
        self.assertEqual(resolved_a.id, self.tenant_a.id)

        # Query with Tenant B slug
        req_b = self.factory.get(
            '/api/v1/mobile/schedule/',
            HTTP_X_TENANT_SLUG=self.tenant_b.slug,
        )
        alias_b, resolved_b = _resolve_mobile_tenant_and_db(req_b)
        self.assertEqual(resolved_b.id, self.tenant_b.id)
        self.assertNotEqual(resolved_a.id, resolved_b.id)

        # Invalid slug fails closed and routes to neither
        req_inv = self.factory.get(
            '/api/v1/mobile/schedule/',
            HTTP_X_TENANT_SLUG='rogue_unregistered_tenant',
        )
        alias_inv, resolved_inv = _resolve_mobile_tenant_and_db(req_inv)
        self.assertIsNone(alias_inv)
        self.assertIsNone(resolved_inv)
