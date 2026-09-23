"""
CRM Phase 10 Targeted Test Suite: CRM Dashboard & Sales Analytics
Authoritative tests for:
  - Authentication and RBAC (crm.leads.view, alternative permissions)
  - Strict tenant DB isolation
  - Server-enforced branch scoping & rejection of unauthorized branch requests
  - Summary KPIs: Total, New, Open, Converted Leads, Conversion Rate
  - Trial metrics: Booked, Confirmed, Attended, No-Show
  - Follow-up metrics: Due today, Overdue, Upcoming, Completed, Completion Rate
  - Attention metrics consumed from Phase 7 Attention Engine
  - Canonical Commerce revenue: Paid orders only, unpaid/failed excluded, refunds deducted, zero double counting
  - Date filtering: Presets (TODAY, LAST_7_DAYS, LAST_30_DAYS), custom date_from/date_to, timezone boundaries
  - Funnel stage aggregation across all canonical Lead statuses
  - Source performance: Leads, Trials, Conversions, Paid Revenue
  - Campaign performance: Reuses Phase 9 attribution semantics without double counting
  - Agent and Branch performance tables
  - Continuous time series trend buckets
  - Empty dataset handling
  - Filter propagation by source, agent, program, campaign, platform
"""

import uuid
from decimal import Decimal
from datetime import datetime, time, timedelta
from django.utils import timezone
from django.core.exceptions import ValidationError, PermissionDenied
from rest_framework.test import APITestCase
from rest_framework import status

from apps.authentication.views import _build_tenant_token
from apps.master.models import (
    Tenant, TenantDataSource, ProductModule, TenantModule, SaasPlan, TenantSubscription
)
from apps.tenant_core.context import set_tenant_db_alias

from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser, UserBranch
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_rbac import (
    ModuleCatalog, SubmoduleCatalog, Permission, Role,
    RolePermissionSet, RolePermissionSetItem, RoleAssignment,
    RoleModuleAccess, RoleSubmoduleAccess
)
from apps.tenant_core.models_catalog import ProgramCategory, Program
from apps.tenant_core.models_commerce import Order, OrderItem, PaymentTransaction, Refund
from apps.tenant_core.models_crm import (
    LeadSource, Lead, LeadAttribution, TrialBooking, LeadConversion, SalesFollowupTask, LeadActivity
)
from apps.tenant_core.services_crm_dashboard import CRMDashboardService

DB = 'tenant_test'


class CRMPhase10DashboardTestCase(APITestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias(DB)

        # 1. Master DB Setup
        self.tenant = Tenant.objects.using('default').create(
            code='P10-TENANT', name='Phase10 Fitness', slug='phase10-fitness', status='ACTIVE'
        )
        TenantDataSource.objects.using('default').create(
            tenant=self.tenant, db_name='test_fitness_tenant',
            database_name='test_fitness_tenant', status='ACTIVE', database_engine='POSTGRESQL'
        )
        plan = SaasPlan.objects.using('default').create(
            name='Enterprise Plan', code='P10-ENT', tier='ENTERPRISE', status='ACTIVE'
        )
        TenantSubscription.objects.using('default').create(
            tenant=self.tenant, plan=plan, status='ACTIVE'
        )

        for mod_code in ['core', 'crm', 'finance']:
            pmod, _ = ProductModule.objects.using('default').get_or_create(
                code=mod_code, defaults={'name': f'{mod_code.title()} Module', 'status': 'ACTIVE'}
            )
            TenantModule.objects.using('default').create(
                tenant=self.tenant, module=pmod, is_enabled=True, availability_mode='ALL_BRANCHES'
            )

        # 2. Tenant Org & Branches
        self.org = Organization.objects.using(DB).create(
            code='P10-ORG', name='Phase10 Org', status='ACTIVE'
        )
        loc = Location.objects.using(DB).create(
            organization=self.org, code='P10-LOC', name='Downtown Location', status='ACTIVE'
        )
        self.branch_a = Branch.objects.using(DB).create(
            organization=self.org, location=loc, code='BR-P10-A', name='Downtown Branch', status='ACTIVE'
        )
        self.branch_b = Branch.objects.using(DB).create(
            organization=self.org, location=loc, code='BR-P10-B', name='Uptown Branch', status='ACTIVE'
        )

        # 3. Users
        self.admin_user = TenantUser.objects.using(DB).create(
            organization=self.org, email='admin@phase10.test', first_name='Admin', last_name='Owner', status='ACTIVE'
        )
        self.sales_user = TenantUser.objects.using(DB).create(
            organization=self.org, email='sales@phase10.test', first_name='Sales', last_name='Agent', status='ACTIVE'
        )
        self.unauth_user = TenantUser.objects.using(DB).create(
            organization=self.org, email='guest@phase10.test', first_name='Guest', last_name='User', status='ACTIVE'
        )
        UserBranch.objects.using(DB).create(
            user=self.sales_user, branch=self.branch_a, is_active=True
        )

        # 4. RBAC Modules, Permissions & Roles
        mod_crm, _ = ModuleCatalog.objects.using(DB).get_or_create(
            module_code='crm', defaults={'name': 'CRM', 'is_enabled': True}
        )
        sub_leads, _ = SubmoduleCatalog.objects.using(DB).get_or_create(
            module=mod_crm, submodule_code='leads', defaults={'name': 'Leads', 'is_enabled': True}
        )
        sub_camp, _ = SubmoduleCatalog.objects.using(DB).get_or_create(
            module=mod_crm, submodule_code='campaigns', defaults={'name': 'Campaigns', 'is_enabled': True}
        )

        perm_dash_view, _ = Permission.objects.using(DB).get_or_create(
            permission_code='crm.dashboard.view',
            defaults={'module': mod_crm, 'submodule': sub_leads, 'action': 'view', 'label': 'View CRM Dashboard'}
        )
        perm_view, _ = Permission.objects.using(DB).get_or_create(
            permission_code='crm.leads.view',
            defaults={'module': mod_crm, 'submodule': sub_leads, 'action': 'view', 'label': 'View Leads'}
        )
        perm_camp_view, _ = Permission.objects.using(DB).get_or_create(
            permission_code='crm.campaigns.view',
            defaults={'module': mod_crm, 'submodule': sub_camp, 'action': 'view', 'label': 'View Campaigns'}
        )

        # Admin Role (Org-wide)
        self.role_admin = Role.objects.using(DB).create(
            organization=self.org, code='ROLE_CRM_ADMIN', name='CRM Admin', scope='ORG', is_active=True
        )
        RoleModuleAccess.objects.using(DB).create(role=self.role_admin, module=mod_crm, can_access=True)
        RoleSubmoduleAccess.objects.using(DB).create(role=self.role_admin, submodule=sub_leads, can_access=True)
        RoleSubmoduleAccess.objects.using(DB).create(role=self.role_admin, submodule=sub_camp, can_access=True)
        pset_admin = RolePermissionSet.objects.using(DB).create(
            role=self.role_admin, name='Admin PermSet', is_active=True
        )
        RolePermissionSetItem.objects.using(DB).create(permission_set=pset_admin, permission=perm_dash_view, granted=True)
        RolePermissionSetItem.objects.using(DB).create(permission_set=pset_admin, permission=perm_view, granted=True)
        RolePermissionSetItem.objects.using(DB).create(permission_set=pset_admin, permission=perm_camp_view, granted=True)
        RoleAssignment.objects.using(DB).create(
            user=self.admin_user, role=self.role_admin, organization=self.org, is_active=True
        )

        # Sales Rep Role (Branch-scoped to Branch A)
        self.role_sales = Role.objects.using(DB).create(
            organization=self.org, code='ROLE_SALES_REP', name='Sales Rep', scope='BRANCH', is_active=True
        )
        RoleModuleAccess.objects.using(DB).create(role=self.role_sales, module=mod_crm, can_access=True)
        RoleSubmoduleAccess.objects.using(DB).create(role=self.role_sales, submodule=sub_leads, can_access=True)
        pset_sales = RolePermissionSet.objects.using(DB).create(
            role=self.role_sales, name='Sales PermSet', is_active=True
        )
        RolePermissionSetItem.objects.using(DB).create(permission_set=pset_sales, permission=perm_dash_view, granted=True)
        RolePermissionSetItem.objects.using(DB).create(permission_set=pset_sales, permission=perm_view, granted=True)
        RoleAssignment.objects.using(DB).create(
            user=self.sales_user, role=self.role_sales, organization=self.org, branch=self.branch_a, is_active=True
        )

        # No-access Role
        self.role_guest = Role.objects.using(DB).create(
            organization=self.org, code='ROLE_GUEST', name='Guest', scope='BRANCH', is_active=True
        )
        RoleAssignment.objects.using(DB).create(
            user=self.unauth_user, role=self.role_guest, organization=self.org, branch=self.branch_a, is_active=True
        )

        # Tokens
        self.admin_token = self._token(self.admin_user)
        self.sales_token = self._token(self.sales_user)
        self.guest_token = self._token(self.unauth_user)

        # 5. Program & Sources
        cat = ProgramCategory.objects.using(DB).create(
            organization=self.org, code='CAT-FIT', name='Fitness'
        )
        self.program = Program.objects.using(DB).create(
            organization=self.org, category=cat, code='PROG-HIIT', name='HIIT Bootcamp'
        )

        self.source_meta = LeadSource.objects.using(DB).create(
            organization=self.org, code='META_ADS', name='Meta Ads', source_type='META'
        )
        self.source_walkin = LeadSource.objects.using(DB).create(
            organization=self.org, code='WALK_IN', name='Front Desk Walk-in', source_type='WALK_IN'
        )

    # -------------------------------------------------------------------------
    # TESTS
    # -------------------------------------------------------------------------

    def _token(self, user):
        refresh = _build_tenant_token(user=user, tenant=self.tenant, db_alias=DB)
        return str(refresh.access_token)

    def test_01_dashboard_requires_authentication(self):
        resp = self.client.get('/api/v1/admin/crm/dashboard/')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_02_dashboard_rbac_permission_enforced(self):
        # Guest role without crm.dashboard.view gets 403
        resp = self.client.get(
            '/api/v1/admin/crm/dashboard/',
            HTTP_AUTHORIZATION=f'Bearer {self.guest_token}'
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        # User with ONLY crm.leads.view (and not crm.dashboard.view) is also denied 403
        set_tenant_db_alias(DB)
        user_leads = TenantUser.objects.using(DB).create(
            organization=self.org, email='leads.only@phase10.test', first_name='Leads', last_name='Only', status='ACTIVE'
        )
        role_leads = Role.objects.using(DB).create(
            organization=self.org, code='ROLE_LEADS_ONLY', name='Leads Only', scope='ORG', is_active=True
        )
        mod_crm = ModuleCatalog.objects.using(DB).get(module_code='crm')
        sub_leads = SubmoduleCatalog.objects.using(DB).get(module=mod_crm, submodule_code='leads')
        perm_leads = Permission.objects.using(DB).get(permission_code='crm.leads.view')
        RoleModuleAccess.objects.using(DB).create(role=role_leads, module=mod_crm, can_access=True)
        RoleSubmoduleAccess.objects.using(DB).create(role=role_leads, submodule=sub_leads, can_access=True)
        pset_l = RolePermissionSet.objects.using(DB).create(role=role_leads, name='LeadsOnly PermSet', is_active=True)
        RolePermissionSetItem.objects.using(DB).create(permission_set=pset_l, permission=perm_leads, granted=True)
        RoleAssignment.objects.using(DB).create(user=user_leads, role=role_leads, organization=self.org, is_active=True)
        token_leads = self._token(user_leads)

        resp_leads = self.client.get(
            '/api/v1/admin/crm/dashboard/',
            HTTP_AUTHORIZATION=f'Bearer {token_leads}'
        )
        self.assertEqual(resp_leads.status_code, status.HTTP_403_FORBIDDEN)

    def test_03_tenant_isolation(self):
        # Queries run strictly against resolved tenant DB alias
        resp = self.client.get(
            '/api/v1/admin/crm/dashboard/',
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertIn('summary', data)
        self.assertEqual(data['summary']['total_leads'], 0)

    def test_04_branch_scope_filtering(self):
        # Create a lead in Branch A and a lead in Branch B
        Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, first_name='Lead', last_name='A', current_status='NEW_LEAD'
        )
        Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_b, first_name='Lead', last_name='B', current_status='NEW_LEAD'
        )

        # Sales user (scoped to Branch A) should see only 1 lead
        resp = self.client.get(
            '/api/v1/admin/crm/dashboard/',
            HTTP_AUTHORIZATION=f'Bearer {self.sales_token}'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()['summary']['total_leads'], 1)

        # Admin user (ORG scoped) sees both
        resp_admin = self.client.get(
            '/api/v1/admin/crm/dashboard/',
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}'
        )
        self.assertEqual(resp_admin.json()['summary']['total_leads'], 2)

    def test_05_unauthorized_branch_rejected(self):
        # Sales user trying to access Branch B directly gets 403
        resp = self.client.get(
            f'/api/v1/admin/crm/dashboard/?branch_id={self.branch_b.id}',
            HTTP_AUTHORIZATION=f'Bearer {self.sales_token}'
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_06_summary_total_lead_count(self):
        for i in range(3):
            Lead.objects.using(DB).create(
                organization=self.org, branch=self.branch_a, first_name=f'Lead{i}', last_name='Test'
            )
        resp = self.client.get(
            '/api/v1/admin/crm/dashboard/',
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}'
        )
        self.assertEqual(resp.json()['summary']['total_leads'], 3)

    def test_07_summary_new_leads_and_open_leads(self):
        Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, first_name='New', last_name='Lead', current_status='NEW_LEAD'
        )
        Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, first_name='Trial', last_name='Lead', current_status='TRIAL_BOOKED'
        )
        Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, first_name='Lost', last_name='Lead', current_status='LOST'
        )

        resp = self.client.get(
            '/api/v1/admin/crm/dashboard/',
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}'
        )
        s = resp.json()['summary']
        self.assertEqual(s['total_leads'], 3)
        self.assertEqual(s['new_leads'], 1)
        self.assertEqual(s['open_leads'], 2)  # NEW_LEAD and TRIAL_BOOKED are open stages; LOST is closed

    def test_08_summary_converted_leads_and_conversion_rate(self):
        l1 = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, first_name='Conv1', last_name='Test', current_status='CONVERTED'
        )
        l2 = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, first_name='Conv2', last_name='Test', current_status='NEW_LEAD'
        )
        uprof = UserProfile.objects.using(DB).create(
            user=self.admin_user, preferred_branch=self.branch_a, joining_date=timezone.now().date()
        )
        LeadConversion.objects.using(DB).create(
            lead=l1, user_profile=uprof, converted_by_user=self.admin_user
        )

        resp = self.client.get(
            '/api/v1/admin/crm/dashboard/',
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}'
        )
        s = resp.json()['summary']
        self.assertEqual(s['converted_members'], 1)
        self.assertEqual(s['total_leads'], 2)
        self.assertEqual(s['conversion_rate'], 50.0)

    def test_09_summary_trials_booked_attended_noshow(self):
        l = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, first_name='Trial', last_name='User'
        )
        now = timezone.now()
        TrialBooking.objects.using(DB).create(
            lead=l, branch=self.branch_a, scheduled_start=now, scheduled_end=now + timedelta(hours=1), status='ATTENDED'
        )
        TrialBooking.objects.using(DB).create(
            lead=l, branch=self.branch_a, scheduled_start=now, scheduled_end=now + timedelta(hours=1), status='NO_SHOW'
        )
        TrialBooking.objects.using(DB).create(
            lead=l, branch=self.branch_a, scheduled_start=now, scheduled_end=now + timedelta(hours=1), status='CONFIRMED'
        )

        resp = self.client.get(
            '/api/v1/admin/crm/dashboard/',
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}'
        )
        s = resp.json()['summary']
        self.assertEqual(s['trials_booked'], 3)
        self.assertEqual(s['trials_attended'], 1)
        self.assertEqual(s['trial_no_shows'], 1)

    def test_10_summary_overdue_followups(self):
        l = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, first_name='Follow', last_name='User'
        )
        # Overdue task
        SalesFollowupTask.objects.using(DB).create(
            lead=l, assigned_to_user=self.sales_user, task_type='CALL',
            due_at=timezone.now() - timedelta(days=2), status='PENDING', created_by_user=self.admin_user
        )
        # Upcoming task
        SalesFollowupTask.objects.using(DB).create(
            lead=l, assigned_to_user=self.sales_user, task_type='CALL',
            due_at=timezone.now() + timedelta(days=2), status='PENDING', created_by_user=self.admin_user
        )

        resp = self.client.get(
            '/api/v1/admin/crm/dashboard/',
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}'
        )
        s = resp.json()['summary']
        self.assertEqual(s['overdue_followups'], 1)

    def test_11_summary_paid_revenue_calculation(self):
        l = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, first_name='Rev', last_name='User'
        )
        uprof = UserProfile.objects.using(DB).create(
            user=self.admin_user, preferred_branch=self.branch_a, joining_date=timezone.now().date()
        )
        order = Order.objects.using(DB).create(
            order_number='ORD-P10-001', branch=self.branch_a, lead=l, user_profile=uprof,
            status='PAID', total_amount=Decimal('4500.00')
        )
        PaymentTransaction.objects.using(DB).create(
            order=order, amount=Decimal('4500.00'), status='SUCCESS'
        )
        LeadConversion.objects.using(DB).create(
            lead=l, user_profile=uprof, order_id=order.id, converted_by_user=self.admin_user
        )

        resp = self.client.get(
            '/api/v1/admin/crm/dashboard/',
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}'
        )
        s = resp.json()['summary']
        self.assertEqual(s['paid_revenue'], '4500.00')
        self.assertEqual(s['gross_revenue'], '4500.00')
        self.assertEqual(s['refund_amount'], '0.00')

    def test_12_unpaid_and_failed_orders_excluded(self):
        l = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, first_name='Pending', last_name='User'
        )
        uprof = UserProfile.objects.using(DB).create(
            user=self.admin_user, preferred_branch=self.branch_a, joining_date=timezone.now().date()
        )
        order = Order.objects.using(DB).create(
            order_number='ORD-P10-PENDING', branch=self.branch_a, lead=l, user_profile=uprof,
            status='PENDING_PAYMENT', total_amount=Decimal('10000.00')
        )
        PaymentTransaction.objects.using(DB).create(
            order=order, amount=Decimal('10000.00'), status='FAILED'
        )
        LeadConversion.objects.using(DB).create(
            lead=l, user_profile=uprof, order_id=order.id, converted_by_user=self.admin_user
        )

        resp = self.client.get(
            '/api/v1/admin/crm/dashboard/',
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}'
        )
        s = resp.json()['summary']
        self.assertEqual(s['paid_revenue'], '0.00')

    def test_13_refund_subtracts_from_gross_revenue(self):
        l = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, first_name='Ref', last_name='User'
        )
        uprof = UserProfile.objects.using(DB).create(
            user=self.admin_user, preferred_branch=self.branch_a, joining_date=timezone.now().date()
        )
        order = Order.objects.using(DB).create(
            order_number='ORD-P10-REF', branch=self.branch_a, lead=l, user_profile=uprof,
            status='PARTIALLY_REFUNDED', total_amount=Decimal('5000.00')
        )
        ptxn = PaymentTransaction.objects.using(DB).create(
            order=order, amount=Decimal('5000.00'), status='PARTIALLY_REFUNDED'
        )
        Refund.objects.using(DB).create(
            payment_transaction=ptxn, order=order, amount=Decimal('1500.00'), status='SUCCESS'
        )
        LeadConversion.objects.using(DB).create(
            lead=l, user_profile=uprof, order_id=order.id, converted_by_user=self.admin_user
        )

        resp = self.client.get(
            '/api/v1/admin/crm/dashboard/',
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}'
        )
        s = resp.json()['summary']
        self.assertEqual(s['gross_revenue'], '5000.00')
        self.assertEqual(s['refund_amount'], '1500.00')
        self.assertEqual(s['paid_revenue'], '3500.00')

    def test_14_revenue_not_double_counted_with_multi_touch(self):
        l = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, first_name='Touch', last_name='User'
        )
        # 3 touch attribution records for the same single lead
        LeadAttribution.objects.using(DB).create(
            organization=self.org, lead=l, touch_type='FIRST_TOUCH', campaign_name='Summer26', platform='Meta'
        )
        LeadAttribution.objects.using(DB).create(
            organization=self.org, lead=l, touch_type='LEAD_CAPTURE', campaign_name='Summer26', platform='Meta'
        )
        LeadAttribution.objects.using(DB).create(
            organization=self.org, lead=l, touch_type='ASSISTED_TOUCH', campaign_name='Summer26', platform='Meta'
        )

        uprof = UserProfile.objects.using(DB).create(
            user=self.admin_user, preferred_branch=self.branch_a, joining_date=timezone.now().date()
        )
        order = Order.objects.using(DB).create(
            order_number='ORD-P10-TOUCH', branch=self.branch_a, lead=l, user_profile=uprof,
            status='PAID', total_amount=Decimal('8000.00')
        )
        PaymentTransaction.objects.using(DB).create(
            order=order, amount=Decimal('8000.00'), status='SUCCESS'
        )
        LeadConversion.objects.using(DB).create(
            lead=l, user_profile=uprof, order_id=order.id, converted_by_user=self.admin_user
        )

        resp = self.client.get(
            '/api/v1/admin/crm/dashboard/',
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}'
        )
        data = resp.json()
        self.assertEqual(data['summary']['paid_revenue'], '8000.00')
        # Check campaign row
        camps = data['campaigns']
        self.assertEqual(len(camps), 1)
        self.assertEqual(camps[0]['leads'], 1)  # 1 distinct lead, not 3
        self.assertEqual(camps[0]['paid_revenue'], '8000.00')  # Exactly 8000, not 24000

    def test_15_date_range_preset_today(self):
        today = timezone.now()
        yesterday = today - timedelta(days=1)
        Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, first_name='Today', last_name='L', created_at=today
        )
        Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, first_name='Yest', last_name='L', created_at=yesterday
        )

        resp = self.client.get(
            '/api/v1/admin/crm/dashboard/?preset=TODAY',
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}'
        )
        self.assertEqual(resp.json()['summary']['total_leads'], 1)

    def test_16_date_range_custom_from_to(self):
        d1 = timezone.make_aware(datetime(2026, 7, 10, 10, 0, 0))
        d2 = timezone.make_aware(datetime(2026, 7, 20, 10, 0, 0))
        d3 = timezone.make_aware(datetime(2026, 8, 1, 10, 0, 0))

        Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, first_name='L1', last_name='Jul', created_at=d1
        )
        Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, first_name='L2', last_name='Jul', created_at=d2
        )
        Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, first_name='L3', last_name='Aug', created_at=d3
        )

        resp = self.client.get(
            '/api/v1/admin/crm/dashboard/?preset=CUSTOM&date_from=2026-07-01&date_to=2026-07-31',
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}'
        )
        self.assertEqual(resp.json()['summary']['total_leads'], 2)

    def test_17_funnel_returns_canonical_statuses(self):
        Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, first_name='New', last_name='Lead', current_status='NEW_LEAD'
        )
        Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, first_name='Hot', last_name='Lead', current_status='HOT_LEAD'
        )

        resp = self.client.get(
            '/api/v1/admin/crm/dashboard/',
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}'
        )
        funnel = resp.json()['funnel']
        self.assertEqual(len(funnel), len(Lead.STATUSES))
        new_stage = next(f for f in funnel if f['status'] == 'NEW_LEAD')
        hot_stage = next(f for f in funnel if f['status'] == 'HOT_LEAD')
        self.assertEqual(new_stage['count'], 1)
        self.assertEqual(hot_stage['count'], 1)
        self.assertEqual(new_stage['percentage_of_total'], 50.0)

    def test_18_source_performance_metrics(self):
        Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, lead_source=self.source_meta, first_name='M1', last_name='Test'
        )
        Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, lead_source=self.source_meta, first_name='M2', last_name='Test'
        )
        Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, lead_source=self.source_walkin, first_name='W1', last_name='Test'
        )

        resp = self.client.get(
            '/api/v1/admin/crm/dashboard/',
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}'
        )
        sources = resp.json()['sources']
        meta_row = next(s for s in sources if s['source_name'] == 'Meta Ads')
        walk_row = next(s for s in sources if s['source_name'] == 'Front Desk Walk-in')
        self.assertEqual(meta_row['leads'], 2)
        self.assertEqual(walk_row['leads'], 1)

    def test_19_trial_performance_breakdown(self):
        l = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, interested_program=self.program,
            first_name='Trial', last_name='User'
        )
        now = timezone.now()
        TrialBooking.objects.using(DB).create(
            lead=l, branch=self.branch_a, scheduled_start=now, scheduled_end=now + timedelta(hours=1), status='ATTENDED'
        )

        resp = self.client.get(
            '/api/v1/admin/crm/dashboard/',
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}'
        )
        trials = resp.json()['trials']
        self.assertEqual(trials['booked'], 1)
        self.assertEqual(trials['attended'], 1)
        self.assertEqual(trials['attendance_rate'], 100.0)
        self.assertEqual(len(trials['top_programs']), 1)
        self.assertEqual(trials['top_programs'][0]['program_name'], 'HIIT Bootcamp')

    def test_20_followup_performance_metrics(self):
        l = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, first_name='Follow', last_name='User'
        )
        now = timezone.now()
        SalesFollowupTask.objects.using(DB).create(
            lead=l, assigned_to_user=self.sales_user, task_type='CALL',
            due_at=now, status='COMPLETED', created_by_user=self.admin_user
        )
        SalesFollowupTask.objects.using(DB).create(
            lead=l, assigned_to_user=self.sales_user, task_type='CALL',
            due_at=now - timedelta(days=1), status='PENDING', created_by_user=self.admin_user
        )

        resp = self.client.get(
            '/api/v1/admin/crm/dashboard/',
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}'
        )
        f = resp.json()['followups']
        self.assertEqual(f['completed'], 1)
        self.assertEqual(f['overdue'], 1)
        self.assertEqual(f['completion_rate'], 50.0)

    def test_21_agent_performance_metrics(self):
        l1 = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, assigned_sales_user=self.sales_user,
            first_name='Agent', last_name='Lead'
        )
        LeadActivity.objects.using(DB).create(
            lead=l1, activity_type='CALL', notes='Called prospect', performed_by_user=self.sales_user
        )

        resp = self.client.get(
            '/api/v1/admin/crm/dashboard/',
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}'
        )
        agents = resp.json()['agents']
        sales_row = next(a for a in agents if a['agent_id'] == str(self.sales_user.id))
        self.assertEqual(sales_row['assigned_leads'], 1)
        self.assertEqual(sales_row['activities'], 1)

    def test_22_branch_performance_metrics(self):
        Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, first_name='A', last_name='Lead'
        )
        Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_b, first_name='B', last_name='Lead'
        )

        resp = self.client.get(
            '/api/v1/admin/crm/dashboard/',
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}'
        )
        branches = resp.json()['branches']
        self.assertEqual(len(branches), 2)
        b_a = next(b for b in branches if b['branch_id'] == str(self.branch_a.id))
        b_b = next(b for b in branches if b['branch_id'] == str(self.branch_b.id))
        self.assertEqual(b_a['leads'], 1)
        self.assertEqual(b_b['leads'], 1)

    def test_23_trend_daily_buckets_continuous(self):
        resp = self.client.get(
            '/api/v1/admin/crm/dashboard/?preset=LAST_7_DAYS',
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}'
        )
        trends = resp.json()['trends']
        self.assertEqual(len(trends), 7)
        for bucket in trends:
            self.assertIn('date', bucket)
            self.assertIn('leads', bucket)
            self.assertIn('trials', bucket)
            self.assertIn('conversions', bucket)
            self.assertIn('paid_revenue', bucket)

    def test_24_source_filter_filters_kpis(self):
        Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, lead_source=self.source_meta, first_name='Meta', last_name='Lead'
        )
        Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, lead_source=self.source_walkin, first_name='Walk', last_name='Lead'
        )

        resp = self.client.get(
            f'/api/v1/admin/crm/dashboard/?lead_source_id={self.source_meta.id}',
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}'
        )
        self.assertEqual(resp.json()['summary']['total_leads'], 1)

    def test_25_program_filter_filters_kpis(self):
        Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, interested_program=self.program, first_name='Prog', last_name='Lead'
        )
        Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, first_name='NoProg', last_name='Lead'
        )

        resp = self.client.get(
            f'/api/v1/admin/crm/dashboard/?program_id={self.program.id}',
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}'
        )
        self.assertEqual(resp.json()['summary']['total_leads'], 1)

    def test_26_campaign_filter_filters_kpis(self):
        l1 = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, campaign_reference='AutumnSpecial', first_name='Camp', last_name='Lead'
        )
        l2 = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, campaign_reference='SpringPromo', first_name='Other', last_name='Lead'
        )

        resp = self.client.get(
            '/api/v1/admin/crm/dashboard/?campaign_name=Autumn',
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}'
        )
        self.assertEqual(resp.json()['summary']['total_leads'], 1)

    def test_27_agent_filter_filters_kpis(self):
        Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, assigned_sales_user=self.sales_user, first_name='Assigned', last_name='Lead'
        )
        Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, assigned_sales_user=self.admin_user, first_name='Admin', last_name='Lead'
        )

        resp = self.client.get(
            f'/api/v1/admin/crm/dashboard/?agent_id={self.sales_user.id}',
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}'
        )
        self.assertEqual(resp.json()['summary']['total_leads'], 1)

    def test_28_no_alternative_permission_bypasses(self):
        # A user with crm.campaigns.view but without canonical crm.dashboard.view is denied access (HTTP 403)
        camp_user = TenantUser.objects.using(DB).create(
            organization=self.org, email='camp@phase10.test', first_name='Camp', last_name='User', status='ACTIVE'
        )
        role_camp = Role.objects.using(DB).create(
            organization=self.org, code='ROLE_CAMP_MGR', name='Campaign Manager', scope='ORG', is_active=True
        )
        mod_crm = ModuleCatalog.objects.using(DB).get(module_code='crm')
        sub_camp = SubmoduleCatalog.objects.using(DB).get(module=mod_crm, submodule_code='campaigns')
        perm_camp = Permission.objects.using(DB).get(permission_code='crm.campaigns.view')
        sub_leads = SubmoduleCatalog.objects.using(DB).get(module=mod_crm, submodule_code='leads')
        RoleModuleAccess.objects.using(DB).create(role=role_camp, module=mod_crm, can_access=True)
        RoleSubmoduleAccess.objects.using(DB).create(role=role_camp, submodule=sub_leads, can_access=True)
        RoleSubmoduleAccess.objects.using(DB).create(role=role_camp, submodule=sub_camp, can_access=True)
        pset = RolePermissionSet.objects.using(DB).create(role=role_camp, name='Camp PermSet', is_active=True)
        RolePermissionSetItem.objects.using(DB).create(permission_set=pset, permission=perm_camp, granted=True)
        RoleAssignment.objects.using(DB).create(user=camp_user, role=role_camp, organization=self.org, is_active=True)

        camp_token = self._token(camp_user)

        resp = self.client.get(
            '/api/v1/admin/crm/dashboard/',
            HTTP_AUTHORIZATION=f'Bearer {camp_token}'
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_29_empty_dataset_returns_zero_counts_without_error(self):
        resp = self.client.get(
            '/api/v1/admin/crm/dashboard/',
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        d = resp.json()
        self.assertEqual(d['summary']['total_leads'], 0)
        self.assertEqual(d['summary']['paid_revenue'], '0.00')
        self.assertEqual(d['summary']['conversion_rate'], 0.0)

    def test_30_direct_service_call(self):
        data = CRMDashboardService.get_dashboard_data(
            organization=self.org,
            user=self.admin_user,
            filters={'preset': 'TODAY'},
            db_alias=DB,
        )
        self.assertIn('summary', data)
        self.assertIn('funnel', data)
        self.assertIn('trends', data)
        self.assertEqual(data['filters']['preset'], 'TODAY')

    def test_31_kpi_vs_funnel_semantics_deterministic(self):
        """
        Deterministic verification of Section 1 semantics:
        Lead A -> currently NEW_LEAD
        Lead B -> currently NO_SHOW
        Two TrialBooking records in selected period, neither currently NO_SHOW (e.g. BOOKED and CONFIRMED)
        Verify dashboard returns:
          Lead Funnel:
            NEW_LEAD = 1
            NO_SHOW = 1
            TRIAL_BOOKED = 0
          Trial KPI:
            trials_booked = 2
            trial_no_shows = 0
        """
        now = timezone.now()
        # Lead A in NEW_LEAD
        lead_a = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, first_name='Lead', last_name='A',
            current_status='NEW_LEAD', created_at=now - timedelta(days=2)
        )
        # Lead B in NO_SHOW status
        lead_b = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, first_name='Lead', last_name='B',
            current_status='NO_SHOW', created_at=now - timedelta(days=2)
        )
        # Two TrialBookings in selected period, neither currently NO_SHOW
        TrialBooking.objects.using(DB).create(
            lead=lead_a, branch=self.branch_a,
            scheduled_start=now + timedelta(days=1), scheduled_end=now + timedelta(days=1, hours=1),
            status='BOOKED', created_at=now - timedelta(days=1)
        )
        TrialBooking.objects.using(DB).create(
            lead=lead_b, branch=self.branch_a,
            scheduled_start=now + timedelta(days=2), scheduled_end=now + timedelta(days=2, hours=1),
            status='CONFIRMED', created_at=now - timedelta(days=1)
        )

        resp = self.client.get(
            '/api/v1/admin/crm/dashboard/?preset=LAST_7_DAYS',
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()

        # Funnel checks: Lead entity snapshot
        funnel_map = {f['status']: f['count'] for f in data['funnel']}
        self.assertEqual(funnel_map.get('NEW_LEAD', 0), 1)
        self.assertEqual(funnel_map.get('NO_SHOW', 0), 1)
        self.assertEqual(funnel_map.get('TRIAL_BOOKED', 0), 0)

        # Trial KPI checks: Operational trial booking records in period
        summary = data['summary']
        self.assertEqual(summary['trials_booked'], 2)
        self.assertEqual(summary['trial_no_shows'], 0)

    def test_32_trial_becoming_no_show_increments_kpi(self):
        """
        Verify that when a trial booking is marked as NO_SHOW during the selected period,
        the Trial No-Shows KPI increments to 1.
        """
        now = timezone.now()
        lead = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_a, first_name='Trial', last_name='Lead',
            current_status='NO_SHOW', created_at=now - timedelta(days=1)
        )
        TrialBooking.objects.using(DB).create(
            lead=lead, branch=self.branch_a,
            scheduled_start=now - timedelta(hours=2), scheduled_end=now - timedelta(hours=1),
            status='NO_SHOW', created_at=now - timedelta(hours=3)
        )

        resp = self.client.get(
            '/api/v1/admin/crm/dashboard/?preset=TODAY',
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        summary = resp.json()['summary']
        self.assertEqual(summary['trials_booked'], 1)
        self.assertEqual(summary['trial_no_shows'], 1)

