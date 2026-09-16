"""
Layer 2 Phase 8 Tests: Module I (Memberships, Entitlements & Changes)

Covers:
- Membership activation from paid Order line items
- Immutable MembershipContractSnapshot generation
- Entitlement definition instantiation & initial ledger allocation
- Entitlement consumption, quota exhaustion protection, and ledger balance tracking
- Booking cancellation entitlement reversal
- Membership freeze calculation & automated end-date extension
- Membership package upgrades & cancellation change policy execution
- History tracking: Branch, Status, and Package history
- BusinessAuditEvent & DomainOutboxEvent publishing
"""

import uuid
from decimal import Decimal
from datetime import date, timedelta
from django.utils import timezone
from django.core.exceptions import ValidationError
from rest_framework.test import APITestCase
from rest_framework import status

from apps.authentication.views import _build_tenant_token
from apps.master.models import Tenant, TenantDataSource, ProductModule, TenantModule, SaasPlan, TenantSubscription
from apps.tenant_core.context import set_tenant_db_alias
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_rbac import (
    Role, RoleAssignment, ModuleCatalog, SubmoduleCatalog,
    Permission, RolePermissionSet, RolePermissionSetItem,
    RoleModuleAccess, RoleSubmoduleAccess,
)
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_catalog import (
    ProgramCategory, Program, Package, PackageVersion, PackagePrice, PackageEntitlementDefinition
)
from apps.tenant_core.models_commerce import Order, OrderItem
from apps.tenant_core.models_memberships import (
    Membership,
    MembershipContractSnapshot,
    MembershipEntitlement,
    MembershipEntitlementLedger,
    MembershipBranchHistory,
    MembershipStatusHistory,
    MembershipFreeze,
    MembershipRenewalPolicy,
    MembershipChangePolicy,
    MembershipChangePolicyRule,
    MembershipChangeRequest,
    MembershipPackageHistory,
)
from apps.tenant_core.services_memberships import MembershipLifecycleService
from apps.tenant_core.models_audit_outbox import BusinessAuditEvent, DomainOutboxEvent


class Layer2Phase8MembershipsTestCase(APITestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')

        # 1. Master DB Setup
        self.tenant = Tenant.objects.using('default').create(
            code='MEMBERSHIP-TENANT',
            name='Membership Gym',
            slug='membership-gym',
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
            name='Membership Plan',
            code='MEMBERSHIP-PLAN',
            tier='ENTERPRISE',
            status='ACTIVE',
        )
        self.sub = TenantSubscription.objects.using('default').create(
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

        # 2. Tenant DB Setup
        self.org = Organization.objects.using('tenant_test').create(
            code='MEM-ORG',
            name='Membership Organization',
            status='ACTIVE',
        )
        self.loc = Location.objects.using('tenant_test').create(
            organization=self.org,
            code='MEM-LOC',
            name='Downtown Location',
            status='ACTIVE',
        )
        self.branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            code='BR-CENTRAL',
            name='Central Branch',
            status='ACTIVE',
        )

        # RBAC
        self.user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='admin@membership.test',
            first_name='Admin',
            last_name='Membership',
            status='ACTIVE',
        )
        self.user.set_password('Secret123!')
        self.user.save(using='tenant_test')

        self.mod_cat, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(
            module_code='core', defaults={'name': 'Core', 'is_enabled': True}
        )
        self.sub_cat, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=self.mod_cat, submodule_code='settings', defaults={'name': 'Settings', 'is_enabled': True}
        )
        self.perm_view, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.mod_cat, submodule=self.sub_cat, action='view',
            defaults={'permission_code': 'core.settings.view', 'label': 'View Settings'}
        )
        self.perm_edit, _ = Permission.objects.using('tenant_test').get_or_create(
            module=self.mod_cat, submodule=self.sub_cat, action='edit',
            defaults={'permission_code': 'core.settings.edit', 'label': 'Edit Settings'}
        )

        self.role = Role.objects.using('tenant_test').create(
            organization=self.org,
            name='Org Admin',
            code='ORG_ADMIN',
            is_system_role=True,
            scope='ORG',
            is_active=True,
        )
        RoleModuleAccess.objects.using('tenant_test').get_or_create(
            role=self.role, module=self.mod_cat, defaults={'can_access': True}
        )
        RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(
            role=self.role, submodule=self.sub_cat, defaults={'can_access': True}
        )
        pset = RolePermissionSet.objects.using('tenant_test').create(role=self.role, name='Admin Perms')
        RolePermissionSetItem.objects.using('tenant_test').get_or_create(permission_set=pset, permission=self.perm_view, defaults={'granted': True})
        RolePermissionSetItem.objects.using('tenant_test').get_or_create(permission_set=pset, permission=self.perm_edit, defaults={'granted': True})
        RoleAssignment.objects.using('tenant_test').create(
            user=self.user, role=self.role, organization=self.org, is_active=True
        )

        # Member User Profile
        self.member_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='member@membership.test',
            first_name='Sara',
            last_name='Connor',
            status='ACTIVE',
        )
        self.member_profile = UserProfile.objects.using('tenant_test').create(
            user=self.member_user,
            member_number='MEM-3001',
            first_name_snapshot='Sara',
            last_name_snapshot='Connor',
            preferred_branch=self.branch,
            member_status='ACTIVE',
        )

        # Catalog Package & Entitlement Definitions
        self.category = ProgramCategory.objects.using('tenant_test').create(
            organization=self.org,
            code='TRAIN-CAT',
            name='Training Category',
            status='ACTIVE',
        )
        self.program = Program.objects.using('tenant_test').create(
            organization=self.org,
            category=self.category,
            code='ELITE-PT',
            name='Elite Training Program',
            status='ACTIVE',
        )
        self.package = Package.objects.using('tenant_test').create(
            organization=self.org,
            program=self.program,
            name='Gold 10 Sessions Package',
            code='PKG-GOLD-10',
            status='ACTIVE',
        )
        self.pkg_version = PackageVersion.objects.using('tenant_test').create(
            package=self.package,
            version_number=1,
            name_snapshot='Gold 10 Sessions v1',
            duration_value=1,
            duration_unit='MONTH',
            effective_from=timezone.now(),
            status='ACTIVE',
            created_by_user=self.user,
        )
        self.pkg_price = PackagePrice.objects.using('tenant_test').create(
            package_version=self.pkg_version,
            currency='INR',
            base_price=Decimal('10000.00'),
            tax_percent=Decimal('18.000'),
            effective_from=timezone.now(),
            created_by_user=self.user,
            status='ACTIVE',
        )
        self.ent_def = PackageEntitlementDefinition.objects.using('tenant_test').create(
            package_version=self.pkg_version,
            entitlement_type='CLASS_SESSIONS',
            allocated_units=Decimal('10.00'),
            is_unlimited=False,
        )

        # Higher Tier Package for Upgrade Test
        self.upgrade_package = Package.objects.using('tenant_test').create(
            organization=self.org,
            program=self.program,
            name='Platinum 25 Sessions Package',
            code='PKG-PLAT-25',
            status='ACTIVE',
        )
        self.upgrade_version = PackageVersion.objects.using('tenant_test').create(
            package=self.upgrade_package,
            version_number=1,
            name_snapshot='Platinum 25 Sessions v1',
            duration_value=3,
            duration_unit='MONTH',
            effective_from=timezone.now(),
            status='ACTIVE',
            created_by_user=self.user,
        )
        self.upgrade_price = PackagePrice.objects.using('tenant_test').create(
            package_version=self.upgrade_version,
            currency='INR',
            base_price=Decimal('22000.00'),
            tax_percent=Decimal('18.000'),
            effective_from=timezone.now(),
            created_by_user=self.user,
            status='ACTIVE',
        )

        # Order & Order Item
        self.order = Order.objects.using('tenant_test').create(
            branch=self.branch,
            order_number='ORD-MEM-001',
            order_type='NEW_MEMBERSHIP',
            status='PAID',
            user_profile=self.member_profile,
            subtotal=Decimal('10000.00'),
            discount_amount=Decimal('0.00'),
            tax_amount=Decimal('1800.00'),
            total_amount=Decimal('11800.00'),
        )
        self.order_item = OrderItem.objects.using('tenant_test').create(
            order=self.order,
            item_type='PACKAGE',
            package=self.package,
            package_version=self.pkg_version,
            package_price=self.pkg_price,
            item_name_snapshot='Gold 10 Sessions Package',
            quantity=Decimal('1.00'),
            unit_price_snapshot=Decimal('10000.00'),
            discount_amount=Decimal('0.00'),
            tax_percent_snapshot=Decimal('18.000'),
            tax_amount=Decimal('1800.00'),
            total_amount=Decimal('11800.00'),
        )

        # Auth Token
        refresh = _build_tenant_token(
            user=self.user,
            tenant=self.tenant,
            db_alias='tenant_test',
        )
        token = str(refresh.access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

    def test_membership_activation_and_contract_snapshot(self):
        """Test activating membership creates immutable contract snapshot and allocates initial entitlements."""
        membership = MembershipLifecycleService.activate_membership_from_order(
            order=self.order,
            order_item=self.order_item,
            start_date=date.today(),
            db_alias='tenant_test',
            created_by_user=self.user,
        )

        self.assertIsNotNone(membership.id)
        self.assertEqual(membership.status, 'ACTIVE')
        self.assertTrue(membership.membership_number.startswith('MEM-'))
        self.assertEqual(membership.home_branch, self.branch)
        self.assertEqual(membership.package, self.package)

        # Check Immutable Contract Snapshot
        snapshot = membership.contract_snapshot
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.final_amount, Decimal('11800.00'))
        self.assertEqual(len(snapshot.entitlements_snapshot), 1)
        self.assertEqual(snapshot.entitlements_snapshot[0]['entitlement_type'], 'CLASS_SESSIONS')

        # Check Entitlements & Initial Ledger Entry
        self.assertEqual(membership.entitlements.count(), 1)
        ent = membership.entitlements.first()
        self.assertEqual(ent.allocated_units, Decimal('10.00'))
        self.assertEqual(ent.consumed_units, Decimal('0.00'))
        self.assertEqual(ent.remaining_units, Decimal('10.00'))

        ledger_entries = ent.ledger_entries.all()
        self.assertEqual(ledger_entries.count(), 1)
        self.assertEqual(ledger_entries[0].transaction_type, 'ALLOCATION')
        self.assertEqual(ledger_entries[0].balance_after, Decimal('10.00'))

        # Check History Logs
        self.assertEqual(membership.branch_history.count(), 1)
        self.assertEqual(membership.status_history.count(), 1)
        self.assertEqual(membership.package_history.count(), 1)

        # Check Audit & Outbox Events
        audit = BusinessAuditEvent.objects.using('tenant_test').filter(action_code='MEMBERSHIP_ACTIVATED').first()
        self.assertIsNotNone(audit)
        outbox = DomainOutboxEvent.objects.using('tenant_test').filter(event_type='membership.activated').first()
        self.assertIsNotNone(outbox)

    def test_entitlement_consumption_and_reversal(self):
        """Test consuming and reversing sessions records immutable ledger rows and updates balances."""
        membership = MembershipLifecycleService.activate_membership_from_order(
            order=self.order,
            order_item=self.order_item,
            start_date=date.today(),
            db_alias='tenant_test',
            created_by_user=self.user,
        )

        # 1. Consume 3 sessions
        booking_id = uuid.uuid4()
        ledger1 = MembershipLifecycleService.consume_entitlement(
            membership=membership,
            entitlement_type='CLASS_SESSIONS',
            units=Decimal('3.00'),
            booking_id=booking_id,
            reason_text="Booked HIIT Class",
            created_by_user=self.user,
            db_alias='tenant_test',
        )
        self.assertEqual(ledger1.transaction_type, 'CONSUMPTION')
        self.assertEqual(ledger1.units, Decimal('-3.00'))
        self.assertEqual(ledger1.balance_after, Decimal('7.00'))

        ent = membership.entitlements.first()
        self.assertEqual(ent.consumed_units, Decimal('3.00'))
        self.assertEqual(ent.remaining_units, Decimal('7.00'))

        # 2. Reverse 1 session
        ledger2 = MembershipLifecycleService.reverse_entitlement(
            membership=membership,
            entitlement_type='CLASS_SESSIONS',
            units=Decimal('1.00'),
            booking_id=booking_id,
            reason_text="Cancelled booking within free cancellation window",
            created_by_user=self.user,
            db_alias='tenant_test',
        )
        self.assertEqual(ledger2.transaction_type, 'REVERSAL')
        self.assertEqual(ledger2.units, Decimal('1.00'))
        self.assertEqual(ledger2.balance_after, Decimal('8.00'))

        ent.refresh_from_db()
        self.assertEqual(ent.consumed_units, Decimal('2.00'))
        self.assertEqual(ent.remaining_units, Decimal('8.00'))

        # 3. Exhaust quota
        MembershipLifecycleService.consume_entitlement(
            membership=membership,
            entitlement_type='CLASS_SESSIONS',
            units=Decimal('8.00'),
            db_alias='tenant_test',
        )
        ent.refresh_from_db()
        self.assertEqual(ent.status, 'EXHAUSTED')

        # 4. Attempting to consume more should fail with ValidationError
        with self.assertRaises(ValidationError):
            MembershipLifecycleService.consume_entitlement(
                membership=membership,
                entitlement_type='CLASS_SESSIONS',
                units=Decimal('1.00'),
                db_alias='tenant_test',
            )

    def test_membership_freeze_and_date_extension(self):
        """Test freezing membership extends validity end date and updates status."""
        membership = MembershipLifecycleService.activate_membership_from_order(
            order=self.order,
            order_item=self.order_item,
            start_date=date.today(),
            db_alias='tenant_test',
            created_by_user=self.user,
        )
        orig_end = membership.end_date

        freeze_from = date.today()
        freeze_until = date.today() + timedelta(days=14)

        freeze = MembershipLifecycleService.apply_freeze(
            membership=membership,
            freeze_from=freeze_from,
            freeze_until=freeze_until,
            reason_text="Medical leave for knee injury",
            approved_by_user=self.user,
            db_alias='tenant_test',
        )

        membership.refresh_from_db()
        self.assertEqual(freeze.extend_membership_days, 14)
        self.assertEqual(membership.end_date, orig_end + timedelta(days=14))
        self.assertEqual(membership.status, 'FROZEN')

        # Verify status history
        st_entry = membership.status_history.filter(to_status='FROZEN').first()
        self.assertIsNotNone(st_entry)

    def test_membership_upgrade_and_cancellation_changes(self):
        """Test policy-based upgrades and cancellations update package history and contract lifecycle."""
        membership = MembershipLifecycleService.activate_membership_from_order(
            order=self.order,
            order_item=self.order_item,
            start_date=date.today(),
            db_alias='tenant_test',
            created_by_user=self.user,
        )

        policy = MembershipChangePolicy.objects.using('tenant_test').create(
            organization=self.org,
            policy_name='Standard Change Policy',
            version_number=1,
            effective_from=timezone.now() - timedelta(days=1),
            status='ACTIVE',
            created_by_user=self.user,
        )
        upgrade_rule = MembershipChangePolicyRule.objects.using('tenant_test').create(
            membership_change_policy=policy,
            change_type='UPGRADE',
            rule_name='Gold to Platinum Upgrade',
            pricing_mode='DIFFERENCE_ONLY',
            effective_mode='IMMEDIATE',
            status='ACTIVE',
        )
        cancel_rule = MembershipChangePolicyRule.objects.using('tenant_test').create(
            membership_change_policy=policy,
            change_type='CANCELLATION',
            rule_name='Standard Cancellation',
            cancellation_fee_type='FIXED',
            cancellation_fee_value=Decimal('1000.00'),
            refund_mode='PRORATED',
            status='ACTIVE',
        )

        # 1. Upgrade
        up_req = MembershipLifecycleService.quote_and_apply_change(
            membership=membership,
            policy_rule=upgrade_rule,
            target_package=self.upgrade_package,
            target_package_version=self.upgrade_version,
            reason="Member requested upgrade to Platinum",
            actor_user=self.user,
            db_alias='tenant_test',
        )

        membership.refresh_from_db()
        self.assertEqual(membership.package, self.upgrade_package)
        self.assertEqual(membership.package_version, self.upgrade_version)

        pkg_hist = membership.package_history.filter(change_type='UPGRADE').first()
        self.assertIsNotNone(pkg_hist)
        self.assertEqual(pkg_hist.to_package, self.upgrade_package)

        # 2. Cancellation
        cancel_req = MembershipLifecycleService.quote_and_apply_change(
            membership=membership,
            policy_rule=cancel_rule,
            reason="Member relocated",
            actor_user=self.user,
            db_alias='tenant_test',
        )

        membership.refresh_from_db()
        self.assertEqual(membership.status, 'CANCELLED')
        self.assertIsNotNone(membership.cancelled_at)
        self.assertEqual(cancel_req.penalty_amount, Decimal('1000.00'))

    def test_membership_rest_apis(self):
        """Test REST API endpoints for membership listing, activation, consumption, and freeze."""
        # Activate via REST API
        activate_payload = {
            'order_id': str(self.order.id),
            'order_item_id': str(self.order_item.id),
            'start_date': date.today().isoformat(),
        }
        res = self.client.post('/api/v1/tenant/memberships/activate/', activate_payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        membership_id = res.data['id']

        # List memberships
        list_res = self.client.get('/api/v1/tenant/memberships/')
        self.assertEqual(list_res.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(list_res.data.get('results', list_res.data)), 1)

        # Consume Entitlement via REST API
        consume_payload = {
            'entitlement_type': 'CLASS_SESSIONS',
            'units': '2.00',
            'reason_text': 'API Test Session Consumption',
        }
        consume_res = self.client.post(f'/api/v1/tenant/memberships/{membership_id}/consume-entitlement/', consume_payload, format='json')
        self.assertEqual(consume_res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Decimal(consume_res.data['units']), Decimal('-2.00'))

        # Freeze Membership via REST API
        freeze_payload = {
            'freeze_from': date.today().isoformat(),
            'freeze_until': (date.today() + timedelta(days=7)).isoformat(),
            'reason_text': 'API Test Freeze',
        }
        freeze_res = self.client.post(f'/api/v1/tenant/memberships/{membership_id}/freeze/', freeze_payload, format='json')
        self.assertEqual(freeze_res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(freeze_res.data['status'], 'ACTIVE')

        # Retrieve Contract Snapshot via REST API
        contract_res = self.client.get(f'/api/v1/tenant/memberships/{membership_id}/contract/')
        self.assertEqual(contract_res.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(contract_res.data['final_amount']), Decimal('11800.00'))
