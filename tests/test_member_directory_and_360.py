"""
backend/tests/test_member_directory_and_360.py
Targeted tests for Member Directory, Member 360, Timeline, Passbook, and Lifecycle Actions.
"""

from decimal import Decimal
from datetime import date, timedelta
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework import status

from apps.authentication.views import _build_tenant_token
import uuid
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
from apps.tenant_core.models_catalog import ProgramCategory, Program, Package, PackageVersion, PackagePrice, PackageEntitlementDefinition
from apps.tenant_core.models_commerce import Order, OrderItem, PaymentTransaction, MemberInvoice
from apps.tenant_core.models_memberships import Membership, MembershipContractSnapshot, MembershipEntitlement, MembershipEntitlementLedger
from apps.tenant_core.services_memberships import MembershipLifecycleService
from apps.tenant_core.services_commerce import CommerceService


class MemberDirectoryAnd360TestCase(APITestCase):
    databases = '__all__'

    def setUp(self):
        set_tenant_db_alias('tenant_test')

        # Master setup
        self.tenant = Tenant.objects.using('default').create(
            code='MEM-TEST-TENANT',
            name='Member Test Gym',
            slug='member-test-gym',
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
            code='ENTERPRISE',
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

        # Tenant setup
        self.org = Organization.objects.using('tenant_test').create(
            name='Apex Gyms Org',
            code='APEX-01',
            status='ACTIVE',
        )
        self.loc = Location.objects.using('tenant_test').create(
            organization=self.org,
            code='APX-LOC',
            name='Apex Location',
            status='ACTIVE',
        )
        self.branch1 = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            name='Apex Downtown',
            code='APX-DT',
            status='ACTIVE',
        )
        self.branch2 = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            name='Apex Uptown',
            code='APX-UP',
            status='ACTIVE',
        )

        self.admin_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='admin@apex.test',
            first_name='Admin',
            last_name='User',
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

        # RBAC permissions for core.users
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
        self.perm_users_edit, _ = Permission.objects.using('tenant_test').get_or_create(
            permission_code='core.users.edit',
            defaults={
                'source_permission_id': uuid.uuid4(),
                'module': self.mod_cat,
                'submodule': self.sub_cat,
                'code': 'core.users.edit',
                'label': 'Edit Users',
                'action': 'edit',
                'is_active': True,
            }
        )
        self.perm_users_create, _ = Permission.objects.using('tenant_test').get_or_create(
            permission_code='core.users.create',
            defaults={
                'source_permission_id': uuid.uuid4(),
                'module': self.mod_cat,
                'submodule': self.sub_cat,
                'code': 'core.users.create',
                'label': 'Create Users',
                'action': 'create',
                'is_active': True,
            }
        )
        self.perm_payments_create, _ = Permission.objects.using('tenant_test').get_or_create(
            permission_code='finance.payments.create',
            defaults={
                'source_permission_id': uuid.uuid4(),
                'module': self.mod_cat,
                'submodule': self.sub_cat,
                'code': 'finance.payments.create',
                'label': 'Create Payments',
                'action': 'create',
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
        for p in (self.perm_users_view, self.perm_users_edit, self.perm_users_create, self.perm_payments_create):
            RolePermissionSetItem.objects.using('tenant_test').create(
                permission_set=ps,
                permission=p,
                granted=True,
                is_allowed=True,
            )

        # Staff user without finance.payments.create permission
        self.staff_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='staff@test.com',
            first_name='Staff',
            last_name='User',
            status='ACTIVE',
            is_login_allowed=True,
        )
        self.staff_user.set_password('Secret123!')
        self.staff_user.save(using='tenant_test')

        self.staff_role = Role.objects.using('tenant_test').create(
            organization=self.org,
            name='Front Desk Staff',
            code='FRONT_DESK',
            is_system_role=False,
            scope='ORG',
            is_active=True,
        )
        RoleAssignment.objects.using('tenant_test').create(
            organization=self.org,
            user=self.staff_user,
            role=self.staff_role,
            is_active=True,
        )
        staff_ps = RolePermissionSet.objects.using('tenant_test').create(
            role=self.staff_role,
            organization=self.org,
            name="STAFF_PSET",
            is_active=True,
        )
        for p in (self.perm_users_view, self.perm_users_edit):
            RolePermissionSetItem.objects.using('tenant_test').create(
                permission_set=staff_ps,
                permission=p,
                granted=True,
                is_allowed=True,
            )
        RoleModuleAccess.objects.using('tenant_test').get_or_create(
            role=self.staff_role, module=self.mod_cat, defaults={'can_access': True}
        )
        RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(
            role=self.staff_role, submodule=self.sub_cat, defaults={'can_access': True}
        )

        # Catalog setup
        self.prog_cat = ProgramCategory.objects.using('tenant_test').create(
            organization=self.org,
            name='Fitness Programs',
            code='FIT-PROG',
            status='ACTIVE',
        )
        self.program = Program.objects.using('tenant_test').create(
            organization=self.org,
            category=self.prog_cat,
            name='Strength & Conditioning',
            code='STR-COND',
            status='ACTIVE',
        )
        self.pkg = Package.objects.using('tenant_test').create(
            organization=self.org,
            program=self.program,
            name='Quarterly Unlimited',
            code='QTR-UNLTD',
            status='ACTIVE',
        )
        self.pkg_v1 = PackageVersion.objects.using('tenant_test').create(
            package=self.pkg,
            version_number=1,
            name_snapshot='Quarterly Unlimited v1',
            duration_value=3,
            duration_unit='MONTH',
            effective_from=timezone.now(),
            status='ACTIVE',
            created_by_user=self.admin_user,
        )
        self.pkg_price = PackagePrice.objects.using('tenant_test').create(
            package_version=self.pkg_v1,
            base_price=Decimal('15000.00'),
            currency='INR',
            effective_from=timezone.now(),
            status='ACTIVE',
            created_by_user=self.admin_user,
        )
        self.ent_def_home = PackageEntitlementDefinition.objects.using('tenant_test').create(
            package_version=self.pkg_v1,
            entitlement_type='HOME_BRANCH_SESSION',
            allocated_units=Decimal('36.00'),
            is_unlimited=False,
        )
        self.ent_def_cross = PackageEntitlementDefinition.objects.using('tenant_test').create(
            package_version=self.pkg_v1,
            entitlement_type='CROSS_BRANCH_SESSION',
            allocated_units=Decimal('6.00'),
            is_unlimited=False,
        )

        # Member user & profile
        self.member_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='sarah.connor@test.com',
            phone='+919876543210',
            first_name='Sarah',
            last_name='Connor',
            status='ACTIVE',
            is_login_allowed=True,
        )
        self.profile = UserProfile.objects.using('tenant_test').create(
            user=self.member_user,
            member_number='MEM-00101',
            first_name_snapshot='Sarah',
            last_name_snapshot='Connor',
            gender='F',
            preferred_branch=self.branch1,
            member_status='ACTIVE',
            joining_date=timezone.now().date() - timedelta(days=60),
            date_of_birth=date(1995, 5, 20),
        )

        # Order & Payment
        self.order = CommerceService.create_order(
            branch=self.branch1,
            items_data=[{
                'item_type': 'PACKAGE',
                'package_id': self.pkg.id,
                'package_version_id': self.pkg_v1.id,
                'package_price_id': self.pkg_price.id,
                'item_name_snapshot': 'Quarterly Unlimited v1',
                'unit_price': '15000.00',
                'quantity': '1.00',
                'tax_percent': '18.000',
            }],
            user_profile=self.profile,
            created_by=self.admin_user,
            db_alias='tenant_test',
        )
        # Record partial payment (leaving outstanding)
        CommerceService.record_payment(
            order_id=str(self.order.id),
            amount=Decimal('10000.00'),
            provider='CASH',
            actor=self.admin_user,
            db_alias='tenant_test',
        )

        # Activate membership
        order_item = self.order.items.first()
        self.membership = MembershipLifecycleService.activate_membership_from_order(
            order=self.order,
            order_item=order_item,
            start_date=timezone.now().date(),
            db_alias='tenant_test',
            created_by_user=self.admin_user,
        )

        # Generate auth token
        refresh = _build_tenant_token(
            user=self.admin_user,
            tenant=self.tenant,
            db_alias='tenant_test',
        )
        self.token = str(refresh.access_token)

        refresh_staff = _build_tenant_token(
            user=self.staff_user,
            tenant=self.tenant,
            db_alias='tenant_test',
        )
        self.staff_token = str(refresh_staff.access_token)
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

    def test_member_directory_listing_and_pagination(self):
        """Test GET /api/v1/tenant/members/ with backend pagination, search, and filters."""
        res = self.client.get('/api/v1/tenant/members/?page=1&page_size=10')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn('count', res.data)
        self.assertIn('results', res.data)
        self.assertEqual(res.data['count'], 1)
        self.assertEqual(len(res.data['results']), 1)

        m = res.data['results'][0]
        self.assertEqual(m['id'], str(self.profile.id))
        self.assertEqual(m['name'], 'Sarah Connor')
        self.assertEqual(m['phone'], '+919876543210')
        self.assertEqual(m['email'], 'sarah.connor@test.com')
        self.assertEqual(m['home_branch'], 'Apex Downtown')
        self.assertEqual(m['package_name'], 'Quarterly Unlimited')
        self.assertEqual(m['package_version_name'], 'v1')
        self.assertEqual(m['membership_status'], 'ACTIVE')
        self.assertEqual(m['home_sessions_remaining'], 36)
        self.assertEqual(m['cross_branch_sessions_remaining'], 6)
        # Total was 15000 + 18% tax (2700) = 17700. Paid 10000. Outstanding = 7700.
        self.assertEqual(m['outstanding_balance'], 7700.0)

    def test_member_directory_search(self):
        """Test server-side search by name, phone, member_number."""
        res1 = self.client.get('/api/v1/tenant/members/?search=Sarah')
        self.assertEqual(res1.status_code, status.HTTP_200_OK)
        self.assertEqual(res1.data['count'], 1)

        res2 = self.client.get('/api/v1/tenant/members/?search=9876543210')
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        self.assertEqual(res2.data['count'], 1)

        res3 = self.client.get('/api/v1/tenant/members/?search=MEM-00101')
        self.assertEqual(res3.status_code, status.HTTP_200_OK)
        self.assertEqual(res3.data['count'], 1)

        res4 = self.client.get('/api/v1/tenant/members/?search=NonExistent')
        self.assertEqual(res4.status_code, status.HTTP_200_OK)
        self.assertEqual(res4.data['count'], 0)

    def test_member_directory_quick_views(self):
        """Test quick views: active, outstanding, etc."""
        res_active = self.client.get('/api/v1/tenant/members/?quick_view=active')
        self.assertEqual(res_active.status_code, status.HTTP_200_OK)
        self.assertEqual(res_active.data['count'], 1)

        res_out = self.client.get('/api/v1/tenant/members/?quick_view=outstanding')
        self.assertEqual(res_out.status_code, status.HTTP_200_OK)
        self.assertEqual(res_out.data['count'], 1)

        res_frozen = self.client.get('/api/v1/tenant/members/?quick_view=frozen')
        self.assertEqual(res_frozen.status_code, status.HTTP_200_OK)
        self.assertEqual(res_frozen.data['count'], 0)

    def test_member_360_aggregate(self):
        """Test GET /api/v1/tenant/members/<id>/360/ returns 6 complete sections."""
        res = self.client.get(f"/api/v1/tenant/members/{self.profile.id}/360/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        data = res.data
        self.assertIn('header', data)
        self.assertIn('overview', data)
        self.assertIn('timeline', data)
        self.assertIn('memberships', data)
        self.assertIn('passbook', data)
        self.assertIn('bookings_and_attendance', data)
        self.assertIn('finance', data)
        self.assertIn('health_and_forms', data)

        # Header check
        header = data['header']
        self.assertEqual(header['name'], 'Sarah Connor')
        self.assertEqual(header['outstanding_balance'], 7700.0)
        self.assertEqual(header['home_sessions_remaining'], 36)
        self.assertEqual(header['cross_branch_sessions_remaining'], 6)
        actions = [a['action'] for a in header['available_actions']]
        self.assertIn('FREEZE', actions)
        self.assertIn('EXTEND', actions)
        self.assertIn('COLLECT_PAYMENT', actions)

        # Finance check
        fin = data['finance']
        self.assertEqual(len(fin['orders']), 1)
        self.assertEqual(len(fin['payments']), 1)
        self.assertEqual(fin['summary']['total_outstanding'], 7700.0)

        # Passbook check
        pb = data['passbook']
        self.assertEqual(len(pb['balances']), 2)
        self.assertGreaterEqual(len(pb['ledger']), 2)

    def test_member_timeline_endpoint(self):
        """Test GET /api/v1/tenant/members/<id>/timeline/."""
        res = self.client.get(f"/api/v1/tenant/members/{self.profile.id}/timeline/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn('results', res.data)
        event_types = [e['event_type'] for e in res.data['results']]
        self.assertIn('ORDER_CREATED', event_types)
        self.assertIn('PAYMENT_RECORDED', event_types)
        self.assertIn('MEMBERSHIP_ACTIVATED', event_types)

    def test_lifecycle_actions_freeze_and_unfreeze(self):
        """Test Freeze and Unfreeze actions."""
        # Freeze
        freeze_from = str(timezone.now().date())
        freeze_until = str(timezone.now().date() + timedelta(days=14))
        res_frz = self.client.post(f"/api/v1/tenant/members/{self.profile.id}/freeze/", {
            'freeze_from': freeze_from,
            'freeze_until': freeze_until,
            'reason': 'Medical leave',
        })
        self.assertEqual(res_frz.status_code, status.HTTP_200_OK)
        self.assertTrue(res_frz.data['success'])

        # Verify frozen in 360
        res_360 = self.client.get(f"/api/v1/tenant/members/{self.profile.id}/360/")
        self.assertEqual(res_360.data['header']['status'], 'FROZEN')

        # Unfreeze
        res_unfrz = self.client.post(f"/api/v1/tenant/members/{self.profile.id}/unfreeze/", {
            'reason': 'Returned early from leave',
        })
        self.assertEqual(res_unfrz.status_code, status.HTTP_200_OK)
        self.assertTrue(res_unfrz.data['success'])

        # Verify restored to active
        res_360_2 = self.client.get(f"/api/v1/tenant/members/{self.profile.id}/360/")
        self.assertEqual(res_360_2.data['header']['status'], 'ACTIVE')

    def test_lifecycle_action_collect_outstanding(self):
        """Test collecting outstanding payment."""
        res = self.client.post(f"/api/v1/tenant/members/{self.profile.id}/collect-outstanding/", {
            'amount': '7700.00',
            'provider': 'CASH',
            'payment_method': 'CASH',
        })
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertTrue(res.data['success'])

        # Outstanding balance should now be 0
        res_360 = self.client.get(f"/api/v1/tenant/members/{self.profile.id}/360/")
        self.assertEqual(res_360.data['header']['outstanding_balance'], 0.0)

    def test_lifecycle_action_adjust_entitlement(self):
        """Test controlled session adjustment."""
        res = self.client.post(f"/api/v1/tenant/members/{self.profile.id}/adjust-entitlement/", {
            'entitlement_type': 'HOME_BRANCH_SESSION',
            'units_delta': '5.0',
            'reason_code': 'COMPLIMENTARY_ADDITION',
            'reason_text': 'Compensation for facility maintenance',
        })
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.data['success'])
        self.assertEqual(res.data['balance_after'], 41.0)

    def test_unauthorized_user_cannot_collect_outstanding_gets_403(self):
        """Staff without finance.payments.create receives HTTP 403 Forbidden on payment collection."""
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.staff_token}")
        res = self.client.post(f"/api/v1/tenant/members/{self.profile.id}/collect-outstanding/", {
            'amount': '1000.00',
            'provider': 'CASH',
            'payment_method': 'CASH',
        })
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        # Restore admin credentials
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.token}")

    def test_payment_idempotency_duplicate_submission_blocked(self):
        """Duplicate payment submission with identical idempotency_key returns idempotent replay without duplicate transactions."""
        key = f"idem-key-{uuid.uuid4().hex}"
        payload = {
            'amount': '1000.00',
            'provider': 'CASH',
            'payment_method': 'CASH',
            'idempotency_key': key,
        }
        res1 = self.client.post(f"/api/v1/tenant/members/{self.profile.id}/collect-outstanding/", payload)
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)
        txn_id = res1.data['transaction_id']

        # Replay with same idempotency key
        res2 = self.client.post(f"/api/v1/tenant/members/{self.profile.id}/collect-outstanding/", payload)
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        self.assertEqual(res2.data['transaction_id'], txn_id)

        # Verify only 1 PaymentTransaction exists with this key
        count = PaymentTransaction.objects.using('tenant_test').filter(idempotency_key=key).count()
        self.assertEqual(count, 1)

    def test_payment_overpay_blocked(self):
        """Attempting to collect more than the order's remaining balance is blocked with 400."""
        res = self.client.post(f"/api/v1/tenant/members/{self.profile.id}/collect-outstanding/", {
            'amount': '99999.00',
            'provider': 'CASH',
            'payment_method': 'CASH',
        })
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('cannot exceed', res.data['error'])

    def test_cancelled_order_excluded_from_outstanding(self):
        """Cancelled orders do not contribute to a member's outstanding balance."""
        # Initial outstanding: 7700
        res1 = self.client.get(f"/api/v1/tenant/members/{self.profile.id}/360/")
        self.assertEqual(res1.data['header']['outstanding_balance'], 7700.0)

        # Create a second order in PENDING_PAYMENT
        set_tenant_db_alias('tenant_test')
        new_order = CommerceService.create_order(
            branch=self.branch1,
            items_data=[{
                'item_type': 'PACKAGE',
                'package_id': self.pkg.id,
                'package_version_id': self.pkg_v1.id,
                'package_price_id': self.pkg_price.id,
                'item_name_snapshot': 'Extra Addon',
                'quantity': Decimal('1.00'),
                'unit_price': Decimal('3000.00'),
                'discount_amount': Decimal('0.00'),
                'tax_percent': Decimal('0.000'),
            }],
            user_profile=self.profile,
            order_type='NEW_MEMBERSHIP',
            db_alias='tenant_test',
        )
        new_order.status = 'PENDING_PAYMENT'
        new_order.save(using='tenant_test')

        # Outstanding increases to 10700
        res2 = self.client.get(f"/api/v1/tenant/members/{self.profile.id}/360/")
        self.assertEqual(res2.data['header']['outstanding_balance'], 10700.0)

        # Cancel the second order
        new_order.status = 'CANCELLED'
        new_order.save(using='tenant_test')

        # Outstanding drops back to 7700 (cancelled order excluded)
        res3 = self.client.get(f"/api/v1/tenant/members/{self.profile.id}/360/")
        self.assertEqual(res3.data['header']['outstanding_balance'], 7700.0)

    def test_rejoin_reuses_profile_identity_and_preserves_history(self):
        """Rejoin reactivates member with a new contract while preserving identity and history."""
        # 1. Cancel active membership
        self.client.post(f"/api/v1/tenant/members/{self.profile.id}/cancel/", {'reason': 'Member left town'})
        self.profile.refresh_from_db(using='tenant_test')
        self.assertEqual(self.profile.member_status, 'INACTIVE')

        old_mem_count = Membership.objects.using('tenant_test').filter(user_profile=self.profile).count()

        # 2. Rejoin
        res = self.client.post(f"/api/v1/tenant/members/{self.profile.id}/rejoin/", {
            'package_id': str(self.pkg.id),
            'payment_amount': '5000.00',
            'reason': 'Returned to city for training',
        })
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertTrue(res.data['success'])

        # 3. Verify UserProfile identity is preserved (NOT duplicated)
        self.profile.refresh_from_db(using='tenant_test')
        self.assertEqual(self.profile.member_status, 'ACTIVE')
        self.assertEqual(res.data['member']['id'], str(self.profile.id))

        # 4. Old membership history is preserved, new membership appended
        new_mem_count = Membership.objects.using('tenant_test').filter(user_profile=self.profile).count()
        self.assertEqual(new_mem_count, old_mem_count + 1)

    def test_cross_tenant_member_access_returns_404(self):
        """Staff from tenant A cannot access or discover members from tenant B (fails closed with 404)."""
        # Create second tenant and member in tenant_test
        other_tenant = Tenant.objects.using('default').create(
            code='OTHER-TENANT',
            name='Other Gym',
            slug='other-gym',
            status='ACTIVE',
        )
        other_org = Organization.objects.using('tenant_test').create(
            name='Other Gym Org',
            code='OTHER-ORG',
            status='ACTIVE',
        )
        other_user = TenantUser.objects.using('tenant_test').create(
            organization=other_org,
            email='other_member@other.com',
            first_name='Secret',
            last_name='Member',
            status='ACTIVE',
        )
        other_profile = UserProfile.objects.using('tenant_test').create(
            user=other_user,
            member_number='MEM-OTHER-99',
            first_name_snapshot='Secret',
            last_name_snapshot='Member',
            member_status='ACTIVE',
        )

        # Query using self.token (authenticated under self.tenant)
        res = self.client.get(f"/api/v1/tenant/members/{other_profile.id}/360/")
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_check_in_consumes_entitlement_and_updates_passbook(self):
        """Check-in records attendance and automatically consumes 1 session unit in the passbook."""
        # Initial home sessions = 36
        res_before = self.client.get(f"/api/v1/tenant/members/{self.profile.id}/360/")
        initial_sessions = res_before.data['header']['home_sessions_remaining']

        # Perform check-in
        res_ci = self.client.post(f"/api/v1/tenant/members/{self.profile.id}/check-in/", {
            'method': 'Front Desk',
        })
        self.assertEqual(res_ci.status_code, status.HTTP_201_CREATED)

        # Check-in consumed 1 session
        res_after = self.client.get(f"/api/v1/tenant/members/{self.profile.id}/360/")
        self.assertEqual(res_after.data['header']['home_sessions_remaining'], initial_sessions - 1)

        # Passbook ledger shows CONSUMPTION
        ledger = res_after.data['passbook']['ledger']
        consumption_entries = [e for e in ledger if e['transaction_type'] == 'CONSUMPTION']
        self.assertGreaterEqual(len(consumption_entries), 1)

    def test_historical_contract_snapshot_preserved_after_catalog_price_change(self):
        """Updating current catalog package prices does not rewrite historical member contract snapshot."""
        # Member was purchased at base_price = 15000.00
        res_before = self.client.get(f"/api/v1/tenant/members/{self.profile.id}/360/")
        historical_price = res_before.data['memberships']['active']['contract_snapshot']['purchase_price']
        self.assertEqual(historical_price, 15000.0)

        # Update current package price in catalog to 25000.00
        self.pkg_price.base_price = Decimal('25000.00')
        self.pkg_price.save(using='tenant_test')

        # Historical contract snapshot remains 15000.00
        res_after = self.client.get(f"/api/v1/tenant/members/{self.profile.id}/360/")
        unchanged_price = res_after.data['memberships']['active']['contract_snapshot']['purchase_price']
        self.assertEqual(unchanged_price, 15000.0)

