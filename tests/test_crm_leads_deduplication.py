"""
backend/tests/test_crm_leads_deduplication.py

Targeted tests for strict Lead Deduplication on intake:
Rule: When an existing phone number or email submits a new inquiry, do NOT create a new row in Leads;
instead, update the existing Lead and log a new activity/touchpoint in their timeline.
"""

import uuid
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIRequestFactory

from apps.master.models import Tenant, TenantDataSource
from config.routers import set_tenant_db_alias
from apps.tenant_core.models_org import Organization, Branch, Location
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_catalog import ProgramCategory, Program
from apps.tenant_core.models_crm import (
    Lead,
    LeadSource,
    LeadActivity,
    LeadStatusHistory,
    LeadCommercialProfile,
)
from apps.tenant_core.services_crm import CRMLeadService
from apps.tenant_core.views_crm import LeadViewSet

DB = 'tenant_test'


class LeadDeduplicationIntakeTestCase(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        super().setUp()
        set_tenant_db_alias(DB)
        self.factory = APIRequestFactory()

        # Master Tenant Setup
        self.tenant = Tenant.objects.using('default').create(
            name='SWEAT Deduplication Test Gym',
            code=f"SW-DEDUP-{uuid.uuid4().hex[:4].upper()}",
            slug=f"sweat-dedup-{uuid.uuid4().hex[:6]}",
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
            name='SWEAT Deduplication Fitness',
            code=f"SW-DEDUP-{uuid.uuid4().hex[:4].upper()}",
            status='ACTIVE',
        )
        self.loc = Location.objects.using(DB).create(
            organization=self.org,
            name='Bandra West',
            code=f"BW-{uuid.uuid4().hex[:3].upper()}",
            city='Mumbai',
        )
        self.branch = Branch.objects.using(DB).create(
            organization=self.org,
            location=self.loc,
            name='SWEAT Bandra West',
            code=f"SW-BW-{uuid.uuid4().hex[:3].upper()}",
            status='ACTIVE',
        )

        self.source = LeadSource.objects.using(DB).create(
            organization=self.org,
            name='Website Intake',
            code=f"WEB-{uuid.uuid4().hex[:4].upper()}",
            source_type='WEBSITE',
            status='ACTIVE',
        )
        self.meta_source = LeadSource.objects.using(DB).create(
            organization=self.org,
            name='Meta Ads',
            code=f"META-{uuid.uuid4().hex[:4].upper()}",
            source_type='META',
            status='ACTIVE',
        )

        cat = ProgramCategory.objects.using(DB).create(
            organization=self.org,
            name='Strength & Conditioning',
            code=f"SC-{uuid.uuid4().hex[:4].upper()}",
            status='ACTIVE',
        )
        self.program = Program.objects.using(DB).create(
            organization=self.org,
            category=cat,
            name='HIIT Functional Training',
            code=f"HIIT-{uuid.uuid4().hex[:4].upper()}",
            status='ACTIVE',
        )

    def tearDown(self):
        set_tenant_db_alias(None)

    def _prepare_request(self, request):
        request.tenant = self.tenant
        request.tenant_slug = self.tenant.slug
        request._state_db = DB
        request.organization = self.org

    def test_01_intake_deduplication_by_phone(self):
        """When an existing phone number submits a new inquiry, do NOT create a new row; update existing and log activity."""
        test_phone = "+919876543210"
        initial_lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name="Aarav",
            last_name="Sharma",
            phone=test_phone,
            email="aarav.initial@test.com",
            branch=self.branch,
            lead_source=self.source,
            db_alias=DB,
        )

        lead_count_before = Lead.objects.using(DB).filter(organization=self.org).count()
        activity_count_before = LeadActivity.objects.using(DB).filter(lead=initial_lead).count()

        # Submit repeat inquiry via LeadViewSet.create with the same phone number
        view = LeadViewSet.as_view({'post': 'create'})
        payload = {
            'first_name': 'Aarav',
            'last_name': 'Sharma',
            'phone': test_phone,
            'email': 'aarav.repeat@test.com',
            'branch': str(self.branch.id),
            'lead_source': str(self.meta_source.id),
            'interested_program': str(self.program.id),
            'fitness_goal': 'Weight loss and core strengthening',
            'attribution': {
                'campaign_name': 'Diwali_Fitness_Challenge_2026',
                'platform': 'META',
                'external_lead_id': 'meta_lead_repeat_123',
            },
        }

        request = self.factory.post('/api/v1/tenant/leads/', payload, format='json')
        self._prepare_request(request)
        response = view(request)

        # HTTP 200 OK returned with existing lead ID and deduplication flag
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['id'], str(initial_lead.id))
        self.assertTrue(response.data.get('is_duplicate'))

        # Strict check: Lead count did NOT increase
        lead_count_after = Lead.objects.using(DB).filter(organization=self.org).count()
        self.assertEqual(lead_count_after, lead_count_before)

        # Refresh existing lead and verify updated fields
        initial_lead.refresh_from_db()
        self.assertEqual(initial_lead.latest_touch_source, 'META')
        self.assertEqual(initial_lead.campaign_reference, 'Diwali_Fitness_Challenge_2026')
        self.assertEqual(initial_lead.interested_program_id, self.program.id)
        self.assertEqual(initial_lead.fitness_goal, 'Weight loss and core strengthening')

        # Verify a new LeadActivity touchpoint was logged in timeline
        activity_count_after = LeadActivity.objects.using(DB).filter(lead=initial_lead).count()
        self.assertEqual(activity_count_after, activity_count_before + 1)
        latest_act = LeadActivity.objects.using(DB).filter(lead=initial_lead).order_by('-activity_at').first()
        self.assertEqual(latest_act.outcome, 'Repeat Inquiry')
        self.assertIn('Repeat inquiry received', latest_act.notes)
        self.assertIn('Diwali_Fitness_Challenge_2026', latest_act.notes)

    def test_02_intake_deduplication_by_email(self):
        """When an existing email submits a new inquiry, do NOT create a new row; update existing and log activity."""
        test_email = "priya.kapoor@example.com"
        initial_lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name="Priya",
            last_name="Kapoor",
            phone="+919811122233",
            email=test_email,
            branch=self.branch,
            lead_source=self.source,
            db_alias=DB,
        )

        lead_count_before = Lead.objects.using(DB).filter(organization=self.org).count()

        # Submit repeat inquiry matching by email with a different phone number
        view = LeadViewSet.as_view({'post': 'create'})
        payload = {
            'first_name': 'Priya',
            'last_name': 'Kapoor',
            'phone': '+919899988877',
            'email': test_email.upper(),  # Verify case-insensitivity
            'branch': str(self.branch.id),
            'lead_source': str(self.source.id),
            'fitness_goal': 'Marathon endurance training',
        }

        request = self.factory.post('/api/v1/tenant/leads/', payload, format='json')
        self._prepare_request(request)
        response = view(request)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['id'], str(initial_lead.id))
        self.assertTrue(response.data.get('is_duplicate'))

        # Lead count did NOT increase
        self.assertEqual(Lead.objects.using(DB).filter(organization=self.org).count(), lead_count_before)

        # Timeline touchpoint logged
        latest_act = LeadActivity.objects.using(DB).filter(lead=initial_lead).order_by('-activity_at').first()
        self.assertEqual(latest_act.outcome, 'Repeat Inquiry')
        self.assertIn('Marathon endurance training', latest_act.notes)

    def test_03_repeat_inquiry_reactivates_lost_lead(self):
        """A repeat inquiry from a previously LOST lead re-engages and reactivates them to INTERESTED."""
        test_phone = "+919700011122"
        lost_lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name="Rohan",
            last_name="Verma",
            phone=test_phone,
            email="rohan.lost@test.com",
            branch=self.branch,
            lead_source=self.source,
            db_alias=DB,
        )
        lost_lead.current_status = 'LOST'
        lost_lead.save(using=DB, update_fields=['current_status'])

        view = LeadViewSet.as_view({'post': 'create'})
        payload = {
            'first_name': 'Rohan',
            'last_name': 'Verma',
            'phone': test_phone,
            'email': 'rohan.lost@test.com',
            'branch': str(self.branch.id),
            'lead_source': str(self.source.id),
        }

        request = self.factory.post('/api/v1/tenant/leads/', payload, format='json')
        self._prepare_request(request)
        response = view(request)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        lost_lead.refresh_from_db()
        self.assertEqual(lost_lead.current_status, 'INTERESTED')

        # Status history records reactivation
        stat_change = LeadStatusHistory.objects.using(DB).filter(
            lead=lost_lead, reason_code='REPEAT_INQUIRY_REACTIVATION'
        ).first()
        self.assertIsNotNone(stat_change)
        self.assertEqual(stat_change.from_status, 'LOST')
        self.assertEqual(stat_change.to_status, 'INTERESTED')

    def test_04_new_contact_creates_distinct_lead_row(self):
        """When an inquiry has an unseen phone and email, a brand new Lead row is created (HTTP 201)."""
        lead_count_before = Lead.objects.using(DB).filter(organization=self.org).count()

        view = LeadViewSet.as_view({'post': 'create'})
        payload = {
            'first_name': 'Kabir',
            'last_name': 'Mehta',
            'phone': f"+9193{str(uuid.uuid4().int)[:8]}",
            'email': f"kabir_{uuid.uuid4().hex[:6]}@test.com",
            'branch': str(self.branch.id),
            'lead_source': str(self.source.id),
        }

        request = self.factory.post('/api/v1/tenant/leads/', payload, format='json')
        self._prepare_request(request)
        response = view(request)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertFalse(response.data.get('is_duplicate', False))
        self.assertEqual(Lead.objects.using(DB).filter(organization=self.org).count(), lead_count_before + 1)

    def test_05_meta_lead_ads_intake_deduplication(self):
        """Meta lead ads repeat submission updates existing lead and logs activity without creating duplicate row."""
        from apps.tenant_core.models_meta_leads import MetaLeadMapping, MetaLeadImport
        from apps.tenant_core.services_meta_leads import _apply_mapping

        test_phone = "+919876599999"
        initial_lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name="Ananya",
            last_name="Roy",
            phone=test_phone,
            email="ananya.roy@example.com",
            branch=self.branch,
            lead_source=self.meta_source,
            db_alias=DB,
        )

        lead_count_before = Lead.objects.using(DB).filter(organization=self.org).count()
        act_count_before = LeadActivity.objects.using(DB).filter(lead=initial_lead).count()

        mapping = MetaLeadMapping.objects.using(DB).create(
            organization=self.org,
            name='Diwali Campaign Form Mapping',
            page_id='999888777666',
            form_id='111222333444',
            field_mappings={
                'first_name': 'first_name',
                'last_name': 'last_name',
                'phone': 'phone_number',
                'email': 'email',
                'fitness_goal': 'fitness_goal',
            },
            branch_mode='FIXED',
            branch=self.branch,
            lead_source=self.meta_source,
            repeat_policy='UPDATE_EXISTING',
            is_active=True,
        )

        event = MetaLeadImport.objects.using(DB).create(
            organization=self.org,
            page_id='999888777666',
            form_id='111222333444',
            external_lead_id=f"meta_lead_{uuid.uuid4().hex[:8]}",
            mode='SIMULATION',
            field_data=[
                {'name': 'first_name', 'values': ['Ananya']},
                {'name': 'last_name', 'values': ['Roy']},
                {'name': 'phone_number', 'values': ['9876599999']},
                {'name': 'email', 'values': ['ananya.roy@example.com']},
                {'name': 'fitness_goal', 'values': ['Weight Loss & Posture Correction']},
            ],
            status='PENDING',
        )

        from django.db import transaction
        with transaction.atomic(using=DB):
            _apply_mapping(event, mapping, DB, None, is_live=False)

        self.assertEqual(event.status, 'IMPORTED')
        self.assertEqual(event.lead_id, initial_lead.id)

        # Confirm strict deduplication: no new row in Lead
        self.assertEqual(Lead.objects.using(DB).filter(organization=self.org).count(), lead_count_before)

        # Confirm activity logged
        act_count_after = LeadActivity.objects.using(DB).filter(lead=initial_lead).count()
        self.assertEqual(act_count_after, act_count_before + 1)
        latest_act = LeadActivity.objects.using(DB).filter(lead=initial_lead).order_by('-activity_at').first()
        self.assertEqual(latest_act.outcome, 'Repeat Inquiry')
        self.assertIn('Repeat inquiry received', latest_act.notes)
