"""
tests/test_crm_leads_intake_targeted.py — Targeted Tests for CRM Leads Intake & Legacy Parity (Part Q)
Tests:
1. Create Lead basic required fields
2. Create Lead with all legacy-compatible fields
3. Invalid branch rejected
4. Inactive branch rejected
5. Invalid program/interest rejected
6. Assigned agent scope validation
7. Lead source validation
8. Optional GST/PAN persistence
9. Duplicate detection behavior preserved
10. Default initial stage
11. LeadStageHistory creation
12. Audit event creation
13. Edit Lead
14. RBAC create denied -> 403
15. RBAC edit denied -> 403
16. Tenant isolation
17. Branch scope
18. Lead API uses valid tenant JWT
19. Pipeline API uses valid tenant JWT
"""

import uuid
from datetime import date, timedelta
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
from apps.tenant_core.models_catalog import ProgramCategory, Program
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
    LeadStatusHistory,
    LeadAssignment,
    LeadCommercialProfile,
)
from apps.tenant_core.services_crm import CRMLeadService
from apps.tenant_core.models_audit_outbox import BusinessAuditEvent, DomainOutboxEvent


class TargetedCRMLeadsIntakeTests(TestCase):
    databases = '__all__'

    def setUp(self):
        super().setUp()
        set_tenant_db_alias('tenant_test')
        self.client = APIClient()

        # 1. Master DB Setup
        self.tenant = Tenant.objects.using('default').create(
            code='CRM-TEST-TENANT',
            name='Targeted CRM Tenant',
            slug='targeted-crm-tenant',
            status='ACTIVE',
        )
        self.ds = TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            db_name='test_fitness_tenant',
            database_name='test_fitness_tenant',
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )
        self.plan = SaasPlan.objects.using('default').create(
            name='Enterprise CRM Plan',
            code='ENT-CRM',
            tier='ENTERPRISE',
            status='ACTIVE',
        )
        self.sub = TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
        )
        self.prod_mod_crm, _ = ProductModule.objects.using('default').get_or_create(
            code='crm', defaults={'name': 'CRM Module', 'status': 'ACTIVE'}
        )
        self.prod_mod_core, _ = ProductModule.objects.using('default').get_or_create(
            code='core', defaults={'name': 'Core Module', 'status': 'ACTIVE'}
        )
        TenantModule.objects.using('default').create(
            tenant=self.tenant, module=self.prod_mod_crm, is_enabled=True, availability_mode='ALL_BRANCHES'
        )
        TenantModule.objects.using('default').create(
            tenant=self.tenant, module=self.prod_mod_core, is_enabled=True, availability_mode='ALL_BRANCHES'
        )

        # 2. Tenant DB Setup
        self.org = Organization.objects.using('tenant_test').create(
            code='CRM-TEST-ORG',
            name='Targeted CRM Org',
            status='ACTIVE',
        )
        self.loc = Location.objects.using('tenant_test').create(
            organization=self.org,
            code='LOC-1',
            name='Location 1',
            status='ACTIVE',
        )
        self.branch_active = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            code='BR-ACTIVE',
            name='Active Branch',
            status='ACTIVE',
        )
        self.branch_inactive = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            code='BR-INACTIVE',
            name='Inactive Branch',
            status='INACTIVE',
        )

        # 3. Programs
        self.category = ProgramCategory.objects.using('tenant_test').create(
            organization=self.org,
            name='Pilates Category',
            status='ACTIVE',
        )
        self.program_active = Program.objects.using('tenant_test').create(
            organization=self.org,
            category=self.category,
            name='Reformer Pilates',
            code='PROG-PILATES',
            status='ACTIVE',
        )
        self.program_inactive = Program.objects.using('tenant_test').create(
            organization=self.org,
            category=self.category,
            name='Archived Bootcamp',
            code='PROG-BOOTCAMP',
            status='INACTIVE',
        )

        # 4. Users (Admin, Sales Agent, Restricted User)
        self.admin_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='admin.intake@test.com',
            first_name='Admin',
            last_name='User',
            status='ACTIVE',
        )
        self.admin_user.set_password('Secret123!')
        self.admin_user.save(using='tenant_test')

        self.agent_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='agent.intake@test.com',
            first_name='Anurag',
            last_name='Sales',
            status='ACTIVE',
        )
        UserBranch.objects.using('tenant_test').create(
            user=self.agent_user,
            branch=self.branch_active,
            scope_type='HOME',
            status='ACTIVE',
            is_active=True,
        )

        self.agent_inactive = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='inactive.agent@test.com',
            first_name='Inactive',
            last_name='Agent',
            status='INACTIVE',
        )

        self.restricted_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='restricted.user@test.com',
            first_name='Restricted',
            last_name='User',
            status='ACTIVE',
        )

        # 5. RBAC Catalogs & Permissions
        self.mod_crm, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(
            module_code='crm', defaults={'name': 'CRM', 'is_enabled': True}
        )
        self.sub_leads, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=self.mod_crm, submodule_code='leads', defaults={'name': 'Leads', 'is_enabled': True}
        )
        self.perm_view, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.mod_crm,
            submodule=self.sub_leads,
            action='view',
            defaults={'permission_code': 'crm.leads.view', 'label': 'View Leads'},
        )
        self.perm_create, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.mod_crm,
            submodule=self.sub_leads,
            action='create',
            defaults={'permission_code': 'crm.leads.create', 'label': 'Create Leads'},
        )
        self.perm_edit, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.mod_crm,
            submodule=self.sub_leads,
            action='edit',
            defaults={'permission_code': 'crm.leads.edit', 'label': 'Edit Leads'},
        )

        # Full CRM Role for Admin
        self.role_admin = Role.objects.using('tenant_test').create(
            organization=self.org,
            code='CRM_ADMIN_ROLE',
            name='CRM Admin Role',
            scope='ORG',
            is_active=True,
            status='ACTIVE',
        )
        RoleModuleAccess.objects.using('tenant_test').get_or_create(role=self.role_admin, module=self.mod_crm, defaults={'can_access': True})
        RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(role=self.role_admin, submodule=self.sub_leads, defaults={'can_access': True})
        self.pset_admin = RolePermissionSet.objects.using('tenant_test').create(
            role=self.role_admin, name='Admin PSet', is_active=True
        )
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=self.pset_admin, permission=self.perm_view, granted=True)
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=self.pset_admin, permission=self.perm_create, granted=True)
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=self.pset_admin, permission=self.perm_edit, granted=True)
        RoleAssignment.objects.using('tenant_test').create(
            user=self.admin_user, role=self.role_admin, organization=self.org, status='ACTIVE', is_active=True
        )

        # View-Only Role for Restricted User
        self.role_view = Role.objects.using('tenant_test').create(
            organization=self.org,
            code='CRM_VIEW_ROLE',
            name='CRM View Role',
            scope='ORG',
            is_active=True,
            status='ACTIVE',
        )
        RoleModuleAccess.objects.using('tenant_test').get_or_create(role=self.role_view, module=self.mod_crm, defaults={'can_access': True})
        RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(role=self.role_view, submodule=self.sub_leads, defaults={'can_access': True})
        self.pset_view = RolePermissionSet.objects.using('tenant_test').create(
            role=self.role_view, name='View PSet', is_active=True
        )
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=self.pset_view, permission=self.perm_view, granted=True)
        RoleAssignment.objects.using('tenant_test').create(
            user=self.restricted_user, role=self.role_view, organization=self.org, status='ACTIVE', is_active=True
        )

        # 6. Lead Source
        self.source_website = LeadSource.objects.using('tenant_test').create(
            organization=self.org,
            code='WEBSITE',
            name='Website Inquiry',
            source_type='ONLINE',
            status='ACTIVE',
        )

    def tearDown(self):
        set_tenant_db_alias(None)
        super().tearDown()

    def get_token(self, user):
        refresh = _build_tenant_token(user=user, tenant=self.tenant, db_alias='tenant_test')
        return str(refresh.access_token)

    # 1. Create Lead basic required fields
    def test_01_create_lead_basic_required_fields(self):
        token = self.get_token(self.admin_user)
        res = self.client.post(
            '/api/v1/tenant/leads/',
            {
                'first_name': 'Aarav',
                'last_name': 'Mehta',
                'email': 'aarav.mehta@test.com',
                'phone': '+919876543210',
            },
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['first_name'], 'Aarav')
        self.assertEqual(res.data['last_name'], 'Mehta')
        self.assertEqual(res.data['current_status'], 'NEW_LEAD')

    # 2. Create Lead with all legacy-compatible fields
    def test_02_create_lead_with_all_legacy_fields(self):
        token = self.get_token(self.admin_user)
        payload = {
            'first_name': 'Sandhya',
            'last_name': 'Kamath',
            'email': 'sandhya.k@legacy.com',
            'phone': '+919988776655',
            'gender': 'Female',
            'date_of_birth': '1992-05-14',
            'country': 'India',
            'area': 'Andheri West, Mumbai',
            'branch': str(self.branch_active.id),
            'interested_program': str(self.program_active.id),
            'fitness_goal': 'Improve mobility and tone core',
            'lead_source': str(self.source_website.id),
            'assigned_sales_user': str(self.agent_user.id),
            'referred_by_name': 'Dr. Kulkarni',
            'billing_name': 'Kamath Health Enterprises',
            'gst_number': '27ABCDE1234F1Z5',
            'pan_number': 'ABCDE1234F',
        }
        res = self.client.post('/api/v1/tenant/leads/', payload, HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['first_name'], 'Sandhya')
        self.assertEqual(str(res.data['branch']), str(self.branch_active.id))
        self.assertEqual(res.data['branch_name'], self.branch_active.name)
        self.assertEqual(str(res.data['interested_program']), str(self.program_active.id))
        self.assertEqual(res.data['interested_program_name'], self.program_active.name)
        self.assertEqual(res.data['billing_name'], 'Kamath Health Enterprises')
        self.assertEqual(res.data['gst_number'], '27ABCDE1234F1Z5')
        self.assertEqual(res.data['pan_number'], 'ABCDE1234F')

    # 3. Invalid branch rejected
    def test_03_invalid_branch_rejected(self):
        token = self.get_token(self.admin_user)
        res = self.client.post(
            '/api/v1/tenant/leads/',
            {
                'first_name': 'Invalid',
                'last_name': 'Branch',
                'branch': str(uuid.uuid4()),
            },
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertIn(res.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_403_FORBIDDEN])

    # 4. Inactive branch rejected
    def test_04_inactive_branch_rejected(self):
        token = self.get_token(self.admin_user)
        res = self.client.post(
            '/api/v1/tenant/leads/',
            {
                'first_name': 'Inactive',
                'last_name': 'BranchTest',
                'branch': str(self.branch_inactive.id),
            },
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertIn(res.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_403_FORBIDDEN])

    # 5. Invalid program/interest rejected
    def test_05_invalid_program_rejected(self):
        token = self.get_token(self.admin_user)
        res = self.client.post(
            '/api/v1/tenant/leads/',
            {
                'first_name': 'Inactive',
                'last_name': 'ProgTest',
                'interested_program': str(self.program_inactive.id),
            },
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('interested_program', res.data)

    # 6. Assigned agent scope validation
    def test_06_assigned_agent_validation(self):
        token = self.get_token(self.admin_user)
        res = self.client.post(
            '/api/v1/tenant/leads/',
            {
                'first_name': 'Agent',
                'last_name': 'Validation',
                'assigned_sales_user': str(self.agent_inactive.id),
            },
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('assigned_sales_user', res.data)

    # 7. Lead source validation
    def test_07_lead_source_validation(self):
        token = self.get_token(self.admin_user)
        res = self.client.post(
            '/api/v1/tenant/leads/',
            {
                'first_name': 'Source',
                'last_name': 'Test',
                'lead_source': str(uuid.uuid4()),
            },
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    # 8. Optional GST/PAN persistence
    def test_08_optional_gst_pan_persistence(self):
        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Commercial',
            last_name='Prospect',
            extra_fields={
                'billing_name': 'Fitness Corp Ltd',
                'gst_number': '27ABCDE1234F1Z5',
                'pan_number': 'ABCDE1234F',
            },
            db_alias='tenant_test',
        )
        cp = LeadCommercialProfile.objects.using('tenant_test').get(lead=lead)
        self.assertEqual(cp.billing_name, 'Fitness Corp Ltd')
        self.assertEqual(cp.gst_number, '27ABCDE1234F1Z5')
        self.assertEqual(cp.pan_number, 'ABCDE1234F')

    # 9. Duplicate detection behavior preserved
    def test_09_duplicate_detection(self):
        CRMLeadService.create_lead(
            organization=self.org,
            first_name='Existing',
            last_name='Lead',
            phone='+919999900001',
            email='existing.lead@test.com',
            db_alias='tenant_test',
        )
        token = self.get_token(self.admin_user)
        res = self.client.post(
            '/api/v1/tenant/leads/check-duplicates/',
            {'phone': '+919999900001', 'email': 'other@test.com'},
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.data['has_duplicate'])
        self.assertEqual(len(res.data['matches']), 1)
        self.assertEqual(res.data['matches'][0]['full_name'], 'Existing Lead')

    # 10. Default initial stage
    def test_10_default_initial_stage(self):
        token = self.get_token(self.admin_user)
        res = self.client.post(
            '/api/v1/tenant/leads/',
            {'first_name': 'Init', 'last_name': 'Stage'},
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['current_status'], 'NEW_LEAD')

    # 11. LeadStageHistory creation
    def test_11_lead_stage_history_creation(self):
        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='History',
            last_name='Tester',
            actor_user=self.admin_user,
            db_alias='tenant_test',
        )
        hist = LeadStatusHistory.objects.using('tenant_test').filter(lead=lead)
        self.assertEqual(hist.count(), 1)
        self.assertEqual(hist.first().to_status, 'NEW_LEAD')
        self.assertEqual(hist.first().reason_code, 'LEAD_CREATION')

    # 12. Audit event creation
    def test_12_audit_event_creation(self):
        CRMLeadService.create_lead(
            organization=self.org,
            first_name='Audit',
            last_name='Verified',
            actor_user=self.admin_user,
            db_alias='tenant_test',
        )
        audit = BusinessAuditEvent.objects.using('tenant_test').filter(action_code='LEAD_CREATE').first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.module, 'crm')
        self.assertEqual(audit.entity_type, 'Lead')

    # 13. Edit Lead
    def test_13_edit_lead(self):
        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Before',
            last_name='Edit',
            db_alias='tenant_test',
        )
        token = self.get_token(self.admin_user)
        res = self.client.patch(
            f'/api/v1/tenant/leads/{lead.id}/',
            {
                'first_name': 'After',
                'fitness_goal': 'Six Pack Abs',
                'billing_name': 'Updated Billing Org',
            },
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['first_name'], 'After')
        self.assertEqual(res.data['fitness_goal'], 'Six Pack Abs')
        self.assertEqual(res.data['billing_name'], 'Updated Billing Org')

    # 14. RBAC create denied -> 403
    def test_14_rbac_create_denied(self):
        token = self.get_token(self.restricted_user)  # Has view, but NOT create
        res = self.client.post(
            '/api/v1/tenant/leads/',
            {'first_name': 'Denied', 'last_name': 'Create'},
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    # 15. RBAC edit denied -> 403
    def test_15_rbac_edit_denied(self):
        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Edit',
            last_name='Blocked',
            db_alias='tenant_test',
        )
        token = self.get_token(self.restricted_user)  # Has view, but NOT edit
        res = self.client.patch(
            f'/api/v1/tenant/leads/{lead.id}/',
            {'first_name': 'Hacked'},
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    # 16. Tenant isolation
    def test_16_tenant_isolation(self):
        # Create second org in tenant DB
        other_org = Organization.objects.using('tenant_test').create(
            code='OTHER-ORG',
            name='Other Gym Chain',
            status='ACTIVE',
        )
        lead_other = CRMLeadService.create_lead(
            organization=other_org,
            first_name='OtherOrg',
            last_name='Lead',
            db_alias='tenant_test',
        )
        token = self.get_token(self.admin_user)
        res = self.client.get('/api/v1/tenant/leads/', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        results = res.data if isinstance(res.data, list) else res.data.get('results', [])
        ids = [item['id'] for item in results]
        self.assertNotIn(str(lead_other.id), ids)

    # 17. Branch scope
    def test_17_branch_scope_filtering(self):
        lead_a = CRMLeadService.create_lead(
            organization=self.org,
            first_name='BranchA',
            last_name='Lead',
            branch=self.branch_active,
            db_alias='tenant_test',
        )
        token = self.get_token(self.admin_user)
        res = self.client.get(
            f'/api/v1/tenant/leads/?branch_id={self.branch_active.id}',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        results = res.data if isinstance(res.data, list) else res.data.get('results', [])
        ids = [item['id'] for item in results]
        self.assertIn(str(lead_a.id), ids)

    # 18. Lead API uses valid tenant JWT
    def test_18_lead_api_valid_tenant_jwt(self):
        # Request without JWT -> 401 / 403
        res_no_auth = self.client.get('/api/v1/tenant/leads/')
        self.assertIn(res_no_auth.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

        # Request with valid tenant JWT -> 200 OK
        token = self.get_token(self.admin_user)
        res_auth = self.client.get('/api/v1/tenant/leads/', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(res_auth.status_code, status.HTTP_200_OK)

    # 19. Pipeline API uses valid tenant JWT & metadata
    def test_19_pipeline_api_valid_tenant_jwt(self):
        token = self.get_token(self.admin_user)
        res = self.client.get('/api/v1/tenant/leads/metadata/', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn('statuses', res.data)
        self.assertIn('genders', res.data)
        self.assertIn('countries', res.data)
        self.assertEqual(res.data['initial_status'], 'NEW_LEAD')
