"""
backend/tests/test_member_directory_filters.py
Targeted tests for Member Directory Filters, Quick Views, Search, Pagination,
Branch Scoping, Status Interaction, and Edge-Case Reliability.
"""

from decimal import Decimal
from datetime import date, timedelta
import uuid
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework import status

from apps.authentication.views import _build_tenant_token
from apps.master.models import Tenant, TenantDataSource, ProductModule, TenantModule, SaasPlan, TenantSubscription
from apps.tenant_core.context import set_tenant_db_alias
from apps.tenant_core.models_org import Organization, Branch, Location
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
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_catalog import ProgramCategory, Program, Package, PackageVersion, PackagePrice
from apps.tenant_core.models_commerce import Order, OrderItem, PaymentTransaction
from apps.tenant_core.models_memberships import Membership, MembershipFreeze
from apps.tenant_core.services_commerce import CommerceService


class MemberDirectoryFiltersTestCase(APITestCase):
    databases = '__all__'

    def setUp(self):
        set_tenant_db_alias('tenant_test')

        # Master setup
        self.tenant = Tenant.objects.using('default').create(
            code='FILTER-TEST-TENANT',
            name='Filter Test Gym',
            slug='filter-test-gym',
            status='ACTIVE',
        )
        self.ds = TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            db_name='tenant_test',
            database_name='tenant_test',
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )
        self.plan = SaasPlan.objects.using('default').create(
            name='Enterprise Plan',
            code='ENTERPRISE_FILTERS',
            tier='ENTERPRISE',
            status='ACTIVE',
        )
        TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
        )
        self.prod_mod_core, _ = ProductModule.objects.using('default').get_or_create(
            code='core',
            defaults={'name': 'Core Module', 'status': 'ACTIVE'},
        )
        TenantModule.objects.using('default').create(
            tenant=self.tenant,
            module=self.prod_mod_core,
            is_enabled=True,
            availability_mode='ALL_BRANCHES',
        )

        # Tenant org and branches
        self.org = Organization.objects.using('tenant_test').create(
            name='Filter Test Org',
            code='FLT-01',
            status='ACTIVE',
        )
        self.loc = Location.objects.using('tenant_test').create(
            organization=self.org,
            code='FLT-LOC',
            name='Filter Location',
            status='ACTIVE',
        )
        self.branch1 = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            name='Downtown Branch',
            code='FLT-DT',
            status='ACTIVE',
        )
        self.branch2 = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            name='Uptown Branch',
            code='FLT-UP',
            status='ACTIVE',
        )

        # Admin user
        self.admin_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='admin@filtertest.com',
            first_name='Admin',
            last_name='Boss',
            status='ACTIVE',
            is_login_allowed=True,
        )
        self.admin_user.set_password('Secret123!')
        self.admin_user.save(using='tenant_test')

        self.admin_role = Role.objects.using('tenant_test').create(
            organization=self.org,
            name='Org Admin',
            code='ORG_ADMIN',
            is_system_role=True,
            scope='ORG',
            is_active=True,
        )
        RoleAssignment.objects.using('tenant_test').create(
            organization=self.org,
            user=self.admin_user,
            role=self.admin_role,
            is_active=True,
        )

        # RBAC permissions
        self.mod_cat, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(
            module_code='core',
            defaults={'name': 'Core Module', 'code': 'core', 'display_order': 1}
        )
        self.sub_cat, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=self.mod_cat,
            submodule_code='users',
            defaults={'name': 'Users Submodule', 'code': 'users'}
        )
        self.perm_users_view, _ = Permission.objects.using('tenant_test').get_or_create(
            permission_code='core.users.view',
            defaults={
                'source_permission_id': uuid.uuid4(),
                'module': self.mod_cat,
                'submodule': self.sub_cat,
                'code': 'core.users.view',
                'label': 'View Users',
                'action': 'view',
                'is_active': True,
            }
        )
        RoleModuleAccess.objects.using('tenant_test').get_or_create(
            role=self.admin_role, module=self.mod_cat, defaults={'can_access': True}
        )
        RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(
            role=self.admin_role, submodule=self.sub_cat, defaults={'can_access': True}
        )
        ps = RolePermissionSet.objects.using('tenant_test').create(
            role=self.admin_role,
            organization=self.org,
            name="ADMIN_PSET",
            is_active=True,
        )
        RolePermissionSetItem.objects.using('tenant_test').create(
            permission_set=ps,
            permission=self.perm_users_view,
            granted=True,
            is_allowed=True,
        )

        # Catalog setup
        self.cat = ProgramCategory.objects.using('tenant_test').create(
            organization=self.org, name='Fitness', code='FIT'
        )
        self.prog = Program.objects.using('tenant_test').create(
            organization=self.org, category=self.cat, name='Strength Program', code='STR'
        )
        self.pkg = Package.objects.using('tenant_test').create(
            organization=self.org, program=self.prog, name='Gold Plan', code='GOLD'
        )
        self.pv = PackageVersion.objects.using('tenant_test').create(
            package=self.pkg,
            version_number=1,
            name_snapshot='Gold Plan v1',
            duration_value=1,
            duration_unit='MONTH',
            effective_from=timezone.now(),
            status='ACTIVE',
            created_by_user=self.admin_user,
        )
        self.price = PackagePrice.objects.using('tenant_test').create(
            package_version=self.pv,
            base_price=Decimal('10000.00'),
            currency='INR',
            effective_from=timezone.now(),
            status='ACTIVE',
            created_by_user=self.admin_user,
        )

        # Generate auth token
        refresh = _build_tenant_token(
            user=self.admin_user,
            tenant=self.tenant,
            db_alias='tenant_test',
        )
        self.token = str(refresh.access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.token}")

    def tearDown(self):
        super().tearDown()
        from django.db import connections
        dynamic_aliases = [
            alias for alias in list(connections)
            if alias.startswith('tenant_') and alias != 'tenant_test'
        ]
        for alias in dynamic_aliases:
            try:
                connections[alias].close()
            except Exception:
                pass
            connections.databases.pop(alias, None)
            if hasattr(connections._connections, alias):
                delattr(connections._connections, alias)

    def _create_member(self, email, first_name, last_name, phone, branch, member_number=None):
        u = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email=email,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            status='ACTIVE',
            is_login_allowed=False,
        )
        p = UserProfile.objects.using('tenant_test').create(
            user=u,
            member_number=member_number or f"MEM-{uuid.uuid4().hex[:6].upper()}",
            first_name_snapshot=first_name,
            last_name_snapshot=last_name,
            preferred_branch=branch,
            member_type='MEMBER',
            member_status='ACTIVE',
            acquisition_source='WALK_IN',
        )
        return p

    def _create_order(self, profile, total_amount, branch, status='PENDING_PAYMENT'):
        order = CommerceService.create_order(
            branch=branch,
            items_data=[{
                'item_type': 'PACKAGE',
                'package_id': self.pkg.id,
                'package_version_id': self.pv.id,
                'package_price_id': self.price.id,
                'item_name_snapshot': self.pkg.name,
                'unit_price': str(total_amount),
                'quantity': '1.00',
                'tax_percent': '0.000',
            }],
            user_profile=profile,
            created_by=self.admin_user,
            db_alias='tenant_test',
        )
        if status != 'PENDING_PAYMENT':
            order.status = status
            order.save(using='tenant_test', update_fields=['status'])
        return order

    def _create_membership(self, profile, branch, status='ACTIVE', start_date=None, end_date=None):
        today = timezone.now().date()
        s_date = start_date or today
        e_date = end_date or (s_date + timedelta(days=60))
        return Membership.objects.using('tenant_test').create(
            user_profile=profile,
            program=self.prog,
            package=self.pkg,
            package_version=self.pv,
            package_price=self.price,
            purchase_branch=branch,
            home_branch=branch,
            membership_number=f"MSHIP-{uuid.uuid4().hex[:6].upper()}",
            start_date=s_date,
            end_date=e_date,
            status=status,
            activated_at=timezone.now(),
        )

    # =========================================================================
    # 1. All Members & Staff Exclusion
    # =========================================================================
    def test_01_all_members_excludes_staff(self):
        # Genuine customer member
        m1 = self._create_member('cust1@test.com', 'Alice', 'Smith', '+919000000001', self.branch1)
        self._create_membership(m1, self.branch1)

        # Staff-only user (no member_number, no acquisition_source, no memberships)
        staff_u = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='trainer@test.com',
            first_name='Coach',
            last_name='Mike',
            user_type='STAFF',
            status='ACTIVE',
        )
        UserProfile.objects.using('tenant_test').create(
            user=staff_u,
            first_name_snapshot='Coach',
            last_name_snapshot='Mike',
            member_number=None,
            acquisition_source=None,
        )

        res = self.client.get('/api/v1/tenant/members/?quick_view=all')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        ids = [m['id'] for m in res.data['results']]
        self.assertIn(str(m1.id), ids)
        self.assertEqual(res.data['count'], 1)

    # =========================================================================
    # 2. Active Membership Canonical Semantics
    # =========================================================================
    def test_02_active_canonical_semantics(self):
        # Member with active account but no membership -> NOT active
        m_no_plan = self._create_member('noplan@test.com', 'No', 'Plan', '+919000000002', self.branch1)

        # Member with active membership
        m_active = self._create_member('active@test.com', 'Active', 'Member', '+919000000003', self.branch1)
        self._create_membership(m_active, self.branch1, status='ACTIVE', end_date=timezone.now().date() + timedelta(days=90))

        res = self.client.get('/api/v1/tenant/members/?quick_view=active')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        ids = [m['id'] for m in res.data['results']]
        self.assertIn(str(m_active.id), ids)
        self.assertNotIn(str(m_no_plan.id), ids)

    # =========================================================================
    # 3 & 4. Expiring Soon and Boundaries
    # =========================================================================
    def test_03_and_04_expiring_soon_boundaries(self):
        today = timezone.now().date()

        # 1. Expiring today (boundary: today) -> INCLUDED
        m_today = self._create_member('today@test.com', 'Exp', 'Today', '+919000000010', self.branch1)
        self._create_membership(m_today, self.branch1, status='ACTIVE', end_date=today)

        # 2. Expiring in 30 days (boundary: threshold last day) -> INCLUDED
        m_30d = self._create_member('in30@test.com', 'Exp', 'Thirty', '+919000000011', self.branch1)
        self._create_membership(m_30d, self.branch1, status='ACTIVE', end_date=today + timedelta(days=30))

        # 3. Expiring in 31 days (boundary: threshold + 1 day) -> EXCLUDED
        m_31d = self._create_member('in31@test.com', 'Exp', 'ThirtyOne', '+919000000012', self.branch1)
        self._create_membership(m_31d, self.branch1, status='ACTIVE', end_date=today + timedelta(days=31))

        # 4. Expired yesterday (boundary: expired yesterday) -> EXCLUDED
        m_yest = self._create_member('yest@test.com', 'Exp', 'Yesterday', '+919000000013', self.branch1)
        self._create_membership(m_yest, self.branch1, status='EXPIRED', end_date=today - timedelta(days=1))

        res = self.client.get('/api/v1/tenant/members/?quick_view=expiring_soon')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        ids = [m['id'] for m in res.data['results']]
        self.assertIn(str(m_today.id), ids)
        self.assertIn(str(m_30d.id), ids)
        self.assertNotIn(str(m_31d.id), ids)
        self.assertNotIn(str(m_yest.id), ids)

    # =========================================================================
    # 5 & 6. Frozen: Active freeze vs historical ended freeze
    # =========================================================================
    def test_05_and_06_frozen_lifecycle(self):
        today = timezone.now().date()

        # Currently Frozen Member
        m_frozen = self._create_member('frozen@test.com', 'Fro', 'Zen', '+919000000020', self.branch1)
        mem_f = self._create_membership(m_frozen, self.branch1, status='FROZEN')
        MembershipFreeze.objects.using('tenant_test').create(
            membership=mem_f,
            freeze_from=today - timedelta(days=2),
            freeze_until=today + timedelta(days=10),
            status='ACTIVE',
            approved_by_user=self.admin_user,
        )

        # Historical ended freeze, now ACTIVE
        m_hist = self._create_member('histfrz@test.com', 'Hist', 'Freeze', '+919000000021', self.branch1)
        mem_h = self._create_membership(m_hist, self.branch1, status='ACTIVE')
        MembershipFreeze.objects.using('tenant_test').create(
            membership=mem_h,
            freeze_from=today - timedelta(days=40),
            freeze_until=today - timedelta(days=20),
            status='COMPLETED',
            approved_by_user=self.admin_user,
        )

        res = self.client.get('/api/v1/tenant/members/?quick_view=frozen')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        ids = [m['id'] for m in res.data['results']]
        self.assertIn(str(m_frozen.id), ids)
        self.assertNotIn(str(m_hist.id), ids)

    # =========================================================================
    # 7 & 8. Expired: Current Active wins over Historical Expired
    # =========================================================================
    def test_07_and_08_expired_and_active_wins(self):
        today = timezone.now().date()

        # Truly Expired Member
        m_expired = self._create_member('expired@test.com', 'Old', 'Timer', '+919000000030', self.branch1)
        self._create_membership(m_expired, self.branch1, status='EXPIRED', end_date=today - timedelta(days=10))

        # Member with Membership A (Expired) AND Membership B (Active)
        m_renewed = self._create_member('renewed@test.com', 'Re', 'Newed', '+919000000031', self.branch1)
        self._create_membership(m_renewed, self.branch1, status='EXPIRED', end_date=today - timedelta(days=30))
        self._create_membership(m_renewed, self.branch1, status='ACTIVE', end_date=today + timedelta(days=60))

        # Test Expired Quick View
        res_exp = self.client.get('/api/v1/tenant/members/?quick_view=expired')
        self.assertEqual(res_exp.status_code, status.HTTP_200_OK)
        exp_ids = [m['id'] for m in res_exp.data['results']]
        self.assertIn(str(m_expired.id), exp_ids)
        self.assertNotIn(str(m_renewed.id), exp_ids, "Active membership must win over historical expired")

        # Test Active Quick View includes renewed member
        res_act = self.client.get('/api/v1/tenant/members/?quick_view=active')
        self.assertEqual(res_act.status_code, status.HTTP_200_OK)
        act_ids = [m['id'] for m in res_act.data['results']]
        self.assertIn(str(m_renewed.id), act_ids)

    # =========================================================================
    # 9, 10, 11. Outstanding Balance Calculations & Exclusions
    # =========================================================================
    def test_09_to_11_outstanding_balance_semantics(self):
        # 9. Order ₹30,000, Paid ₹20,000 -> Outstanding ₹10,000 (INCLUDED)
        m_partial = self._create_member('partial@test.com', 'Part', 'Paid', '+919000000040', self.branch1)
        ord1 = self._create_order(m_partial, 30000, self.branch1, status='PARTIALLY_PAID')
        PaymentTransaction.objects.using('tenant_test').create(
            order=ord1,
            user_profile=m_partial,
            amount=Decimal('20000.00'),
            currency='INR',
            provider='CASH',
            status='SUCCESS',
        )

        # 10. Order ₹15,000, Paid ₹15,000 -> Fully Paid (EXCLUDED)
        m_paid = self._create_member('fullypaid@test.com', 'Fully', 'Paid', '+919000000041', self.branch1)
        ord2 = self._create_order(m_paid, 15000, self.branch1, status='PAID')
        PaymentTransaction.objects.using('tenant_test').create(
            order=ord2,
            user_profile=m_paid,
            amount=Decimal('15000.00'),
            currency='INR',
            provider='CASH',
            status='SUCCESS',
        )

        # 11. Order ₹25,000, Cancelled (EXCLUDED)
        m_cancelled = self._create_member('cancelled@test.com', 'Canc', 'Order', '+919000000042', self.branch1)
        self._create_order(m_cancelled, 25000, self.branch1, status='CANCELLED')

        res = self.client.get('/api/v1/tenant/members/?quick_view=outstanding')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        ids = [m['id'] for m in res.data['results']]
        self.assertIn(str(m_partial.id), ids)
        self.assertNotIn(str(m_paid.id), ids)
        self.assertNotIn(str(m_cancelled.id), ids)

    # =========================================================================
    # 12, 13, 14, 15. Server-side Search Tests
    # =========================================================================
    def test_12_to_15_search_member_number_name_phone_email(self):
        m = self._create_member(
            email='clark.kent@dailyplanet.com',
            first_name='Clark',
            last_name='Kent',
            phone='+919876500001',
            branch=self.branch1,
            member_number='MEM-KRYPTON-01'
        )
        self._create_membership(m, self.branch1)

        # 12. Search by member number
        res_num = self.client.get('/api/v1/tenant/members/?search=KRYPTON')
        self.assertEqual(res_num.data['count'], 1)
        self.assertEqual(res_num.data['results'][0]['id'], str(m.id))

        # 13. Search by name (first, last, full)
        res_first = self.client.get('/api/v1/tenant/members/?search=Clark')
        self.assertEqual(res_first.data['count'], 1)
        res_full = self.client.get('/api/v1/tenant/members/?search=Clark Kent')
        self.assertEqual(res_full.data['count'], 1)

        # 14. Search by phone
        res_phone = self.client.get('/api/v1/tenant/members/?search=9876500001')
        self.assertEqual(res_phone.data['count'], 1)

        # 15. Search by email
        res_email = self.client.get('/api/v1/tenant/members/?search=clark.kent@dailyplanet.com')
        self.assertEqual(res_email.data['count'], 1)

        # Nonexistent value
        res_none = self.client.get('/api/v1/tenant/members/?search=BatmanNotExist')
        self.assertEqual(res_none.data['count'], 0)

    # =========================================================================
    # 16. Branch / Location Filter
    # =========================================================================
    def test_16_branch_filter(self):
        m_dt = self._create_member('dt@test.com', 'Down', 'Town', '+919000000051', self.branch1)
        self._create_membership(m_dt, self.branch1)

        m_up = self._create_member('up@test.com', 'Up', 'Town', '+919000000052', self.branch2)
        self._create_membership(m_up, self.branch2)

        res = self.client.get(f"/api/v1/tenant/members/?location={self.branch1.id}")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        ids = [m['id'] for m in res.data['results']]
        self.assertIn(str(m_dt.id), ids)
        self.assertNotIn(str(m_up.id), ids)

    # =========================================================================
    # 17. Status Filter
    # =========================================================================
    def test_17_status_filter(self):
        m_act = self._create_member('s_act@test.com', 'St', 'Active', '+919000000061', self.branch1)
        self._create_membership(m_act, self.branch1, status='ACTIVE')

        m_frz = self._create_member('s_frz@test.com', 'St', 'Frozen', '+919000000062', self.branch1)
        self._create_membership(m_frz, self.branch1, status='FROZEN')

        res_a = self.client.get('/api/v1/tenant/members/?status=ACTIVE')
        ids_a = [m['id'] for m in res_a.data['results']]
        self.assertIn(str(m_act.id), ids_a)
        self.assertNotIn(str(m_frz.id), ids_a)

        res_f = self.client.get('/api/v1/tenant/members/?status=FROZEN')
        ids_f = [m['id'] for m in res_f.data['results']]
        self.assertIn(str(m_frz.id), ids_f)
        self.assertNotIn(str(m_act.id), ids_f)

    # =========================================================================
    # 18 & 19. Combined Filters: Search + Quick View & Branch + Quick View
    # =========================================================================
    def test_18_and_19_filter_combinations(self):
        # Member 1: Downtown branch, Active, Name: "Diana Prince"
        m1 = self._create_member('diana@test.com', 'Diana', 'Prince', '+919000000071', self.branch1)
        self._create_membership(m1, self.branch1, status='ACTIVE')

        # Member 2: Uptown branch, Active, Name: "Diana Ross"
        m2 = self._create_member('diana.ross@test.com', 'Diana', 'Ross', '+919000000072', self.branch2)
        self._create_membership(m2, self.branch2, status='ACTIVE')

        # Member 3: Downtown branch, Expired, Name: "Diana Barry"
        m3 = self._create_member('diana.barry@test.com', 'Diana', 'Barry', '+919000000073', self.branch1)
        self._create_membership(m3, self.branch1, status='EXPIRED', end_date=timezone.now().date() - timedelta(days=5))

        # Search "Diana" + quick_view=active -> m1 and m2
        res_sq = self.client.get('/api/v1/tenant/members/?search=Diana&quick_view=active')
        self.assertEqual(res_sq.data['count'], 2)
        sq_ids = [m['id'] for m in res_sq.data['results']]
        self.assertIn(str(m1.id), sq_ids)
        self.assertIn(str(m2.id), sq_ids)
        self.assertNotIn(str(m3.id), sq_ids)

        # Branch1 + quick_view=active + search "Diana" -> only m1
        res_all = self.client.get(f"/api/v1/tenant/members/?location={self.branch1.id}&quick_view=active&search=Diana")
        self.assertEqual(res_all.data['count'], 1)
        self.assertEqual(res_all.data['results'][0]['id'], str(m1.id))

    # =========================================================================
    # 20. Pagination and Stale Page Self-Healing Contract
    # =========================================================================
    def test_20_pagination_and_stale_page_recovery(self):
        for i in range(5):
            m = self._create_member(f"page{i}@test.com", f"Page{i}", "User", f"+9190000001{i}", self.branch1)
            self._create_membership(m, self.branch1)

        # Normal page 1 with page_size=2
        res1 = self.client.get('/api/v1/tenant/members/?page=1&page_size=2')
        self.assertEqual(res1.data['count'], 5)
        self.assertEqual(len(res1.data['results']), 2)
        self.assertEqual(res1.data['total_pages'], 3)

        # Stale page 10 requested -> self-heals to page 1 rather than empty
        res_stale = self.client.get('/api/v1/tenant/members/?page=10&page_size=2')
        self.assertEqual(res_stale.data['count'], 5)
        self.assertEqual(res_stale.data['page'], 1)
        self.assertEqual(len(res_stale.data['results']), 2)

    # =========================================================================
    # 21. Member vs Staff Separation
    # =========================================================================
    def test_21_member_vs_staff_separation(self):
        # 1 Admin user (already created in setUp)
        # 1 Staff user
        staff_u = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='frontdesk@test.com',
            first_name='Front',
            last_name='Desk',
            user_type='STAFF',
            status='ACTIVE',
        )
        UserProfile.objects.using('tenant_test').create(
            user=staff_u,
            first_name_snapshot='Front',
            last_name_snapshot='Desk',
            member_number=None,
            acquisition_source=None,
        )

        res = self.client.get('/api/v1/tenant/members/')
        self.assertEqual(res.data['count'], 0, "No members should be returned when only staff exist")

    # =========================================================================
    # 22 & 23. Tenant & Organization Isolation
    # =========================================================================
    def test_22_and_23_organization_isolation(self):
        # Member in current org
        m_my_org = self._create_member('org1@test.com', 'My', 'Org', '+919000000081', self.branch1)
        self._create_membership(m_my_org, self.branch1)

        # Member in another organization
        other_org = Organization.objects.using('tenant_test').create(
            name='Other Org', code='OTHER-ORG', status='ACTIVE'
        )
        other_u = TenantUser.objects.using('tenant_test').create(
            organization=other_org,
            email='otherorg@test.com',
            first_name='Other',
            last_name='OrgUser',
            status='ACTIVE',
        )
        UserProfile.objects.using('tenant_test').create(
            user=other_u,
            member_number='MEM-OTHER-01',
            first_name_snapshot='Other',
            last_name_snapshot='OrgUser',
            acquisition_source='ONLINE',
        )

        res = self.client.get('/api/v1/tenant/members/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        ids = [m['id'] for m in res.data['results']]
        self.assertIn(str(m_my_org.id), ids)
        self.assertEqual(res.data['count'], 1)

    # =========================================================================
    # 24. Branch Scoping
    # =========================================================================
    def test_24_branch_scoping(self):
        m1 = self._create_member('br1@test.com', 'Branch', 'One', '+919000000091', self.branch1)
        self._create_membership(m1, self.branch1)

        m2 = self._create_member('br2@test.com', 'Branch', 'Two', '+919000000092', self.branch2)
        self._create_membership(m2, self.branch2)

        res = self.client.get(f"/api/v1/tenant/members/?location={self.branch2.id}")
        self.assertEqual(res.data['count'], 1)
        self.assertEqual(res.data['results'][0]['id'], str(m2.id))

    # =========================================================================
    # 25. Duplicate Row Prevention
    # =========================================================================
    def test_25_duplicate_row_prevention(self):
        # Member with multiple memberships and multiple orders
        m = self._create_member('multi@test.com', 'Multi', 'Join', '+919000000099', self.branch1)
        self._create_membership(m, self.branch1, status='EXPIRED', end_date=timezone.now().date() - timedelta(days=20))
        self._create_membership(m, self.branch1, status='ACTIVE', end_date=timezone.now().date() + timedelta(days=60))
        self._create_order(m, 10000, self.branch1)
        self._create_order(m, 20000, self.branch1)

        res = self.client.get('/api/v1/tenant/members/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['count'], 1)
        self.assertEqual(len(res.data['results']), 1)
