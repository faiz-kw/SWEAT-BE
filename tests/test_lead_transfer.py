"""
backend/tests/test_lead_transfer.py — Comprehensive Unit & Integration Tests for Lead Transfer Engine

Covers Block 1: RM Handover & Pipeline Transfer:
1. Dynamic Source Representatives discovery (active & deactivated staff with pipeline)
2. Live Scope Preview & Sample Leads drawer verification
3. Transactional Transfer Execution: Single RM Mode
4. Transactional Transfer Execution: Equal Round-Robin Mode
5. Transactional Transfer Execution: Round-Robin with Custom % Weight Sliders
6. Transactional Transfer Execution: Least Busy Auto-Balance Mode
7. Task Reassignment & Due Date Preservation (including cross-owned pending tasks)
8. Historical Closer Credit & Trial Booking Integrity Preservation
9. Offboarding Safeguards: Auto-assignment exclusion & Meta Lead Ad auto-switching
10. 10-Second Undo / Rollback capability & Audit Snapshot restoration
11. RBAC Permission Enforcement (crm.leads.transfer — 403 vs 200)
"""

import uuid
from decimal import Decimal
from datetime import timedelta
from django.utils import timezone
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework import status

from apps.master.models import Tenant, TenantDataSource
from config.routers import set_tenant_db_alias
from apps.tenant_core.models_org import Organization, Branch, Location
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_rbac import Role, RoleAssignment, Permission, RolePermissionSet, RolePermissionSetItem
from apps.tenant_core.models_crm import (
    Lead,
    LeadSource,
    LeadAssignment,
    LeadActivity,
    LeadConversion,
    TrialBooking,
    SalesFollowupTask,
    CRMAgentAssignmentConfig,
    LeadTransferBatch,
)
from apps.tenant_core.models_meta_leads import MetaLeadMapping
from apps.tenant_core.services_lead_transfer import (
    get_transfer_source_reps,
    preview_transfer,
    execute_transfer,
    rollback_transfer,
    get_transfer_history,
)
from apps.tenant_core.views_crm import LeadTransferViewSet

DB = 'tenant_test'


class LeadTransferEngineTestCase(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias(DB)
        self.factory = APIRequestFactory()

        # Master Tenant Setup
        self.tenant = Tenant.objects.using('default').create(
            name='SWEAT Test Gym',
            code=f"SW-{uuid.uuid4().hex[:4].upper()}",
            slug=f"sweat-test-{uuid.uuid4().hex[:6]}",
            status="ACTIVE",
        )
        TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            db_name='test_fitness_tenant',
            database_name='test_fitness_tenant',
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )

        # Organization & Branch
        self.org = Organization.objects.using(DB).create(
            name='SWEAT Fitness',
            code=f"SW-{uuid.uuid4().hex[:4].upper()}",
            status='ACTIVE',
        )
        self.loc = Location.objects.using(DB).create(
            organization=self.org,
            name='Indiranagar',
            code=f"IN-{uuid.uuid4().hex[:3].upper()}",
            city='Bangalore',
        )
        self.branch1 = Branch.objects.using(DB).create(
            organization=self.org,
            location=self.loc,
            name='SWEAT Indiranagar',
            code=f"BR-{uuid.uuid4().hex[:3].upper()}",
            status='ACTIVE',
        )
        self.branch2 = Branch.objects.using(DB).create(
            organization=self.org,
            location=self.loc,
            name='SWEAT Koramangala',
            code=f"BR-{uuid.uuid4().hex[:3].upper()}",
            status='ACTIVE',
        )

        # Lead Source
        self.source = LeadSource.objects.using(DB).create(
            organization=self.org,
            code='WALK_IN',
            name='Walk-in',
            status='ACTIVE',
        )

        # Admin User (Superuser)
        self.admin = TenantUser.objects.using(DB).create(
            organization=self.org,
            email=f"admin_{uuid.uuid4().hex[:6]}@test.com",
            first_name='Admin',
            last_name='Officer',
            user_type='STAFF',
            status='ACTIVE',
            is_login_allowed=True,
            home_branch=self.branch1,
        )
        self.admin.is_superuser = True
        self.admin._auth_type = 'tenant'
        self.admin.save(using=DB)
        self.admin_profile = UserProfile.objects.using(DB).create(user=self.admin)

        # Source RM 1 (Active Counselor with leads)
        self.rm1 = TenantUser.objects.using(DB).create(
            organization=self.org,
            email=f"rm1_{uuid.uuid4().hex[:6]}@test.com",
            first_name='Rahul',
            last_name='Sharma',
            user_type='STAFF',
            status='ACTIVE',
            is_login_allowed=True,
            home_branch=self.branch1,
        )
        self.rm1._auth_type = 'tenant'
        self.rm1.save(using=DB)
        UserProfile.objects.using(DB).create(user=self.rm1)

        # Source RM 2 (Deactivated Counselor with leads)
        self.rm2_deactivated = TenantUser.objects.using(DB).create(
            organization=self.org,
            email=f"rm2_{uuid.uuid4().hex[:6]}@test.com",
            first_name='Priya',
            last_name='Verma',
            user_type='STAFF',
            status='INACTIVE',
            is_login_allowed=False,
            home_branch=self.branch1,
        )
        self.rm2_deactivated._auth_type = 'tenant'
        self.rm2_deactivated.save(using=DB)
        UserProfile.objects.using(DB).create(user=self.rm2_deactivated)

        # Target Rep A
        self.target_a = TenantUser.objects.using(DB).create(
            organization=self.org,
            email=f"target_a_{uuid.uuid4().hex[:6]}@test.com",
            first_name='Amit',
            last_name='Singh',
            user_type='STAFF',
            status='ACTIVE',
            is_login_allowed=True,
            home_branch=self.branch1,
        )
        self.target_a._auth_type = 'tenant'
        self.target_a.save(using=DB)
        UserProfile.objects.using(DB).create(user=self.target_a)

        # Target Rep B (Cross Branch)
        self.target_b = TenantUser.objects.using(DB).create(
            organization=self.org,
            email=f"target_b_{uuid.uuid4().hex[:6]}@test.com",
            first_name='Sneha',
            last_name='Patel',
            user_type='STAFF',
            status='ACTIVE',
            is_login_allowed=True,
            home_branch=self.branch2,
        )
        self.target_b._auth_type = 'tenant'
        self.target_b.save(using=DB)
        UserProfile.objects.using(DB).create(user=self.target_b)

        # Configure Sales Role for eligible target reps
        sales_role, _ = Role.objects.using(DB).get_or_create(
            organization=self.org,
            code='SALES_REP',
            defaults={'name': 'Sales Representative', 'is_active': True, 'status': 'ACTIVE'},
        )
        RoleAssignment.objects.using(DB).create(
            user=self.target_a,
            organization=self.org,
            role=sales_role,
            branch=self.branch1,
            status='ACTIVE',
            is_active=True,
        )
        RoleAssignment.objects.using(DB).create(
            user=self.target_b,
            organization=self.org,
            role=sales_role,
            branch=self.branch2,
            status='ACTIVE',
            is_active=True,
        )

    def _create_lead(self, assigned_user=None, status='NEW_LEAD', branch=None):
        return Lead.objects.using(DB).create(
            organization=self.org,
            branch=branch or self.branch1,
            lead_source=self.source,
            first_name=f"Lead_{uuid.uuid4().hex[:4]}",
            last_name="Prospect",
            phone_normalized=f"+9198{uuid.uuid4().int % 100000000:08d}",
            email_normalized=f"prospect_{uuid.uuid4().hex[:5]}@example.com",
            current_status=status,
            assigned_sales_user=assigned_user,
        )

    def test_01_get_transfer_source_reps(self):
        """Verifies active and deactivated staff with pipeline are discovered with live stats."""
        lead1 = self._create_lead(assigned_user=self.rm1, status='HOT_LEAD')
        lead2 = self._create_lead(assigned_user=self.rm1, status='TRIAL_BOOKED')
        lead3 = self._create_lead(assigned_user=self.rm1, status='CONVERTED')

        TrialBooking.objects.using(DB).create(
            branch=self.branch1,
            lead=lead2,
            scheduled_start=timezone.now() + timedelta(days=1),
            scheduled_end=timezone.now() + timedelta(days=1, hours=1),
            status='CONFIRMED',
            created_by_user=self.rm1,
        )

        SalesFollowupTask.objects.using(DB).create(
            lead=lead1,
            assigned_to_user=self.rm1,
            created_by_user=self.admin,
            task_type='CALL',
            due_at=timezone.now() + timedelta(days=2),
            status='PENDING',
        )

        self._create_lead(assigned_user=self.rm2_deactivated, status='NEW_LEAD')
        self._create_lead(assigned_user=self.rm2_deactivated, status='LOST')

        source_reps = get_transfer_source_reps(self.org, db_alias=DB)
        rep_ids = [r['id'] for r in source_reps]

        self.assertIn(str(self.rm1.id), rep_ids)
        self.assertIn(str(self.rm2_deactivated.id), rep_ids)

        rm1_stat = next(r for r in source_reps if r['id'] == str(self.rm1.id))
        self.assertEqual(rm1_stat['lead_count'], 3)
        self.assertEqual(rm1_stat['active_lead_count'], 2)
        self.assertEqual(rm1_stat['converted_lead_count'], 1)
        self.assertEqual(rm1_stat['trial_count'], 1)
        self.assertEqual(rm1_stat['task_count'], 1)
        self.assertEqual(rm1_stat['hot_count'], 1)
        self.assertFalse(rm1_stat['is_deactivated'])

        rm2_stat = next(r for r in source_reps if r['id'] == str(self.rm2_deactivated.id))
        self.assertEqual(rm2_stat['lead_count'], 2)
        self.assertTrue(rm2_stat['is_deactivated'])

    def test_02_preview_lead_transfer(self):
        """Verifies real-time preview returns accurate counts, temperatures, and sample leads."""
        for _ in range(4):
            self._create_lead(assigned_user=self.rm1, status='HOT_LEAD')
        self._create_lead(assigned_user=self.rm1, status='CONVERTED')
        self._create_lead(assigned_user=self.rm1, status='LOST')

        preview = preview_transfer(
            organization=self.org,
            source_user_id=str(self.rm1.id),
            include_converted=True,
            include_lost=True,
            db_alias=DB,
        )

        self.assertEqual(preview['total_leads'], 6)
        self.assertEqual(preview['active_leads'], 4)
        self.assertEqual(preview['converted_leads'], 1)
        self.assertEqual(preview['lost_leads'], 1)
        self.assertEqual(preview['hot_leads'], 4)
        self.assertGreaterEqual(len(preview['sample_leads']), 1)
        sample_phone = preview['sample_leads'][0]['phone']
        self.assertIn('*', sample_phone)

        filtered_preview = preview_transfer(
            organization=self.org,
            source_user_id=str(self.rm1.id),
            include_converted=False,
            include_lost=False,
            db_alias=DB,
        )
        self.assertEqual(filtered_preview['total_leads'], 4)

    def test_03_execute_transfer_single_mode(self):
        """Executes full pipeline transfer in SINGLE mode to one rep with audit log and notification."""
        leads = [self._create_lead(assigned_user=self.rm1) for _ in range(5)]

        res = execute_transfer(
            organization=self.org,
            actor_user=self.admin,
            source_user_id=str(self.rm1.id),
            target_user_ids=[str(self.target_a.id)],
            transfer_mode='SINGLE',
            reason='Resignation Handover',
            db_alias=DB,
        )

        self.assertTrue(res['success'])
        self.assertEqual(res['transferred_leads_count'], 5)
        batch_id = res['batch_id']

        for lead in leads:
            lead.refresh_from_db(using=DB)
            self.assertEqual(lead.assigned_sales_user, self.target_a)

        assignment = LeadAssignment.objects.using(DB).filter(lead=leads[0], status='ACTIVE').first()
        self.assertIsNotNone(assignment)
        self.assertEqual(assignment.assigned_to_user, self.target_a)
        self.assertEqual(assignment.assignment_strategy, 'HANDOVER_SINGLE')

        activity = LeadActivity.objects.using(DB).filter(lead=leads[0], outcome='REASSIGNED').first()
        self.assertIsNotNone(activity)

        batch = LeadTransferBatch.objects.using(DB).filter(id=batch_id).first()
        self.assertIsNotNone(batch)
        self.assertEqual(batch.leads_count, 5)
        self.assertEqual(batch.status, 'COMPLETED')

    def test_04_execute_transfer_round_robin_mode(self):
        """Divides leads evenly 1-by-1 across 2 target representatives."""
        leads = [self._create_lead(assigned_user=self.rm1) for _ in range(6)]

        res = execute_transfer(
            organization=self.org,
            actor_user=self.admin,
            source_user_id=str(self.rm1.id),
            target_user_ids=[str(self.target_a.id), str(self.target_b.id)],
            transfer_mode='ROUND_ROBIN',
            db_alias=DB,
        )

        self.assertTrue(res['success'])
        self.assertEqual(res['transferred_leads_count'], 6)

        count_a = Lead.objects.using(DB).filter(assigned_sales_user=self.target_a).count()
        count_b = Lead.objects.using(DB).filter(assigned_sales_user=self.target_b).count()

        self.assertEqual(count_a, 3)
        self.assertEqual(count_b, 3)

    def test_05_execute_transfer_round_robin_with_custom_weights(self):
        """Distributes leads according to custom % weight sliders (70% to A, 30% to B)."""
        leads = [self._create_lead(assigned_user=self.rm1) for _ in range(10)]

        weights = {
            str(self.target_a.id): 70.0,
            str(self.target_b.id): 30.0,
        }

        res = execute_transfer(
            organization=self.org,
            actor_user=self.admin,
            source_user_id=str(self.rm1.id),
            target_user_ids=[str(self.target_a.id), str(self.target_b.id)],
            transfer_mode='ROUND_ROBIN',
            custom_weights=weights,
            db_alias=DB,
        )

        self.assertTrue(res['success'])
        count_a = Lead.objects.using(DB).filter(assigned_sales_user=self.target_a).count()
        count_b = Lead.objects.using(DB).filter(assigned_sales_user=self.target_b).count()

        self.assertEqual(count_a, 7)
        self.assertEqual(count_b, 3)

    def test_06_execute_transfer_least_busy_mode(self):
        """Auto-balances leads by assigning more to the representative with fewest active leads."""
        for _ in range(4):
            self._create_lead(assigned_user=self.target_a, status='NEW_LEAD')

        for _ in range(4):
            self._create_lead(assigned_user=self.rm1, status='NEW_LEAD')

        res = execute_transfer(
            organization=self.org,
            actor_user=self.admin,
            source_user_id=str(self.rm1.id),
            target_user_ids=[str(self.target_a.id), str(self.target_b.id)],
            transfer_mode='LEAST_BUSY',
            db_alias=DB,
        )

        self.assertTrue(res['success'])
        count_a = Lead.objects.using(DB).filter(assigned_sales_user=self.target_a).count()
        count_b = Lead.objects.using(DB).filter(assigned_sales_user=self.target_b).count()

        self.assertEqual(count_a, 4)
        self.assertEqual(count_b, 4)

    def test_07_preserve_pending_task_due_dates_and_cross_owned_tasks(self):
        """Verifies pending follow-ups are transferred while keeping original due dates intact."""
        lead = self._create_lead(assigned_user=self.rm1)
        fixed_due = timezone.now() + timedelta(days=4, hours=3)

        task = SalesFollowupTask.objects.using(DB).create(
            lead=lead,
            assigned_to_user=self.rm1,
            created_by_user=self.admin,
            task_type='CALL',
            due_at=fixed_due,
            status='PENDING',
        )

        other_user = self.target_b
        lead_other = self._create_lead(assigned_user=other_user)
        cross_task = SalesFollowupTask.objects.using(DB).create(
            lead=lead_other,
            assigned_to_user=self.rm1,
            created_by_user=self.admin,
            task_type='MEETING',
            due_at=fixed_due,
            status='PENDING',
        )

        res = execute_transfer(
            organization=self.org,
            actor_user=self.admin,
            source_user_id=str(self.rm1.id),
            target_user_ids=[str(self.target_a.id)],
            transfer_mode='SINGLE',
            db_alias=DB,
        )

        self.assertTrue(res['success'])

        task.refresh_from_db(using=DB)
        cross_task.refresh_from_db(using=DB)

        self.assertEqual(task.assigned_to_user, self.target_a)
        self.assertEqual(cross_task.assigned_to_user, self.target_a)
        self.assertEqual(task.due_at, fixed_due)
        self.assertEqual(cross_task.due_at, fixed_due)

    def test_08_preserve_closer_credit_and_trials(self):
        """Verifies LeadConversion closer credit and TrialBooking records are preserved."""
        lead = self._create_lead(assigned_user=self.rm1, status='CONVERTED')

        conversion = LeadConversion.objects.using(DB).create(
            lead=lead,
            user_profile=self.admin_profile,
            converted_by_user=self.rm1,
        )

        trial = TrialBooking.objects.using(DB).create(
            branch=self.branch1,
            lead=lead,
            scheduled_start=timezone.now() + timedelta(days=1),
            scheduled_end=timezone.now() + timedelta(days=1, hours=1),
            status='COMPLETED',
            created_by_user=self.rm1,
        )

        execute_transfer(
            organization=self.org,
            actor_user=self.admin,
            source_user_id=str(self.rm1.id),
            target_user_ids=[str(self.target_a.id)],
            transfer_mode='SINGLE',
            include_converted=True,
            db_alias=DB,
        )

        conversion.refresh_from_db(using=DB)
        trial.refresh_from_db(using=DB)

        self.assertEqual(conversion.converted_by_user, self.rm1)
        self.assertEqual(trial.created_by_user, self.rm1)

    def test_09_offboarding_safeguards_auto_assignment_and_meta(self):
        """Verifies RM is excluded from future auto-assignment and Meta Lead Ads are auto-switched."""
        config = CRMAgentAssignmentConfig.objects.using(DB).create(
            organization=self.org,
            excluded_user_ids=[],
        )

        meta_mapping = MetaLeadMapping.objects.using(DB).create(
            organization=self.org,
            page_id='12345678',
            form_id='87654321',
            name='Test Meta Form',
            is_active=True,
            lead_source=self.source,
            assigned_sales_user=self.rm1,
        )

        self._create_lead(assigned_user=self.rm1)

        execute_transfer(
            organization=self.org,
            actor_user=self.admin,
            source_user_id=str(self.rm1.id),
            target_user_ids=[str(self.target_a.id)],
            transfer_mode='SINGLE',
            stop_auto_assign=True,
            auto_switch_meta=True,
            db_alias=DB,
        )

        config.refresh_from_db(using=DB)
        meta_mapping.refresh_from_db(using=DB)

        self.assertIn(str(self.rm1.id), config.excluded_user_ids)
        self.assertEqual(meta_mapping.assigned_sales_user, self.target_a)

    def test_10_rollback_transfer(self):
        """Verifies 10-second Undo restores previous representative ownership."""
        leads = [self._create_lead(assigned_user=self.rm1) for _ in range(3)]

        res = execute_transfer(
            organization=self.org,
            actor_user=self.admin,
            source_user_id=str(self.rm1.id),
            target_user_ids=[str(self.target_a.id)],
            transfer_mode='SINGLE',
            db_alias=DB,
        )

        batch_id = res['batch_id']

        for lead in leads:
            lead.refresh_from_db(using=DB)
            self.assertEqual(lead.assigned_sales_user, self.target_a)

        roll_res = rollback_transfer(
            organization=self.org,
            batch_id=batch_id,
            actor_user=self.admin,
            db_alias=DB,
        )

        self.assertTrue(roll_res['success'])
        self.assertEqual(roll_res['status'], 'ROLLED_BACK')

        for lead in leads:
            lead.refresh_from_db(using=DB)
            self.assertEqual(lead.assigned_sales_user, self.rm1)

        batch = LeadTransferBatch.objects.using(DB).get(id=batch_id)
        self.assertEqual(batch.status, 'ROLLED_BACK')

    def test_11_rbac_permission_enforcement(self):
        """Verifies user without 'crm.leads.transfer' gets 403 Forbidden, and authorized user gets 200 OK."""
        non_admin = TenantUser.objects.using(DB).create(
            organization=self.org,
            email=f"counselor_{uuid.uuid4().hex[:6]}@test.com",
            first_name='Basic',
            last_name='Staff',
            user_type='STAFF',
            status='ACTIVE',
            is_login_allowed=True,
        )
        non_admin.is_superuser = False
        non_admin.save(using=DB)
        non_admin._auth_type = 'tenant'
        UserProfile.objects.using(DB).create(user=non_admin)

        # 1. Non-admin calls execute endpoint -> 403 Forbidden
        req = self.factory.post('/api/v1/tenant/crm/lead-transfers/execute/', {
            'source_user_id': str(self.rm1.id),
            'target_user_ids': [str(self.target_a.id)],
            'transfer_mode': 'SINGLE',
        }, format='json')
        force_authenticate(req, user=non_admin)
        req._tenant_db_alias = DB
        req.organization = self.org

        view = LeadTransferViewSet.as_view({'post': 'execute'})
        resp = view(req)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        # 2. Superuser / Admin calls execute endpoint -> 200 OK
        req_admin = self.factory.post('/api/v1/tenant/crm/lead-transfers/execute/', {
            'source_user_id': str(self.rm1.id),
            'target_user_ids': [str(self.target_a.id)],
            'transfer_mode': 'SINGLE',
        }, format='json')
        force_authenticate(req_admin, user=self.admin)
        req_admin._tenant_db_alias = DB
        req_admin.organization = self.org

        self._create_lead(assigned_user=self.rm1)
        resp_admin = view(req_admin)
        self.assertEqual(resp_admin.status_code, status.HTTP_200_OK)
        self.assertTrue(resp_admin.data['success'])

    def test_12_transfer_unassigned_leads_pool(self):
        """Unassigned leads can be assigned to reps via source_user_id='unassigned'."""
        # Create 4 unassigned leads
        unassigned_leads = [self._create_lead(assigned_user=None) for _ in range(4)]

        # Preview unassigned
        preview = preview_transfer(
            organization=self.org,
            source_user_id='unassigned',
            db_alias=DB,
        )
        self.assertEqual(preview['total_leads'], 4)

        # Execute assignment to target_a
        res = execute_transfer(
            organization=self.org,
            actor_user=self.admin,
            source_user_id='unassigned',
            target_user_ids=[str(self.target_a.id)],
            transfer_mode='SINGLE',
            db_alias=DB,
        )
        self.assertTrue(res['success'])
        self.assertEqual(res['transferred_leads_count'], 4)

        assigned_count = Lead.objects.using(DB).filter(assigned_sales_user=self.target_a).count()
        self.assertEqual(assigned_count, 4)

        # Rollback restores them back to unassigned (None)
        roll = rollback_transfer(
            organization=self.org,
            batch_id=res['batch_id'],
            actor_user=self.admin,
            db_alias=DB,
        )
        self.assertTrue(roll['success'])
        still_unassigned = Lead.objects.using(DB).filter(id__in=[l.id for l in unassigned_leads], assigned_sales_user__isnull=True).count()
        self.assertEqual(still_unassigned, 4)
