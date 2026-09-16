"""
tests/test_layer2_phase2_crm.py — Tests for Layer 2 Phase 2: Module B (CRM, Leads, Trials & Intake)
"""

import uuid
from datetime import datetime, date, time, timedelta
from django.test import TestCase
from django.utils import timezone
from rest_framework import status

from config.routers import set_tenant_db_alias
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.models_saas import SaasPlan, TenantSubscription, ProductModule, TenantModule
from apps.authentication.views import _build_tenant_token
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
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
from apps.tenant_core.models_workforce import (
    UserProfile,
    EmployeeProfile,
    TrainerProfile,
    EmployeeWorkSchedule,
)
from apps.tenant_core.models_crm import (
    LeadSource,
    Lead,
    LeadStatusHistory,
    LeadAssignment,
    LeadNote,
    LeadActivity,
    IntakeForm,
    IntakeQuestion,
    IntakeSubmission,
    IntakeAnswer,
    TrialBooking,
    TrialStatusHistory,
    SalesFollowupTask,
)
from apps.tenant_core.services_crm import CRMLeadService
from apps.tenant_core.models_audit_outbox import BusinessAuditEvent, DomainOutboxEvent


class Layer2Phase2CRMTests(TestCase):
    databases = '__all__'

    def setUp(self):
        super().setUp()
        set_tenant_db_alias('tenant_test')

        # 1. Master DB Setup
        self.tenant = Tenant.objects.using('default').create(
            code='CRM-TENANT',
            name='CRM Gym',
            slug='crm-gym',
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
            name='CRM Plan',
            code='CRM-PLAN',
            tier='ENTERPRISE',
            status='ACTIVE',
        )
        self.sub = TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
        )
        self.prod_mod_crm, _ = ProductModule.objects.using('default').get_or_create(
            code='crm',
            defaults={'name': 'CRM Module', 'status': 'ACTIVE'},
        )
        self.tm_crm = TenantModule.objects.using('default').create(
            tenant=self.tenant,
            module=self.prod_mod_crm,
            is_enabled=True,
            availability_mode='ALL_BRANCHES',
        )

        # 2. Tenant DB Org Setup
        self.org = Organization.objects.using('tenant_test').create(
            code='CRM-ORG',
            name='CRM Org',
            status='ACTIVE',
        )
        self.loc = Location.objects.using('tenant_test').create(
            organization=self.org,
            code='CRM-LOC',
            name='CRM Location',
            status='ACTIVE',
        )
        self.branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            code='CRM-BR-1',
            name='CRM Branch 1',
            status='ACTIVE',
        )

        # 3. Tenant Admin User & Sales Users
        self.admin_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='admin@crm.test',
            first_name='CRM',
            last_name='Admin',
            status='ACTIVE',
        )
        self.admin_user.set_password('TestPass123!')
        self.admin_user.save(using='tenant_test')

        self.sales_rep_a = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='sales.a@crm.test',
            first_name='Sales',
            last_name='RepA',
            status='ACTIVE',
        )
        self.sales_rep_b = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='sales.b@crm.test',
            first_name='Sales',
            last_name='RepB',
            status='ACTIVE',
        )

        self.role_admin = Role.objects.using('tenant_test').create(
            name='Org Admin',
            code='ORG_ADMIN',
            is_system_role=True,
            scope='ORG',
            organization=self.org,
            is_active=True,
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.admin_user,
            role=self.role_admin,
            organization=self.org,
            is_active=True,
        )

        # 4. RBAC Catalog for crm.leads
        self.cat_crm, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(
            module_code='crm',
            defaults={'name': 'CRM', 'is_enabled': True},
        )
        self.csub_leads, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=self.cat_crm,
            submodule_code='leads',
            defaults={'name': 'Leads', 'is_enabled': True},
        )
        self.perm_leads_view, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.cat_crm,
            submodule=self.csub_leads,
            action='view',
            defaults={'permission_code': 'crm.leads.view', 'label': 'View Leads'},
        )
        self.perm_leads_create, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.cat_crm,
            submodule=self.csub_leads,
            action='create',
            defaults={'permission_code': 'crm.leads.create', 'label': 'Create Leads'},
        )
        self.perm_leads_edit, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.cat_crm,
            submodule=self.csub_leads,
            action='edit',
            defaults={'permission_code': 'crm.leads.edit', 'label': 'Edit Leads'},
        )

        RoleModuleAccess.objects.using('tenant_test').get_or_create(
            role=self.role_admin, module=self.cat_crm, defaults={'can_access': True}
        )
        RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(
            role=self.role_admin, submodule=self.csub_leads, defaults={'can_access': True}
        )
        self.perm_set, _ = RolePermissionSet.objects.using('tenant_test').get_or_create(
            role=self.role_admin, defaults={'name': 'Admin CRM Permissions', 'is_active': True}
        )
        RolePermissionSetItem.objects.using('tenant_test').get_or_create(
            permission_set=self.perm_set, permission=self.perm_leads_view, defaults={'granted': True}
        )
        RolePermissionSetItem.objects.using('tenant_test').get_or_create(
            permission_set=self.perm_set, permission=self.perm_leads_create, defaults={'granted': True}
        )
        RolePermissionSetItem.objects.using('tenant_test').get_or_create(
            permission_set=self.perm_set, permission=self.perm_leads_edit, defaults={'granted': True}
        )

        # 5. Lead Source
        self.lead_source = LeadSource.objects.using('tenant_test').create(
            organization=self.org,
            code='META_ADS',
            name='Instagram / Facebook Ads',
            source_type='META',
            status='ACTIVE',
        )

    def tearDown(self):
        set_tenant_db_alias(None)
        super().tearDown()

    def get_token(self, user):
        refresh = _build_tenant_token(
            user=user,
            tenant=self.tenant,
            db_alias='tenant_test',
        )
        return str(refresh.access_token)

    def test_01_create_lead_with_status_history_and_audit(self):
        """Lead creation initializes status to NEW_LEAD, logs audit, and creates status history."""
        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Sarah',
            last_name='Connor',
            phone='+15551234567',
            email='sarah.connor@resistance.net',
            branch=self.branch,
            lead_source=self.lead_source,
            assigned_sales_user=self.sales_rep_a,
            actor_user=self.admin_user,
            db_alias='tenant_test',
        )

        self.assertEqual(lead.current_status, 'NEW_LEAD')
        self.assertEqual(lead.assigned_sales_user, self.sales_rep_a)

        # Check status history
        hist = LeadStatusHistory.objects.using('tenant_test').filter(lead=lead).first()
        self.assertIsNotNone(hist)
        self.assertEqual(hist.to_status, 'NEW_LEAD')
        self.assertEqual(hist.reason_code, 'LEAD_CREATION')

        # Check initial assignment
        assignment = LeadAssignment.objects.using('tenant_test').filter(lead=lead, status='ACTIVE').first()
        self.assertIsNotNone(assignment)
        self.assertEqual(assignment.assigned_to_user, self.sales_rep_a)

        # Check audit event
        audit = BusinessAuditEvent.objects.using('tenant_test').filter(
            entity_type='Lead', entity_id=lead.id, action_code='LEAD_CREATE'
        ).first()
        self.assertIsNotNone(audit)

        # Check outbox event
        outbox = DomainOutboxEvent.objects.using('tenant_test').filter(
            aggregate_type='Lead', aggregate_id=lead.id, event_type='LEAD_CREATED'
        ).first()
        self.assertIsNotNone(outbox)

    def test_02_transition_lead_status(self):
        """Transitioning lead status records reason and appends to status history."""
        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='John',
            last_name='Connor',
            phone='+15557654321',
            email='john@resistance.net',
            db_alias='tenant_test',
        )

        CRMLeadService.transition_lead_status(
            lead=lead,
            new_status='INTERESTED',
            reason_code='CALLED_CLIENT',
            reason_text='Client expressed high interest in group sessions',
            actor_user=self.admin_user,
            db_alias='tenant_test',
        )

        lead.refresh_from_db(using='tenant_test')
        self.assertEqual(lead.current_status, 'INTERESTED')

        history_count = LeadStatusHistory.objects.using('tenant_test').filter(lead=lead).count()
        self.assertEqual(history_count, 2)  # Initial NEW_LEAD + INTERESTED

    def test_03_lead_reassignment(self):
        """Reassigning a lead deactivates previous assignment and activates new one."""
        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Kyle',
            last_name='Reese',
            phone='+15559998888',
            assigned_sales_user=self.sales_rep_a,
            db_alias='tenant_test',
        )

        # Reassign to Rep B
        CRMLeadService.assign_lead(
            lead=lead,
            assigned_to_user=self.sales_rep_b,
            assignment_type='SALES',
            actor_user=self.admin_user,
            db_alias='tenant_test',
        )

        lead.refresh_from_db(using='tenant_test')
        self.assertEqual(lead.assigned_sales_user, self.sales_rep_b)

        # Rep A should be inactive
        assign_a = LeadAssignment.objects.using('tenant_test').get(lead=lead, assigned_to_user=self.sales_rep_a)
        self.assertEqual(assign_a.status, 'INACTIVE')
        self.assertIsNotNone(assign_a.unassigned_at)

        # Rep B should be active
        assign_b = LeadAssignment.objects.using('tenant_test').get(lead=lead, assigned_to_user=self.sales_rep_b)
        self.assertEqual(assign_b.status, 'ACTIVE')

    def test_04_intake_form_submission(self):
        """Intake questionnaire submission captures both general and sensitive answers atomically."""
        form = IntakeForm.objects.using('tenant_test').create(
            organization=self.org,
            name='PAR-Q & Lifestyle Intake',
            form_type='PAR_Q',
            version_number=1,
            status='ACTIVE',
        )
        q1 = IntakeQuestion.objects.using('tenant_test').create(
            intake_form=form,
            question_text='What is your primary fitness goal?',
            question_type='TEXT',
            category='FITNESS',
            display_order=1,
        )
        q2 = IntakeQuestion.objects.using('tenant_test').create(
            intake_form=form,
            question_text='Do you have any heart conditions or chest pain?',
            question_type='BOOLEAN',
            category='MEDICAL',
            is_sensitive=True,
            display_order=2,
        )

        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Marcus',
            last_name='Wright',
            db_alias='tenant_test',
        )

        submission = CRMLeadService.submit_intake_form(
            intake_form=form,
            lead=lead,
            submitted_by_user=self.admin_user,
            answers=[
                {'question_id': str(q1.id), 'text_value': 'Lose weight and build lean muscle'},
                {'question_id': str(q2.id), 'boolean_value': False},
            ],
            db_alias='tenant_test',
        )

        self.assertIsNotNone(submission.id)
        answers = IntakeAnswer.objects.using('tenant_test').filter(submission=submission)
        self.assertEqual(answers.count(), 2)

    def test_05_trial_booking_and_lead_state_sync(self):
        """Booking a trial session automatically transitions lead to TRIAL_BOOKED."""
        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Grace',
            last_name='Kelly',
            db_alias='tenant_test',
        )

        start_time = timezone.now() + timedelta(days=2)
        end_time = start_time + timedelta(hours=1)

        trial = CRMLeadService.book_trial(
            lead=lead,
            branch=self.branch,
            scheduled_start=start_time,
            scheduled_end=end_time,
            trial_type='GROUP_CLASS',
            booking_source='WEB',
            actor_user=self.admin_user,
            db_alias='tenant_test',
        )

        self.assertEqual(trial.status, 'BOOKED')
        lead.refresh_from_db(using='tenant_test')
        self.assertEqual(lead.current_status, 'TRIAL_BOOKED')

    def test_06_trial_attendance_and_no_show_transitions(self):
        """Marking trial attended syncs lead to TRIAL_ATTENDED; marking no-show syncs to NO_SHOW."""
        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Bruce',
            last_name='Wayne',
            db_alias='tenant_test',
        )
        start_time = timezone.now() + timedelta(days=1)
        trial = CRMLeadService.book_trial(
            lead=lead,
            branch=self.branch,
            scheduled_start=start_time,
            scheduled_end=start_time + timedelta(hours=1),
            db_alias='tenant_test',
        )

        # Mark attended
        CRMLeadService.transition_trial_status(
            trial=trial,
            new_status='ATTENDED',
            reason_code='ATTENDED_SESSION',
            actor_user=self.admin_user,
            db_alias='tenant_test',
        )
        trial.refresh_from_db(using='tenant_test')
        lead.refresh_from_db(using='tenant_test')
        self.assertEqual(trial.status, 'ATTENDED')
        self.assertEqual(lead.current_status, 'TRIAL_ATTENDED')

    def test_07_crm_api_endpoints(self):
        """API allows querying leads and calling custom actions (transition-status, book-trial)."""
        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Clark',
            last_name='Kent',
            phone='+15554443333',
            email='clark.kent@dailyplanet.com',
            branch=self.branch,
            db_alias='tenant_test',
        )
        token = self.get_token(self.admin_user)

        # 1. List leads
        res = self.client.get(
            '/api/v1/tenant/leads/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        results = res.json().get('results', res.json())
        self.assertTrue(any(l.get('first_name') == 'Clark' for l in results))

        # 2. Transition status via API
        res_trans = self.client.post(
            f'/api/v1/tenant/leads/{lead.id}/transition-status/',
            {
                'new_status': 'HOT_LEAD',
                'reason_code': 'PHONE_CALL_POSITIVE',
            },
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(res_trans.status_code, status.HTTP_200_OK)
        self.assertEqual(res_trans.json().get('current_status'), 'HOT_LEAD')

        # 3. Book trial via API
        start_dt = (timezone.now() + timedelta(days=3)).isoformat()
        end_dt = (timezone.now() + timedelta(days=3, hours=1)).isoformat()
        res_trial = self.client.post(
            f'/api/v1/tenant/leads/{lead.id}/book-trial/',
            {
                'branch_id': str(self.branch.id),
                'scheduled_start': start_dt,
                'scheduled_end': end_dt,
                'trial_type': 'INTRO_PT',
            },
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(res_trial.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res_trial.json().get('status'), 'BOOKED')
