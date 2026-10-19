"""
backend/tests/test_crm_lead_create_token_safety.py

Targeted security tests for LeadViewSet.create token withholding:
1. New email and phone, no existing user: 201; 'auth' and 'tokens' present; new TenantUser (LEAD); zero UserProfile; converted_user_profile_id is None.
2. Existing TenantUser (staff-type) matched by email: 201; no 'auth', 'tokens', or 'user' in response; TenantUser count unchanged.
3. Existing TenantUser (member-type) matched by phone: 201; no 'auth', 'tokens', or 'user' in response; TenantUser count unchanged.
4. Existing TenantUser with user_type LEAD matched by email: 201; no 'auth', 'tokens', or 'user' in response; TenantUser count unchanged.
"""

import uuid
from django.test import TestCase
from rest_framework.test import APIRequestFactory
from rest_framework import status

from apps.master.models import Tenant, TenantDataSource
from config.routers import set_tenant_db_alias
from apps.tenant_core.models_org import Organization, Branch, Location
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_crm import Lead, LeadSource
from apps.tenant_core.views_crm import LeadViewSet

DB = 'tenant_test'


class LeadCreateTokenSafetyTestCase(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias(DB)
        self.factory = APIRequestFactory()

        # Master Tenant Setup
        self.tenant = Tenant.objects.using('default').create(
            name='SWEAT Security Test Gym',
            code=f"SW-SEC-{uuid.uuid4().hex[:4].upper()}",
            slug=f"sweat-sec-{uuid.uuid4().hex[:6]}",
            status="ACTIVE",
        )
        TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            db_name='test_fitness_tenant',
            database_name='test_fitness_tenant',
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )

        # Organization, Location & Branch
        self.org = Organization.objects.using(DB).create(
            name='SWEAT Security Fitness',
            code=f"SW-SEC-{uuid.uuid4().hex[:4].upper()}",
            status='ACTIVE',
        )
        self.loc = Location.objects.using(DB).create(
            organization=self.org,
            name='Koramangala',
            code=f"KM-{uuid.uuid4().hex[:3].upper()}",
            city='Bangalore',
        )
        self.branch = Branch.objects.using(DB).create(
            organization=self.org,
            location=self.loc,
            name='SWEAT Koramangala',
            code=f"BR-{uuid.uuid4().hex[:3].upper()}",
            status='ACTIVE',
        )

        # Lead Source
        self.source = LeadSource.objects.using(DB).create(
            organization=self.org,
            name='Web Ingestion',
            code=f"WEB-{uuid.uuid4().hex[:4].upper()}",
            source_type='DIGITAL_MARKETING',
            status='ACTIVE',
        )

    def tearDown(self):
        set_tenant_db_alias(None)

    def _prepare_unauthenticated_request(self, request):
        request.tenant = self.tenant
        request.tenant_slug = self.tenant.slug
        request._state_db = DB

    def test_new_user_creation_issues_tokens_and_no_member_profile(self):
        """a. New email and phone, no existing user: 201; 'auth' and 'tokens' present; new TenantUser (LEAD); zero UserProfile; converted_user_profile_id is None."""
        view = LeadViewSet.as_view({'post': 'create'})
        unique_email = f"prospect_{uuid.uuid4().hex[:6]}@test.com"
        unique_phone = f"+9198{str(uuid.uuid4().int)[:8]}"
        payload = {
            'first_name': 'New',
            'last_name': 'Prospect',
            'email': unique_email,
            'phone': unique_phone,
            'branch': str(self.branch.id),
            'lead_source': str(self.source.id),
        }

        user_count_before = TenantUser.objects.using(DB).count()
        profile_count_before = UserProfile.objects.using(DB).count()

        request = self.factory.post('/api/v1/tenant/leads/', payload, format='json')
        self._prepare_unauthenticated_request(request)
        response = view(request)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn('auth', response.data)
        self.assertIn('tokens', response.data)
        self.assertIn('user', response.data)
        self.assertIsNotNone(response.data['auth']['access'])
        self.assertIsNotNone(response.data['auth']['refresh'])

        # Verify one new TenantUser with user_type LEAD
        self.assertEqual(TenantUser.objects.using(DB).count(), user_count_before + 1)
        created_user = TenantUser.objects.using(DB).get(email=unique_email)
        self.assertEqual(created_user.user_type, 'LEAD')

        # Verify zero UserProfile created
        self.assertEqual(UserProfile.objects.using(DB).count(), profile_count_before)

        # Verify lead created and converted_user_profile_id is None
        lead_id = response.data['id']
        lead = Lead.objects.using(DB).get(id=lead_id)
        self.assertIsNone(lead.converted_user_profile_id)
        self.assertIsNone(lead.converted_user_profile)

    def test_existing_staff_user_matched_by_email_withholds_tokens(self):
        """b. Existing TenantUser (staff-type) matched by email: 201; no 'auth', 'tokens' or 'user' keys; TenantUser count unchanged."""
        existing_email = f"staff_{uuid.uuid4().hex[:6]}@test.com"
        staff_user = TenantUser.objects.using(DB).create(
            organization=self.org,
            email=existing_email,
            first_name='Coach',
            last_name='Vikram',
            user_type='STAFF',
            status='ACTIVE',
            is_login_allowed=True,
        )

        user_count_before = TenantUser.objects.using(DB).count()
        view = LeadViewSet.as_view({'post': 'create'})
        payload = {
            'first_name': 'Vikram',
            'last_name': 'Duplicate',
            'email': existing_email,
            'phone': f"+9197{str(uuid.uuid4().int)[:8]}",
            'branch': str(self.branch.id),
            'lead_source': str(self.source.id),
        }

        request = self.factory.post('/api/v1/tenant/leads/', payload, format='json')
        self._prepare_unauthenticated_request(request)
        response = view(request)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertNotIn('auth', response.data)
        self.assertNotIn('tokens', response.data)
        self.assertNotIn('user', response.data)
        self.assertEqual(TenantUser.objects.using(DB).count(), user_count_before)

    def test_existing_member_user_matched_by_phone_withholds_tokens(self):
        """c. Existing TenantUser (member-type) matched by phone: 201; no 'auth', 'tokens' or 'user' keys; TenantUser count unchanged."""
        existing_phone = f"+9196{str(uuid.uuid4().int)[:8]}"
        member_user = TenantUser.objects.using(DB).create(
            organization=self.org,
            email=f"member_{uuid.uuid4().hex[:6]}@test.com",
            phone=existing_phone,
            first_name='Existing',
            last_name='Member',
            user_type='MEMBER',
            status='ACTIVE',
            is_login_allowed=True,
        )

        user_count_before = TenantUser.objects.using(DB).count()
        view = LeadViewSet.as_view({'post': 'create'})
        payload = {
            'first_name': 'Existing',
            'last_name': 'Caller',
            'email': f"different_email_{uuid.uuid4().hex[:6]}@test.com",
            'phone': existing_phone,
            'branch': str(self.branch.id),
            'lead_source': str(self.source.id),
        }

        request = self.factory.post('/api/v1/tenant/leads/', payload, format='json')
        self._prepare_unauthenticated_request(request)
        response = view(request)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertNotIn('auth', response.data)
        self.assertNotIn('tokens', response.data)
        self.assertNotIn('user', response.data)
        self.assertEqual(TenantUser.objects.using(DB).count(), user_count_before)

    def test_existing_lead_user_matched_by_email_withholds_tokens(self):
        """d. Existing TenantUser with user_type LEAD matched by email: 201; no 'auth', 'tokens' or 'user' keys; TenantUser count unchanged."""
        existing_lead_email = f"leaduser_{uuid.uuid4().hex[:6]}@test.com"
        lead_user = TenantUser.objects.using(DB).create(
            organization=self.org,
            email=existing_lead_email,
            first_name='Prior',
            last_name='Lead',
            user_type='LEAD',
            status='ACTIVE',
            is_login_allowed=True,
        )

        user_count_before = TenantUser.objects.using(DB).count()
        view = LeadViewSet.as_view({'post': 'create'})
        payload = {
            'first_name': 'Prior',
            'last_name': 'LeadAgain',
            'email': existing_lead_email,
            'phone': f"+9195{str(uuid.uuid4().int)[:8]}",
            'branch': str(self.branch.id),
            'lead_source': str(self.source.id),
        }

        request = self.factory.post('/api/v1/tenant/leads/', payload, format='json')
        self._prepare_unauthenticated_request(request)
        response = view(request)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertNotIn('auth', response.data)
        self.assertNotIn('tokens', response.data)
        self.assertNotIn('user', response.data)
        self.assertEqual(TenantUser.objects.using(DB).count(), user_count_before)
