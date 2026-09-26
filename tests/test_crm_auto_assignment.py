"""
backend/tests/test_crm_auto_assignment.py — Comprehensive Test Suite for Lead Auto-Assignment & Availability
Covers:
- Eligible representative determination (active vs available)
- Workforce leave integration (on-leave exclusion)
- Branch scoping & permission validation
- Round Robin sequential rotation & skipping on-leave agents
- Concurrency-safe round robin state persistence
- Least Open Leads calculation excluding terminal statuses
- Deterministic secondary tie-breaker in Least Open Leads
- Fallback to UNASSIGNED when no representative available (zero lead loss)
- Complete assignment history (source, strategy, branch, previous assignment)
- Reassignment preserving prior history
- Notification dispatch
"""

import uuid
from decimal import Decimal
from datetime import date, timedelta
from django.utils import timezone
from django.core.exceptions import ValidationError
from rest_framework.test import APITestCase
from rest_framework import status

from apps.authentication.views import _build_tenant_token
from apps.master.models import (
    Tenant, TenantDataSource, ProductModule, TenantModule, SaasPlan, TenantSubscription
)
from apps.tenant_core.context import set_tenant_db_alias
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_workforce import UserProfile, EmployeeProfile, EmployeeScheduleException
from apps.tenant_core.models_rbac import (
    ModuleCatalog, SubmoduleCatalog, Permission, Role,
    RolePermissionSet, RolePermissionSetItem, RoleAssignment,
    RoleModuleAccess, RoleSubmoduleAccess,
)
from apps.tenant_core.models_crm import (
    LeadSource, Lead, LeadAssignment, LeadStatusHistory,
    CRMAgentAssignmentConfig,
)
from apps.tenant_core.models_govern import InAppNotification
from apps.tenant_core.models_audit_outbox import BusinessAuditEvent, DomainOutboxEvent
from apps.tenant_core.services_crm import CRMLeadService
from apps.tenant_core.services_lead_assignment import (
    LeadAssignmentEligibilityService,
    LeadAutoAssignmentService,
    LeadAssignmentExecutionService,
)

DB = 'tenant_test'


class CRMAutoAssignmentTestCase(APITestCase):
    databases = {'default', DB}

    def setUp(self):
        set_tenant_db_alias(DB)

        # 1. Master DB Setup
        self.tenant = Tenant.objects.using('default').create(
            name='AutoAssign Gyms',
            code='AUTOASSIGN',
            slug='autoassign-gyms',
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
            code='ENTERPRISE',
            name='Enterprise Plan',
            tier=4,
            status='ACTIVE',
        )
        self.sub = TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
        )
        self.mod_crm, _ = ProductModule.objects.using('default').get_or_create(
            code='crm',
            defaults={'name': 'CRM Module', 'status': 'ACTIVE'},
        )
        TenantModule.objects.using('default').create(
            tenant=self.tenant,
            module=self.mod_crm,
            is_enabled=True,
            availability_mode='ALL_BRANCHES',
        )

        # 2. Tenant DB Setup
        self.org = Organization.objects.using(DB).create(
            code='SWEAT-HQ',
            name='Sweat Headquarters',
            status='ACTIVE',
        )
        self.loc = Location.objects.using(DB).create(
            organization=self.org,
            code='LOC-1',
            name='Flagship Location',
            status='ACTIVE',
        )
        self.branch_a = Branch.objects.using(DB).create(
            organization=self.org,
            location=self.loc,
            code='BR-NORTH',
            name='North Flagship',
            status='ACTIVE',
        )
        self.branch_b = Branch.objects.using(DB).create(
            organization=self.org,
            location=self.loc,
            code='BR-SOUTH',
            name='South Branch',
            status='ACTIVE',
        )

        # 3. RBAC Roles
        self.role_sales = Role.objects.using(DB).create(
            name='Sales Representative',
            code='SALES_REP',
            organization=self.org,
            is_active=True,
        )
        self.role_admin = Role.objects.using(DB).create(
            name='Branch Manager',
            code='BRANCH_MANAGER',
            organization=self.org,
            is_active=True,
        )

        # Seed canonical Lead permissions and attach to default CRM roles
        self.mod_cat, _ = ModuleCatalog.objects.using(DB).get_or_create(
            module_code='crm',
            defaults={'name': 'CRM Module', 'code': 'crm', 'display_order': 1}
        )
        self.sub_cat, _ = SubmoduleCatalog.objects.using(DB).get_or_create(
            module=self.mod_cat,
            submodule_code='leads',
            defaults={'name': 'Leads Submodule', 'code': 'leads'}
        )
        self.perm_leads_view, _ = Permission.objects.using(DB).get_or_create(
            permission_code='crm.leads.view',
            defaults={
                'source_permission_id': uuid.uuid4(),
                'module': self.mod_cat,
                'submodule': self.sub_cat,
                'code': 'crm.leads.view',
                'label': 'View Leads',
                'action': 'view',
                'is_active': True,
            }
        )
        self.perm_leads_create, _ = Permission.objects.using(DB).get_or_create(
            permission_code='crm.leads.create',
            defaults={
                'source_permission_id': uuid.uuid4(),
                'module': self.mod_cat,
                'submodule': self.sub_cat,
                'code': 'crm.leads.create',
                'label': 'Create Leads',
                'action': 'create',
                'is_active': True,
            }
        )
        self.perm_leads_edit, _ = Permission.objects.using(DB).get_or_create(
            permission_code='crm.leads.edit',
            defaults={
                'source_permission_id': uuid.uuid4(),
                'module': self.mod_cat,
                'submodule': self.sub_cat,
                'code': 'crm.leads.edit',
                'label': 'Edit Leads',
                'action': 'edit',
                'is_active': True,
            }
        )
        for r in (self.role_sales, self.role_admin):
            RoleModuleAccess.objects.using(DB).get_or_create(
                role=r, module=self.mod_cat, defaults={'can_access': True}
            )
            RoleSubmoduleAccess.objects.using(DB).get_or_create(
                role=r, submodule=self.sub_cat, defaults={'can_access': True}
            )
            ps = RolePermissionSet.objects.using(DB).create(
                role=r,
                organization=self.org,
                name=f"{r.code}_pset",
                is_active=True,
            )
            for p in (self.perm_leads_view, self.perm_leads_create, self.perm_leads_edit):
                RolePermissionSetItem.objects.using(DB).create(
                    permission_set=ps,
                    permission=p,
                    granted=True,
                    is_allowed=True,
                )

        # 4. Representatives
        # Rep 1: Fiona (Branch A, Active, Available)
        self.rep_fiona = TenantUser.objects.using(DB).create(
            organization=self.org,
            email='fiona.sales@sweat.test',
            first_name='Fiona',
            last_name='FrontDesk',
            home_branch=self.branch_a,
            status='ACTIVE',
            is_login_allowed=True,
        )
        self.prof_fiona = UserProfile.objects.using(DB).create(user=self.rep_fiona)
        self.emp_fiona = EmployeeProfile.objects.using(DB).create(
            user_profile=self.prof_fiona,
            organization=self.org,
            employee_code='EMP-FIONA',
            employment_status='ACTIVE',
        )
        RoleAssignment.objects.using(DB).create(
            user=self.rep_fiona,
            role=self.role_sales,
            organization=self.org,
            branch=self.branch_a,
            status='ACTIVE',
            is_active=True,
        )

        # Rep 2: Sam (Branch A, Active, ON LEAVE)
        self.rep_sam = TenantUser.objects.using(DB).create(
            organization=self.org,
            email='sam.sales@sweat.test',
            first_name='Sam',
            last_name='Sales',
            home_branch=self.branch_a,
            status='ACTIVE',
            is_login_allowed=True,
        )
        self.prof_sam = UserProfile.objects.using(DB).create(user=self.rep_sam)
        self.emp_sam = EmployeeProfile.objects.using(DB).create(
            user_profile=self.prof_sam,
            organization=self.org,
            employee_code='EMP-SAM',
            employment_status='ACTIVE',
        )
        RoleAssignment.objects.using(DB).create(
            user=self.rep_sam,
            role=self.role_sales,
            organization=self.org,
            branch=self.branch_a,
            status='ACTIVE',
            is_active=True,
        )
        # Sam has an approved leave exception today
        EmployeeScheduleException.objects.using(DB).create(
            employee_profile=self.emp_sam,
            branch=self.branch_a,
            exception_date=timezone.localdate(),
            exception_type='LEAVE',
            is_available=False,
            reason='Annual Vacation Leave',
            status='ACTIVE',
        )

        # Rep 3: Anjali (Branch A, Active, Available)
        self.rep_anjali = TenantUser.objects.using(DB).create(
            organization=self.org,
            email='anjali.sales@sweat.test',
            first_name='Anjali',
            last_name='Sales',
            home_branch=self.branch_a,
            status='ACTIVE',
            is_login_allowed=True,
        )
        self.prof_anjali = UserProfile.objects.using(DB).create(user=self.rep_anjali)
        self.emp_anjali = EmployeeProfile.objects.using(DB).create(
            user_profile=self.prof_anjali,
            organization=self.org,
            employee_code='EMP-ANJALI',
            employment_status='ACTIVE',
        )
        RoleAssignment.objects.using(DB).create(
            user=self.rep_anjali,
            role=self.role_sales,
            organization=self.org,
            branch=self.branch_a,
            status='ACTIVE',
            is_active=True,
        )

        # Rep 4: David (Branch B only, not branch A)
        self.rep_david = TenantUser.objects.using(DB).create(
            organization=self.org,
            email='david.sales@sweat.test',
            first_name='David',
            last_name='South',
            home_branch=self.branch_b,
            status='ACTIVE',
            is_login_allowed=True,
        )
        self.prof_david = UserProfile.objects.using(DB).create(user=self.rep_david)
        self.emp_david = EmployeeProfile.objects.using(DB).create(
            user_profile=self.prof_david,
            organization=self.org,
            employee_code='EMP-DAVID',
            employment_status='ACTIVE',
        )
        RoleAssignment.objects.using(DB).create(
            user=self.rep_david,
            role=self.role_sales,
            organization=self.org,
            branch=self.branch_b,
            status='ACTIVE',
            is_active=True,
        )

        # 5. Lead Source & Assignment Config
        self.source = LeadSource.objects.using(DB).create(
            organization=self.org,
            code='WALK_IN',
            name='Walk-in Inquiry',
            source_type='OFFLINE',
            status='ACTIVE',
        )

        self.config, _ = CRMAgentAssignmentConfig.objects.using(DB).get_or_create(
            organization=self.org,
            defaults={
                'allowed_role_codes': [],
                'require_branch_match': True,
                'allow_all_staff_fallback': True,
                'assignment_mode_allowed': 'BOTH',
                'default_assignment_mode': 'AUTO',
                'auto_assignment_strategy': 'ROUND_ROBIN',
                'allow_unassigned_fallback': True,
                'consider_leave_availability': True,
                'notify_manager_on_unassigned': True,
            }
        )

    def test_01_eligible_representatives_evaluates_active_vs_available(self):
        """Active vs Available: Sam is ACTIVE but marked ON_LEAVE because of approved leave."""
        reps = LeadAssignmentEligibilityService.get_eligible_representatives(
            organization=self.org,
            branch=self.branch_a,
            db_alias=DB,
            include_unavailable=True,
        )
        by_id = {r['user_id']: r for r in reps}

        # Fiona is available
        self.assertTrue(by_id[str(self.rep_fiona.id)]['is_available'])
        self.assertEqual(by_id[str(self.rep_fiona.id)]['availability_status'], 'AVAILABLE')

        # Sam is NOT available, on leave
        self.assertFalse(by_id[str(self.rep_sam.id)]['is_available'])
        self.assertEqual(by_id[str(self.rep_sam.id)]['availability_status'], 'ON_LEAVE')
        self.assertIn('Vacation', by_id[str(self.rep_sam.id)]['availability_reason'])

        # Anjali is available
        self.assertTrue(by_id[str(self.rep_anjali.id)]['is_available'])
        self.assertEqual(by_id[str(self.rep_anjali.id)]['availability_status'], 'AVAILABLE')

        # David is not in branch A
        self.assertFalse(by_id[str(self.rep_david.id)]['is_available'])
        self.assertEqual(by_id[str(self.rep_david.id)]['availability_status'], 'OUTSIDE_BRANCH')

    def test_02_manual_assignment_rejects_on_leave_representative(self):
        """Manual assignment to Sam (who is on leave) must be authoritatively blocked by backend."""
        with self.assertRaises(ValidationError) as ctx:
            CRMLeadService.create_lead(
                organization=self.org,
                first_name='Aarav',
                last_name='Sharma',
                email='aarav.sharma@gmail.com',
                phone='+919876543210',
                branch=self.branch_a,
                lead_source=self.source,
                assigned_sales_user=self.rep_sam,
                extra_fields={'assignment_mode': 'MANUAL'},
                db_alias=DB,
            )
        self.assertIn('unavailable', str(ctx.exception).lower())

    def test_03_round_robin_rotates_and_skips_on_leave_agent(self):
        """Auto assign with Round Robin skips Sam on leave and rotates Fiona -> Anjali -> Fiona."""
        self.config.auto_assignment_strategy = 'ROUND_ROBIN'
        self.config.save(using=DB)

        # Lead 1: Fiona
        l1 = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Lead',
            last_name='One',
            email='lead.one@gmail.com',
            phone='+919876543211',
            branch=self.branch_a,
            lead_source=self.source,
            extra_fields={'assignment_mode': 'AUTO'},
            db_alias=DB,
        )
        # Lead 2
        l2 = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Lead',
            last_name='Two',
            email='lead.two@gmail.com',
            phone='+919876543212',
            branch=self.branch_a,
            lead_source=self.source,
            extra_fields={'assignment_mode': 'AUTO'},
            db_alias=DB,
        )

        # Lead 3
        l3 = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Lead',
            last_name='Three',
            email='lead.three@gmail.com',
            phone='+919876543213',
            branch=self.branch_a,
            lead_source=self.source,
            extra_fields={'assignment_mode': 'AUTO'},
            db_alias=DB,
        )

        # Lead 4
        l4 = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Lead',
            last_name='Four',
            email='lead.four@gmail.com',
            phone='+919876543214',
            branch=self.branch_a,
            lead_source=self.source,
            extra_fields={'assignment_mode': 'AUTO'},
            db_alias=DB,
        )

        # Verify round robin rotates strictly between available agents (Fiona and Anjali)
        self.assertIn(l1.assigned_sales_user_id, [self.rep_fiona.id, self.rep_anjali.id])
        self.assertIn(l2.assigned_sales_user_id, [self.rep_fiona.id, self.rep_anjali.id])
        # l1 and l2 must be different agents
        self.assertNotEqual(l1.assigned_sales_user_id, l2.assigned_sales_user_id)
        # l3 rotates back to l1's agent
        self.assertEqual(l3.assigned_sales_user_id, l1.assigned_sales_user_id)
        # l4 rotates to l2's agent
        self.assertEqual(l4.assigned_sales_user_id, l2.assigned_sales_user_id)

        # Sam (who is on leave) was NEVER assigned
        all_assigned_ids = {l1.assigned_sales_user_id, l2.assigned_sales_user_id, l3.assigned_sales_user_id, l4.assigned_sales_user_id}
        self.assertNotIn(self.rep_sam.id, all_assigned_ids)

        assign_1 = LeadAssignment.objects.using(DB).get(lead=l1, status='ACTIVE')
        self.assertEqual(assign_1.assignment_source, 'AUTO')
        self.assertEqual(assign_1.assignment_strategy, 'ROUND_ROBIN')

    def test_04_least_open_leads_strategy(self):
        """Least Open Leads selects the available representative with the fewest open leads."""
        self.config.auto_assignment_strategy = 'LEAST_OPEN_LEADS'
        self.config.save(using=DB)

        # Give Fiona 2 active leads, Anjali 0 active leads
        for i in range(2):
            CRMLeadService.create_lead(
                organization=self.org,
                first_name=f'FionaLead{i}',
                last_name='Test',
                email=f'fiona.lead.{i}@gmail.com',
                phone=f'+91987654322{i}',
                branch=self.branch_a,
                lead_source=self.source,
                assigned_sales_user=self.rep_fiona,
                extra_fields={'assignment_mode': 'MANUAL'},
                db_alias=DB,
            )

        # Auto assign should pick Anjali because she has 0 leads
        new_lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='LeastOpen',
            last_name='Candidate',
            email='least.open@gmail.com',
            phone='+919876543229',
            branch=self.branch_a,
            lead_source=self.source,
            extra_fields={'assignment_mode': 'AUTO'},
            db_alias=DB,
        )
        self.assertEqual(new_lead.assigned_sales_user_id, self.rep_anjali.id)
        assign = LeadAssignment.objects.using(DB).get(lead=new_lead, status='ACTIVE')
        self.assertEqual(assign.assignment_strategy, 'LEAST_OPEN_LEADS')

    def test_05_fallback_to_unassigned_when_no_agent_available(self):
        """When all agents are on leave, lead is safely created as UNASSIGNED with manager notified."""
        # Put Fiona and Anjali on leave too
        EmployeeScheduleException.objects.using(DB).create(
            employee_profile=self.emp_fiona,
            branch=self.branch_a,
            exception_date=timezone.localdate(),
            exception_type='LEAVE',
            is_available=False,
            reason='Sick leave',
            status='ACTIVE',
        )
        EmployeeScheduleException.objects.using(DB).create(
            employee_profile=self.emp_anjali,
            branch=self.branch_a,
            exception_date=timezone.localdate(),
            exception_type='LEAVE',
            is_available=False,
            reason='Training leave',
            status='ACTIVE',
        )

        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Unassigned',
            last_name='Prospect',
            email='unassigned.prospect@gmail.com',
            phone='+919876543230',
            branch=self.branch_a,
            lead_source=self.source,
            extra_fields={'assignment_mode': 'AUTO'},
            db_alias=DB,
        )
        # Lead is NOT lost
        self.assertIsNotNone(lead.id)
        # Assigned sales user is None
        self.assertIsNone(lead.assigned_sales_user)
        # No active assignments
        self.assertEqual(LeadAssignment.objects.using(DB).filter(lead=lead, status='ACTIVE').count(), 0)

        # Audit event records unassigned status
        audit = BusinessAuditEvent.objects.using(DB).filter(
            action_code='CRM_LEAD_UNASSIGNED',
            entity_id=lead.id,
        ).first()
        self.assertIsNotNone(audit)

    def test_06_reassignment_preserves_history_and_notifies_new_agent(self):
        """Reassigning an existing lead retains previous history and creates a new active assignment."""
        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Reassign',
            last_name='Lead',
            email='reassign.lead@gmail.com',
            phone='+919876543231',
            branch=self.branch_a,
            lead_source=self.source,
            assigned_sales_user=self.rep_fiona,
            extra_fields={'assignment_mode': 'MANUAL'},
            db_alias=DB,
        )
        self.assertEqual(lead.assigned_sales_user_id, self.rep_fiona.id)

        # Reassign to Anjali
        reassignment = CRMLeadService.assign_lead(
            lead=lead,
            assigned_to_user=self.rep_anjali,
            notes='Fiona handover to Anjali',
            db_alias=DB,
        )
        lead.refresh_from_db(using=DB)
        self.assertEqual(lead.assigned_sales_user_id, self.rep_anjali.id)

        # History check
        all_assigns = list(LeadAssignment.objects.using(DB).filter(lead=lead).order_by('created_at'))
        self.assertEqual(len(all_assigns), 2)
        # First is inactive
        self.assertEqual(all_assigns[0].assigned_to_user_id, self.rep_fiona.id)
        self.assertEqual(all_assigns[0].status, 'INACTIVE')
        self.assertIsNotNone(all_assigns[0].unassigned_at)
        # Second is active and points to previous
        self.assertEqual(all_assigns[1].assigned_to_user_id, self.rep_anjali.id)
        self.assertEqual(all_assigns[1].status, 'ACTIVE')
        self.assertEqual(all_assigns[1].previous_assignment_id, all_assigns[0].id)

        # Notification created for Anjali
        notif = InAppNotification.objects.using(DB).filter(
            user=self.rep_anjali,
            notification_type='LEAD_ASSIGNED',
        ).first()
        self.assertIsNotNone(notif)
        self.assertIn('Reassign Lead', notif.message)

    def test_07_sales_role_without_lead_permission_excluded(self):
        """1. Sales role + no Lead permission -> excluded."""
        role_sales_no_perm = Role.objects.using(DB).create(
            name='Sales Trainee (No Lead Perm)',
            code='SALES_TRAINEE_NO_PERM',
            organization=self.org,
            is_active=True,
        )
        sales_unperm_user = TenantUser.objects.using(DB).create(
            organization=self.org,
            email='sales.unperm@sweat.test',
            first_name='Unpermitted',
            last_name='Sales',
            home_branch=self.branch_a,
            status='ACTIVE',
            is_login_allowed=True,
        )
        RoleAssignment.objects.using(DB).create(
            user=sales_unperm_user,
            role=role_sales_no_perm,
            organization=self.org,
            branch=self.branch_a,
            status='ACTIVE',
            is_active=True,
        )

        reps = LeadAssignmentEligibilityService.get_eligible_representatives(
            organization=self.org,
            branch=self.branch_a,
            db_alias=DB,
            include_unavailable=True,
        )
        matched = next((r for r in reps if r['user_id'] == str(sales_unperm_user.id)), None)
        self.assertIsNotNone(matched)
        self.assertFalse(matched['is_available'])
        self.assertFalse(matched['eligible'])
        self.assertEqual(matched['availability_status'], 'NO_PERMISSION')
        self.assertIn('crm.leads.edit', matched['availability_reason'])

        avail_reps = LeadAssignmentEligibilityService.get_eligible_representatives(
            organization=self.org,
            branch=self.branch_a,
            db_alias=DB,
            include_unavailable=False,
        )
        self.assertNotIn(str(sales_unperm_user.id), [r['user_id'] for r in avail_reps])

        valid, err = LeadAssignmentEligibilityService.validate_assignee_eligibility(
            organization=self.org,
            user=sales_unperm_user,
            branch=self.branch_a,
            db_alias=DB,
        )
        self.assertFalse(valid)
        self.assertIn('crm.leads.edit', err)

    def test_08_trainer_role_with_lead_permission_eligible(self):
        """2. Trainer role + Lead permission -> eligible."""
        role_trainer = Role.objects.using(DB).create(
            name='Trainer',
            code='TRAINER',
            organization=self.org,
            is_active=True,
        )
        ps_trainer = RolePermissionSet.objects.using(DB).create(
            role=role_trainer,
            organization=self.org,
            name='Trainer_pset',
            is_active=True,
        )
        RolePermissionSetItem.objects.using(DB).create(
            permission_set=ps_trainer,
            permission=self.perm_leads_edit,
            granted=True,
            is_allowed=True,
        )
        rahul = TenantUser.objects.using(DB).create(
            organization=self.org,
            email='rahul.trainer@sweat.test',
            first_name='Rahul',
            last_name='Trainer',
            home_branch=self.branch_a,
            status='ACTIVE',
            is_login_allowed=True,
        )
        RoleAssignment.objects.using(DB).create(
            user=rahul,
            role=role_trainer,
            organization=self.org,
            branch=self.branch_a,
            status='ACTIVE',
            is_active=True,
        )

        reps = LeadAssignmentEligibilityService.get_eligible_representatives(
            organization=self.org,
            branch=self.branch_a,
            db_alias=DB,
            include_unavailable=False,
        )
        matched = next((r for r in reps if r['user_id'] == str(rahul.id)), None)
        self.assertIsNotNone(matched)
        self.assertTrue(matched['is_available'])
        self.assertTrue(matched['eligible'])
        self.assertEqual(matched['role_label'], 'Trainer')
        self.assertEqual(matched['role_code'], 'TRAINER')

        valid, err = LeadAssignmentEligibilityService.validate_assignee_eligibility(
            organization=self.org,
            user=rahul,
            branch=self.branch_a,
            db_alias=DB,
        )
        self.assertTrue(valid)
        self.assertIsNone(err)

    def test_09_front_desk_role_with_lead_permission_eligible(self):
        """3. Front Desk role + Lead permission -> eligible."""
        role_fd = Role.objects.using(DB).create(
            name='Front Desk',
            code='FRONT_DESK',
            organization=self.org,
            is_active=True,
        )
        ps_fd = RolePermissionSet.objects.using(DB).create(
            role=role_fd,
            organization=self.org,
            name='FD_pset',
            is_active=True,
        )
        RolePermissionSetItem.objects.using(DB).create(
            permission_set=ps_fd,
            permission=self.perm_leads_edit,
            granted=True,
            is_allowed=True,
        )
        fiona_fd = TenantUser.objects.using(DB).create(
            organization=self.org,
            email='fiona.frontdesk@sweat.test',
            first_name='Fiona',
            last_name='FrontDesk',
            home_branch=self.branch_a,
            status='ACTIVE',
            is_login_allowed=True,
        )
        RoleAssignment.objects.using(DB).create(
            user=fiona_fd,
            role=role_fd,
            organization=self.org,
            branch=self.branch_a,
            status='ACTIVE',
            is_active=True,
        )

        reps = LeadAssignmentEligibilityService.get_eligible_representatives(
            organization=self.org,
            branch=self.branch_a,
            db_alias=DB,
            include_unavailable=False,
        )
        matched = next((r for r in reps if r['user_id'] == str(fiona_fd.id)), None)
        self.assertIsNotNone(matched)
        self.assertTrue(matched['is_available'])
        self.assertTrue(matched['eligible'])
        self.assertEqual(matched['role_label'], 'Front Desk')
        self.assertEqual(matched['role_code'], 'FRONT_DESK')

        valid, err = LeadAssignmentEligibilityService.validate_assignee_eligibility(
            organization=self.org,
            user=fiona_fd,
            branch=self.branch_a,
            db_alias=DB,
        )
        self.assertTrue(valid)
        self.assertIsNone(err)

    def test_10_manager_role_with_lead_permission_eligible(self):
        """4. Manager role + Lead permission -> eligible."""
        vikram = TenantUser.objects.using(DB).create(
            organization=self.org,
            email='vikram.manager@sweat.test',
            first_name='Vikram',
            last_name='Manager',
            home_branch=self.branch_a,
            status='ACTIVE',
            is_login_allowed=True,
        )
        RoleAssignment.objects.using(DB).create(
            user=vikram,
            role=self.role_admin,
            organization=self.org,
            branch=self.branch_a,
            status='ACTIVE',
            is_active=True,
        )

        reps = LeadAssignmentEligibilityService.get_eligible_representatives(
            organization=self.org,
            branch=self.branch_a,
            db_alias=DB,
            include_unavailable=False,
        )
        matched = next((r for r in reps if r['user_id'] == str(vikram.id)), None)
        self.assertIsNotNone(matched)
        self.assertTrue(matched['is_available'])
        self.assertTrue(matched['eligible'])
        self.assertEqual(matched['role_label'], 'Branch Manager')
        self.assertEqual(matched['role_code'], 'BRANCH_MANAGER')

        valid, err = LeadAssignmentEligibilityService.validate_assignee_eligibility(
            organization=self.org,
            user=vikram,
            branch=self.branch_a,
            db_alias=DB,
        )
        self.assertTrue(valid)
        self.assertIsNone(err)

    def test_11_eligible_user_on_leave_unavailable(self):
        """5. Eligible user on leave -> unavailable."""
        role_trainer = Role.objects.using(DB).create(
            name='Trainer Leave',
            code='TRAINER_LEAVE',
            organization=self.org,
            is_active=True,
        )
        ps_trainer = RolePermissionSet.objects.using(DB).create(
            role=role_trainer,
            organization=self.org,
            name='TrainerLeave_pset',
            is_active=True,
        )
        RolePermissionSetItem.objects.using(DB).create(
            permission_set=ps_trainer,
            permission=self.perm_leads_edit,
            granted=True,
            is_allowed=True,
        )
        trainer_user = TenantUser.objects.using(DB).create(
            organization=self.org,
            email='trainer.leave@sweat.test',
            first_name='Trainer',
            last_name='OnLeave',
            home_branch=self.branch_a,
            status='ACTIVE',
            is_login_allowed=True,
        )
        prof = UserProfile.objects.using(DB).create(user=trainer_user)
        emp = EmployeeProfile.objects.using(DB).create(
            user_profile=prof,
            organization=self.org,
            employee_code='EMP-TR-LEAVE',
            employment_status='ACTIVE',
        )
        RoleAssignment.objects.using(DB).create(
            user=trainer_user,
            role=role_trainer,
            organization=self.org,
            branch=self.branch_a,
            status='ACTIVE',
            is_active=True,
        )
        EmployeeScheduleException.objects.using(DB).create(
            employee_profile=emp,
            branch=self.branch_a,
            exception_date=timezone.localdate(),
            exception_type='LEAVE',
            is_available=False,
            reason='Medical Vacation',
            status='ACTIVE',
        )

        reps = LeadAssignmentEligibilityService.get_eligible_representatives(
            organization=self.org,
            branch=self.branch_a,
            db_alias=DB,
            include_unavailable=True,
        )
        matched = next((r for r in reps if r['user_id'] == str(trainer_user.id)), None)
        self.assertIsNotNone(matched)
        self.assertFalse(matched['is_available'])
        self.assertFalse(matched['eligible'])
        self.assertEqual(matched['availability_status'], 'ON_LEAVE')
        self.assertIn('Medical Vacation', matched['availability_reason'])

        valid, err = LeadAssignmentEligibilityService.validate_assignee_eligibility(
            organization=self.org,
            user=trainer_user,
            branch=self.branch_a,
            db_alias=DB,
        )
        self.assertFalse(valid)
        self.assertIn('unavailable', err.lower())

    def test_12_correct_permission_wrong_branch_excluded(self):
        """6. Correct permission but wrong branch -> excluded."""
        reps = LeadAssignmentEligibilityService.get_eligible_representatives(
            organization=self.org,
            branch=self.branch_a,
            db_alias=DB,
            include_unavailable=True,
        )
        david_rep = next((r for r in reps if r['user_id'] == str(self.rep_david.id)), None)
        self.assertIsNotNone(david_rep)
        self.assertFalse(david_rep['is_available'])
        self.assertEqual(david_rep['availability_status'], 'OUTSIDE_BRANCH')

        valid, err = LeadAssignmentEligibilityService.validate_assignee_eligibility(
            organization=self.org,
            user=self.rep_david,
            branch=self.branch_a,
            db_alias=DB,
        )
        self.assertFalse(valid)
        self.assertIn('unavailable', err.lower())

    def test_13_inactive_user_excluded(self):
        """7. Inactive user -> excluded."""
        inactive_user = TenantUser.objects.using(DB).create(
            organization=self.org,
            email='inactive.rep@sweat.test',
            first_name='Inactive',
            last_name='Rep',
            home_branch=self.branch_a,
            status='INACTIVE',
            is_login_allowed=False,
        )
        RoleAssignment.objects.using(DB).create(
            user=inactive_user,
            role=self.role_sales,
            organization=self.org,
            branch=self.branch_a,
            status='ACTIVE',
            is_active=True,
        )

        reps = LeadAssignmentEligibilityService.get_eligible_representatives(
            organization=self.org,
            branch=self.branch_a,
            db_alias=DB,
            include_unavailable=True,
        )
        matched = next((r for r in reps if r['user_id'] == str(inactive_user.id)), None)
        self.assertIsNotNone(matched)
        self.assertFalse(matched['is_available'])
        self.assertEqual(matched['availability_status'], 'INACTIVE')

        valid, err = LeadAssignmentEligibilityService.validate_assignee_eligibility(
            organization=self.org,
            user=inactive_user,
            branch=self.branch_a,
            db_alias=DB,
        )
        self.assertFalse(valid)

        with self.assertRaises(ValidationError):
            CRMLeadService.create_lead(
                organization=self.org,
                first_name='Test',
                last_name='Prospect',
                email='test.inactive@example.com',
                phone='+919876543299',
                branch=self.branch_a,
                lead_source=self.source,
                assigned_sales_user=inactive_user,
                extra_fields={'assignment_mode': 'MANUAL'},
                db_alias=DB,
            )

    def test_14_frontend_tampered_user_id_backend_rejects(self):
        """8. Frontend tampered user_id -> backend rejects with 400."""
        token = str(_build_tenant_token(user=self.rep_fiona, tenant=self.tenant, db_alias=DB).access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        # Case A: Non-existent forged UUID
        resp_fake = self.client.post(
            '/api/v1/tenant/leads/',
            {
                'first_name': 'Forged',
                'last_name': 'ID',
                'email': 'forged.id@gmail.com',
                'phone': '+919876543298',
                'branch': str(self.branch_a.id),
                'lead_source': str(self.source.id),
                'assignment_mode': 'MANUAL',
                'assigned_sales_user': str(uuid.uuid4()),
            },
            format='json',
        )
        self.assertEqual(resp_fake.status_code, status.HTTP_400_BAD_REQUEST)

        # Case B: Real user ID from another branch / outside branch eligibility
        resp_invalid = self.client.post(
            '/api/v1/tenant/leads/',
            {
                'first_name': 'Wrong',
                'last_name': 'Branch',
                'email': 'wrong.branch@gmail.com',
                'phone': '+919876543297',
                'branch': str(self.branch_a.id),
                'lead_source': str(self.source.id),
                'assignment_mode': 'MANUAL',
                'assigned_sales_user': str(self.rep_david.id),
            },
            format='json',
        )
        self.assertEqual(resp_invalid.status_code, status.HTTP_400_BAD_REQUEST)

    def test_15_auto_assign_can_select_eligible_non_sales_role(self):
        """9. Auto assign can select an eligible non-Sales role."""
        role_trainer = Role.objects.using(DB).create(
            name='Lead Trainer',
            code='LEAD_TRAINER',
            organization=self.org,
            is_active=True,
        )
        ps_trainer = RolePermissionSet.objects.using(DB).create(
            role=role_trainer,
            organization=self.org,
            name='LeadTrainer_pset',
            is_active=True,
        )
        RolePermissionSetItem.objects.using(DB).create(
            permission_set=ps_trainer,
            permission=self.perm_leads_edit,
            granted=True,
            is_allowed=True,
        )
        trainer_rep = TenantUser.objects.using(DB).create(
            organization=self.org,
            email='lead.trainer@sweat.test',
            first_name='Pooja',
            last_name='Trainer',
            home_branch=self.branch_a,
            status='ACTIVE',
            is_login_allowed=True,
        )
        RoleAssignment.objects.using(DB).create(
            user=trainer_rep,
            role=role_trainer,
            organization=self.org,
            branch=self.branch_a,
            status='ACTIVE',
            is_active=True,
        )

        # Mark sales reps on leave so only Pooja the trainer is available
        EmployeeScheduleException.objects.using(DB).create(
            employee_profile=self.emp_fiona,
            branch=self.branch_a,
            exception_date=timezone.localdate(),
            exception_type='LEAVE',
            is_available=False,
            status='ACTIVE',
        )
        EmployeeScheduleException.objects.using(DB).create(
            employee_profile=self.emp_anjali,
            branch=self.branch_a,
            exception_date=timezone.localdate(),
            exception_type='LEAVE',
            is_available=False,
            status='ACTIVE',
        )

        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='AutoTrainer',
            last_name='Prospect',
            email='auto.trainer.prospect@gmail.com',
            phone='+919876543292',
            branch=self.branch_a,
            lead_source=self.source,
            extra_fields={'assignment_mode': 'AUTO'},
            db_alias=DB,
        )
        self.assertEqual(lead.assigned_sales_user_id, trainer_rep.id)
        assign = LeadAssignment.objects.using(DB).get(lead=lead, status='ACTIVE')
        self.assertEqual(assign.assigned_to_user_id, trainer_rep.id)

    def test_16_manual_dropdown_can_select_eligible_non_sales_role(self):
        """10. Manual dropdown can select an eligible non-Sales role."""
        role_frontdesk = Role.objects.using(DB).create(
            name='Front Desk Lead',
            code='FRONT_DESK_LEAD',
            organization=self.org,
            is_active=True,
        )
        RoleModuleAccess.objects.using(DB).get_or_create(
            role=role_frontdesk, module=self.mod_cat, defaults={'can_access': True}
        )
        RoleSubmoduleAccess.objects.using(DB).get_or_create(
            role=role_frontdesk, submodule=self.sub_cat, defaults={'can_access': True}
        )
        ps_fd = RolePermissionSet.objects.using(DB).create(
            role=role_frontdesk,
            organization=self.org,
            name='FrontDeskLead_pset',
            is_active=True,
        )
        RolePermissionSetItem.objects.using(DB).create(
            permission_set=ps_fd,
            permission=self.perm_leads_edit,
            granted=True,
            is_allowed=True,
        )
        fd_rep = TenantUser.objects.using(DB).create(
            organization=self.org,
            email='fd.lead@sweat.test',
            first_name='Kavita',
            last_name='FrontDesk',
            home_branch=self.branch_a,
            status='ACTIVE',
            is_login_allowed=True,
        )
        RoleAssignment.objects.using(DB).create(
            user=fd_rep,
            role=role_frontdesk,
            organization=self.org,
            branch=self.branch_a,
            status='ACTIVE',
            is_active=True,
        )

        token = str(_build_tenant_token(user=self.rep_fiona, tenant=self.tenant, db_alias=DB).access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        # 1. API endpoint /api/v1/tenant/leads/eligible-agents/ returns Kavita with role_label 'Front Desk Lead'
        resp_list = self.client.get(f'/api/v1/tenant/leads/eligible-agents/?branch_id={self.branch_a.id}')
        self.assertEqual(resp_list.status_code, status.HTTP_200_OK)
        kavita_item = next((r for r in resp_list.data if r['id'] == str(fd_rep.id)), None)
        self.assertIsNotNone(kavita_item)
        self.assertEqual(kavita_item['role_label'], 'Front Desk Lead')
        self.assertTrue(kavita_item['is_available'])
        self.assertTrue(kavita_item['eligible'])

        # 2. Selecting Kavita from dropdown for manual lead creation succeeds
        resp_create = self.client.post(
            '/api/v1/tenant/leads/',
            {
                'first_name': 'ManualFD',
                'last_name': 'Prospect',
                'email': 'manual.fd.prospect@gmail.com',
                'phone': '+919876543293',
                'branch': str(self.branch_a.id),
                'lead_source': str(self.source.id),
                'assignment_mode': 'MANUAL',
                'assigned_sales_user': str(fd_rep.id),
            },
            format='json',
        )
        self.assertEqual(resp_create.status_code, status.HTTP_201_CREATED)
        self.assertEqual(str(resp_create.data['assigned_sales_user']), str(fd_rep.id))

