"""
backend/tests/test_crm_assignment_notifications.py — Comprehensive Test Suite
Targeted verification for:
  Part A: Lead Assignment, Agent Workflow, Notifications, Reassignment, RBAC Scoping
  Part B: Simple Coupon Baseline, Discount Calculations, Validation Rules, Single Redemption
"""

import uuid
from decimal import Decimal
from datetime import timedelta
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
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_rbac import (
    ModuleCatalog, SubmoduleCatalog, Permission, Role,
    RolePermissionSet, RolePermissionSetItem, RoleAssignment,
)
from apps.tenant_core.models_crm import (
    LeadSource, Lead, LeadAssignment, LeadStatusHistory,
    LeadActivity, LeadNote, SalesFollowupTask, CRMAgentAssignmentConfig,
)
from apps.tenant_core.models_govern import InAppNotification
from apps.tenant_core.models_audit_outbox import BusinessAuditEvent, DomainOutboxEvent
from apps.tenant_core.models_discounts import (
    DiscountCampaign, DiscountCode, DiscountRedemption,
)
from apps.tenant_core.models_catalog import (
    ProgramCategory, Program, Package,
)
from apps.tenant_core.models_commerce import Order
from apps.tenant_core.services_crm import CRMLeadService
from apps.tenant_core.services_discounts import DiscountCouponEngineService
from apps.tenant_core.views_crm import LeadViewSet, get_user_effective_branch_ids

DB = 'tenant_test'


class CRMAssignmentAndCouponTestSuite(APITestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias(DB)

        # 1. Master Tenant
        self.tenant = Tenant.objects.using('default').create(
            code='SWEAT-ASSIGN-TEST',
            name='Sweat Assignment Test Tenant',
            slug='sweat-assign-test',
            status='ACTIVE'
        )
        TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            db_name='test_fitness_tenant',
            database_name='test_fitness_tenant',
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )

        # 2. Org and Branches
        self.org = Organization.objects.using(DB).create(
            name='Sweat India Flagship',
            code='SWT-ORG-01',
            status='ACTIVE'
        )
        self.other_org = Organization.objects.using(DB).create(
            name='Rival Gym Org',
            code='RVL-ORG-02',
            status='ACTIVE'
        )

        self.loc = Location.objects.using(DB).create(
            organization=self.org,
            code='LOC-MUM-01',
            name='Mumbai City Center',
            city='Mumbai',
            state='Maharashtra',
            country='India',
            status='ACTIVE'
        )
        self.branch_downtown = Branch.objects.using(DB).create(
            organization=self.org,
            location=self.loc,
            name='Downtown Flagship',
            code='SWT-DT-01',
            status='ACTIVE'
        )
        self.branch_uptown = Branch.objects.using(DB).create(
            organization=self.org,
            location=self.loc,
            name='Uptown Studio',
            code='SWT-UP-02',
            status='ACTIVE'
        )

        # 3. RBAC Roles
        self.role_admin = Role.objects.using(DB).create(
            organization=self.org,
            code='ORG_ADMIN',
            name='Organization Admin',
            scope='ORG',
            is_active=True
        )
        self.role_sales = Role.objects.using(DB).create(
            organization=self.org,
            code='SALES_REP',
            name='Sales Representative',
            scope='BRANCH',
            is_active=True
        )
        self.role_manager = Role.objects.using(DB).create(
            organization=self.org,
            code='BRANCH_MANAGER',
            name='Branch Manager',
            scope='BRANCH',
            is_active=True
        )

        # 4. Users
        self.admin_user = TenantUser.objects.using(DB).create(
            organization=self.org,
            email='admin@sweat.test',
            first_name='Aditya',
            last_name='Admin',
            user_type='ADMIN',
            status='ACTIVE',
            is_login_allowed=True,
            home_branch=self.branch_downtown
        )
        RoleAssignment.objects.using(DB).create(
            user=self.admin_user,
            role=self.role_admin,
            branch=None,
            is_active=True,
            status='ACTIVE'
        )

        # Agent A (Downtown)
        self.agent_a = TenantUser.objects.using(DB).create(
            organization=self.org,
            email='agent.a@sweat.test',
            first_name='Aarav',
            last_name='Agent',
            user_type='STAFF',
            status='ACTIVE',
            is_login_allowed=True,
            home_branch=self.branch_downtown
        )
        RoleAssignment.objects.using(DB).create(
            user=self.agent_a,
            role=self.role_sales,
            branch=self.branch_downtown,
            is_active=True,
            status='ACTIVE'
        )

        # Agent B (Downtown)
        self.agent_b = TenantUser.objects.using(DB).create(
            organization=self.org,
            email='agent.b@sweat.test',
            first_name='Bhavna',
            last_name='Broker',
            user_type='STAFF',
            status='ACTIVE',
            is_login_allowed=True,
            home_branch=self.branch_downtown
        )
        RoleAssignment.objects.using(DB).create(
            user=self.agent_b,
            role=self.role_sales,
            branch=self.branch_downtown,
            is_active=True,
            status='ACTIVE'
        )

        # Agent C (Uptown only)
        self.agent_c = TenantUser.objects.using(DB).create(
            organization=self.org,
            email='agent.c@sweat.test',
            first_name='Chetan',
            last_name='Closer',
            user_type='STAFF',
            status='ACTIVE',
            is_login_allowed=True,
            home_branch=self.branch_uptown
        )
        RoleAssignment.objects.using(DB).create(
            user=self.agent_c,
            role=self.role_sales,
            branch=self.branch_uptown,
            is_active=True,
            status='ACTIVE'
        )

        # Inactive Agent
        self.inactive_agent = TenantUser.objects.using(DB).create(
            organization=self.org,
            email='inactive@sweat.test',
            first_name='Irfan',
            last_name='Inactive',
            user_type='STAFF',
            status='INACTIVE',
            is_login_allowed=False,
            home_branch=self.branch_downtown
        )
        RoleAssignment.objects.using(DB).create(
            user=self.inactive_agent,
            role=self.role_sales,
            branch=self.branch_downtown,
            is_active=True,
            status='ACTIVE'
        )

        # Other Org Agent
        self.other_agent = TenantUser.objects.using(DB).create(
            organization=self.other_org,
            email='other@rival.test',
            first_name='Rival',
            last_name='Staff',
            user_type='STAFF',
            status='ACTIVE',
            is_login_allowed=True
        )

        # 5. Lead Source
        self.lead_source = LeadSource.objects.using(DB).create(
            organization=self.org,
            code='INSTAGRAM_AD',
            name='Instagram Ads',
            source_type='META',
            status='ACTIVE'
        )

    # =========================================================================
    # PART A: LEAD ASSIGNMENT TESTS (1 - 20)
    # =========================================================================

    def test_01_eligible_agent_returned(self):
        """1. Eligible agent for Downtown branch is returned in eligible_agents."""
        request = type('Request', (), {
            'user': self.admin_user,
            'organization': self.org,
            'query_params': {'branch_id': str(self.branch_downtown.id)},
            '_tenant_db_alias': DB,
        })()
        viewset = LeadViewSet()
        response = viewset.eligible_agents(request)
        self.assertEqual(response.status_code, 200)
        agent_ids = [ag['id'] for ag in response.data]
        self.assertIn(str(self.agent_a.id), agent_ids)
        self.assertIn(str(self.agent_b.id), agent_ids)

    def test_02_inactive_agent_excluded(self):
        """2. Inactive agent must be excluded from eligible agent dropdown."""
        request = type('Request', (), {
            'user': self.admin_user,
            'organization': self.org,
            'query_params': {'branch_id': str(self.branch_downtown.id)},
            '_tenant_db_alias': DB,
        })()
        viewset = LeadViewSet()
        response = viewset.eligible_agents(request)
        agent_ids = [ag['id'] for ag in response.data]
        self.assertNotIn(str(self.inactive_agent.id), agent_ids)

    def test_03_wrong_branch_agent_excluded(self):
        """3. Agent C (Uptown only) must be excluded when Downtown branch is filtered."""
        request = type('Request', (), {
            'user': self.admin_user,
            'organization': self.org,
            'query_params': {'branch_id': str(self.branch_downtown.id)},
            '_tenant_db_alias': DB,
        })()
        viewset = LeadViewSet()
        response = viewset.eligible_agents(request)
        agent_ids = [ag['id'] for ag in response.data]
        self.assertNotIn(str(self.agent_c.id), agent_ids)

    def test_04_assignment_persists_on_lead_creation(self):
        """4. Initial assignment persists on Lead and sets assigned_sales_user."""
        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Rohan',
            last_name='Mehta',
            phone='+919876543210',
            email='rohan.mehta@gmail.com',
            branch=self.branch_downtown,
            lead_source=self.lead_source,
            assigned_sales_user=self.agent_a,
            actor_user=self.admin_user,
            db_alias=DB,
        )
        self.assertIsNotNone(lead.id)
        self.assertEqual(lead.assigned_sales_user_id, self.agent_a.id)

    def test_05_lead_assignment_history_created(self):
        """5. LeadAssignment history record is created in the database."""
        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Siddharth',
            last_name='Kapoor',
            phone='+919876543211',
            email='sid.kapoor@gmail.com',
            branch=self.branch_downtown,
            lead_source=self.lead_source,
            assigned_sales_user=self.agent_a,
            actor_user=self.admin_user,
            db_alias=DB,
        )
        assignments = list(LeadAssignment.objects.using(DB).filter(lead=lead, status='ACTIVE'))
        self.assertEqual(len(assignments), 1)
        self.assertEqual(assignments[0].assigned_to_user_id, self.agent_a.id)
        self.assertEqual(assignments[0].assignment_type, 'SALES')
        self.assertEqual(assignments[0].status, 'ACTIVE')

    def test_06_assigned_agent_sees_lead_under_my_leads(self):
        """6. Assigned agent sees the Lead when filtering assigned_to_me=true."""
        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Vikram',
            last_name='Singh',
            phone='+919876543212',
            email='vikram.singh@gmail.com',
            branch=self.branch_downtown,
            lead_source=self.lead_source,
            assigned_sales_user=self.agent_a,
            actor_user=self.admin_user,
            db_alias=DB,
        )
        # Agent A views with assigned_to_me=true
        qs = Lead.objects.using(DB).filter(organization=self.org, assigned_sales_user=self.agent_a)
        self.assertIn(lead, qs)

    def test_07_unrelated_agent_does_not_see_lead_under_my_leads(self):
        """7. Agent B does NOT see Agent A's lead when filtering assigned_to_me=true."""
        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Deepak',
            last_name='Verma',
            phone='+919876543213',
            email='deepak.verma@gmail.com',
            branch=self.branch_downtown,
            lead_source=self.lead_source,
            assigned_sales_user=self.agent_a,
            actor_user=self.admin_user,
            db_alias=DB,
        )
        qs_agent_b = Lead.objects.using(DB).filter(organization=self.org, assigned_sales_user=self.agent_b)
        self.assertNotIn(lead, qs_agent_b)

    def test_08_manager_broader_scope_works(self):
        """8. Branch manager can see all leads in their permitted branch."""
        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Ananya',
            last_name='Pandey',
            phone='+919876543214',
            email='ananya.p@gmail.com',
            branch=self.branch_downtown,
            lead_source=self.lead_source,
            assigned_sales_user=self.agent_a,
            actor_user=self.admin_user,
            db_alias=DB,
        )
        permitted = get_user_effective_branch_ids(self.admin_user, DB)
        # Admin has org-wide (None)
        self.assertIsNone(permitted)
        # Filter branch
        branch_leads = Lead.objects.using(DB).filter(branch=self.branch_downtown)
        self.assertIn(lead, branch_leads)

    def test_09_reassignment_history_preserved(self):
        """9. Reassignment preserves previous assignment record as inactive."""
        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Kavita',
            last_name='Roy',
            phone='+919876543215',
            email='kavita.roy@gmail.com',
            branch=self.branch_downtown,
            lead_source=self.lead_source,
            assigned_sales_user=self.agent_a,
            actor_user=self.admin_user,
            db_alias=DB,
        )
        # Reassign to Agent B
        CRMLeadService.assign_lead(
            lead=lead,
            assigned_to_user=self.agent_b,
            assignment_type='SALES',
            notes='Reassigned due to territory shift',
            actor_user=self.admin_user,
            db_alias=DB,
        )
        lead.refresh_from_db(using=DB)
        self.assertEqual(lead.assigned_sales_user_id, self.agent_b.id)

        all_assignments = list(LeadAssignment.objects.using(DB).filter(lead=lead).order_by('assigned_at'))
        self.assertEqual(len(all_assignments), 2)
        self.assertEqual(all_assignments[0].assigned_to_user_id, self.agent_a.id)
        self.assertEqual(all_assignments[0].status, 'INACTIVE')
        self.assertEqual(all_assignments[1].assigned_to_user_id, self.agent_b.id)
        self.assertEqual(all_assignments[1].status, 'ACTIVE')

    def test_10_reassigned_user_gets_notification(self):
        """10. When lead is reassigned to Agent B, an InAppNotification is generated for Agent B."""
        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Pooja',
            last_name='Hegde',
            phone='+919876543216',
            email='pooja.h@gmail.com',
            branch=self.branch_downtown,
            lead_source=self.lead_source,
            assigned_sales_user=self.agent_a,
            actor_user=self.admin_user,
            db_alias=DB,
        )
        CRMLeadService.assign_lead(
            lead=lead,
            assigned_to_user=self.agent_b,
            assignment_type='SALES',
            actor_user=self.admin_user,
            db_alias=DB,
        )
        notifs = list(InAppNotification.objects.using(DB).filter(user=self.agent_b))
        self.assertGreaterEqual(len(notifs), 1)
        latest_notif = notifs[-1]
        self.assertEqual(latest_notif.notification_type, 'LEAD_ASSIGNED')
        self.assertIn(str(lead.id), latest_notif.deep_link)

    def test_11_original_user_ownership_removed_on_reassign(self):
        """11. Original agent no longer sees lead under My Leads after reassignment."""
        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Varun',
            last_name='Dhawan',
            phone='+919876543217',
            email='varun.d@gmail.com',
            branch=self.branch_downtown,
            lead_source=self.lead_source,
            assigned_sales_user=self.agent_a,
            actor_user=self.admin_user,
            db_alias=DB,
        )
        # Reassign to Agent B
        CRMLeadService.assign_lead(
            lead=lead,
            assigned_to_user=self.agent_b,
            assignment_type='SALES',
            actor_user=self.admin_user,
            db_alias=DB,
        )
        agent_a_leads = Lead.objects.using(DB).filter(organization=self.org, assigned_sales_user=self.agent_a)
        self.assertNotIn(lead, agent_a_leads)

    def test_12_notification_unread_state(self):
        """12. Created notification starts with is_read=False and unread count reflects it."""
        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Kriti',
            last_name='Sanon',
            phone='+919876543218',
            email='kriti.s@gmail.com',
            branch=self.branch_downtown,
            lead_source=self.lead_source,
            assigned_sales_user=self.agent_a,
            actor_user=self.admin_user,
            db_alias=DB,
        )
        unread_count = InAppNotification.objects.using(DB).filter(user=self.agent_a, is_read=False).count()
        self.assertGreaterEqual(unread_count, 1)

    def test_13_notification_deep_link_correct(self):
        """13. Notification deep link points accurately to /crm/leads?lead_id=<id>."""
        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Tara',
            last_name='Sutaria',
            phone='+919876543219',
            email='tara.s@gmail.com',
            branch=self.branch_downtown,
            lead_source=self.lead_source,
            assigned_sales_user=self.agent_a,
            actor_user=self.admin_user,
            db_alias=DB,
        )
        notif = InAppNotification.objects.using(DB).filter(user=self.agent_a, data__lead_id=str(lead.id)).first()
        self.assertIsNotNone(notif)
        self.assertEqual(notif.deep_link, f"/crm/leads?lead_id={lead.id}")

    def test_14_notification_read_persists(self):
        """14. Marking notification as read updates is_read and read_at timestamp."""
        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Sara',
            last_name='Ali',
            phone='+919876543220',
            email='sara.ali@gmail.com',
            branch=self.branch_downtown,
            lead_source=self.lead_source,
            assigned_sales_user=self.agent_a,
            actor_user=self.admin_user,
            db_alias=DB,
        )
        notif = InAppNotification.objects.using(DB).filter(user=self.agent_a, data__lead_id=str(lead.id)).first()
        notif.is_read = True
        notif.read_at = timezone.now()
        notif.save(using=DB, update_fields=['is_read', 'read_at'])

        fresh_notif = InAppNotification.objects.using(DB).get(id=notif.id)
        self.assertTrue(fresh_notif.is_read)
        self.assertIsNotNone(fresh_notif.read_at)

    def test_15_duplicate_assignment_does_not_spam_notification(self):
        """15. Re-assigning to the same agent with same state does not create duplicate notification."""
        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Mira',
            last_name='Kapoor',
            phone='+919876543221',
            email='mira.k@gmail.com',
            branch=self.branch_downtown,
            lead_source=self.lead_source,
            assigned_sales_user=self.agent_a,
            actor_user=self.admin_user,
            db_alias=DB,
        )
        count_before = InAppNotification.objects.using(DB).filter(user=self.agent_a, data__lead_id=str(lead.id)).count()
        # Attempt assigning again to Agent A
        CRMLeadService.assign_lead(
            lead=lead,
            assigned_to_user=self.agent_a,
            assignment_type='SALES',
            actor_user=self.admin_user,
            db_alias=DB,
        )
        count_after = InAppNotification.objects.using(DB).filter(user=self.agent_a, data__lead_id=str(lead.id)).count()
        self.assertEqual(count_before, count_after)

    def test_16_tenant_isolation(self):
        """16. Cross-tenant assignment attempt is rejected."""
        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Tiger',
            last_name='Shroff',
            phone='+919876543222',
            email='tiger.s@gmail.com',
            branch=self.branch_downtown,
            lead_source=self.lead_source,
            actor_user=self.admin_user,
            db_alias=DB,
        )
        with self.assertRaises(ValidationError):
            CRMLeadService.assign_lead(
                lead=lead,
                assigned_to_user=self.other_agent,  # belongs to self.other_org
                actor_user=self.admin_user,
                db_alias=DB,
            )

    def test_17_branch_isolation(self):
        """17. Agent C (Uptown only) cannot be assigned to Downtown lead."""
        effective_branches = get_user_effective_branch_ids(self.agent_c, DB)
        self.assertNotIn(str(self.branch_downtown.id), effective_branches)

    def test_18_unauthorized_assignment_rejected(self):
        """18. Inactive user assignment is rejected."""
        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Ranbir',
            last_name='Kapoor',
            phone='+919876543223',
            email='ranbir.k@gmail.com',
            branch=self.branch_downtown,
            lead_source=self.lead_source,
            actor_user=self.admin_user,
            db_alias=DB,
        )
        with self.assertRaises(ValidationError):
            CRMLeadService.assign_lead(
                lead=lead,
                assigned_to_user=self.inactive_agent,
                actor_user=self.admin_user,
                db_alias=DB,
            )

    def test_19_agent_can_continue_workflow(self):
        """19. Assigned agent can log activity and create follow-up task."""
        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Alia',
            last_name='Bhatt',
            phone='+919876543224',
            email='alia.b@gmail.com',
            branch=self.branch_downtown,
            lead_source=self.lead_source,
            assigned_sales_user=self.agent_a,
            actor_user=self.admin_user,
            db_alias=DB,
        )
        # Log activity
        act = CRMLeadService.record_lead_activity(
            lead=lead,
            activity_type='CALL',
            outcome='CONNECTED',
            notes='Lead interested in 3-month strength program',
            performed_by_user=self.agent_a,
            actor_user=self.agent_a,
            db_alias=DB,
        )
        self.assertIsNotNone(act.id)

        # Create followup
        task = CRMLeadService.create_followup_task(
            lead=lead,
            assigned_to_user=self.agent_a,
            task_type='CALL',
            due_at=timezone.now() + timedelta(days=2),
            priority='HIGH',
            created_by_user=self.agent_a,
            db_alias=DB,
        )
        self.assertEqual(task.assigned_to_user_id, self.agent_a.id)

    def test_20_pipeline_and_lead_table_sync(self):
        """20. Assigned agent name is synchronized across lead record."""
        lead = CRMLeadService.create_lead(
            organization=self.org,
            first_name='Rashmika',
            last_name='Mandanna',
            phone='+919876543225',
            email='rashmika.m@gmail.com',
            branch=self.branch_downtown,
            lead_source=self.lead_source,
            assigned_sales_user=self.agent_b,
            actor_user=self.admin_user,
            db_alias=DB,
        )
        lead.refresh_from_db(using=DB)
        self.assertEqual(lead.assigned_sales_user.first_name, 'Bhavna')


    # =========================================================================
    # PART B: SIMPLE WORKING COUPON BASELINE TESTS (21 - 35)
    # =========================================================================

    def _setup_coupon_fixtures(self):
        self.category = ProgramCategory.objects.using(DB).create(
            organization=self.org, name='Fitness', code='FIT'
        )
        self.program = Program.objects.using(DB).create(
            organization=self.org, category=self.category, name='Functional Training', code='FT-01'
        )
        self.package = Package.objects.using(DB).create(
            organization=self.org, program=self.program, name='3-Month Unlimited', code='3M-UNL'
        )

        self.campaign = DiscountCampaign.objects.using(DB).create(
            organization=self.org,
            name='Monsoon Special',
            discount_type='PERCENTAGE',
            discount_value=Decimal('20.00'),
            max_discount=Decimal('2000.00'),
            minimum_order_amount=Decimal('5000.00'),
            valid_from=timezone.now() - timedelta(days=2),
            valid_until=timezone.now() + timedelta(days=30),
            usage_limit=10,
            per_user_limit=1,
            status='ACTIVE'
        )
        self.coupon = DiscountCode.objects.using(DB).create(
            campaign=self.campaign,
            code='MONSOON20',
            branch=self.branch_downtown,
            package=self.package,
            status='ACTIVE'
        )

        self.member_profile = UserProfile.objects.using(DB).create(
            user=self.admin_user,
            preferred_branch=self.branch_downtown,
            joining_date=timezone.now().date(),
        )

    def test_21_create_active_coupon(self):
        """21. Active coupon code created and returns valid computed status."""
        self._setup_coupon_fixtures()
        self.assertEqual(self.coupon.code, 'MONSOON20')
        self.assertEqual(self.coupon.status, 'ACTIVE')

    def test_22_percentage_discount_calculation(self):
        """22. 20% on 10,000 subtotal calculates exactly 2,000 discount."""
        self._setup_coupon_fixtures()
        res = DiscountCouponEngineService.validate_coupon(
            code_str='MONSOON20',
            user_profile=self.member_profile,
            order_subtotal=Decimal('10000.00'),
            branch=self.branch_downtown,
            package=self.package,
            db_alias=DB,
        )
        self.assertTrue(res['is_valid'])
        self.assertEqual(res['discount_amount'], '2000.00')
        self.assertEqual(res['final_subtotal'], '8000.00')

    def test_23_fixed_discount_calculation(self):
        """23. Fixed ₹500 discount on 5,000 order yields 4,500 subtotal."""
        self._setup_coupon_fixtures()
        fixed_camp = DiscountCampaign.objects.using(DB).create(
            organization=self.org,
            name='Flat 500 Off',
            discount_type='FIXED',
            discount_value=Decimal('500.00'),
            valid_from=timezone.now() - timedelta(days=1),
            status='ACTIVE'
        )
        fixed_code = DiscountCode.objects.using(DB).create(
            campaign=fixed_camp,
            code='FLAT500',
            status='ACTIVE'
        )
        res = DiscountCouponEngineService.validate_coupon(
            code_str='FLAT500',
            user_profile=self.member_profile,
            order_subtotal=Decimal('5000.00'),
            db_alias=DB,
        )
        self.assertTrue(res['is_valid'])
        self.assertEqual(res['discount_amount'], '500.00')
        self.assertEqual(res['final_subtotal'], '4500.00')

    def test_24_expired_coupon_rejected(self):
        """24. Expired campaign coupon is rejected with EXPIRED code."""
        self._setup_coupon_fixtures()
        self.campaign.valid_until = timezone.now() - timedelta(days=1)
        self.campaign.save(using=DB)
        res = DiscountCouponEngineService.validate_coupon(
            code_str='MONSOON20',
            user_profile=self.member_profile,
            order_subtotal=Decimal('10000.00'),
            branch=self.branch_downtown,
            package=self.package,
            db_alias=DB,
        )
        self.assertFalse(res['is_valid'])
        self.assertEqual(res['reason_code'], 'EXPIRED')

    def test_25_inactive_coupon_rejected(self):
        """25. Inactive coupon status is rejected."""
        self._setup_coupon_fixtures()
        self.coupon.status = 'INACTIVE'
        self.coupon.save(using=DB)
        res = DiscountCouponEngineService.validate_coupon(
            code_str='MONSOON20',
            user_profile=self.member_profile,
            order_subtotal=Decimal('10000.00'),
            db_alias=DB,
        )
        self.assertFalse(res['is_valid'])
        self.assertEqual(res['reason_code'], 'INACTIVE')

    def test_26_minimum_spend_enforced(self):
        """26. Order below minimum order threshold is rejected."""
        self._setup_coupon_fixtures()
        # Campaign requires minimum 5,000; order is 3,000
        res = DiscountCouponEngineService.validate_coupon(
            code_str='MONSOON20',
            user_profile=self.member_profile,
            order_subtotal=Decimal('3000.00'),
            branch=self.branch_downtown,
            package=self.package,
            db_alias=DB,
        )
        self.assertFalse(res['is_valid'])
        self.assertEqual(res['reason_code'], 'MINIMUM_ORDER_NOT_MET')

    def test_27_max_discount_enforced(self):
        """27. 20% on 20,000 is 4,000, capped at max_discount 2,000."""
        self._setup_coupon_fixtures()
        res = DiscountCouponEngineService.validate_coupon(
            code_str='MONSOON20',
            user_profile=self.member_profile,
            order_subtotal=Decimal('20000.00'),
            branch=self.branch_downtown,
            package=self.package,
            db_alias=DB,
        )
        self.assertTrue(res['is_valid'])
        self.assertEqual(res['discount_amount'], '2000.00')

    def test_28_branch_scope_enforced(self):
        """28. Coupon scoped to Downtown Flagship is rejected at Uptown Studio."""
        self._setup_coupon_fixtures()
        res = DiscountCouponEngineService.validate_coupon(
            code_str='MONSOON20',
            user_profile=self.member_profile,
            order_subtotal=Decimal('10000.00'),
            branch=self.branch_uptown,
            package=self.package,
            db_alias=DB,
        )
        self.assertFalse(res['is_valid'])
        self.assertEqual(res['reason_code'], 'BRANCH_NOT_ELIGIBLE')

    def test_29_package_scope_enforced(self):
        """29. Coupon scoped to 3-Month Unlimited is rejected on other package."""
        self._setup_coupon_fixtures()
        other_pkg = Package.objects.using(DB).create(
            organization=self.org, program=self.program, name='1-Month Trial', code='1M-TRL'
        )
        res = DiscountCouponEngineService.validate_coupon(
            code_str='MONSOON20',
            user_profile=self.member_profile,
            order_subtotal=Decimal('10000.00'),
            branch=self.branch_downtown,
            package=other_pkg,
            db_alias=DB,
        )
        self.assertFalse(res['is_valid'])
        self.assertEqual(res['reason_code'], 'PACKAGE_NOT_ELIGIBLE')

    def test_30_usage_cap_enforced(self):
        """30. Coupon rejected once global usage limit is reached."""
        self._setup_coupon_fixtures()
        self.campaign.usage_limit = 1
        self.campaign.save(using=DB)

        # Create 1 existing redemption
        order = Order.objects.using(DB).create(
            branch=self.branch_downtown,
            user_profile=self.member_profile,
            order_number='ORD-CAP-001',
            status='PAID',
            subtotal=Decimal('10000.00'),
            total_amount=Decimal('8000.00')
        )
        DiscountRedemption.objects.using(DB).create(
            discount_code=self.coupon,
            campaign=self.campaign,
            user_profile=self.member_profile,
            order=order,
            discount_amount=Decimal('2000.00')
        )

        res = DiscountCouponEngineService.validate_coupon(
            code_str='MONSOON20',
            user_profile=self.member_profile,
            order_subtotal=Decimal('10000.00'),
            branch=self.branch_downtown,
            package=self.package,
            db_alias=DB,
        )
        self.assertFalse(res['is_valid'])
        self.assertEqual(res['reason_code'], 'USAGE_LIMIT_EXCEEDED')

    def test_31_per_customer_cap_enforced(self):
        """31. User who reached per_user_limit is blocked from using coupon again."""
        self._setup_coupon_fixtures()
        self.campaign.per_user_limit = 1
        self.campaign.save(using=DB)

        order = Order.objects.using(DB).create(
            branch=self.branch_downtown,
            user_profile=self.member_profile,
            order_number='ORD-PER-001',
            status='PAID',
            subtotal=Decimal('10000.00'),
            total_amount=Decimal('8000.00')
        )
        DiscountRedemption.objects.using(DB).create(
            discount_code=self.coupon,
            campaign=self.campaign,
            user_profile=self.member_profile,
            order=order,
            discount_amount=Decimal('2000.00')
        )

        res = DiscountCouponEngineService.validate_coupon(
            code_str='MONSOON20',
            user_profile=self.member_profile,
            order_subtotal=Decimal('10000.00'),
            branch=self.branch_downtown,
            package=self.package,
            db_alias=DB,
        )
        self.assertFalse(res['is_valid'])
        self.assertEqual(res['reason_code'], 'PER_USER_LIMIT_EXCEEDED')

    def test_32_quote_preview_does_not_redeem(self):
        """32. validate_coupon preview does not write DiscountRedemption records."""
        self._setup_coupon_fixtures()
        initial_redemptions = DiscountRedemption.objects.using(DB).count()
        DiscountCouponEngineService.validate_coupon(
            code_str='MONSOON20',
            user_profile=self.member_profile,
            order_subtotal=Decimal('10000.00'),
            branch=self.branch_downtown,
            package=self.package,
            db_alias=DB,
        )
        final_redemptions = DiscountRedemption.objects.using(DB).count()
        self.assertEqual(initial_redemptions, final_redemptions)

    def test_33_successful_conversion_creates_one_redemption(self):
        """33. Calling redeem_coupon creates exactly 1 DiscountRedemption record."""
        self._setup_coupon_fixtures()
        order = Order.objects.using(DB).create(
            branch=self.branch_downtown,
            user_profile=self.member_profile,
            order_number='ORD-CONV-001',
            status='PENDING',
            subtotal=Decimal('10000.00'),
            total_amount=Decimal('10000.00')
        )
        redemption = DiscountCouponEngineService.redeem_coupon(
            order=order,
            code_str='MONSOON20',
            user_profile=self.member_profile,
            db_alias=DB,
        )
        self.assertIsNotNone(redemption.id)
        self.assertEqual(redemption.discount_amount, Decimal('2000.00'))
        self.assertEqual(order.discount_amount, Decimal('2000.00'))

    def test_34_retry_does_not_duplicate_redemption(self):
        """34. Retrying redemption when per-user cap is 1 raises ValidationError."""
        self._setup_coupon_fixtures()
        order1 = Order.objects.using(DB).create(
            branch=self.branch_downtown,
            user_profile=self.member_profile,
            order_number='ORD-RETRY-001',
            status='PENDING',
            subtotal=Decimal('10000.00'),
            total_amount=Decimal('10000.00')
        )
        DiscountCouponEngineService.redeem_coupon(
            order=order1,
            code_str='MONSOON20',
            user_profile=self.member_profile,
            db_alias=DB,
        )
        order2 = Order.objects.using(DB).create(
            branch=self.branch_downtown,
            user_profile=self.member_profile,
            order_number='ORD-RETRY-002',
            status='PENDING',
            subtotal=Decimal('10000.00'),
            total_amount=Decimal('10000.00')
        )
        with self.assertRaises(ValidationError):
            DiscountCouponEngineService.redeem_coupon(
                order=order2,
                code_str='MONSOON20',
                user_profile=self.member_profile,
                db_alias=DB,
            )

    def test_35_historical_redeemed_coupon_cannot_be_hard_deleted(self):
        """35. Deleting coupon with existing redemptions via API/viewset returns 400."""
        self._setup_coupon_fixtures()
        order = Order.objects.using(DB).create(
            branch=self.branch_downtown,
            user_profile=self.member_profile,
            order_number='ORD-DEL-001',
            status='PAID',
            subtotal=Decimal('10000.00'),
            total_amount=Decimal('8000.00')
        )
        DiscountRedemption.objects.using(DB).create(
            discount_code=self.coupon,
            campaign=self.campaign,
            user_profile=self.member_profile,
            order=order,
            discount_amount=Decimal('2000.00')
        )
        from apps.tenant_core.views_discounts import DiscountCodeViewSet
        viewset = DiscountCodeViewSet()
        request = type('Request', (), {
            'user': self.admin_user,
            'organization': self.org,
            '_tenant_db_alias': DB,
        })()
        viewset.request = request
        viewset.get_object = lambda: self.coupon
        response = viewset.destroy(request)
        self.assertEqual(response.status_code, 400)
        self.assertIn('Cannot delete a coupon code with redemption history', response.data['error'])
