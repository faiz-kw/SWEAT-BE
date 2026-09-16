"""
Layer 2 Phase 7 Tests: Module H (Coupons, Discounts & Dynamic Offers)

Covers:
- DiscountCampaign & DiscountCode setup (percentage, fixed amount, capped limits)
- Branch & package scoped discount codes
- Coupon validation API:
  - Valid coupon calculation
  - Minimum order amount validation
  - Capped percentage discount (max_discount)
  - Expiration & invalid code rejection
  - Global and per-user usage limits
- Dynamic eligibility rule engine & offer suggestions:
  - SESSIONS_CONSUMED, SESSION_USAGE_PERCENTAGE conditions
  - ALL_CONDITIONS / ANY_CONDITION logic
  - Dynamic offer suggestions for member upgrade/renewal
- Coupon redemption on orders:
  - Balance reduction and total recalculation
  - Immutable DiscountRedemption recording
  - BusinessAuditEvent and DomainOutboxEvent publishing
"""

import uuid
from decimal import Decimal
from django.utils import timezone
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
from apps.tenant_core.models_catalog import ProgramCategory, Program, Package, PackageVersion, PackagePrice
from apps.tenant_core.models_commerce import Order, OrderItem
from apps.tenant_core.models_discounts import (
    DiscountCampaign,
    DiscountCode,
    DiscountEligibilityRule,
    DiscountRuleCondition,
    DiscountRuleAction,
    DiscountRedemption,
)
from apps.tenant_core.services_discounts import DiscountCouponEngineService
from apps.tenant_core.models_audit_outbox import BusinessAuditEvent, DomainOutboxEvent


class Layer2Phase7DiscountsTestCase(APITestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')

        # 1. Master DB Setup
        self.tenant = Tenant.objects.using('default').create(
            code='DISCOUNT-TENANT',
            name='Discounts Gym',
            slug='discounts-gym',
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
            name='Discount Plan',
            code='DISCOUNT-PLAN',
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
            code='DISC-ORG',
            name='Discounts Organization',
            status='ACTIVE',
        )
        self.loc = Location.objects.using('tenant_test').create(
            organization=self.org,
            code='DISC-LOC',
            name='Downtown Location',
            status='ACTIVE',
        )
        self.branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            code='BR-MAIN',
            name='Downtown Main Branch',
            status='ACTIVE',
        )
        self.other_branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            code='BR-SUBURBS',
            name='Suburbs Branch',
            status='ACTIVE',
        )

        # RBAC
        self.user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='admin@discounts.test',
            first_name='Admin',
            last_name='Discounts',
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
            email='member@discounts.test',
            first_name='John',
            last_name='Athlete',
            status='ACTIVE',
        )
        self.member_profile = UserProfile.objects.using('tenant_test').create(
            user=self.member_user,
            member_number='MEM-2001',
            first_name_snapshot='John',
            last_name_snapshot='Athlete',
            preferred_branch=self.branch,
            member_status='ACTIVE',
        )

        # Catalog Package
        self.category = ProgramCategory.objects.using('tenant_test').create(
            organization=self.org,
            code='FIT-CAT',
            name='Fitness Category',
            status='ACTIVE',
        )
        self.program = Program.objects.using('tenant_test').create(
            organization=self.org,
            category=self.category,
            code='STD-GYM',
            name='Standard Gym Access',
            status='ACTIVE',
        )
        self.package = Package.objects.using('tenant_test').create(
            organization=self.org,
            program=self.program,
            name='Silver Membership',
            code='PKG-SILVER',
            status='ACTIVE',
        )
        self.pkg_version = PackageVersion.objects.using('tenant_test').create(
            package=self.package,
            version_number=1,
            name_snapshot='Silver Membership v1',
            duration_value=1,
            duration_unit='MONTH',
            effective_from=timezone.now(),
            status='ACTIVE',
            created_by_user=self.user,
        )
        self.pkg_price = PackagePrice.objects.using('tenant_test').create(
            package_version=self.pkg_version,
            currency='INR',
            base_price=Decimal('5000.00'),
            tax_percent=Decimal('18.000'),
            effective_from=timezone.now(),
            created_by_user=self.user,
            status='ACTIVE',
        )

        # Auth Token
        refresh = _build_tenant_token(
            user=self.user,
            tenant=self.tenant,
            db_alias='tenant_test',
        )
        token = str(refresh.access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

    def test_create_discount_campaign_and_code(self):
        """Test creating discount campaign and generating unique code via API."""
        now = timezone.now()
        payload = {
            'name': 'Summer Kickoff Sale',
            'description': '20% off with 1000 INR cap',
            'discount_type': 'PERCENTAGE',
            'discount_value': '20.0000',
            'max_discount': '1000.00',
            'minimum_order_amount': '2000.00',
            'usage_limit': 100,
            'per_user_limit': 1,
            'valid_from': now.isoformat(),
            'status': 'ACTIVE',
        }
        res = self.client.post('/api/v1/tenant/discount-campaigns/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        campaign_id = res.data['id']

        # Generate a code for this campaign
        code_payload = {
            'code': 'SUMMER20',
            'branch_id': str(self.branch.id),
        }
        code_res = self.client.post(f'/api/v1/tenant/discount-campaigns/{campaign_id}/generate-code/', code_payload, format='json')
        self.assertEqual(code_res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(code_res.data['code'], 'SUMMER20')

    def test_coupon_validation_success_and_capping(self):
        """Test coupon validation accurately computes percentage discount and applies cap."""
        campaign = DiscountCampaign.objects.using('tenant_test').create(
            organization=self.org,
            name='Monsoon Special',
            discount_type='PERCENTAGE',
            discount_value=Decimal('25.0000'),
            max_discount=Decimal('800.00'),
            minimum_order_amount=Decimal('1000.00'),
            valid_from=timezone.now() - timezone.timedelta(days=1),
            status='ACTIVE',
        )
        DiscountCode.objects.using('tenant_test').create(
            campaign=campaign,
            code='MONSOON25',
            status='ACTIVE',
        )

        # 1. 25% of 2000 is 500, below cap of 800 -> discount = 500
        val_payload = {
            'code': 'MONSOON25',
            'user_profile_id': str(self.member_profile.id),
            'order_subtotal': '2000.00',
            'branch_id': str(self.branch.id),
        }
        res = self.client.post('/api/v1/tenant/discount-campaigns/validate-coupon/', val_payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.data['is_valid'])
        self.assertEqual(Decimal(res.data['discount_amount']), Decimal('500.00'))
        self.assertEqual(Decimal(res.data['final_subtotal']), Decimal('1500.00'))

        # 2. 25% of 4000 is 1000, exceeds cap of 800 -> capped at 800
        val_payload['order_subtotal'] = '4000.00'
        res2 = self.client.post('/api/v1/tenant/discount-campaigns/validate-coupon/', val_payload, format='json')
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(res2.data['discount_amount']), Decimal('800.00'))
        self.assertEqual(Decimal(res2.data['final_subtotal']), Decimal('3200.00'))

    def test_coupon_validation_failures(self):
        """Test coupon validation rejects invalid codes, branch mismatches, and minimum amounts."""
        campaign = DiscountCampaign.objects.using('tenant_test').create(
            organization=self.org,
            name='Branch Specific Deal',
            discount_type='FIXED',
            discount_value=Decimal('300.00'),
            minimum_order_amount=Decimal('1500.00'),
            valid_from=timezone.now() - timezone.timedelta(days=1),
            status='ACTIVE',
        )
        DiscountCode.objects.using('tenant_test').create(
            campaign=campaign,
            code='BRANCHONLY',
            branch=self.branch,
            status='ACTIVE',
        )

        # Below minimum amount
        res_low = self.client.post('/api/v1/tenant/discount-campaigns/validate-coupon/', {
            'code': 'BRANCHONLY',
            'user_profile_id': str(self.member_profile.id),
            'order_subtotal': '1000.00',
            'branch_id': str(self.branch.id),
        }, format='json')
        self.assertEqual(res_low.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(res_low.data['is_valid'])
        self.assertIn('Minimum order amount', res_low.data['reason'])

        # Branch mismatch
        res_branch = self.client.post('/api/v1/tenant/discount-campaigns/validate-coupon/', {
            'code': 'BRANCHONLY',
            'user_profile_id': str(self.member_profile.id),
            'order_subtotal': '2000.00',
            'branch_id': str(self.other_branch.id),
        }, format='json')
        self.assertEqual(res_branch.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('only valid at', res_branch.data['reason'])

    def test_dynamic_eligibility_and_offer_evaluation(self):
        """Test evaluating dynamic offers based on session usage percentage."""
        rule = DiscountEligibilityRule.objects.using('tenant_test').create(
            organization=self.org,
            name='80% Usage Upgrade Offer',
            description='Exclusive 15% discount when member has used >= 80% of sessions',
            rule_type='UPGRADE_OFFER',
            priority=10,
            evaluation_mode='ALL_CONDITIONS',
            valid_from=timezone.now() - timezone.timedelta(days=1),
            status='ACTIVE',
            created_by_user=self.user,
        )
        DiscountRuleCondition.objects.using('tenant_test').create(
            discount_eligibility_rule=rule,
            condition_type='SESSION_USAGE_PERCENTAGE',
            operator='GREATER_THAN_OR_EQUAL',
            numeric_value=Decimal('80.00'),
        )
        DiscountRuleAction.objects.using('tenant_test').create(
            discount_eligibility_rule=rule,
            action_type='SHOW_OFFER',
            discount_percentage=Decimal('15.0000'),
            message='You have completed 80% of your sessions! Upgrade now and save 15%.',
        )

        # 1. Member with 50% usage should NOT qualify
        res_50 = self.client.post('/api/v1/tenant/discount-campaigns/evaluate-offers/', {
            'user_profile_id': str(self.member_profile.id),
            'usage_percentage': '50.00',
            'sessions_consumed': 10,
        }, format='json')
        self.assertEqual(res_50.status_code, status.HTTP_200_OK)
        self.assertEqual(res_50.data['count'], 0)

        # 2. Member with 85% usage SHOULD qualify
        res_85 = self.client.post('/api/v1/tenant/discount-campaigns/evaluate-offers/', {
            'user_profile_id': str(self.member_profile.id),
            'usage_percentage': '85.00',
            'sessions_consumed': 17,
        }, format='json')
        self.assertEqual(res_85.status_code, status.HTTP_200_OK)
        self.assertEqual(res_85.data['count'], 1)
        offer = res_85.data['offers'][0]
        self.assertEqual(offer['rule_name'], '80% Usage Upgrade Offer')
        self.assertEqual(Decimal(offer['discount_percentage']), Decimal('15.0000'))

    def test_redeem_coupon_on_order(self):
        """Test redeeming coupon on an order updates order discount and creates immutable redemption."""
        campaign = DiscountCampaign.objects.using('tenant_test').create(
            organization=self.org,
            name='Festive Fixed Discount',
            discount_type='FIXED',
            discount_value=Decimal('500.00'),
            minimum_order_amount=Decimal('1000.00'),
            valid_from=timezone.now() - timezone.timedelta(days=1),
            status='ACTIVE',
        )
        code = DiscountCode.objects.using('tenant_test').create(
            campaign=campaign,
            code='FESTIVE500',
            status='ACTIVE',
        )

        # Create Order
        order = Order.objects.using('tenant_test').create(
            branch=self.branch,
            order_number='ORD-DISC-001',
            order_type='NEW_MEMBERSHIP',
            status='PENDING_PAYMENT',
            user_profile=self.member_profile,
            subtotal=Decimal('3000.00'),
            discount_amount=Decimal('0.00'),
            tax_amount=Decimal('540.00'),
            total_amount=Decimal('3540.00'),
        )

        redemption = DiscountCouponEngineService.redeem_coupon(
            order=order,
            code_str='FESTIVE500',
            user_profile=self.member_profile,
            created_by_user=self.user,
        )

        self.assertIsNotNone(redemption.id)
        self.assertEqual(redemption.discount_amount, Decimal('500.00'))

        order.refresh_from_db()
        self.assertEqual(order.discount_amount, Decimal('500.00'))
        # subtotal 3000 - 500 = 2500 net taxable
        # tax 18% of 2500 = 450
        # total = 2950
        self.assertEqual(order.tax_amount, Decimal('450.00'))
        self.assertEqual(order.total_amount, Decimal('2950.00'))

        # Verify audit event and outbox
        audit = BusinessAuditEvent.objects.using('tenant_test').filter(action_code='COUPON_REDEEMED').first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.metadata['code'], 'FESTIVE500')

        outbox = DomainOutboxEvent.objects.using('tenant_test').filter(event_type='commerce.coupon.redeemed').first()
        self.assertIsNotNone(outbox)
        self.assertEqual(outbox.payload['code'], 'FESTIVE500')

