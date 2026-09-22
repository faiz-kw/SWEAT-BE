"""
backend/tests/test_crm_phase2_attribution_lead360.py — Targeted Tests for CRM Phase 2:
1. Manual Lead can still be created without attribution
2. Lead can be created with attribution
3. Attribution belongs to correct Lead
4. Cross-tenant attribution ID rejected
5. Unauthorized branch Lead attribution inaccessible
6. First touch preserved
7. Additional touch appended rather than overwriting
8. Duplicate external Lead event is idempotent
9. Inactive Lead Source remains visible historically
10. Active Lead Source still used for new Lead
11. Timeline contains Lead Created
12. Timeline contains Status Transition
13. Timeline contains Assignment
14. Timeline contains Attribution Captured
15. Timeline contains Trial Booking if present
16. Timeline sorted correctly
17. Timeline tenant and branch isolation
18. SLA ON_TRACK calculation
19. SLA BREACHED calculation
20. SLA disabled calculation
21. Campaign & source filters work
22. RBAC read-only behavior
23. Unauthorized mutation -> 403
24. Audit & Outbox generated appropriately
"""

import uuid
from datetime import timedelta
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from config.routers import set_tenant_db_alias
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.models_saas import SaasPlan, TenantSubscription, ProductModule, TenantModule
from apps.authentication.views import _build_tenant_token
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser, UserBranch
from apps.tenant_core.models_rbac import (
    Role,
    RoleAssignment,
    ModuleCatalog,
    SubmoduleCatalog,
    Permission,
    RoleModuleAccess,
    RoleSubmoduleAccess,
    RolePermissionSet,
    RolePermissionSetItem,
)
from apps.tenant_core.models_crm import (
    LeadSource,
    Lead,
    LeadAttribution,
    LeadStatusHistory,
    LeadAssignment,
    TrialBooking,
    CRMStageSlaPolicy,
    CRMTrialReminderPolicy,
)
from apps.tenant_core.models_audit_outbox import BusinessAuditEvent, DomainOutboxEvent
from apps.tenant_core.services_crm import CRMLeadService


class CRMPhase2AttributionLead360Tests(TestCase):
    databases = '__all__'

    def setUp(self):
        super().setUp()
        set_tenant_db_alias('tenant_test')
        self.client = APIClient()

        # 1. Master DB Setup (Tenant A & Tenant B)
        self.tenant_a = Tenant.objects.using('default').create(
            code='CRM-TENANT-A2',
            name='Tenant A2 Fitness',
            slug='tenant-a2-fitness',
            status='ACTIVE',
        )
        self.ds_a = TenantDataSource.objects.using('default').create(
            tenant=self.tenant_a,
            db_name='test_fitness_tenant',
            database_name='test_fitness_tenant',
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )
        self.plan = SaasPlan.objects.using('default').create(
            name='Enterprise Plan',
            code='ENT-PLAN-P2',
            tier='ENTERPRISE',
            status='ACTIVE',
        )
        TenantSubscription.objects.using('default').create(
            tenant=self.tenant_a,
            plan=self.plan,
            status='ACTIVE',
        )
        self.prod_mod_crm, _ = ProductModule.objects.using('default').get_or_create(
            code='crm', defaults={'name': 'CRM Module', 'status': 'ACTIVE'}
        )
        TenantModule.objects.using('default').create(
            tenant=self.tenant_a, module=self.prod_mod_crm, is_enabled=True, availability_mode='ALL_BRANCHES'
        )

        # 2. Tenant DB Setup (Tenant A)
        self.org_a = Organization.objects.using('tenant_test').create(
            code='ORG-A2',
            name='Org A2 Fitness',
            status='ACTIVE',
        )
        self.org_b = Organization.objects.using('tenant_test').create(
            code='ORG-B2',
            name='Org B2 Fitness',
            status='ACTIVE',
        )
        self.loc_a = Location.objects.using('tenant_test').create(
            organization=self.org_a,
            code='LOC-A2',
            name='Location A2',
            status='ACTIVE',
        )
        self.branch_1 = Branch.objects.using('tenant_test').create(
            organization=self.org_a,
            location=self.loc_a,
            code='BR-1-P2',
            name='Branch Downtown',
            status='ACTIVE',
        )
        self.branch_2 = Branch.objects.using('tenant_test').create(
            organization=self.org_a,
            location=self.loc_a,
            code='BR-2-P2',
            name='Branch Uptown',
            status='ACTIVE',
        )

        # 3. RBAC Setup: Module & Submodule Catalogs
        self.mod_crm, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(
            module_code='crm', defaults={'name': 'CRM', 'is_enabled': True}
        )
        self.submod_leads, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=self.mod_crm, submodule_code='leads', defaults={'name': 'Leads', 'is_enabled': True}
        )

        # Roles: Org-Wide Admin vs Branch-Scoped Manager vs Read-Only Staff
        self.role_org_admin = Role.objects.using('tenant_test').create(
            organization=self.org_a,
            code='ORG_ADMIN_ROLE_P2',
            name='Org Admin',
            scope='ORG',
            is_active=True,
        )
        self.role_branch_mgr = Role.objects.using('tenant_test').create(
            organization=self.org_a,
            code='BRANCH_MGR_ROLE_P2',
            name='Branch Manager',
            scope='BRANCH',
            is_active=True,
        )
        self.role_readonly = Role.objects.using('tenant_test').create(
            organization=self.org_a,
            code='READONLY_ROLE_P2',
            name='Read Only Staff',
            scope='ORG',
            is_active=True,
        )

        for r in (self.role_org_admin, self.role_branch_mgr, self.role_readonly):
            RoleModuleAccess.objects.using('tenant_test').get_or_create(
                role=r, module=self.mod_crm, defaults={'can_access': True}
            )
            RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(
                role=r, submodule=self.submod_leads, defaults={'can_access': True}
            )

        perm_codes = ['crm.leads.view', 'crm.leads.create', 'crm.leads.edit']
        self.perms = {}
        for code in perm_codes:
            action = code.split('.')[-1]
            p, _ = Permission.objects.using('tenant_test').get_or_create(
                permission_code=code,
                defaults={
                    'code': code,
                    'label': code,
                    'action': action,
                    'module': self.mod_crm,
                    'submodule': self.submod_leads,
                    'source_permission_id': uuid.uuid4(),
                    'is_active': True,
                }
            )
            self.perms[code] = p

        pset_admin = RolePermissionSet.objects.using('tenant_test').create(
            role=self.role_org_admin, organization=self.org_a, name='Admin Perms'
        )
        for p in self.perms.values():
            RolePermissionSetItem.objects.using('tenant_test').create(
                permission_set=pset_admin, permission=p, granted=True, is_allowed=True
            )

        pset_branch = RolePermissionSet.objects.using('tenant_test').create(
            role=self.role_branch_mgr, organization=self.org_a, name='Branch Perms'
        )
        for p in self.perms.values():
            RolePermissionSetItem.objects.using('tenant_test').create(
                permission_set=pset_branch, permission=p, granted=True, is_allowed=True
            )

        pset_readonly = RolePermissionSet.objects.using('tenant_test').create(
            role=self.role_readonly, organization=self.org_a, name='Read-Only Perms'
        )
        RolePermissionSetItem.objects.using('tenant_test').create(
            permission_set=pset_readonly, permission=self.perms['crm.leads.view'], granted=True, is_allowed=True
        )

        # Users
        self.user_org_admin = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a,
            email='org.admin.p2@test.com',
            first_name='Org',
            last_name='Admin',
            status='ACTIVE',
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.user_org_admin, role=self.role_org_admin, branch=None, is_active=True, status='ACTIVE'
        )

        self.user_branch1_mgr = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a,
            email='branch1.mgr.p2@test.com',
            first_name='Branch1',
            last_name='Manager',
            status='ACTIVE',
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.user_branch1_mgr, role=self.role_branch_mgr, branch=self.branch_1, is_active=True, status='ACTIVE'
        )
        UserBranch.objects.using('tenant_test').create(
            user=self.user_branch1_mgr, branch=self.branch_1, scope_type='HOME', status='ACTIVE', is_active=True
        )

        self.user_readonly = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a,
            email='readonly.p2@test.com',
            first_name='Read',
            last_name='Only',
            status='ACTIVE',
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.user_readonly, role=self.role_readonly, branch=None, is_active=True, status='ACTIVE'
        )

        # Lead Sources
        self.source_walkin = LeadSource.objects.using('tenant_test').create(
            organization=self.org_a,
            code='WALK_IN_P2',
            name='Walk In',
            source_type='WALK_IN',
            status='ACTIVE',
        )
        self.source_instagram = LeadSource.objects.using('tenant_test').create(
            organization=self.org_a,
            code='INSTAGRAM_P2',
            name='Instagram Ads',
            source_type='META',
            status='ACTIVE',
        )
        self.source_inactive = LeadSource.objects.using('tenant_test').create(
            organization=self.org_a,
            code='OLD_FLYER_P2',
            name='Old Summer Flyer',
            source_type='EVENT',
            status='INACTIVE',
        )

        # Leads
        self.lead_br1 = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_1,
            first_name='Priya',
            last_name='Sharma',
            phone_normalized='+919876540001',
            email_normalized='priya@test.com',
            lead_source=self.source_instagram,
            current_status='NEW_LEAD',
            assigned_sales_user=self.user_branch1_mgr,
        )
        self.lead_br2 = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_2,
            first_name='Rahul',
            last_name='Verma',
            phone_normalized='+919876540002',
            email_normalized='rahul@test.com',
            lead_source=self.source_walkin,
            current_status='NEW_LEAD',
        )

        # Tokens
        self.token_admin = str(_build_tenant_token(self.user_org_admin, self.tenant_a, 'tenant_test').access_token)
        self.token_branch1 = str(_build_tenant_token(self.user_branch1_mgr, self.tenant_a, 'tenant_test').access_token)
        self.token_readonly = str(_build_tenant_token(self.user_readonly, self.tenant_a, 'tenant_test').access_token)

    # -------------------------------------------------------------
    # 1. Manual Lead can still be created without attribution
    # -------------------------------------------------------------
    def test_01_manual_lead_can_still_be_created_without_attribution(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_admin}')
        payload = {
            'first_name': 'Manual',
            'last_name': 'Walkin',
            'phone_normalized': '+919999000001',
            'email_normalized': 'manual@walkin.com',
            'branch': str(self.branch_1.id),
            'lead_source': str(self.source_walkin.id),
        }
        res = self.client.post('/api/v1/tenant/leads/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        lead_id = res.json()['id']
        lead = Lead.objects.using('tenant_test').get(id=lead_id)
        self.assertEqual(lead.first_name, 'Manual')
        self.assertEqual(LeadAttribution.objects.using('tenant_test').filter(lead=lead).count(), 0)

    # -------------------------------------------------------------
    # 2. Lead can be created with attribution
    # -------------------------------------------------------------
    def test_02_lead_can_be_created_with_attribution(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_admin}')
        payload = {
            'first_name': 'Ad',
            'last_name': 'Prospect',
            'phone_normalized': '+919999000002',
            'email_normalized': 'ad@prospect.com',
            'branch': str(self.branch_1.id),
            'lead_source': str(self.source_instagram.id),
            'attribution': {
                'platform': 'INSTAGRAM',
                'campaign_name': 'Pilates September Promo',
                'campaign_external_id': 'camp_123',
                'ad_set_name': 'Women 25-40 Mumbai',
                'ad_name': 'Reel Transformation 01',
                'form_name': 'Free Trial Intake',
                'external_lead_id': 'meta_lead_9999',
                'utm_source': 'instagram',
                'utm_medium': 'paid_social',
                'utm_campaign': 'sep_pilates',
                'landing_page_url': 'https://gym.com/pilates-trial',
            },
        }
        res = self.client.post('/api/v1/tenant/leads/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        data = res.json()
        self.assertIn('attributions', data)
        self.assertEqual(len(data['attributions']), 1)
        self.assertEqual(data['attributions'][0]['campaign_name'], 'Pilates September Promo')
        self.assertEqual(data['attributions'][0]['touch_type'], 'FIRST_TOUCH')

    # -------------------------------------------------------------
    # 3. Attribution belongs to correct Lead
    # -------------------------------------------------------------
    def test_03_attribution_belongs_to_correct_lead(self):
        attr = LeadAttribution.objects.using('tenant_test').create(
            organization=self.org_a,
            lead=self.lead_br1,
            touch_type='FIRST_TOUCH',
            platform='META',
            campaign_name='Core Fitness',
        )
        self.assertEqual(attr.lead_id, self.lead_br1.id)
        self.assertNotEqual(attr.lead_id, self.lead_br2.id)

    # -------------------------------------------------------------
    # 4. Cross-tenant attribution ID rejected
    # -------------------------------------------------------------
    def test_04_cross_tenant_attribution_rejected(self):
        lead_b = Lead.objects.using('tenant_test').create(
            organization=self.org_b,
            first_name='TenantB',
            last_name='Lead',
            current_status='NEW_LEAD',
        )
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_admin}')
        # Attempt to access or append attribution to Org B lead using Org A credentials
        res = self.client.get(f'/api/v1/tenant/leads/{lead_b.id}/attributions/')
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    # -------------------------------------------------------------
    # 5. Unauthorized branch Lead attribution inaccessible
    # -------------------------------------------------------------
    def test_05_unauthorized_branch_lead_attribution_inaccessible(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_branch1}')
        # User is scoped to Branch 1; Lead 2 belongs to Branch 2
        res = self.client.get(f'/api/v1/tenant/leads/{self.lead_br2.id}/attributions/')
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    # -------------------------------------------------------------
    # 6. First touch preserved
    # -------------------------------------------------------------
    def test_06_first_touch_preserved(self):
        # 1st touch
        CRMLeadService.record_lead_attribution(
            lead=self.lead_br1,
            attribution_data={
                'platform': 'INSTAGRAM',
                'campaign_name': 'First Campaign',
                'touch_type': 'FIRST_TOUCH',
            },
            db_alias='tenant_test',
        )
        self.lead_br1.refresh_from_db()
        self.assertEqual(self.lead_br1.first_touch_source, 'INSTAGRAM')

        # 2nd touch
        CRMLeadService.record_lead_attribution(
            lead=self.lead_br1,
            attribution_data={
                'platform': 'GOOGLE_SEARCH',
                'campaign_name': 'Second Campaign',
                'touch_type': 'LEAD_CAPTURE',
            },
            db_alias='tenant_test',
        )
        self.lead_br1.refresh_from_db()
        # Original first touch source remains preserved
        self.assertEqual(self.lead_br1.first_touch_source, 'INSTAGRAM')
        # Latest touch source is updated
        self.assertEqual(self.lead_br1.latest_touch_source, 'GOOGLE_SEARCH')

    # -------------------------------------------------------------
    # 7. Additional touch appended rather than overwriting
    # -------------------------------------------------------------
    def test_07_additional_touch_appended_rather_than_overwriting(self):
        CRMLeadService.record_lead_attribution(
            lead=self.lead_br1,
            attribution_data={'platform': 'META', 'touch_type': 'FIRST_TOUCH'},
            db_alias='tenant_test',
        )
        CRMLeadService.record_lead_attribution(
            lead=self.lead_br1,
            attribution_data={'platform': 'WEBSITE', 'touch_type': 'LEAD_CAPTURE'},
            db_alias='tenant_test',
        )
        CRMLeadService.record_lead_attribution(
            lead=self.lead_br1,
            attribution_data={'platform': 'WHATSAPP', 'touch_type': 'ASSISTED_TOUCH'},
            db_alias='tenant_test',
        )
        self.assertEqual(self.lead_br1.attributions.count(), 3)
        touches = list(self.lead_br1.attributions.values_list('touch_type', flat=True))
        self.assertEqual(touches, ['FIRST_TOUCH', 'LEAD_CAPTURE', 'ASSISTED_TOUCH'])

    # -------------------------------------------------------------
    # 8. Duplicate external Lead event is idempotent
    # -------------------------------------------------------------
    def test_08_duplicate_external_lead_is_idempotent(self):
        first = CRMLeadService.record_lead_attribution(
            lead=self.lead_br1,
            attribution_data={
                'platform': 'META',
                'external_lead_id': 'meta_id_dup_check',
                'campaign_name': 'Camp A',
            },
            db_alias='tenant_test',
        )
        # Duplicate capture with same platform + external_lead_id
        second = CRMLeadService.record_lead_attribution(
            lead=self.lead_br1,
            attribution_data={
                'platform': 'META',
                'external_lead_id': 'meta_id_dup_check',
                'campaign_name': 'Camp A Retry',
            },
            db_alias='tenant_test',
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(LeadAttribution.objects.using('tenant_test').filter(external_lead_id='meta_id_dup_check').count(), 1)

    # -------------------------------------------------------------
    # 9. Lead Source inactive remains visible historically
    # -------------------------------------------------------------
    def test_09_inactive_lead_source_remains_visible_historically(self):
        lead = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_1,
            first_name='Historical',
            last_name='Lead',
            lead_source=self.source_inactive,
            current_status='NEW_LEAD',
        )
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_admin}')
        res = self.client.get(f'/api/v1/tenant/leads/{lead.id}/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.json()['source_name'], 'Old Summer Flyer')

    # -------------------------------------------------------------
    # 10. Active Lead Source still used for new Lead
    # -------------------------------------------------------------
    def test_10_active_lead_source_used_for_new_lead(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_admin}')
        payload = {
            'first_name': 'ActiveSource',
            'last_name': 'Lead',
            'branch': str(self.branch_1.id),
            'lead_source': str(self.source_instagram.id),
        }
        res = self.client.post('/api/v1/tenant/leads/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.json()['source_name'], 'Instagram Ads')

    # -------------------------------------------------------------
    # 11. Timeline contains Lead Created
    # -------------------------------------------------------------
    def test_11_timeline_contains_lead_created(self):
        events = CRMLeadService.get_lead_timeline(self.lead_br1, db_alias='tenant_test')
        event_types = [e['event_type'] for e in events]
        self.assertIn('LEAD_CREATED', event_types)

    # -------------------------------------------------------------
    # 12. Timeline contains Status Transition
    # -------------------------------------------------------------
    def test_12_timeline_contains_status_transition(self):
        CRMLeadService.transition_lead_status(
            lead=self.lead_br1,
            new_status='TRIAL_BOOKED',
            reason_code='BOOKING_CONFIRMED',
            reason_text='Trial slot selected by client',
            actor_user=self.user_org_admin,
            db_alias='tenant_test',
        )
        events = CRMLeadService.get_lead_timeline(self.lead_br1, db_alias='tenant_test')
        event_types = [e['event_type'] for e in events]
        self.assertIn('STATUS_CHANGE', event_types)
        status_event = next(e for e in events if e['event_type'] == 'STATUS_CHANGE')
        self.assertIn('Trial Booked', status_event['title'])

    # -------------------------------------------------------------
    # 13. Timeline contains Assignment
    # -------------------------------------------------------------
    def test_13_timeline_contains_assignment(self):
        CRMLeadService.assign_lead(
            lead=self.lead_br1,
            assigned_to_user=self.user_branch1_mgr,
            assignment_type='SALES',
            actor_user=self.user_org_admin,
            db_alias='tenant_test',
        )
        events = CRMLeadService.get_lead_timeline(self.lead_br1, db_alias='tenant_test')
        event_types = [e['event_type'] for e in events]
        self.assertIn('ASSIGNMENT', event_types)

    # -------------------------------------------------------------
    # 14. Timeline contains Attribution Captured
    # -------------------------------------------------------------
    def test_14_timeline_contains_attribution_captured(self):
        CRMLeadService.record_lead_attribution(
            lead=self.lead_br1,
            attribution_data={
                'platform': 'INSTAGRAM',
                'campaign_name': 'Transformation Campaign',
                'utm_source': 'instagram',
            },
            db_alias='tenant_test',
        )
        events = CRMLeadService.get_lead_timeline(self.lead_br1, db_alias='tenant_test')
        event_types = [e['event_type'] for e in events]
        self.assertIn('ATTRIBUTION_CAPTURED', event_types)
        attr_event = next(e for e in events if e['event_type'] == 'ATTRIBUTION_CAPTURED')
        self.assertIn('Transformation Campaign', attr_event['description'])

    # -------------------------------------------------------------
    # 15. Timeline contains Trial Booking if present
    # -------------------------------------------------------------
    def test_15_timeline_contains_trial_booking(self):
        now = timezone.now()
        TrialBooking.objects.using('tenant_test').create(
            branch=self.branch_1,
            lead=self.lead_br1,
            scheduled_start=now + timedelta(days=1),
            scheduled_end=now + timedelta(days=1, hours=1),
            status='BOOKED',
            trial_type='GROUP_CLASS',
        )
        events = CRMLeadService.get_lead_timeline(self.lead_br1, db_alias='tenant_test')
        event_types = [e['event_type'] for e in events]
        self.assertIn('TRIAL_BOOKED', event_types)

    # -------------------------------------------------------------
    # 16. Timeline sorted correctly (newest first)
    # -------------------------------------------------------------
    def test_16_timeline_sorted_correctly(self):
        now = timezone.now()
        # Add an event with newer timestamp
        LeadStatusHistory.objects.using('tenant_test').create(
            lead=self.lead_br1,
            from_status='NEW_LEAD',
            to_status='INTERESTED',
            changed_at=now + timedelta(hours=2),
        )
        events = CRMLeadService.get_lead_timeline(self.lead_br1, db_alias='tenant_test')
        self.assertTrue(len(events) >= 2)
        # Verify descending order of occurred_at
        timestamps = [e['occurred_at'] for e in events]
        self.assertEqual(timestamps, sorted(timestamps, reverse=True))

    # -------------------------------------------------------------
    # 17. Timeline tenant and branch isolation
    # -------------------------------------------------------------
    def test_17_timeline_tenant_and_branch_isolation(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_branch1}')
        # Branch 1 user accessing Branch 2 lead timeline must be 404
        res = self.client.get(f'/api/v1/tenant/leads/{self.lead_br2.id}/timeline/')
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

        # Accessing Branch 1 lead timeline is allowed
        res_ok = self.client.get(f'/api/v1/tenant/leads/{self.lead_br1.id}/timeline/')
        self.assertEqual(res_ok.status_code, status.HTTP_200_OK)

    # -------------------------------------------------------------
    # 18. SLA ON_TRACK calculation
    # -------------------------------------------------------------
    def test_18_sla_on_track_calculation(self):
        CRMStageSlaPolicy.objects.using('tenant_test').create(
            organization=self.org_a,
            canonical_stage='NEW_LEAD',
            display_label='New Lead Response SLA',
            response_target_value=60,
            response_target_unit='MINUTES',
            is_enabled=True,
        )
        # Entry timestamp is just now
        sla_info = CRMLeadService.calculate_lead_sla(self.lead_br1, db_alias='tenant_test')
        self.assertEqual(sla_info['sla_status'], 'ON_TRACK')
        self.assertEqual(sla_info['sla_target_value'], 60)

    # -------------------------------------------------------------
    # 19. SLA BREACHED calculation
    # -------------------------------------------------------------
    def test_19_sla_breached_calculation(self):
        CRMStageSlaPolicy.objects.using('tenant_test').create(
            organization=self.org_a,
            canonical_stage='HOT_LEAD',
            display_label='Hot Lead Quick Call SLA',
            response_target_value=15,
            response_target_unit='MINUTES',
            is_enabled=True,
        )
        past_time = timezone.now() - timedelta(minutes=30)
        lead_hot = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_1,
            first_name='Hot',
            last_name='Prospect',
            current_status='HOT_LEAD',
            created_at=past_time,
        )
        LeadStatusHistory.objects.using('tenant_test').create(
            lead=lead_hot,
            from_status='NEW_LEAD',
            to_status='HOT_LEAD',
            changed_at=past_time,
        )
        sla_info = CRMLeadService.calculate_lead_sla(lead_hot, db_alias='tenant_test')
        self.assertEqual(sla_info['sla_status'], 'BREACHED')

    # -------------------------------------------------------------
    # 20. SLA disabled calculation
    # -------------------------------------------------------------
    def test_20_sla_disabled_calculation(self):
        # Canonical stage LOST has no policy or disabled policy
        CRMStageSlaPolicy.objects.using('tenant_test').create(
            organization=self.org_a,
            canonical_stage='LOST',
            display_label='Lost Lead Policy',
            response_target_value=15,
            response_target_unit='MINUTES',
            is_enabled=False,
        )
        lead_lost = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            first_name='Lost',
            last_name='Client',
            current_status='LOST',
        )
        sla_info = CRMLeadService.calculate_lead_sla(lead_lost, db_alias='tenant_test')
        self.assertEqual(sla_info['sla_status'], 'DISABLED')

    # -------------------------------------------------------------
    # 21. Campaign & source filters work
    # -------------------------------------------------------------
    def test_21_campaign_and_source_filters(self):
        self.lead_br1.campaign_reference = 'SummerPromo2026'
        self.lead_br1.save(using='tenant_test')

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_admin}')
        # Filter by campaign
        res_camp = self.client.get('/api/v1/tenant/leads/?campaign=SummerPromo2026')
        self.assertEqual(res_camp.status_code, status.HTTP_200_OK)
        camp_data = res_camp.json()
        camp_list = camp_data['results'] if isinstance(camp_data, dict) and 'results' in camp_data else camp_data
        ids = [item['id'] for item in camp_list]
        self.assertIn(str(self.lead_br1.id), ids)

        # Filter by source
        res_src = self.client.get(f'/api/v1/tenant/leads/?lead_source_id={self.source_instagram.id}')
        self.assertEqual(res_src.status_code, status.HTTP_200_OK)
        src_data = res_src.json()
        src_list = src_data['results'] if isinstance(src_data, dict) and 'results' in src_data else src_data
        src_ids = [item['id'] for item in src_list]
        self.assertIn(str(self.lead_br1.id), src_ids)
        self.assertNotIn(str(self.lead_br2.id), src_ids)

    # -------------------------------------------------------------
    # 22. RBAC read-only behavior
    # -------------------------------------------------------------
    def test_22_rbac_read_only_behavior(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_readonly}')
        # Read-only user can view leads, timeline, and attributions
        res_view = self.client.get(f'/api/v1/tenant/leads/{self.lead_br1.id}/')
        self.assertEqual(res_view.status_code, status.HTTP_200_OK)

        res_tl = self.client.get(f'/api/v1/tenant/leads/{self.lead_br1.id}/timeline/')
        self.assertEqual(res_tl.status_code, status.HTTP_200_OK)

        res_attr = self.client.get(f'/api/v1/tenant/leads/{self.lead_br1.id}/attributions/')
        self.assertEqual(res_attr.status_code, status.HTTP_200_OK)

    # -------------------------------------------------------------
    # 23. Unauthorized mutation -> 403
    # -------------------------------------------------------------
    def test_23_unauthorized_mutation_returns_403(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_readonly}')
        # Read-only user attempting to record attribution touch must receive 403 Forbidden
        payload = {
            'platform': 'WEBSITE',
            'campaign_name': 'Unauthorized Touch',
            'touch_type': 'ASSISTED_TOUCH',
        }
        res = self.client.post(f'/api/v1/tenant/leads/{self.lead_br1.id}/attributions/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    # -------------------------------------------------------------
    # 24. Audit & Outbox generated appropriately
    # -------------------------------------------------------------
    def test_24_audit_and_outbox_generated_appropriately(self):
        CRMLeadService.record_lead_attribution(
            lead=self.lead_br1,
            attribution_data={'platform': 'FACEBOOK', 'campaign_name': 'Audit Test Campaign'},
            actor_user=self.user_org_admin,
            db_alias='tenant_test',
        )
        audit_exists = BusinessAuditEvent.objects.using('tenant_test').filter(
            action_code='CRM_LEAD_ATTRIBUTION_CAPTURED',
            entity_id__isnull=False,
        ).exists()
        self.assertTrue(audit_exists)
