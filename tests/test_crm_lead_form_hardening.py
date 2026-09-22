"""
tests/test_crm_lead_form_hardening.py — Targeted Tests for CRM Lead Form Hardening & CRM Setup
Tests the 26 specific requirements:
1. active Lead Sources returned from backend
2. inactive source not returned in New Lead source list (?active_only=true)
3. tenant can create Lead Source (auto-code generation server-side)
4. tenant can edit Lead Source
5. tenant can deactivate Lead Source (soft-deactivate, status=INACTIVE)
6. historical Lead keeps inactive Lead Source relationship
7. cross-tenant Lead Source access rejected
8. view-only CRM settings user cannot modify sources (RBAC)
9. settings edit permission can modify sources
10. New Lead accepts valid @gmail.com
11. Gmail validation case-insensitive
12. Yahoo rejected
13. Outlook rejected
14. malformed Gmail rejected
15. valid 10-digit phone accepted
16. 9-digit phone rejected
17. 11-digit phone rejected
18. alpha-containing phone rejected
19. direct API bypass cannot bypass email validation
20. direct API bypass cannot bypass phone validation
21. normalized phone duplicate detection remains correct
22. create Lead succeeds without Section 5 attribution
23. empty/manual Lead does not create fake attribution row
24. existing Lead attribution APIs remain operational
25. zero frontend hardcoded Lead Source fallback
26. CRM Setup respects tenant isolation
"""

import os
import re
import uuid
from django.test import TestCase
from django.core.exceptions import ValidationError
from rest_framework import status
from rest_framework.test import APIClient

from config.routers import set_tenant_db_alias
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.models_saas import SaasPlan, TenantSubscription, ProductModule, TenantModule
from apps.authentication.views import _build_tenant_token
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
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
    LeadAttribution,
)
from apps.tenant_core.services_crm import (
    CRMLeadService,
    validate_lead_email,
    validate_lead_phone,
)


class CRMLeadFormHardeningTargetedTests(TestCase):
    databases = '__all__'

    def setUp(self):
        super().setUp()
        set_tenant_db_alias('tenant_test')
        self.client = APIClient()

        # Master Tenant Setup
        self.tenant = Tenant.objects.using('default').create(
            code='CRM-TENANT-A',
            name='Tenant A Athletics',
            slug='tenant-a-athletics',
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
            code='ENT-CRM-A',
            tier='ENTERPRISE',
            status='ACTIVE',
        )
        self.sub = TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
        )
        self.mod_crm, _ = ProductModule.objects.using('default').get_or_create(
            code='crm', defaults={'name': 'CRM Module', 'status': 'ACTIVE'}
        )
        self.mod_core, _ = ProductModule.objects.using('default').get_or_create(
            code='core', defaults={'name': 'Core Module', 'status': 'ACTIVE'}
        )
        TenantModule.objects.using('default').create(
            tenant=self.tenant, module=self.mod_crm, is_enabled=True, availability_mode='ALL_BRANCHES'
        )
        TenantModule.objects.using('default').create(
            tenant=self.tenant, module=self.mod_core, is_enabled=True, availability_mode='ALL_BRANCHES'
        )

        # Tenant B Setup for Isolation Tests
        self.tenant_b = Tenant.objects.using('default').create(
            code='CRM-TENANT-B',
            name='Tenant B Fitness',
            slug='tenant-b-fitness',
            status='ACTIVE',
        )
        self.ds_b = TenantDataSource.objects.using('default').create(
            tenant=self.tenant_b,
            db_name='test_fitness_tenant_b',
            database_name='test_fitness_tenant_b',
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )
        TenantSubscription.objects.using('default').create(
            tenant=self.tenant_b,
            plan=self.plan,
            status='ACTIVE',
        )
        TenantModule.objects.using('default').create(
            tenant=self.tenant_b, module=self.mod_crm, is_enabled=True, availability_mode='ALL_BRANCHES'
        )

        # Tenant Core Org Setup
        self.org_a = Organization.objects.using('tenant_test').create(
            code='ORG-A',
            name='Tenant A Athletics Org',
            status='ACTIVE',
        )
        self.org_b = Organization.objects.using('tenant_test').create(
            code='ORG-B',
            name='Tenant B Fitness Org',
            status='ACTIVE',
        )

        self.location_a = Location.objects.using('tenant_test').create(
            organization=self.org_a,
            name='Downtown Hub',
            city='Mumbai',
        )
        self.branch_a = Branch.objects.using('tenant_test').create(
            organization=self.org_a,
            location=self.location_a,
            name='Downtown Branch',
            code='DT-BR-01',
            status='ACTIVE',
        )

        self.category_a = ProgramCategory.objects.using('tenant_test').create(
            organization=self.org_a,
            name='Fitness',
            code='FIT',
        )
        self.program_a = Program.objects.using('tenant_test').create(
            organization=self.org_a,
            category=self.category_a,
            name='Strength Training',
            code='ST-01',
            status='ACTIVE',
        )

        # Setup RBAC modules & permissions
        self.mod_crm_catalog, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(
            module_code='crm', defaults={'name': 'CRM', 'is_enabled': True}
        )
        self.submod_leads, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=self.mod_crm_catalog, submodule_code='leads', defaults={'name': 'Leads', 'is_enabled': True}
        )
        self.submod_settings, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=self.mod_crm_catalog, submodule_code='settings', defaults={'name': 'CRM Settings', 'is_enabled': True}
        )

        perm_codes = [
            'crm.leads.view',
            'crm.leads.create',
            'crm.leads.edit',
            'crm.settings.view',
            'crm.settings.edit',
        ]
        self.perms = {}
        for code in perm_codes:
            action = code.split('.')[-1]
            subm = self.submod_settings if 'settings' in code else self.submod_leads
            p, _ = Permission.objects.using('tenant_test').get_or_create(
                permission_code=code,
                defaults={
                    'code': code,
                    'label': code,
                    'action': action,
                    'module': self.mod_crm_catalog,
                    'submodule': subm,
                    'source_permission_id': uuid.uuid4(),
                    'is_active': True,
                }
            )
            self.perms[code] = p

        # Role 1: Full Admin / Manager with crm.settings.edit
        self.role_admin = Role.objects.using('tenant_test').create(
            organization=self.org_a,
            name='CRM Manager',
            code='CRM_MGR',
            scope='ORG',
            is_active=True,
        )
        RoleModuleAccess.objects.using('tenant_test').get_or_create(
            role=self.role_admin, module=self.mod_crm_catalog, defaults={'can_access': True}
        )
        RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(
            role=self.role_admin, submodule=self.submod_leads, defaults={'can_access': True}
        )
        RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(
            role=self.role_admin, submodule=self.submod_settings, defaults={'can_access': True}
        )
        pset_admin = RolePermissionSet.objects.using('tenant_test').create(
            role=self.role_admin, organization=self.org_a, name='Admin Perms'
        )
        for p in self.perms.values():
            RolePermissionSetItem.objects.using('tenant_test').get_or_create(
                permission_set=pset_admin, permission=p, defaults={'granted': True, 'is_allowed': True}
            )

        # Role 2: View-Only Rep with crm.settings.view (NO crm.settings.edit)
        self.role_rep = Role.objects.using('tenant_test').create(
            organization=self.org_a,
            name='Sales Rep',
            code='SALES_REP',
            scope='ORG',
            is_active=True,
        )
        RoleModuleAccess.objects.using('tenant_test').get_or_create(
            role=self.role_rep, module=self.mod_crm_catalog, defaults={'can_access': True}
        )
        RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(
            role=self.role_rep, submodule=self.submod_leads, defaults={'can_access': True}
        )
        RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(
            role=self.role_rep, submodule=self.submod_settings, defaults={'can_access': True}
        )
        pset_rep = RolePermissionSet.objects.using('tenant_test').create(
            role=self.role_rep, organization=self.org_a, name='Rep Perms'
        )
        for code in ['crm.leads.view', 'crm.leads.create', 'crm.settings.view']:
            RolePermissionSetItem.objects.using('tenant_test').get_or_create(
                permission_set=pset_rep, permission=self.perms[code], defaults={'granted': True, 'is_allowed': True}
            )

        # Users
        self.user_admin = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a,
            email='manager@gmail.com',
            first_name='Admin',
            last_name='Manager',
            status='ACTIVE',
            is_login_allowed=True,
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.user_admin,
            role=self.role_admin,
            is_active=True,
        )

        self.user_rep = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a,
            email='rep@gmail.com',
            first_name='Sales',
            last_name='Rep',
            status='ACTIVE',
            is_login_allowed=True,
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.user_rep,
            role=self.role_rep,
            is_active=True,
        )

        # User in Tenant B
        self.user_b = TenantUser.objects.using('tenant_test').create(
            organization=self.org_b,
            email='tenant_b_admin@gmail.com',
            first_name='TenantB',
            last_name='User',
            status='ACTIVE',
            is_login_allowed=True,
        )
        # Grant user_b crm.leads.view in org_b for tenant isolation testing
        role_b = Role.objects.using('tenant_test').create(
            organization=self.org_b,
            name='Tenant B Manager',
            code='TB_MGR',
            scope='ORG',
            is_active=True,
        )
        RoleModuleAccess.objects.using('tenant_test').get_or_create(role=role_b, module=self.mod_crm_catalog, defaults={'can_access': True})
        RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(role=role_b, submodule=self.submod_leads, defaults={'can_access': True})
        RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(role=role_b, submodule=self.submod_settings, defaults={'can_access': True})
        pset_b = RolePermissionSet.objects.using('tenant_test').create(role=role_b, organization=self.org_b, name='B Perms')
        RolePermissionSetItem.objects.using('tenant_test').get_or_create(
            permission_set=pset_b, permission=self.perms['crm.leads.view'], defaults={'granted': True, 'is_allowed': True}
        )
        RoleAssignment.objects.using('tenant_test').create(user=self.user_b, role=role_b, is_active=True)

        # Tokens
        self.token_admin = str(_build_tenant_token(self.user_admin, self.tenant, 'tenant_test').access_token)
        self.token_rep = str(_build_tenant_token(self.user_rep, self.tenant, 'tenant_test').access_token)
        self.token_b = str(_build_tenant_token(self.user_b, self.tenant_b, 'tenant_test').access_token)

    def tearDown(self):
        set_tenant_db_alias(None)
        from django.db import connections
        for alias in list(connections.databases.keys()):
            if alias not in ('default', 'tenant_test'):
                try:
                    connections[alias].close()
                except Exception:
                    pass
                connections.databases.pop(alias, None)
                if hasattr(connections._connections, alias):
                    delattr(connections._connections, alias)
        super().tearDown()

    # -------------------------------------------------------------
    # 1. active Lead Sources returned from backend
    # -------------------------------------------------------------
    def test_01_active_lead_sources_returned(self):
        LeadSource.objects.using('tenant_test').create(
            organization=self.org_a,
            code='SRC_ACTIVE_1',
            name='Active Source 1',
            status='ACTIVE',
        )
        res = self.client.get(
            '/api/v1/tenant/lead-sources/',
            HTTP_AUTHORIZATION=f'Bearer {self.token_admin}',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        results = res.data.get('results', res.data) if isinstance(res.data, dict) else res.data
        names = [s['name'] for s in results]
        self.assertIn('Active Source 1', names)

    # -------------------------------------------------------------
    # 2. inactive source not returned in New Lead source list (?active_only=true)
    # -------------------------------------------------------------
    def test_02_inactive_source_not_returned_in_active_only(self):
        LeadSource.objects.using('tenant_test').create(
            organization=self.org_a,
            code='SRC_INACTIVE',
            name='Decommissioned Channel',
            status='INACTIVE',
        )
        res = self.client.get(
            '/api/v1/tenant/lead-sources/?active_only=true',
            HTTP_AUTHORIZATION=f'Bearer {self.token_admin}',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        results = res.data.get('results', res.data) if isinstance(res.data, dict) else res.data
        names = [s['name'] for s in results]
        self.assertNotIn('Decommissioned Channel', names)

    # -------------------------------------------------------------
    # 3. tenant can create Lead Source (auto-code generation server-side)
    # -------------------------------------------------------------
    def test_03_tenant_can_create_lead_source_with_server_generated_code(self):
        res = self.client.post(
            '/api/v1/tenant/lead-sources/',
            {'name': 'Instagram Campaign 2026', 'source_type': 'META'},
            HTTP_AUTHORIZATION=f'Bearer {self.token_admin}',
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['name'], 'Instagram Campaign 2026')
        self.assertEqual(res.data['status'], 'ACTIVE')
        # Technical code was generated automatically server-side
        self.assertTrue(bool(res.data['code']))
        self.assertIn('INSTAGRAM_CAMPAIGN_2026', res.data['code'])

    # -------------------------------------------------------------
    # 4. tenant can edit Lead Source
    # -------------------------------------------------------------
    def test_04_tenant_can_edit_lead_source(self):
        src = LeadSource.objects.using('tenant_test').create(
            organization=self.org_a,
            code='EDIT_TEST',
            name='Old Name',
            status='ACTIVE',
        )
        res = self.client.patch(
            f'/api/v1/tenant/lead-sources/{src.id}/',
            {'name': 'Updated Source Name'},
            HTTP_AUTHORIZATION=f'Bearer {self.token_admin}',
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        src.refresh_from_db(using='tenant_test')
        self.assertEqual(src.name, 'Updated Source Name')

    # -------------------------------------------------------------
    # 5. tenant can deactivate Lead Source (soft-deactivate, status=INACTIVE)
    # -------------------------------------------------------------
    def test_05_tenant_can_deactivate_lead_source_softly(self):
        src = LeadSource.objects.using('tenant_test').create(
            organization=self.org_a,
            code='DEACT_TEST',
            name='To Deactivate',
            status='ACTIVE',
        )
        # DELETE endpoint performs soft deactivation
        res = self.client.delete(
            f'/api/v1/tenant/lead-sources/{src.id}/',
            HTTP_AUTHORIZATION=f'Bearer {self.token_admin}',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        src.refresh_from_db(using='tenant_test')
        self.assertEqual(src.status, 'INACTIVE')

    # -------------------------------------------------------------
    # 6. historical Lead keeps inactive Lead Source relationship
    # -------------------------------------------------------------
    def test_06_historical_lead_keeps_inactive_lead_source_relationship(self):
        src = LeadSource.objects.using('tenant_test').create(
            organization=self.org_a,
            code='HIST_SRC',
            name='Historical Source',
            status='ACTIVE',
        )
        lead = CRMLeadService.create_lead(
            organization=self.org_a,
            first_name='Historical',
            last_name='Lead',
            email='historical.lead@gmail.com',
            phone='9876543210',
            branch=self.branch_a,
            lead_source=src,
            assigned_sales_user=self.user_admin,
            db_alias='tenant_test',
        )
        # Deactivate source
        src.status = 'INACTIVE'
        src.save(using='tenant_test')

        # Lead relationship remains valid and points to the same LeadSource instance
        lead.refresh_from_db(using='tenant_test')
        self.assertEqual(lead.lead_source_id, src.id)
        self.assertEqual(lead.lead_source.name, 'Historical Source')

    # -------------------------------------------------------------
    # 7. cross-tenant Lead Source access rejected
    # -------------------------------------------------------------
    def test_07_cross_tenant_lead_source_access_rejected(self):
        src_a = LeadSource.objects.using('tenant_test').create(
            organization=self.org_a,
            code='TENANT_A_SRC',
            name='Tenant A Private Source',
            status='ACTIVE',
        )
        # Tenant B requests Tenant A's lead source
        res = self.client.get(
            f'/api/v1/tenant/lead-sources/{src_a.id}/',
            HTTP_AUTHORIZATION=f'Bearer {self.token_b}',
        )
        self.assertIn(res.status_code, [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND])

    # -------------------------------------------------------------
    # 8. view-only CRM settings user cannot modify sources (RBAC)
    # -------------------------------------------------------------
    def test_08_view_only_user_cannot_modify_sources(self):
        res = self.client.post(
            '/api/v1/tenant/lead-sources/',
            {'name': 'Unauthorized Creation', 'source_type': 'META'},
            HTTP_AUTHORIZATION=f'Bearer {self.token_rep}',
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    # -------------------------------------------------------------
    # 9. settings edit permission can modify sources
    # -------------------------------------------------------------
    def test_09_settings_edit_permission_can_modify_sources(self):
        res = self.client.post(
            '/api/v1/tenant/lead-sources/',
            {'name': 'Authorized Creation', 'source_type': 'WEBSITE'},
            HTTP_AUTHORIZATION=f'Bearer {self.token_admin}',
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

    # -------------------------------------------------------------
    # 10. New Lead accepts valid @gmail.com
    # -------------------------------------------------------------
    def test_10_new_lead_accepts_valid_gmail(self):
        self.assertEqual(validate_lead_email('rahul@gmail.com'), 'rahul@gmail.com')
        self.assertEqual(validate_lead_email('rahul.sharma@gmail.com'), 'rahul.sharma@gmail.com')
        self.assertEqual(validate_lead_email('rahul123@gmail.com'), 'rahul123@gmail.com')
        self.assertEqual(validate_lead_email('rahul+trial@gmail.com'), 'rahul+trial@gmail.com')

    # -------------------------------------------------------------
    # 11. Gmail validation case-insensitive
    # -------------------------------------------------------------
    def test_11_gmail_validation_case_insensitive(self):
        self.assertEqual(validate_lead_email('RAHUL@GMAIL.COM'), 'rahul@gmail.com')
        self.assertEqual(validate_lead_email('User.Name@Gmail.Com'), 'user.name@gmail.com')

    # -------------------------------------------------------------
    # 12. Yahoo rejected
    # -------------------------------------------------------------
    def test_12_yahoo_rejected(self):
        with self.assertRaises(ValidationError):
            validate_lead_email('rahul@yahoo.com')

    # -------------------------------------------------------------
    # 13. Outlook rejected
    # -------------------------------------------------------------
    def test_13_outlook_rejected(self):
        with self.assertRaises(ValidationError):
            validate_lead_email('rahul@outlook.com')
        with self.assertRaises(ValidationError):
            validate_lead_email('rahul@company.com')

    # -------------------------------------------------------------
    # 14. malformed Gmail rejected
    # -------------------------------------------------------------
    def test_14_malformed_gmail_rejected(self):
        malformed = [
            'rahul@gmail',
            '@gmail.com',
            'rahul@gmail.co',
            'user@gmail.com.fake.com',
            'rahul @gmail.com',
            'random text',
        ]
        for m in malformed:
            with self.assertRaises(ValidationError):
                validate_lead_email(m)

    # -------------------------------------------------------------
    # 15. valid 10-digit phone accepted
    # -------------------------------------------------------------
    def test_15_valid_10_digit_phone_accepted(self):
        self.assertEqual(validate_lead_phone('9876543210'), '+919876543210')
        self.assertEqual(validate_lead_phone('+919876543210'), '+919876543210')
        self.assertEqual(validate_lead_phone('98765 43210'), '+919876543210')

    # -------------------------------------------------------------
    # 16. 9-digit phone rejected
    # -------------------------------------------------------------
    def test_16_nine_digit_phone_rejected(self):
        with self.assertRaises(ValidationError):
            validate_lead_phone('987654321')

    # -------------------------------------------------------------
    # 17. 11-digit phone rejected
    # -------------------------------------------------------------
    def test_17_eleven_digit_phone_rejected(self):
        with self.assertRaises(ValidationError):
            validate_lead_phone('98765432101')

    # -------------------------------------------------------------
    # 18. alpha-containing phone rejected
    # -------------------------------------------------------------
    def test_18_alpha_containing_phone_rejected(self):
        with self.assertRaises(ValidationError):
            validate_lead_phone('98765abc10')

    # -------------------------------------------------------------
    # 19. direct API bypass cannot bypass email validation
    # -------------------------------------------------------------
    def test_19_direct_api_bypass_cannot_bypass_email_validation(self):
        src = LeadSource.objects.using('tenant_test').create(
            organization=self.org_a, code='S19', name='S19', status='ACTIVE'
        )
        res = self.client.post(
            '/api/v1/tenant/leads/',
            {
                'first_name': 'Test',
                'last_name': 'Bypass',
                'email': 'hacker@yahoo.com',
                'phone': '9876543210',
                'branch': str(self.branch_a.id),
                'interested_program': str(self.program_a.id),
                'lead_source': str(src.id),
                'assigned_sales_user': str(self.user_admin.id),
            },
            HTTP_AUTHORIZATION=f'Bearer {self.token_admin}',
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('email', str(res.data))

    # -------------------------------------------------------------
    # 20. direct API bypass cannot bypass phone validation
    # -------------------------------------------------------------
    def test_20_direct_api_bypass_cannot_bypass_phone_validation(self):
        src = LeadSource.objects.using('tenant_test').create(
            organization=self.org_a, code='S20', name='S20', status='ACTIVE'
        )
        res = self.client.post(
            '/api/v1/tenant/leads/',
            {
                'first_name': 'Test',
                'last_name': 'Bypass',
                'email': 'valid.person@gmail.com',
                'phone': '12345',  # invalid length
                'branch': str(self.branch_a.id),
                'interested_program': str(self.program_a.id),
                'lead_source': str(src.id),
                'assigned_sales_user': str(self.user_admin.id),
            },
            HTTP_AUTHORIZATION=f'Bearer {self.token_admin}',
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('phone', str(res.data))

    # -------------------------------------------------------------
    # 21. normalized phone duplicate detection remains correct
    # -------------------------------------------------------------
    def test_21_normalized_phone_duplicate_detection(self):
        src = LeadSource.objects.using('tenant_test').create(
            organization=self.org_a, code='S21', name='S21', status='ACTIVE'
        )
        CRMLeadService.create_lead(
            organization=self.org_a,
            first_name='Existing',
            last_name='Lead',
            email='existing.lead@gmail.com',
            phone='9876543210',
            branch=self.branch_a,
            lead_source=src,
            assigned_sales_user=self.user_admin,
            db_alias='tenant_test',
        )
        # Check duplicate with space formatting "98765 43210"
        res = self.client.post(
            '/api/v1/tenant/leads/check-duplicates/',
            {'phone': '98765 43210'},
            HTTP_AUTHORIZATION=f'Bearer {self.token_admin}',
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.data['has_duplicate'])
        self.assertEqual(len(res.data['matches']), 1)

    # -------------------------------------------------------------
    # 22. create Lead succeeds without Section 5 attribution
    # -------------------------------------------------------------
    def test_22_create_lead_succeeds_without_attribution(self):
        src = LeadSource.objects.using('tenant_test').create(
            organization=self.org_a, code='S22', name='S22', status='ACTIVE'
        )
        res = self.client.post(
            '/api/v1/tenant/leads/',
            {
                'first_name': 'No',
                'last_name': 'Attribution',
                'email': 'no.attribution@gmail.com',
                'phone': '9876543210',
                'branch': str(self.branch_a.id),
                'interested_program': str(self.program_a.id),
                'lead_source': str(src.id),
                'assigned_sales_user': str(self.user_admin.id),
            },
            HTTP_AUTHORIZATION=f'Bearer {self.token_admin}',
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['first_name'], 'No')

    # -------------------------------------------------------------
    # 23. empty/manual Lead does not create fake attribution row
    # -------------------------------------------------------------
    def test_23_empty_manual_lead_does_not_create_fake_attribution_row(self):
        src = LeadSource.objects.using('tenant_test').create(
            organization=self.org_a, code='S23', name='S23', status='ACTIVE'
        )
        res = self.client.post(
            '/api/v1/tenant/leads/',
            {
                'first_name': 'Clean',
                'last_name': 'Lead',
                'email': 'clean.lead@gmail.com',
                'phone': '9876543210',
                'branch': str(self.branch_a.id),
                'interested_program': str(self.program_a.id),
                'lead_source': str(src.id),
                'assigned_sales_user': str(self.user_admin.id),
                'attribution': {},  # Empty object should not produce attribution record
            },
            HTTP_AUTHORIZATION=f'Bearer {self.token_admin}',
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        lead_id = res.data['id']
        attributions = LeadAttribution.objects.using('tenant_test').filter(lead_id=lead_id)
        self.assertEqual(attributions.count(), 0)

    # -------------------------------------------------------------
    # 24. existing Lead attribution APIs remain operational
    # -------------------------------------------------------------
    def test_24_existing_lead_attribution_apis_remain_operational(self):
        src = LeadSource.objects.using('tenant_test').create(
            organization=self.org_a, code='S24', name='S24', status='ACTIVE'
        )
        lead = CRMLeadService.create_lead(
            organization=self.org_a,
            first_name='Attr',
            last_name='Test',
            email='attr.test@gmail.com',
            phone='9876543210',
            branch=self.branch_a,
            lead_source=src,
            assigned_sales_user=self.user_admin,
            attribution_data={
                'platform': 'META',
                'campaign_name': 'Spring Launch',
                'utm_source': 'instagram',
            },
            db_alias='tenant_test',
        )
        # Retrieve lead detail
        res = self.client.get(
            f'/api/v1/tenant/leads/{lead.id}/',
            HTTP_AUTHORIZATION=f'Bearer {self.token_admin}',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIsNotNone(res.data.get('latest_attribution'))
        self.assertEqual(res.data['latest_attribution']['platform'], 'META')
        self.assertEqual(res.data['latest_attribution']['campaign_name'], 'Spring Launch')

    # -------------------------------------------------------------
    # 25. zero frontend hardcoded Lead Source fallback
    # -------------------------------------------------------------
    def test_25_zero_frontend_hardcoded_lead_source_fallback(self):
        frontend_modal_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            'src',
            'components',
            'crm',
            'NewLeadModal.tsx',
        )
        with open(frontend_modal_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Must not contain hardcoded default source lists
        self.assertNotIn('DEFAULT_SOURCES', content)
        self.assertNotIn('const defaultSources', content)
        self.assertIn('Unable to load Lead Sources', content)
        self.assertIn('No active lead sources configured', content)

    # -------------------------------------------------------------
    # 26. CRM Setup respects tenant isolation
    # -------------------------------------------------------------
    def test_26_crm_setup_respects_tenant_isolation(self):
        # Tenant A creates a custom source
        res_a = self.client.post(
            '/api/v1/tenant/lead-sources/',
            {'name': 'Tenant A Exclusive Expo', 'source_type': 'OTHER'},
            HTTP_AUTHORIZATION=f'Bearer {self.token_admin}',
            format='json',
        )
        self.assertEqual(res_a.status_code, status.HTTP_201_CREATED)

        # Tenant B lists sources
        res_b = self.client.get(
            '/api/v1/tenant/lead-sources/',
            HTTP_AUTHORIZATION=f'Bearer {self.token_b}',
        )
        self.assertEqual(res_b.status_code, status.HTTP_200_OK)
        data_b = res_b.data.get('results', res_b.data) if isinstance(res_b.data, dict) else res_b.data
        names_b = [s['name'] for s in data_b]
        # Tenant B must NOT see Tenant A's private source
        self.assertNotIn('Tenant A Exclusive Expo', names_b)
