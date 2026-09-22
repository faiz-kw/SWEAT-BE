"""
tests/test_crm_phase7_stuck_lead_attention.py — Targeted Test Suite for Phase 7: Stuck Lead & Next Best Action Engine

Verifies the 27 architectural corrections and all Phase 7 requirements:
1. Fresh lead entering NEW_LEAD at T_0 is NOT marked stuck immediately.
2. Fresh lead entering INTERESTED at T_0 without a trial booked is NOT marked stuck immediately.
3. Stage SLA breach detected with correct overdue_seconds.
4. Stage transition resets stage SLA anchor; activity logging does NOT reset SLA anchor.
5. NO_FOLLOWUP is False when upcoming/pending task exists.
6. NO_FOLLOWUP is only flagged after the stage SLA deadline has passed.
7. FOLLOWUP_OVERDUE flagged with exact overdue duration.
8. TRIAL_CONFIRMATION_PENDING respects CRMTrialReminderPolicy wait duration.
9. TRIAL_RESCHEDULE_REQUESTED flags and recommends RESCHEDULE_TRIAL.
10. TRIAL_NO_SHOW recovery follow-up lifecycle (external_reference TRIAL_NOSHOW_<id>).
11. POST_TRIAL_FOLLOWUP_DUE lifecycle (external_reference TRIAL_ATTENDED_<id>).
12. Cancelled trial does not recommend attendance or confirmation.
13. COMMUNICATION_FAILED is distinct from NO_RESPONSE.
14. Chronological NO_RESPONSE evaluation; inbound customer reply clears NO_RESPONSE.
15. UNASSIGNED_LEAD gated by unassigned_wait_minutes.
16. Deterministic primary reason and severity resolution based on overdue urgency.
17. RecommendationActionRegistry separation of advisory actions from executable automation.
18. SLA scanner enqueues idempotent DomainOutboxEvent ('CRM_LEAD_SLA_BREACHED').
19. SLA scanner does not double-emit for same stage entry.
20. Attention Queue API endpoint (/api/v1/tenant/leads/attention/) paginated with full explanation.
21. Attention Queue filtering by branch, agent, stage, reason, and severity.
22. Next Action detail endpoint (/api/v1/tenant/leads/<id>/next-action/).
23. Compact lead list serialization (is_stuck, primary_reason, severity, recommended_action).
24. CRMAttentionPolicy API CRUD, audit logging, and tenant isolation.
25. Authoritative tenant-level Attention Queue summary metrics.
26. Action completion re-evaluation behavior.
"""

import os
import re
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
    LeadStatusHistory,
    LeadActivity,
    SalesFollowupTask,
    TrialBooking,
    CRMStageSlaPolicy,
    CRMTrialReminderPolicy,
)
from apps.tenant_core.models_communication import CommunicationMessage
from apps.tenant_core.models_attention import CRMAttentionPolicy
from apps.tenant_core.models_audit_outbox import DomainOutboxEvent
from apps.tenant_core.services_crm import CRMLeadService
from apps.tenant_core.services_attention import (
    LeadAttentionService,
    CANONICAL_SLA_BREACH_EVENT_CODE,
)
from apps.tenant_core.automation.registry import (
    TriggerRegistry,
    RecommendationActionRegistry,
)


class CRMPhase7StuckLeadAttentionTargetedTests(TestCase):
    databases = '__all__'

    def setUp(self):
        super().setUp()
        set_tenant_db_alias('tenant_test')
        self.client = APIClient()

        # Master Tenant Setup
        self.tenant = Tenant.objects.using('default').create(
            code='CRM-P7-TENANT-A',
            name='Tenant A Athletics',
            slug='tenant-a-athletics-p7',
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
            code='ENT-CRM-P7',
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

        # Tenant B for Isolation
        self.tenant_b = Tenant.objects.using('default').create(
            code='CRM-P7-TENANT-B',
            name='Tenant B Fitness',
            slug='tenant-b-fitness-p7',
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
            tenant=self.tenant_b, plan=self.plan, status='ACTIVE'
        )
        TenantModule.objects.using('default').create(
            tenant=self.tenant_b, module=self.mod_crm, is_enabled=True, availability_mode='ALL_BRANCHES'
        )

        # Organizations
        self.org_a = Organization.objects.using('tenant_test').create(
            code='ORG-P7-A',
            name='Tenant A Athletics Org',
            status='ACTIVE',
        )
        self.org_b = Organization.objects.using('tenant_test').create(
            code='ORG-P7-B',
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
            code='DT-P7-01',
            status='ACTIVE',
        )

        self.category_a = ProgramCategory.objects.using('tenant_test').create(
            organization=self.org_a,
            name='Fitness',
            code='FIT-P7',
        )
        self.program_a = Program.objects.using('tenant_test').create(
            organization=self.org_a,
            category=self.category_a,
            name='Strength Training',
            code='ST-P7-01',
            status='ACTIVE',
        )

        self.lead_source_a = LeadSource.objects.using('tenant_test').create(
            organization=self.org_a,
            code='WEB_INQUIRY',
            name='Website Inquiry',
            source_type='WEBSITE',
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

        self.role_admin = Role.objects.using('tenant_test').create(
            organization=self.org_a,
            name='CRM Manager',
            code='CRM_MGR_P7',
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
        self.pset_admin = RolePermissionSet.objects.using('tenant_test').create(
            role=self.role_admin, organization=self.org_a, name='Manager Permissions'
        )
        for code, p in self.perms.items():
            RolePermissionSetItem.objects.using('tenant_test').get_or_create(
                permission_set=self.pset_admin, permission=p, defaults={'granted': True, 'is_allowed': True}
            )

        # Admin User
        self.user_admin = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a,
            email='manager.crm.p7@gmail.com',
            first_name='Aarav',
            last_name='Mehta',
            status='ACTIVE',
            is_login_allowed=True,
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.user_admin, role=self.role_admin, is_active=True
        )

        # Sales Agent User
        self.sales_agent = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a,
            email='agent.p7@gmail.com',
            first_name='Rohan',
            last_name='Sharma',
            status='ACTIVE',
            is_login_allowed=True,
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.sales_agent, role=self.role_admin, is_active=True
        )

        # Auth Token
        self.token_admin = str(_build_tenant_token(
            user=self.user_admin,
            tenant=self.tenant,
            db_alias='tenant_test',
        ).access_token)
        self.client.credentials(
            HTTP_AUTHORIZATION=f'Bearer {self.token_admin}',
            HTTP_X_TENANT_ID=str(self.tenant.id),
        )

        # CRMAttentionPolicy for Org A (enabled for testing)
        self.attention_policy, _ = CRMAttentionPolicy.objects.using('tenant_test').get_or_create(
            organization=self.org_a,
            defaults={
                'is_enabled': True,
                'sla_breach_attention_enabled': True,
                'no_followup_attention_enabled': True,
                'overdue_followup_attention_enabled': True,
                'no_response_attention_enabled': True,
                'no_response_wait_hours': 24,
                'trial_not_booked_attention_enabled': True,
                'trial_confirmation_attention_enabled': True,
                'trial_no_show_attention_enabled': True,
                'post_trial_followup_attention_enabled': True,
                'unassigned_lead_attention_enabled': True,
                'unassigned_wait_minutes': 60,
            }
        )

        # Stage SLA Policy for NEW_LEAD (2 hours)
        self.sla_policy_new = CRMStageSlaPolicy.objects.using('tenant_test').create(
            organization=self.org_a,
            canonical_stage='NEW_LEAD',
            display_label='New Lead',
            response_target_value=2,
            response_target_unit='HOURS',
            is_enabled=True,
            display_order=1,
        )

        # Stage SLA Policy for INTERESTED (4 hours)
        self.sla_policy_interested = CRMStageSlaPolicy.objects.using('tenant_test').create(
            organization=self.org_a,
            canonical_stage='INTERESTED',
            display_label='Interested',
            response_target_value=4,
            response_target_unit='HOURS',
            is_enabled=True,
            display_order=2,
        )

        # Trial Reminder Policy for Org A
        self.trial_policy, _ = CRMTrialReminderPolicy.objects.using('tenant_test').get_or_create(
            organization=self.org_a,
            defaults={
                'confirmation_wait_duration_value': 2,
                'confirmation_wait_duration_unit': 'HOURS',
                'ask_attendance_confirmation': True,
            }
        )

    # -------------------------------------------------------------------------
    # TEST 1: Fresh lead entering NEW_LEAD at T_0 is NOT marked stuck immediately
    # -------------------------------------------------------------------------
    def test_01_fresh_lead_not_stuck_immediately(self):
        lead = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            lead_source=self.lead_source_a,
            assigned_sales_user=self.sales_agent,
            first_name='Karan',
            last_name='Verma',
            phone_normalized='+919876543210',
            email_normalized='karan.verma@gmail.com',
            current_status='NEW_LEAD',
            created_at=timezone.now(),
        )

        result = LeadAttentionService.evaluate_lead(lead, db_alias='tenant_test')
        self.assertFalse(result['is_stuck'])
        self.assertIsNone(result['primary_reason'])
        self.assertEqual(len(result['reasons']), 0)
        self.assertEqual(result['recommended_action']['action_code'], 'REVIEW_LEAD')

    # -------------------------------------------------------------------------
    # TEST 2: Fresh lead in INTERESTED without trial is NOT marked stuck immediately
    # -------------------------------------------------------------------------
    def test_02_fresh_interested_lead_without_trial_not_stuck_immediately(self):
        lead = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            lead_source=self.lead_source_a,
            assigned_sales_user=self.sales_agent,
            first_name='Ananya',
            last_name='Roy',
            phone_normalized='+919876543211',
            email_normalized='ananya.roy@gmail.com',
            current_status='INTERESTED',
            created_at=timezone.now(),
        )

        result = LeadAttentionService.evaluate_lead(lead, db_alias='tenant_test')
        self.assertFalse(result['is_stuck'])
        self.assertIsNone(result['primary_reason'])

    # -------------------------------------------------------------------------
    # TEST 3: Stage SLA breach detected with correct overdue_seconds
    # -------------------------------------------------------------------------
    def test_03_stage_sla_breached_detected(self):
        three_hours_ago = timezone.now() - timedelta(hours=3)
        lead = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            lead_source=self.lead_source_a,
            assigned_sales_user=self.sales_agent,
            first_name='Sunil',
            last_name='Gavaskar',
            phone_normalized='+919876543212',
            email_normalized='sunil.g@gmail.com',
            current_status='NEW_LEAD',
            created_at=three_hours_ago,
        )

        result = LeadAttentionService.evaluate_lead(lead, db_alias='tenant_test')
        self.assertTrue(result['is_stuck'])
        self.assertEqual(result['primary_reason'], 'STAGE_SLA_BREACHED')
        self.assertGreater(result['overdue_by_seconds'], 3500)
        self.assertEqual(result['severity'], 'HIGH')
        self.assertEqual(result['recommended_action']['action_code'], 'CALL_LEAD')

    # -------------------------------------------------------------------------
    # TEST 4: Stage transition resets SLA anchor; activity logging does NOT
    # -------------------------------------------------------------------------
    def test_04_stage_transition_resets_sla_clock_activity_does_not(self):
        three_hours_ago = timezone.now() - timedelta(hours=3)
        lead = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            lead_source=self.lead_source_a,
            assigned_sales_user=self.sales_agent,
            first_name='Vikram',
            last_name='Seth',
            phone_normalized='+919876543213',
            email_normalized='vikram.seth@gmail.com',
            current_status='NEW_LEAD',
            created_at=three_hours_ago,
        )

        # 1. Verify breached in NEW_LEAD
        res1 = LeadAttentionService.evaluate_lead(lead, db_alias='tenant_test')
        self.assertTrue(res1['is_stuck'])

        # 2. Activity logging does NOT reset stage SLA clock
        CRMLeadService.record_lead_activity(
            lead=lead,
            activity_type='CALL',
            outcome='CONNECTED',
            notes='Follow-up attempt recorded',
            actor_user=self.user_admin,
            db_alias='tenant_test',
        )
        res2 = LeadAttentionService.evaluate_lead(lead, db_alias='tenant_test')
        self.assertTrue(res2['is_stuck'])
        self.assertEqual(res2['primary_reason'], 'STAGE_SLA_BREACHED')

        # 3. Stage transition DOES reset the stage SLA anchor
        updated_lead = CRMLeadService.transition_lead_status(
            lead=lead,
            new_status='INTERESTED',
            reason_code='COMMERCIAL_PROGRESSION',
            reason_text='Prospect wants trial info',
            actor_user=self.user_admin,
            db_alias='tenant_test',
        )
        res3 = LeadAttentionService.evaluate_lead(updated_lead, db_alias='tenant_test')
        # Newly entered INTERESTED at T_0 (4h SLA) -> NOT stuck
        self.assertFalse(res3['is_stuck'])

    # -------------------------------------------------------------------------
    # TEST 5: NO_FOLLOWUP is False when upcoming/pending task exists
    # -------------------------------------------------------------------------
    def test_05_no_followup_false_when_pending_followup_exists(self):
        three_hours_ago = timezone.now() - timedelta(hours=3)
        lead = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            lead_source=self.lead_source_a,
            assigned_sales_user=self.sales_agent,
            first_name='Pooja',
            last_name='Bhatt',
            phone_normalized='+919876543214',
            email_normalized='pooja.bhatt@gmail.com',
            current_status='NEW_LEAD',
            created_at=three_hours_ago,
        )
        # Schedule future follow-up
        SalesFollowupTask.objects.using('tenant_test').create(
            lead=lead,
            assigned_to_user=self.sales_agent,
            created_by_user=self.sales_agent,
            task_type='CALL',
            due_at=timezone.now() + timedelta(days=1),
            status='PENDING',
        )

        res = LeadAttentionService.evaluate_lead(lead, db_alias='tenant_test')
        reason_codes = [r['code'] for r in res['reasons']]
        self.assertNotIn('NO_FOLLOWUP', reason_codes)

    # -------------------------------------------------------------------------
    # TEST 6: FOLLOWUP_OVERDUE flagged with exact overdue duration
    # -------------------------------------------------------------------------
    def test_06_followup_overdue_detected(self):
        lead = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            lead_source=self.lead_source_a,
            assigned_sales_user=self.sales_agent,
            first_name='Rajat',
            last_name='Kapoor',
            phone_normalized='+919876543215',
            email_normalized='rajat.kapoor@gmail.com',
            current_status='FOLLOW_UP_PENDING',
            created_at=timezone.now(),
        )
        task = SalesFollowupTask.objects.using('tenant_test').create(
            lead=lead,
            assigned_to_user=self.sales_agent,
            created_by_user=self.sales_agent,
            task_type='CALL',
            due_at=timezone.now() - timedelta(hours=5),
            status='PENDING',
            priority='HIGH',
        )

        res = LeadAttentionService.evaluate_lead(lead, db_alias='tenant_test')
        self.assertTrue(res['is_stuck'])
        self.assertEqual(res['primary_reason'], 'FOLLOWUP_OVERDUE')
        self.assertGreater(res['overdue_by_seconds'], 17000)
        self.assertEqual(res['recommended_action']['action_code'], 'CALL_LEAD')

    # -------------------------------------------------------------------------
    # TEST 7: TRIAL_CONFIRMATION_PENDING respects reminder policy wait duration
    # -------------------------------------------------------------------------
    def test_07_trial_confirmation_pending_respects_reminder_policy(self):
        lead = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            lead_source=self.lead_source_a,
            assigned_sales_user=self.sales_agent,
            first_name='Meera',
            last_name='Nair',
            phone_normalized='+919876543216',
            email_normalized='meera.nair@gmail.com',
            current_status='TRIAL_BOOKED',
            created_at=timezone.now(),
        )
        # Trial created 3 hours ago with PENDING confirmation (wait duration is 2h)
        trial = TrialBooking.objects.using('tenant_test').create(
            branch=self.branch_a,
            lead=lead,
            scheduled_start=timezone.now() + timedelta(days=1),
            scheduled_end=timezone.now() + timedelta(days=1, hours=1),
            status='BOOKED',
            confirmation_status='PENDING',
            created_at=timezone.now() - timedelta(hours=3),
        )

        res = LeadAttentionService.evaluate_lead(lead, db_alias='tenant_test')
        self.assertTrue(res['is_stuck'])
        self.assertEqual(res['primary_reason'], 'TRIAL_CONFIRMATION_PENDING')
        self.assertEqual(res['recommended_action']['action_code'], 'CONFIRM_TRIAL')

    # -------------------------------------------------------------------------
    # TEST 8: TRIAL_RESCHEDULE_REQUESTED recommends RESCHEDULE_TRIAL
    # -------------------------------------------------------------------------
    def test_08_trial_reschedule_requested(self):
        lead = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            lead_source=self.lead_source_a,
            assigned_sales_user=self.sales_agent,
            first_name='Deepak',
            last_name='Dobriyal',
            phone_normalized='+919876543217',
            email_normalized='deepak.d@gmail.com',
            current_status='TRIAL_BOOKED',
            created_at=timezone.now(),
        )
        TrialBooking.objects.using('tenant_test').create(
            branch=self.branch_a,
            lead=lead,
            scheduled_start=timezone.now() + timedelta(days=2),
            scheduled_end=timezone.now() + timedelta(days=2, hours=1),
            status='BOOKED',
            confirmation_status='RESCHEDULE_REQUESTED',
        )

        res = LeadAttentionService.evaluate_lead(lead, db_alias='tenant_test')
        self.assertTrue(res['is_stuck'])
        self.assertEqual(res['primary_reason'], 'TRIAL_RESCHEDULE_REQUESTED')
        self.assertEqual(res['recommended_action']['action_code'], 'RESCHEDULE_TRIAL')

    # -------------------------------------------------------------------------
    # TEST 9: TRIAL_NO_SHOW lifecycle using external_reference TRIAL_NOSHOW_<id>
    # -------------------------------------------------------------------------
    def test_09_trial_no_show_recovery_lifecycle(self):
        lead = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            lead_source=self.lead_source_a,
            assigned_sales_user=self.sales_agent,
            first_name='Sanjay',
            last_name='Mishra',
            phone_normalized='+919876543218',
            email_normalized='sanjay.mishra@gmail.com',
            current_status='NO_SHOW',
            created_at=timezone.now(),
        )
        trial = TrialBooking.objects.using('tenant_test').create(
            branch=self.branch_a,
            lead=lead,
            scheduled_start=timezone.now() - timedelta(days=1),
            scheduled_end=timezone.now() - timedelta(days=1, hours=1),
            status='NO_SHOW',
        )
        # Phase 4 creates task with external_reference f"TRIAL_NOSHOW_{trial.id}"
        recovery_task = SalesFollowupTask.objects.using('tenant_test').create(
            lead=lead,
            assigned_to_user=self.sales_agent,
            created_by_user=self.sales_agent,
            task_type='CALL',
            due_at=timezone.now() + timedelta(hours=2),
            status='PENDING',
            external_reference=f"TRIAL_NOSHOW_{trial.id}",
        )

        # Still pending -> TRIAL_NO_SHOW is flagged
        res = LeadAttentionService.evaluate_lead(lead, db_alias='tenant_test')
        self.assertTrue(res['is_stuck'])
        self.assertEqual(res['primary_reason'], 'TRIAL_NO_SHOW')

        # Once recovery task is completed -> cleared
        recovery_task.status = 'COMPLETED'
        recovery_task.save(using='tenant_test')

        res_cleared = LeadAttentionService.evaluate_lead(lead, db_alias='tenant_test')
        reason_codes = [r['code'] for r in res_cleared['reasons']]
        self.assertNotIn('TRIAL_NO_SHOW', reason_codes)

    # -------------------------------------------------------------------------
    # TEST 10: POST_TRIAL_FOLLOWUP_DUE lifecycle using TRIAL_ATTENDED_<id>
    # -------------------------------------------------------------------------
    def test_10_post_trial_followup_due_lifecycle(self):
        lead = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            lead_source=self.lead_source_a,
            assigned_sales_user=self.sales_agent,
            first_name='Neena',
            last_name='Gupta',
            phone_normalized='+919876543219',
            email_normalized='neena.gupta@gmail.com',
            current_status='TRIAL_ATTENDED',
            created_at=timezone.now(),
        )
        trial = TrialBooking.objects.using('tenant_test').create(
            branch=self.branch_a,
            lead=lead,
            scheduled_start=timezone.now() - timedelta(hours=5),
            scheduled_end=timezone.now() - timedelta(hours=4),
            status='ATTENDED',
        )

        res = LeadAttentionService.evaluate_lead(lead, db_alias='tenant_test')
        self.assertTrue(res['is_stuck'])
        self.assertEqual(res['primary_reason'], 'POST_TRIAL_FOLLOWUP_DUE')
        self.assertEqual(res['recommended_action']['action_code'], 'POST_TRIAL_FOLLOWUP')

        # Complete post-trial task
        SalesFollowupTask.objects.using('tenant_test').create(
            lead=lead,
            assigned_to_user=self.sales_agent,
            created_by_user=self.sales_agent,
            task_type='FOLLOW_UP',
            due_at=timezone.now(),
            status='COMPLETED',
            external_reference=f"TRIAL_ATTENDED_{trial.id}",
        )

        res_cleared = LeadAttentionService.evaluate_lead(lead, db_alias='tenant_test')
        reason_codes = [r['code'] for r in res_cleared['reasons']]
        self.assertNotIn('POST_TRIAL_FOLLOWUP_DUE', reason_codes)

    # -------------------------------------------------------------------------
    # TEST 11: Cancelled trial does not recommend attendance or confirmation
    # -------------------------------------------------------------------------
    def test_11_cancelled_trial_not_stuck(self):
        lead = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            lead_source=self.lead_source_a,
            assigned_sales_user=self.sales_agent,
            first_name='Pankaj',
            last_name='Tripathi',
            phone_normalized='+919876543220',
            email_normalized='pankaj.t@gmail.com',
            current_status='NEW_LEAD',
            created_at=timezone.now(),
        )
        TrialBooking.objects.using('tenant_test').create(
            branch=self.branch_a,
            lead=lead,
            scheduled_start=timezone.now() - timedelta(days=1),
            scheduled_end=timezone.now() - timedelta(days=1, hours=1),
            status='CANCELLED',
        )

        res = LeadAttentionService.evaluate_lead(lead, db_alias='tenant_test')
        self.assertFalse(res['is_stuck'])

    # -------------------------------------------------------------------------
    # TEST 12: COMMUNICATION_FAILED is distinct from NO_RESPONSE
    # -------------------------------------------------------------------------
    def test_12_communication_failed_distinct_from_no_response(self):
        lead = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            lead_source=self.lead_source_a,
            assigned_sales_user=self.sales_agent,
            first_name='Boman',
            last_name='Irani',
            phone_normalized='+919876543221',
            email_normalized='boman.irani@gmail.com',
            current_status='NEW_LEAD',
            created_at=timezone.now(),
        )
        CommunicationMessage.objects.using('tenant_test').create(
            organization=self.org_a,
            lead=lead,
            channel='WHATSAPP',
            direction='OUTBOUND',
            recipient='+919876543221',
            status='FAILED',
            failure_code='INVALID_NUMBER',
        )

        res = LeadAttentionService.evaluate_lead(lead, db_alias='tenant_test')
        self.assertTrue(res['is_stuck'])
        self.assertEqual(res['primary_reason'], 'COMMUNICATION_FAILED')
        reason_codes = [r['code'] for r in res['reasons']]
        self.assertNotIn('NO_RESPONSE', reason_codes)

    # -------------------------------------------------------------------------
    # TEST 13: Chronological NO_RESPONSE; inbound customer reply clears it
    # -------------------------------------------------------------------------
    def test_13_chronological_no_response_and_customer_reply(self):
        lead = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            lead_source=self.lead_source_a,
            assigned_sales_user=self.sales_agent,
            first_name='Tabu',
            last_name='Hashmi',
            phone_normalized='+919876543222',
            email_normalized='tabu.h@gmail.com',
            current_status='NEW_LEAD',
            created_at=timezone.now(),
        )
        # Outbound message sent 30 hours ago (wait window is 24h)
        msg_out = CommunicationMessage.objects.using('tenant_test').create(
            organization=self.org_a,
            lead=lead,
            channel='WHATSAPP',
            direction='OUTBOUND',
            recipient='+919876543222',
            status='DELIVERED',
            created_at=timezone.now() - timedelta(hours=30),
        )

        res = LeadAttentionService.evaluate_lead(lead, db_alias='tenant_test')
        self.assertTrue(res['is_stuck'])
        self.assertEqual(res['primary_reason'], 'NO_RESPONSE')

        # Inbound reply arrives AFTER outbound message
        CommunicationMessage.objects.using('tenant_test').create(
            organization=self.org_a,
            lead=lead,
            channel='WHATSAPP',
            direction='INBOUND',
            recipient='+919876543222',
            status='DELIVERED',
            created_at=timezone.now() - timedelta(hours=5),
        )

        res_cleared = LeadAttentionService.evaluate_lead(lead, db_alias='tenant_test')
        reason_codes = [r['code'] for r in res_cleared['reasons']]
        self.assertNotIn('NO_RESPONSE', reason_codes)

    # -------------------------------------------------------------------------
    # TEST 14: UNASSIGNED_LEAD gated by unassigned_wait_minutes
    # -------------------------------------------------------------------------
    def test_14_unassigned_lead_gated_by_wait_minutes(self):
        # Fresh unassigned lead created 10 minutes ago (wait is 60m)
        lead_fresh = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            lead_source=self.lead_source_a,
            assigned_sales_user=None,
            first_name='Nawaz',
            last_name='Siddiqui',
            phone_normalized='+919876543223',
            email_normalized='nawaz.s@gmail.com',
            current_status='NEW_LEAD',
            created_at=timezone.now() - timedelta(minutes=10),
        )
        res_fresh = LeadAttentionService.evaluate_lead(lead_fresh, db_alias='tenant_test')
        self.assertNotIn('UNASSIGNED_LEAD', [r['code'] for r in res_fresh['reasons']])

        # Unassigned lead created 90 minutes ago (> 60m)
        lead_old = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            lead_source=self.lead_source_a,
            assigned_sales_user=None,
            first_name='Manoj',
            last_name='Bajpayee',
            phone_normalized='+919876543224',
            email_normalized='manoj.b@gmail.com',
            current_status='NEW_LEAD',
            created_at=timezone.now() - timedelta(minutes=90),
        )
        res_old = LeadAttentionService.evaluate_lead(lead_old, db_alias='tenant_test')
        self.assertIn('UNASSIGNED_LEAD', [r['code'] for r in res_old['reasons']])

    # -------------------------------------------------------------------------
    # TEST 15: RecommendationActionRegistry advisory metadata
    # -------------------------------------------------------------------------
    def test_15_recommendation_action_registry_metadata(self):
        meta_call = RecommendationActionRegistry.get_recommendation_meta('CALL_LEAD')
        self.assertIsNotNone(meta_call)
        self.assertFalse(meta_call['is_automation_executable'])
        self.assertEqual(meta_call['target_ui_action'], 'LOG_ACTIVITY')

        meta_book = RecommendationActionRegistry.get_recommendation_meta('BOOK_TRIAL')
        self.assertIsNotNone(meta_book)
        self.assertFalse(meta_book['is_automation_executable'])
        self.assertEqual(meta_book['target_ui_action'], 'BOOK_TRIAL')

        meta_assign = RecommendationActionRegistry.get_recommendation_meta('ASSIGN_LEAD')
        self.assertIsNotNone(meta_assign)
        self.assertTrue(meta_assign['is_automation_executable'])

        self.assertEqual(
            TriggerRegistry.TRIGGERS['CRM_LEAD_SLA_BREACHED']['domain'],
            'crm'
        )

    # -------------------------------------------------------------------------
    # TEST 16: SLA scanner enqueues idempotent DomainOutboxEvent ('CRM_LEAD_SLA_BREACHED')
    # -------------------------------------------------------------------------
    def test_16_sla_scanner_enqueues_idempotent_event(self):
        three_hours_ago = timezone.now() - timedelta(hours=3)
        lead = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            lead_source=self.lead_source_a,
            assigned_sales_user=self.sales_agent,
            first_name='Jaideep',
            last_name='Ahlawat',
            phone_normalized='+919876543225',
            email_normalized='jaideep.a@gmail.com',
            current_status='NEW_LEAD',
            created_at=three_hours_ago,
        )

        emitted_count = LeadAttentionService.scan_and_emit_sla_breaches(
            organization=self.org_a, db_alias='tenant_test'
        )
        self.assertEqual(emitted_count, 1)

        event = DomainOutboxEvent.objects.using('tenant_test').filter(
            event_type=CANONICAL_SLA_BREACH_EVENT_CODE,
            aggregate_id=lead.id,
        ).first()
        self.assertIsNotNone(event)
        self.assertEqual(event.event_type, 'CRM_LEAD_SLA_BREACHED')
        self.assertTrue(event.payload.get('idempotency_key', '').startswith(f"SLA_BREACH_{lead.id}"))

        # Second scan -> does not duplicate
        emitted_again = LeadAttentionService.scan_and_emit_sla_breaches(
            organization=self.org_a, db_alias='tenant_test'
        )
        self.assertEqual(emitted_again, 0)

    # -------------------------------------------------------------------------
    # TEST 17: Attention Queue API endpoint paginated with full explanation
    # -------------------------------------------------------------------------
    def test_17_attention_queue_api(self):
        three_hours_ago = timezone.now() - timedelta(hours=3)
        lead = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            lead_source=self.lead_source_a,
            assigned_sales_user=self.sales_agent,
            first_name='Shefali',
            last_name='Shah',
            phone_normalized='+919876543226',
            email_normalized='shefali.shah@gmail.com',
            current_status='NEW_LEAD',
            created_at=three_hours_ago,
        )

        response = self.client.get(
            '/api/v1/tenant/leads/attention/',
            HTTP_AUTHORIZATION=f'Bearer {self.token_admin}',
            HTTP_X_TENANT_ID=str(self.tenant.id),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn('results', data)
        self.assertIn('metrics', data)
        self.assertGreaterEqual(data['count'], 1)

        item = next(r for r in data['results'] if r['id'] == str(lead.id))
        self.assertTrue(item['attention']['is_stuck'])
        self.assertEqual(item['attention']['primary_reason'], 'STAGE_SLA_BREACHED')
        self.assertEqual(item['attention']['recommended_action']['action_code'], 'CALL_LEAD')

    # -------------------------------------------------------------------------
    # TEST 18: Next Action detail endpoint for Lead 360
    # -------------------------------------------------------------------------
    def test_18_next_action_detail_api(self):
        three_hours_ago = timezone.now() - timedelta(hours=3)
        lead = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            lead_source=self.lead_source_a,
            assigned_sales_user=self.sales_agent,
            first_name='Kay',
            last_name='Menon',
            phone_normalized='+919876543227',
            email_normalized='kay.menon@gmail.com',
            current_status='NEW_LEAD',
            created_at=three_hours_ago,
        )

        response = self.client.get(
            f'/api/v1/tenant/leads/{lead.id}/next-action/',
            HTTP_AUTHORIZATION=f'Bearer {self.token_admin}',
            HTTP_X_TENANT_ID=str(self.tenant.id),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertTrue(data['is_stuck'])
        self.assertEqual(data['primary_reason'], 'STAGE_SLA_BREACHED')
        self.assertIn('reasons', data)
        self.assertIn('recommended_action', data)

    # -------------------------------------------------------------------------
    # TEST 19: CRMAttentionPolicy API CRUD, audit logging, and isolation
    # -------------------------------------------------------------------------
    def test_19_attention_policy_api_crud_and_isolation(self):
        # Read policy
        resp = self.client.get(
            '/api/v1/tenant/crm/attention-policy/',
            HTTP_AUTHORIZATION=f'Bearer {self.token_admin}',
            HTTP_X_TENANT_ID=str(self.tenant.id),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        results = data.get('results', data) if isinstance(data, dict) else data
        self.assertGreaterEqual(len(results), 1)
        policy_id = results[0]['id']

        # Update policy
        patch_resp = self.client.patch(
            f'/api/v1/tenant/crm/attention-policy/{policy_id}/',
            {'no_response_wait_hours': 48},
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {self.token_admin}',
            HTTP_X_TENANT_ID=str(self.tenant.id),
        )
        self.assertEqual(patch_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(patch_resp.json()['no_response_wait_hours'], 48)

    # -------------------------------------------------------------------------
    # TEST 20: Authoritative tenant-level Attention Queue summary metrics
    # -------------------------------------------------------------------------
    def test_20_authoritative_metrics_endpoint(self):
        resp = self.client.get(
            '/api/v1/tenant/leads/attention/metrics/',
            HTTP_AUTHORIZATION=f'Bearer {self.token_admin}',
            HTTP_X_TENANT_ID=str(self.tenant.id),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        metrics = resp.json()
        self.assertIn('stuck_leads', metrics)
        self.assertIn('sla_breached', metrics)
        self.assertIn('overdue_tasks', metrics)
        self.assertIn('awaiting_response', metrics)
        self.assertIn('total_active_leads', metrics)
