"""
backend/tests/test_crm_phase3_activities_followups.py — Targeted Tests for CRM Phase 3:
1. Activity list uses real DB records
2. Create Activity
3. Activity linked to correct Lead
4. Unauthorized Lead activity access denied
5. Cross-branch activity create denied
6. Cross-tenant activity denied
7. Follow-up create
8. Follow-up update/reschedule
9. Follow-up complete
10. Overdue calculation (due_at < now, status pending/in-progress)
11. Completed follow-up not overdue
12. Work queue overdue count
13. Work queue today count
14. Work queue respects branch scope
15. Work queue respects tenant scope
16. Assigned agent validated
17. Inactive/unauthorized agent cannot be assigned
18. Activity appears in Lead timeline
19. Follow-up created appears in timeline
20. Follow-up completion appears in timeline
21. Lead 360 activities endpoint/data
22. Lead 360 follow-ups endpoint/data
23. RBAC read-only Activity user cannot create
24. RBAC read-only Follow-up user cannot modify
25. Unauthorized mutation returns 403
26. Audit events generated
27. Existing Phase 2 timeline still works
28. No mock runtime data
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
    LeadActivity,
    SalesFollowupTask,
    LeadNote,
    LeadStatusHistory,
    LeadAssignment,
    TrialBooking,
)
from apps.tenant_core.models_audit_outbox import BusinessAuditEvent
from apps.tenant_core.services_crm import CRMLeadService


class CRMPhase3ActivitiesFollowupsTests(TestCase):
    databases = '__all__'

    def setUp(self):
        super().setUp()
        set_tenant_db_alias('tenant_test')
        self.client = APIClient()

        # 1. Master DB Setup (Tenant A)
        self.tenant_a = Tenant.objects.using('default').create(
            code='CRM-TENANT-A3',
            name='Tenant A3 Fitness',
            slug='tenant-a3-fitness',
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
            code='ENT-PLAN-P3',
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

        # 2. Tenant DB Setup (Tenant A & B)
        self.org_a = Organization.objects.using('tenant_test').create(
            code='ORG-A3',
            name='Org A3 Fitness',
            status='ACTIVE',
        )
        self.org_b = Organization.objects.using('tenant_test').create(
            code='ORG-B3',
            name='Org B3 Fitness',
            status='ACTIVE',
        )
        self.loc_a = Location.objects.using('tenant_test').create(
            organization=self.org_a,
            code='LOC-A3',
            name='Location A3',
            status='ACTIVE',
        )
        self.branch_1 = Branch.objects.using('tenant_test').create(
            organization=self.org_a,
            location=self.loc_a,
            code='BR-1-P3',
            name='Branch Downtown P3',
            status='ACTIVE',
        )
        self.branch_2 = Branch.objects.using('tenant_test').create(
            organization=self.org_a,
            location=self.loc_a,
            code='BR-2-P3',
            name='Branch Uptown P3',
            status='ACTIVE',
        )

        # 3. RBAC Setup: Module & Submodule Catalogs
        self.mod_crm, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(
            module_code='crm', defaults={'name': 'CRM', 'is_enabled': True}
        )
        self.submod_leads, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=self.mod_crm, submodule_code='leads', defaults={'name': 'Leads', 'is_enabled': True}
        )

        # Roles
        self.role_org_admin = Role.objects.using('tenant_test').create(
            organization=self.org_a,
            code='ORG_ADMIN_ROLE_P3',
            name='Org Admin P3',
            scope='ORG',
            is_active=True,
        )
        self.role_branch_mgr = Role.objects.using('tenant_test').create(
            organization=self.org_a,
            code='BRANCH_MGR_ROLE_P3',
            name='Branch Manager P3',
            scope='BRANCH',
            is_active=True,
        )
        self.role_readonly = Role.objects.using('tenant_test').create(
            organization=self.org_a,
            code='READONLY_ROLE_P3',
            name='Read Only Staff P3',
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
                },
            )
            self.perms[code] = p

        # Assign full perms to org_admin and branch_mgr
        for r in (self.role_org_admin, self.role_branch_mgr):
            ps = RolePermissionSet.objects.using('tenant_test').create(role=r, name=f'{r.code}_perms')
            for p in self.perms.values():
                RolePermissionSetItem.objects.using('tenant_test').create(permission_set=ps, permission=p)

        # Assign view-only to readonly role
        ps_ro = RolePermissionSet.objects.using('tenant_test').create(role=self.role_readonly, name='ro_perms')
        RolePermissionSetItem.objects.using('tenant_test').create(
            permission_set=ps_ro, permission=self.perms['crm.leads.view']
        )

        # 4. Users Setup
        self.user_org_admin = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a,
            email='admin_p3@fitness.test',
            first_name='Admin',
            last_name='P3',
            status='ACTIVE',
        )
        UserBranch.objects.using('tenant_test').create(
            user=self.user_org_admin, branch=self.branch_1, scope_type='HOME', status='ACTIVE', is_active=True
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.user_org_admin, role=self.role_org_admin, branch=None, is_active=True, status='ACTIVE'
        )

        # Branch Manager user (Branch 1 only)
        self.user_branch1_mgr = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a,
            email='mgr_b1_p3@fitness.test',
            first_name='Manager',
            last_name='Branch1',
            status='ACTIVE',
        )
        UserBranch.objects.using('tenant_test').create(
            user=self.user_branch1_mgr, branch=self.branch_1, scope_type='HOME', status='ACTIVE', is_active=True
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.user_branch1_mgr, role=self.role_branch_mgr, branch=self.branch_1, is_active=True, status='ACTIVE'
        )

        # Sales Agent 1 (Active, Branch 1)
        self.user_sales_agent = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a,
            email='sales1_p3@fitness.test',
            first_name='Ayesha',
            last_name='Sales',
            status='ACTIVE',
        )
        UserBranch.objects.using('tenant_test').create(
            user=self.user_sales_agent, branch=self.branch_1, scope_type='HOME', status='ACTIVE', is_active=True
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.user_sales_agent, role=self.role_branch_mgr, branch=self.branch_1, is_active=True, status='ACTIVE'
        )

        # Inactive Agent (Branch 1)
        self.user_inactive_agent = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a,
            email='inactive_p3@fitness.test',
            first_name='Inactive',
            last_name='User',
            status='INACTIVE',
        )
        UserBranch.objects.using('tenant_test').create(
            user=self.user_inactive_agent, branch=self.branch_1, scope_type='HOME', status='ACTIVE', is_active=True
        )

        # Read-only user
        self.user_readonly = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a,
            email='readonly_p3@fitness.test',
            first_name='Read',
            last_name='Only',
            status='ACTIVE',
        )
        UserBranch.objects.using('tenant_test').create(
            user=self.user_readonly, branch=self.branch_1, scope_type='HOME', status='ACTIVE', is_active=True
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.user_readonly, role=self.role_readonly, branch=None, is_active=True, status='ACTIVE'
        )

        # 5. Lead Source
        self.lead_source = LeadSource.objects.using('tenant_test').create(
            organization=self.org_a,
            name='Instagram Ads P3',
            code='IG-ADS-P3',
            source_type='META',
            status='ACTIVE',
        )

        # 6. Sample Leads in Branch 1, Branch 2, and Org B
        self.lead_b1 = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_1,
            first_name='Ganesh',
            last_name='Naik',
            phone_normalized='+919876543210',
            email_normalized='ganesh.naik@test.com',
            lead_source=self.lead_source,
            assigned_sales_user=self.user_sales_agent,
            current_status='INTERESTED',
        )

        self.lead_b2 = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_2,
            first_name='Pooja',
            last_name='Sharma',
            phone_normalized='+919876543211',
            email_normalized='pooja.sharma@test.com',
            lead_source=self.lead_source,
            current_status='NEW_LEAD',
        )

        self.lead_org_b = Lead.objects.using('tenant_test').create(
            organization=self.org_b,
            first_name='OrgB',
            last_name='Lead',
            current_status='NEW_LEAD',
        )

        # Tokens
        self.token_admin = str(_build_tenant_token(self.user_org_admin, self.tenant_a, 'tenant_test').access_token)
        self.token_b1_mgr = str(_build_tenant_token(self.user_branch1_mgr, self.tenant_a, 'tenant_test').access_token)
        self.token_readonly = str(_build_tenant_token(self.user_readonly, self.tenant_a, 'tenant_test').access_token)

    # 1. Activity list uses real DB records
    def test_01_activity_list_uses_real_db_records(self):
        LeadActivity.objects.using('tenant_test').create(
            lead=self.lead_b1,
            activity_type='CALL',
            outcome='Connected with customer',
            notes='Discussed morning batch',
            performed_by_user=self.user_sales_agent,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_admin}')
        res = self.client.get('/api/v1/tenant/lead-activities/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.data if isinstance(res.data, list) else res.data.get('results', [])
        self.assertEqual(len(data), 1)
        self.assertEqual(str(data[0]['lead']), str(self.lead_b1.id))
        self.assertEqual(data[0]['activity_type'], 'CALL')
        self.assertEqual(data[0]['outcome'], 'Connected with customer')

    # 2. Create Activity
    def test_02_create_activity(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_admin}')
        payload = {
            'lead': str(self.lead_b1.id),
            'activity_type': 'WHATSAPP',
            'outcome': 'Sent trial details brochure',
            'notes': 'Customer requested WhatsApp message',
        }
        res = self.client.post('/api/v1/tenant/lead-activities/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['activity_type'], 'WHATSAPP')
        self.assertEqual(res.data['outcome'], 'Sent trial details brochure')

        act = LeadActivity.objects.using('tenant_test').filter(lead=self.lead_b1, activity_type='WHATSAPP').first()
        self.assertIsNotNone(act)

    # 3. Activity linked to correct Lead
    def test_03_activity_linked_to_correct_lead(self):
        act = CRMLeadService.record_lead_activity(
            lead=self.lead_b1,
            activity_type='CALL',
            outcome='Customer asked to call back',
            performed_by_user=self.user_sales_agent,
        )
        self.assertEqual(act.lead_id, self.lead_b1.id)
        self.assertEqual(LeadActivity.objects.using('tenant_test').filter(lead=self.lead_b2).count(), 0)

    # 4. Unauthorized Lead activity access denied
    def test_04_unauthorized_lead_activity_access_denied(self):
        LeadActivity.objects.using('tenant_test').create(
            lead=self.lead_b2,
            activity_type='CALL',
            outcome='Branch 2 call',
            performed_by_user=self.user_org_admin,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_b1_mgr}')
        res = self.client.get('/api/v1/tenant/lead-activities/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.data if isinstance(res.data, list) else res.data.get('results', [])
        b2_acts = [a for a in data if a['lead'] == str(self.lead_b2.id)]
        self.assertEqual(len(b2_acts), 0)

    # 5. Cross-branch activity create denied
    def test_05_cross_branch_activity_create_denied(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_b1_mgr}')
        payload = {
            'lead': str(self.lead_b2.id),
            'activity_type': 'CALL',
            'outcome': 'Unauthorized cross-branch call',
        }
        res = self.client.post('/api/v1/tenant/lead-activities/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    # 6. Cross-tenant / Cross-org activity denied
    def test_06_cross_tenant_activity_denied(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_admin}')
        payload = {
            'lead': str(self.lead_org_b.id),
            'activity_type': 'CALL',
            'outcome': 'Cross-tenant probe',
        }
        res = self.client.post('/api/v1/tenant/lead-activities/', payload, format='json')
        self.assertIn(res.status_code, [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND])

    # 7. Follow-up create
    def test_07_followup_create(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_admin}')
        due_at = (timezone.now() + timedelta(days=1)).isoformat()
        payload = {
            'lead': str(self.lead_b1.id),
            'task_type': 'CALL',
            'priority': 'HIGH',
            'due_at': due_at,
            'notes': 'Follow up on Pilates trial offer',
            'assigned_to_user': str(self.user_sales_agent.id),
        }
        res = self.client.post('/api/v1/tenant/sales-followup-tasks/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['task_type'], 'CALL')
        self.assertEqual(res.data['priority'], 'HIGH')
        self.assertEqual(res.data['status'], 'PENDING')
        self.assertEqual(str(res.data['assigned_to_user']), str(self.user_sales_agent.id))

    # 8. Follow-up update/reschedule
    def test_08_followup_update_reschedule(self):
        task = SalesFollowupTask.objects.using('tenant_test').create(
            lead=self.lead_b1,
            task_type='CALL',
            priority='NORMAL',
            due_at=timezone.now() + timedelta(hours=2),
            created_by_user=self.user_org_admin,
            assigned_to_user=self.user_sales_agent,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_admin}')
        new_due = (timezone.now() + timedelta(days=2)).isoformat()
        payload = {
            'due_at': new_due,
            'reason': 'Customer requested later call',
        }
        res = self.client.post(f'/api/v1/tenant/sales-followup-tasks/{task.id}/reschedule/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        task.refresh_from_db(using='tenant_test')
        self.assertIn('Customer requested later call', task.outcome)

    # 9. Follow-up complete
    def test_09_followup_complete(self):
        task = SalesFollowupTask.objects.using('tenant_test').create(
            lead=self.lead_b1,
            task_type='CALL',
            priority='HIGH',
            due_at=timezone.now() + timedelta(hours=1),
            created_by_user=self.user_org_admin,
            assigned_to_user=self.user_sales_agent,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_admin}')
        payload = {
            'outcome': 'Customer scheduled morning trial',
            'log_activity': True,
        }
        res = self.client.post(f'/api/v1/tenant/sales-followup-tasks/{task.id}/complete/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['status'], 'COMPLETED')
        self.assertEqual(res.data['outcome'], 'Customer scheduled morning trial')

        act = LeadActivity.objects.using('tenant_test').filter(lead=self.lead_b1, outcome__icontains='Customer scheduled morning trial').first()
        self.assertIsNotNone(act)

    # 10. Overdue calculation
    def test_10_overdue_calculation(self):
        past_task = SalesFollowupTask.objects.using('tenant_test').create(
            lead=self.lead_b1,
            task_type='CALL',
            priority='HIGH',
            due_at=timezone.now() - timedelta(hours=3),
            status='PENDING',
            created_by_user=self.user_org_admin,
            assigned_to_user=self.user_sales_agent,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_admin}')
        res = self.client.get(f'/api/v1/tenant/sales-followup-tasks/{past_task.id}/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.data['is_overdue'])

    # 11. Completed follow-up not overdue
    def test_11_completed_followup_not_overdue(self):
        completed_task = SalesFollowupTask.objects.using('tenant_test').create(
            lead=self.lead_b1,
            task_type='CALL',
            priority='HIGH',
            due_at=timezone.now() - timedelta(hours=3),
            status='COMPLETED',
            created_by_user=self.user_org_admin,
            assigned_to_user=self.user_sales_agent,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_admin}')
        res = self.client.get(f'/api/v1/tenant/sales-followup-tasks/{completed_task.id}/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertFalse(res.data['is_overdue'])

    # 12. Work queue overdue count
    def test_12_work_queue_overdue_count(self):
        SalesFollowupTask.objects.using('tenant_test').create(
            lead=self.lead_b1,
            task_type='CALL',
            priority='HIGH',
            due_at=timezone.now() - timedelta(days=1),
            status='PENDING',
            created_by_user=self.user_org_admin,
            assigned_to_user=self.user_sales_agent,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_admin}')
        res = self.client.get('/api/v1/tenant/sales-followup-tasks/work-queue/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(res.data['counts']['overdue'], 1)

    # 13. Work queue today count
    def test_13_work_queue_today_count(self):
        now = timezone.now()
        due_today = now.replace(hour=23, minute=50)
        if due_today <= now:
            due_today = now + timedelta(minutes=30)

        SalesFollowupTask.objects.using('tenant_test').create(
            lead=self.lead_b1,
            task_type='CALL',
            priority='NORMAL',
            due_at=due_today,
            status='PENDING',
            created_by_user=self.user_org_admin,
            assigned_to_user=self.user_sales_agent,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_admin}')
        res = self.client.get('/api/v1/tenant/sales-followup-tasks/work-queue/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(res.data['counts']['due_today'], 1)

    # 14. Work queue respects branch scope
    def test_14_work_queue_respects_branch_scope(self):
        SalesFollowupTask.objects.using('tenant_test').create(
            lead=self.lead_b2,
            task_type='CALL',
            priority='HIGH',
            due_at=timezone.now() - timedelta(hours=5),
            status='PENDING',
            created_by_user=self.user_org_admin,
            assigned_to_user=self.user_org_admin,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_b1_mgr}')
        res = self.client.get('/api/v1/tenant/sales-followup-tasks/work-queue/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['counts']['overdue'], 0)

    # 15. Work queue respects tenant scope
    def test_15_work_queue_respects_tenant_scope(self):
        SalesFollowupTask.objects.using('tenant_test').create(
            lead=self.lead_org_b,
            task_type='CALL',
            priority='HIGH',
            due_at=timezone.now() - timedelta(hours=2),
            status='PENDING',
            created_by_user=self.user_org_admin,
            assigned_to_user=self.user_org_admin,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_admin}')
        res = self.client.get('/api/v1/tenant/sales-followup-tasks/work-queue/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        org_a_tasks_count = SalesFollowupTask.objects.using('tenant_test').filter(lead__organization=self.org_a, status='PENDING').count()
        self.assertEqual(res.data['counts']['total_pending'], org_a_tasks_count)

    # 16. Assigned agent validated
    def test_16_assigned_agent_validated(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_admin}')
        payload = {
            'lead': str(self.lead_b1.id),
            'task_type': 'CALL',
            'priority': 'NORMAL',
            'due_at': (timezone.now() + timedelta(days=1)).isoformat(),
            'assigned_to_user': str(self.user_sales_agent.id),
        }
        res = self.client.post('/api/v1/tenant/sales-followup-tasks/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(str(res.data['assigned_to_user']), str(self.user_sales_agent.id))

    # 17. Inactive/unauthorized agent cannot be assigned
    def test_17_inactive_or_unauthorized_agent_cannot_be_assigned(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_admin}')
        payload = {
            'lead': str(self.lead_b1.id),
            'task_type': 'CALL',
            'priority': 'NORMAL',
            'due_at': (timezone.now() + timedelta(days=1)).isoformat(),
            'assigned_to_user': str(self.user_inactive_agent.id),
        }
        res = self.client.post('/api/v1/tenant/sales-followup-tasks/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('inactive', str(res.data).lower())

    # 18. Activity appears in Lead timeline
    def test_18_activity_appears_in_lead_timeline(self):
        CRMLeadService.record_lead_activity(
            lead=self.lead_b1,
            activity_type='VISIT',
            outcome='Facility tour completed',
            notes='Showed Pilates studio and strength zone',
            performed_by_user=self.user_sales_agent,
            duration_minutes=25,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_admin}')
        res = self.client.get(f'/api/v1/tenant/leads/{self.lead_b1.id}/timeline/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        events = res.data if isinstance(res.data, list) else res.data.get('events', [])
        act_events = [e for e in events if e['event_type'] == 'LEAD_ACTIVITY']
        self.assertTrue(len(act_events) >= 1)
        self.assertEqual(act_events[0]['metadata']['activity_type'], 'VISIT')
        self.assertEqual(act_events[0]['metadata']['outcome'], 'Facility tour completed')

    # 19. Follow-up created appears in timeline
    def test_19_followup_created_appears_in_timeline(self):
        CRMLeadService.create_followup_task(
            lead=self.lead_b1,
            task_type='TRIAL_FOLLOWUP',
            due_at=timezone.now() + timedelta(days=1),
            priority='HIGH',
            assigned_to_user=self.user_sales_agent,
            created_by_user=self.user_org_admin,
            outcome='Check if customer liked the trial',
        )
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_admin}')
        res = self.client.get(f'/api/v1/tenant/leads/{self.lead_b1.id}/timeline/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        events = res.data if isinstance(res.data, list) else res.data.get('events', [])
        task_events = [e for e in events if e['event_type'] == 'FOLLOWUP_TASK']
        self.assertTrue(len(task_events) >= 1)
        self.assertEqual(task_events[0]['metadata']['task_type'], 'TRIAL_FOLLOWUP')

    # 20. Follow-up completion appears in timeline
    def test_20_followup_completion_appears_in_timeline(self):
        task = CRMLeadService.create_followup_task(
            lead=self.lead_b1,
            task_type='CALL',
            due_at=timezone.now() + timedelta(hours=1),
            priority='HIGH',
            assigned_to_user=self.user_sales_agent,
            created_by_user=self.user_org_admin,
        )
        CRMLeadService.complete_followup_task(
            task=task,
            completed_by_user=self.user_sales_agent,
            outcome='Call answered, member agreed to visit',
        )
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_admin}')
        res = self.client.get(f'/api/v1/tenant/leads/{self.lead_b1.id}/timeline/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        events = res.data if isinstance(res.data, list) else res.data.get('events', [])
        completed_events = [e for e in events if e['event_type'] == 'FOLLOWUP_COMPLETED']
        self.assertTrue(len(completed_events) >= 1)
        self.assertEqual(completed_events[0]['metadata']['outcome'], 'Call answered, member agreed to visit')

    # 21. Lead 360 activities endpoint/data
    def test_21_lead_360_activities_endpoint(self):
        LeadActivity.objects.using('tenant_test').create(
            lead=self.lead_b1,
            activity_type='CALL',
            outcome='Spoke with customer',
            performed_by_user=self.user_sales_agent,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_admin}')
        res = self.client.get(f'/api/v1/tenant/leads/{self.lead_b1.id}/activities/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data), 1)
        self.assertEqual(res.data[0]['activity_type'], 'CALL')

    # 22. Lead 360 follow-ups endpoint/data
    def test_22_lead_360_followups_endpoint(self):
        SalesFollowupTask.objects.using('tenant_test').create(
            lead=self.lead_b1,
            task_type='CALL',
            priority='HIGH',
            due_at=timezone.now() + timedelta(days=1),
            created_by_user=self.user_org_admin,
            assigned_to_user=self.user_sales_agent,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_admin}')
        res = self.client.get(f'/api/v1/tenant/leads/{self.lead_b1.id}/followups/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data), 1)
        self.assertEqual(res.data[0]['task_type'], 'CALL')

    # 23. RBAC read-only Activity user cannot create
    def test_23_rbac_readonly_activity_user_cannot_create(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_readonly}')
        payload = {
            'lead': str(self.lead_b1.id),
            'activity_type': 'CALL',
            'outcome': 'Attempt by readonly user',
        }
        res = self.client.post('/api/v1/tenant/lead-activities/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    # 24. RBAC read-only Follow-up user cannot modify
    def test_24_rbac_readonly_followup_user_cannot_modify(self):
        task = SalesFollowupTask.objects.using('tenant_test').create(
            lead=self.lead_b1,
            task_type='CALL',
            priority='HIGH',
            due_at=timezone.now() + timedelta(days=1),
            created_by_user=self.user_org_admin,
            assigned_to_user=self.user_sales_agent,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_readonly}')
        res = self.client.post(
            f'/api/v1/tenant/sales-followup-tasks/{task.id}/complete/',
            {'outcome': 'Attempt by readonly user'},
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    # 25. Unauthorized mutation returns 403
    def test_25_unauthorized_mutation_returns_403(self):
        task_b2 = SalesFollowupTask.objects.using('tenant_test').create(
            lead=self.lead_b2,
            task_type='CALL',
            priority='HIGH',
            due_at=timezone.now() + timedelta(days=1),
            created_by_user=self.user_org_admin,
            assigned_to_user=self.user_org_admin,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_b1_mgr}')
        res = self.client.post(
            f'/api/v1/tenant/sales-followup-tasks/{task_b2.id}/complete/',
            {'outcome': 'Cross-branch complete'},
            format='json',
        )
        self.assertIn(res.status_code, [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND])

    # 26. Audit events generated
    def test_26_audit_events_generated(self):
        CRMLeadService.record_lead_activity(
            lead=self.lead_b1,
            activity_type='CALL',
            outcome='Audited interaction',
            performed_by_user=self.user_sales_agent,
        )
        task = CRMLeadService.create_followup_task(
            lead=self.lead_b1,
            task_type='CALL',
            due_at=timezone.now() + timedelta(hours=2),
            priority='HIGH',
            assigned_to_user=self.user_sales_agent,
            created_by_user=self.user_org_admin,
        )
        CRMLeadService.complete_followup_task(
            task=task,
            completed_by_user=self.user_sales_agent,
            outcome='Audited completion',
        )
        events = BusinessAuditEvent.objects.using('tenant_test').filter(
            action_code__in=['CRM_LEAD_ACTIVITY_LOGGED', 'CRM_FOLLOWUP_TASK_CREATED', 'CRM_FOLLOWUP_TASK_COMPLETED']
        )
        action_codes = [e.action_code for e in events]
        self.assertIn('CRM_LEAD_ACTIVITY_LOGGED', action_codes)
        self.assertIn('CRM_FOLLOWUP_TASK_CREATED', action_codes)
        self.assertIn('CRM_FOLLOWUP_TASK_COMPLETED', action_codes)

    # 27. Existing Phase 2 timeline still works
    def test_27_existing_phase2_timeline_still_works(self):
        LeadNote.objects.using('tenant_test').create(
            lead=self.lead_b1,
            note_text='Test note',
            created_by_user=self.user_sales_agent,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_admin}')
        res = self.client.get(f'/api/v1/tenant/leads/{self.lead_b1.id}/timeline/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        events = res.data if isinstance(res.data, list) else res.data.get('events', [])
        types = [e['event_type'] for e in events]
        self.assertIn('LEAD_NOTE', types)
        self.assertIn('LEAD_CREATED', types)

    # 28. No mock runtime data
    def test_28_no_mock_runtime_data(self):
        LeadActivity.objects.using('tenant_test').all().delete()
        SalesFollowupTask.objects.using('tenant_test').all().delete()
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_admin}')

        res_act = self.client.get('/api/v1/tenant/lead-activities/')
        self.assertEqual(res_act.status_code, status.HTTP_200_OK)
        act_data = res_act.data if isinstance(res_act.data, list) else res_act.data.get('results', [])
        self.assertEqual(len(act_data), 0)

        res_f = self.client.get('/api/v1/tenant/sales-followup-tasks/')
        self.assertEqual(res_f.status_code, status.HTTP_200_OK)
        f_data = res_f.data if isinstance(res_f.data, list) else res_f.data.get('results', [])
        self.assertEqual(len(f_data), 0)
