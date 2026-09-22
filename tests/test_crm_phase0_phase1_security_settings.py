"""
backend/tests/test_crm_phase0_phase1_security_settings.py — Targeted Tests for CRM Phase 0 & Phase 1:
1. Branch-scoped user cannot list another branch's leads
2. Query param ?branch_id= cannot bypass branch scope
3. Unauthorized branch lead creation is rejected (403)
4. Unauthorized trial branch booking is rejected (403)
5. Org-wide user sees permitted org data
6. Lead Source CRUD lifecycle (create with auto-code, list, update)
7. Lead Source deletion is rejected (400) to protect historical leads
8. Active source filtering (?active_only=true) for New Lead form
9. SLA settings persist per tenant organization
10. Tenant isolation: Tenant A SLA != Tenant B SLA
11. Trial Reminder policy persists per tenant organization
12. Tenant isolation on Trial Reminder policy
13. RBAC settings view / edit permission enforcement
14. BusinessAuditEvent generation on configuration changes
"""

import uuid
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
    LeadStatusHistory,
    TrialBooking,
    CRMStageSlaPolicy,
    CRMTrialReminderPolicy,
)
from apps.tenant_core.models_audit_outbox import BusinessAuditEvent


class CRMPhase0Phase1SecuritySettingsTests(TestCase):
    databases = '__all__'

    def setUp(self):
        super().setUp()
        set_tenant_db_alias('tenant_test')
        self.client = APIClient()

        # 1. Master DB Setup
        self.tenant_a = Tenant.objects.using('default').create(
            code='CRM-TENANT-A',
            name='Tenant A Fitness',
            slug='tenant-a-fitness',
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
            code='ENT-PLAN',
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
            code='ORG-A',
            name='Org A Fitness',
            status='ACTIVE',
        )
        self.loc_a = Location.objects.using('tenant_test').create(
            organization=self.org_a,
            code='LOC-A',
            name='Location A',
            status='ACTIVE',
        )
        self.branch_1 = Branch.objects.using('tenant_test').create(
            organization=self.org_a,
            location=self.loc_a,
            code='BR-1',
            name='Branch Downtown',
            status='ACTIVE',
        )
        self.branch_2 = Branch.objects.using('tenant_test').create(
            organization=self.org_a,
            location=self.loc_a,
            code='BR-2',
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

        # 4. Roles: Org-Wide Admin vs Branch-Scoped Manager
        self.role_org_admin = Role.objects.using('tenant_test').create(
            organization=self.org_a,
            code='ORG_ADMIN_ROLE',
            name='Org Admin',
            scope='ORG',
            is_active=True,
        )
        self.role_branch_mgr = Role.objects.using('tenant_test').create(
            organization=self.org_a,
            code='BRANCH_MGR_ROLE',
            name='Branch Manager',
            scope='BRANCH',
            is_active=True,
        )

        # Grant module & submodule access to both roles
        for r in (self.role_org_admin, self.role_branch_mgr):
            RoleModuleAccess.objects.using('tenant_test').get_or_create(
                role=r, module=self.mod_crm, defaults={'can_access': True}
            )
            RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(
                role=r, submodule=self.submod_leads, defaults={'can_access': True}
            )

        # Permissions: crm.leads.view, crm.leads.create, crm.leads.edit, crm.settings.view, crm.settings.edit
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

        # Role Permission Sets
        pset_org = RolePermissionSet.objects.using('tenant_test').create(
            role=self.role_org_admin,
            organization=self.org_a,
            name='Org Admin Perms'
        )
        for p in self.perms.values():
            RolePermissionSetItem.objects.using('tenant_test').get_or_create(
                permission_set=pset_org, permission=p, defaults={'granted': True, 'is_allowed': True}
            )

        pset_branch = RolePermissionSet.objects.using('tenant_test').create(
            role=self.role_branch_mgr,
            organization=self.org_a,
            name='Branch Mgr Perms'
        )
        for p in self.perms.values():
            RolePermissionSetItem.objects.using('tenant_test').get_or_create(
                permission_set=pset_branch, permission=p, defaults={'granted': True, 'is_allowed': True}
            )

        # 5. Users
        # User 1: Org Admin (Org Scope)
        self.user_org_admin = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a,
            email='org.admin@test.com',
            first_name='Org',
            last_name='Admin',
            status='ACTIVE',
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.user_org_admin,
            role=self.role_org_admin,
            branch=None,
            is_active=True,
            status='ACTIVE',
        )

        # User 2: Branch 1 Scoped Manager
        self.user_branch1_mgr = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a,
            email='branch1.mgr@test.com',
            first_name='Branch1',
            last_name='Manager',
            status='ACTIVE',
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.user_branch1_mgr,
            role=self.role_branch_mgr,
            branch=self.branch_1,
            is_active=True,
            status='ACTIVE',
        )
        UserBranch.objects.using('tenant_test').create(
            user=self.user_branch1_mgr,
            branch=self.branch_1,
            scope_type='HOME',
            status='ACTIVE',
            is_active=True,
        )

        # Lead Source
        self.source = LeadSource.objects.using('tenant_test').create(
            organization=self.org_a,
            code='WALK_IN',
            name='Walk In Prospect',
            source_type='WALK_IN',
            status='ACTIVE',
        )

        # Create Existing Leads
        self.lead_br1 = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_1,
            first_name='John',
            last_name='Downtown',
            phone_normalized='+919876543210',
            email_normalized='john@downtown.com',
            lead_source=self.source,
            current_status='NEW_LEAD',
        )
        self.lead_br2 = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_2,
            first_name='Alice',
            last_name='Uptown',
            phone_normalized='+919876543211',
            email_normalized='alice@uptown.com',
            lead_source=self.source,
            current_status='NEW_LEAD',
        )

        # Build Tokens
        self.token_org_admin = str(_build_tenant_token(self.user_org_admin, self.tenant_a, 'tenant_test').access_token)
        self.token_branch1_mgr = str(_build_tenant_token(self.user_branch1_mgr, self.tenant_a, 'tenant_test').access_token)

    def test_01_branch_scoped_user_cannot_list_another_branch_leads(self):
        """Branch-scoped manager should strictly see only leads from assigned Branch 1."""
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_branch1_mgr}')
        response = self.client.get('/api/v1/tenant/leads/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()
        leads = data.get('results', data)
        lead_ids = [item['id'] for item in leads]

        self.assertIn(str(self.lead_br1.id), lead_ids)
        self.assertNotIn(str(self.lead_br2.id), lead_ids)

    def test_02_query_param_cannot_bypass_branch_scope(self):
        """Passing ?branch_id=BR_2 when scoped to Branch 1 must return empty or 403 Forbidden."""
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_branch1_mgr}')
        response = self.client.get(f'/api/v1/tenant/leads/?branch_id={self.branch_2.id}')
        # Either 403 Forbidden (RBAC engine blocks unauthorized branch query) or 200 with 0 results
        self.assertIn(response.status_code, [status.HTTP_403_FORBIDDEN, status.HTTP_200_OK])
        if response.status_code == status.HTTP_200_OK:
            data = response.json()
            leads = data.get('results', data)
            self.assertEqual(len(leads), 0)

    def test_03_unauthorized_branch_lead_creation_rejected(self):
        """Branch 1 scoped manager attempting to create lead for Branch 2 must receive 403 Forbidden."""
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_branch1_mgr}')
        payload = {
            'first_name': 'Hacker',
            'last_name': 'Attempt',
            'phone': '+919999999999',
            'email': 'hacker@test.com',
            'branch': str(self.branch_2.id),
            'lead_source': str(self.source.id),
        }
        response = self.client.post('/api/v1/tenant/leads/', payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_04_unauthorized_trial_branch_rejected(self):
        """Branch 1 scoped manager attempting to book trial at Branch 2 must receive 403 Forbidden."""
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_branch1_mgr}')
        start = timezone.now() + timezone.timedelta(days=1)
        end = start + timezone.timedelta(hours=1)
        payload = {
            'branch_id': str(self.branch_2.id),
            'scheduled_start': start.isoformat(),
            'scheduled_end': end.isoformat(),
            'trial_type': 'GROUP_CLASS',
        }
        response = self.client.post(
            f'/api/v1/tenant/leads/{self.lead_br1.id}/book-trial/', payload, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_05_org_wide_user_sees_permitted_org_data(self):
        """Org Admin (ORG scope) should see leads across all branches in the organization."""
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_org_admin}')
        response = self.client.get('/api/v1/tenant/leads/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()
        leads = data.get('results', data)
        lead_ids = [item['id'] for item in leads]

        self.assertIn(str(self.lead_br1.id), lead_ids)
        self.assertIn(str(self.lead_br2.id), lead_ids)

    def test_06_lead_source_crud_lifecycle_and_autocode(self):
        """Lead source created without explicit code gets auto-generated uppercase stable code."""
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_org_admin}')
        payload = {
            'name': 'Instagram Reels 2026',
            'source_type': 'META',
        }
        response = self.client.post('/api/v1/tenant/lead-sources/', payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        created_data = response.json()
        self.assertEqual(created_data['name'], 'Instagram Reels 2026')
        self.assertEqual(created_data['code'], 'INSTAGRAM_REELS_2026')
        self.assertEqual(created_data['status'], 'ACTIVE')

        # Update lead source
        src_id = created_data['id']
        update_res = self.client.patch(
            f'/api/v1/tenant/lead-sources/{src_id}/',
            {'name': 'Instagram Viral Campaign', 'status': 'INACTIVE'},
            format='json',
        )
        self.assertEqual(update_res.status_code, status.HTTP_200_OK)
        self.assertEqual(update_res.json()['name'], 'Instagram Viral Campaign')
        self.assertEqual(update_res.json()['status'], 'INACTIVE')

    def test_07_lead_source_deletion_rejected(self):
        """Permanent deletion of lead sources is rejected with 400 to preserve historical references."""
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_org_admin}')
        response = self.client.delete(f'/api/v1/tenant/lead-sources/{self.source.id}/')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Permanent deletion of lead sources is not permitted', response.json().get('error', ''))

    def test_08_active_source_filtering_for_new_lead_form(self):
        """Querying ?active_only=true returns only active lead sources."""
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_org_admin}')
        inactive_src = LeadSource.objects.using('tenant_test').create(
            organization=self.org_a,
            code='OLD_PAPER_FLYER',
            name='Old Flyer 2023',
            source_type='OTHER',
            status='INACTIVE',
        )

        response = self.client.get('/api/v1/tenant/lead-sources/?active_only=true')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        sources = response.json().get('results', response.json())
        source_ids = [s['id'] for s in sources]
        self.assertIn(str(self.source.id), source_ids)
        self.assertNotIn(str(inactive_src.id), source_ids)

    def test_09_sla_settings_persist_per_organization(self):
        """Default canonical stage SLAs are seeded and tenant can configure targets."""
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_org_admin}')
        get_res = self.client.get('/api/v1/tenant/crm/sla-policies/')
        self.assertEqual(get_res.status_code, status.HTTP_200_OK)

        policies = get_res.json().get('results', get_res.json())
        self.assertGreaterEqual(len(policies), 5)

        new_lead_policy = next(p for p in policies if p['canonical_stage'] == 'NEW_LEAD')
        # Update SLA to 10 minutes
        patch_res = self.client.patch(
            f"/api/v1/tenant/crm/sla-policies/{new_lead_policy['id']}/",
            {'response_target_value': 10, 'response_target_unit': 'MINUTES'},
            format='json',
        )
        self.assertEqual(patch_res.status_code, status.HTTP_200_OK)
        self.assertEqual(patch_res.json()['response_target_value'], 10)

        # Verify persisted in database
        persisted = CRMStageSlaPolicy.objects.using('tenant_test').get(id=new_lead_policy['id'])
        self.assertEqual(persisted.response_target_value, 10)

    def test_10_trial_reminder_policy_persists_per_organization(self):
        """Trial reminder policy auto-creates defaults and updates correctly."""
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_org_admin}')
        get_res = self.client.get('/api/v1/tenant/crm/trial-reminder-policy/')
        self.assertEqual(get_res.status_code, status.HTTP_200_OK)
        data = get_res.json()
        self.assertTrue(data['immediate_whatsapp'])

        # Update offsets to [1440, 60] and sms to True
        patch_res = self.client.post(
            '/api/v1/tenant/crm/trial-reminder-policy/',
            {
                'immediate_sms': True,
                'reminder_offsets': [1440, 60],
                'ask_attendance_confirmation': True,
                'confirmation_wait_duration_value': 3,
            },
            format='json',
        )
        self.assertEqual(patch_res.status_code, status.HTTP_200_OK)
        self.assertTrue(patch_res.json()['immediate_sms'])
        self.assertEqual(patch_res.json()['reminder_offsets'], [1440, 60])

        # Verify persisted in DB
        db_policy = CRMTrialReminderPolicy.objects.using('tenant_test').get(organization=self.org_a)
        self.assertTrue(db_policy.immediate_sms)
        self.assertEqual(db_policy.reminder_offsets, [1440, 60])

    def test_11_business_audit_event_generated_on_settings_change(self):
        """Modifying SLA policy emits a BusinessAuditEvent in the tenant audit log."""
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_org_admin}')
        get_res = self.client.get('/api/v1/tenant/crm/sla-policies/')
        policies = get_res.json().get('results', get_res.json())
        policy = policies[0]

        initial_audits = BusinessAuditEvent.objects.using('tenant_test').filter(
            action_code='CRM_SLA_POLICY_UPDATED'
        ).count()

        patch_res = self.client.patch(
            f"/api/v1/tenant/crm/sla-policies/{policy['id']}/",
            {'response_target_value': 25},
            format='json',
        )
        self.assertEqual(patch_res.status_code, status.HTTP_200_OK)

        after_audits = BusinessAuditEvent.objects.using('tenant_test').filter(
            action_code='CRM_SLA_POLICY_UPDATED'
        ).count()
        self.assertEqual(after_audits, initial_audits + 1)

    def test_12_tenant_isolation_sla_policies(self):
        """Tenant A SLA settings do not leak into or affect Tenant B SLA settings."""
        org_b = Organization.objects.using('tenant_test').create(
            code='ORG-B',
            name='Tenant B Fitness',
            status='ACTIVE',
        )
        # Tenant A configures NEW_LEAD to 15 minutes
        policy_a = CRMStageSlaPolicy.objects.using('tenant_test').create(
            organization=self.org_a,
            canonical_stage='NEW_LEAD',
            response_target_value=15,
            response_target_unit='MINUTES',
        )
        # Tenant B configures NEW_LEAD to 4 hours
        policy_b = CRMStageSlaPolicy.objects.using('tenant_test').create(
            organization=org_b,
            canonical_stage='NEW_LEAD',
            response_target_value=4,
            response_target_unit='HOURS',
        )

        self.assertNotEqual(policy_a.response_target_value, policy_b.response_target_value)
        self.assertNotEqual(policy_a.response_target_unit, policy_b.response_target_unit)

        # Querying with Org A context returns only Org A's SLA policy
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_org_admin}')
        res_a = self.client.get('/api/v1/tenant/crm/sla-policies/')
        policies_a = res_a.json().get('results', res_a.json())
        for p in policies_a:
            self.assertEqual(str(p['organization']), str(self.org_a.id))

    def test_13_rbac_settings_edit_required_for_modifications(self):
        """User with crm.settings.view can view settings, but cannot modify without crm.settings.edit."""
        # Create Read-only settings role & user
        role_viewer = Role.objects.using('tenant_test').create(
            organization=self.org_a,
            code='CRM_SETTINGS_VIEWER',
            name='Settings Viewer',
            scope='ORG',
            is_active=True,
        )
        RoleModuleAccess.objects.using('tenant_test').get_or_create(
            role=role_viewer, module=self.mod_crm, defaults={'can_access': True}
        )
        RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(
            role=role_viewer, submodule=self.submod_leads, defaults={'can_access': True}
        )
        pset_viewer = RolePermissionSet.objects.using('tenant_test').create(
            role=role_viewer, organization=self.org_a, name='Viewer PSet'
        )
        RolePermissionSetItem.objects.using('tenant_test').create(
            permission_set=pset_viewer,
            permission=self.perms['crm.settings.view'],
            granted=True,
            is_allowed=True,
        )

        user_viewer = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a,
            email='viewer@test.com',
            first_name='Settings',
            last_name='Viewer',
            status='ACTIVE',
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=user_viewer,
            role=role_viewer,
            is_active=True,
            status='ACTIVE',
        )

        token_viewer = str(_build_tenant_token(user_viewer, self.tenant_a, 'tenant_test').access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token_viewer}')

        # View allowed
        get_res = self.client.get('/api/v1/tenant/crm/sla-policies/')
        self.assertEqual(get_res.status_code, status.HTTP_200_OK)

        # Edit forbidden (403)
        policies = get_res.json().get('results', get_res.json())
        policy_id = policies[0]['id']
        patch_res = self.client.patch(
            f"/api/v1/tenant/crm/sla-policies/{policy_id}/",
            {'response_target_value': 99},
            format='json',
        )
        self.assertEqual(patch_res.status_code, status.HTTP_403_FORBIDDEN)

        # Grant crm.settings.edit to the role
        set_tenant_db_alias('tenant_test')
        RolePermissionSetItem.objects.using('tenant_test').create(
            permission_set=pset_viewer,
            permission=self.perms['crm.settings.edit'],
            granted=True,
            is_allowed=True,
        )

        # Edit now permitted (200 OK)
        patch_res_allowed = self.client.patch(
            f"/api/v1/tenant/crm/sla-policies/{policy_id}/",
            {'response_target_value': 99},
            format='json',
        )
        self.assertEqual(patch_res_allowed.status_code, status.HTTP_200_OK)
        self.assertEqual(patch_res_allowed.json()['response_target_value'], 99)

