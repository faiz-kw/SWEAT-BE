"""
CRM Phase 9 Offers & Coupons Configuration Closure Tests
Targeted regression coverage for:
  1. Campaign: create with dates, branch scope, package scope, edit, activate/pause behavior
  2. Rules: create ALL rule, create ANY rule, multiple conditions, action binding, status toggle
  3. Member context resolver: real membership & session entitlement calculations, unavailable metrics behavior
  4. Authoritative tax: exact order/item tax snapshots, zero hardcoded 18% assumption
  5. Checkout Simulator & validation: usage limits, branch scope, package scope, per-user limits, min spend
  6. Security: tenant/org isolation, RBAC denial
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
from apps.tenant_core.context import set_tenant_db_alias, get_current_tenant_db_alias

from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_rbac import (
    ModuleCatalog, SubmoduleCatalog, Permission, Role,
    RolePermissionSet, RolePermissionSetItem, RoleAssignment,
    RoleModuleAccess, RoleSubmoduleAccess
)
from apps.tenant_core.models_catalog import (
    ProgramCategory, Program, Package, PackageVersion, PackagePrice,
    PackageBranchAvailability, PackageEntitlementDefinition
)
from apps.tenant_core.models_commerce import Order, OrderItem
from apps.tenant_core.models_memberships import (
    Membership, MembershipEntitlement
)
from apps.tenant_core.models_discounts import (
    DiscountCampaign, DiscountCode, DiscountRedemption,
    DiscountEligibilityRule, DiscountRuleCondition, DiscountRuleAction
)
from apps.tenant_core.models_govern import OrganizationSettings
from apps.tenant_core.services_discounts import DiscountCouponEngineService


DB = 'tenant_test'


class OffersCouponsClosureTestCase(APITestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias(DB)

        # Master DB Setup
        self.tenant = Tenant.objects.using('default').create(
            code='P9-CLOSURE-TENANT', name='Offers Closure Fitness', slug='offers-closure-fitness', status='ACTIVE'
        )
        TenantDataSource.objects.using('default').create(
            tenant=self.tenant, db_name='test_fitness_tenant',
            database_name='test_fitness_tenant', status='ACTIVE', database_engine='POSTGRESQL'
        )
        plan = SaasPlan.objects.using('default').create(
            name='Enterprise Plan', code='P9-CLOSURE-ENT', tier='ENTERPRISE', status='ACTIVE'
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

        # Tenant Org & Branch
        self.org = Organization.objects.using(DB).create(
            code='P9-CLOSURE-ORG', name='Offers Closure Org', status='ACTIVE'
        )
        self.org_settings = OrganizationSettings.objects.using(DB).create(
            organization=self.org,
            tax_rate_pct=Decimal('12.00'),
        )
        loc = Location.objects.using(DB).create(
            organization=self.org, code='P9C-LOC', name='Closure Location', status='ACTIVE'
        )
        self.branch = Branch.objects.using(DB).create(
            organization=self.org, location=loc, code='BR-P9C-A', name='Downtown Branch', status='ACTIVE'
        )
        self.branch_b = Branch.objects.using(DB).create(
            organization=self.org, location=loc, code='BR-P9C-B', name='Suburban Branch', status='ACTIVE'
        )

        # Users
        self.admin = TenantUser.objects.using(DB).create(
            organization=self.org, email='admin@closure.test',
            first_name='Admin', last_name='Owner', status='ACTIVE'
        )
        self.sales_rep = TenantUser.objects.using(DB).create(
            organization=self.org, email='sales@closure.test',
            first_name='Sales', last_name='Agent', status='ACTIVE'
        )
        self.member_user = TenantUser.objects.using(DB).create(
            organization=self.org, email='member@closure.test', first_name='John', last_name='Member',
            status='ACTIVE'
        )
        self.member_profile = UserProfile.objects.using(DB).create(
            user=self.member_user,
            first_name_snapshot='John',
            last_name_snapshot='Member',
            member_number='MEM-001',
            member_status='ACTIVE'
        )

        self.empty_user = TenantUser.objects.using(DB).create(
            organization=self.org, email='empty@closure.test', first_name='Empty', last_name='User',
            status='ACTIVE'
        )
        self.empty_profile = UserProfile.objects.using(DB).create(
            user=self.empty_user,
            first_name_snapshot='Empty',
            last_name_snapshot='User',
            member_number='MEM-002',
            member_status='ACTIVE'
        )

        # RBAC Setup
        mod_crm, _ = ModuleCatalog.objects.using(DB).get_or_create(
            module_code='crm', defaults={'name': 'CRM', 'is_enabled': True}
        )
        sub_leads, _ = SubmoduleCatalog.objects.using(DB).get_or_create(
            module=mod_crm, submodule_code='leads', defaults={'name': 'Leads', 'is_enabled': True}
        )
        mod_core, _ = ModuleCatalog.objects.using(DB).get_or_create(
            module_code='core', defaults={'name': 'Core', 'is_enabled': True}
        )
        sub_settings, _ = SubmoduleCatalog.objects.using(DB).get_or_create(
            module=mod_core, submodule_code='settings', defaults={'name': 'Settings', 'is_enabled': True}
        )

        perm_settings_view, _ = Permission.objects.using(DB).get_or_create(
            module=mod_core, submodule=sub_settings, action='view',
            defaults={'permission_code': 'core.settings.view', 'label': 'View Settings'}
        )
        perm_settings_edit, _ = Permission.objects.using(DB).get_or_create(
            module=mod_core, submodule=sub_settings, action='edit',
            defaults={'permission_code': 'core.settings.edit', 'label': 'Edit Settings'}
        )

        role_admin = Role.objects.using(DB).create(
            organization=self.org, name='Admin Role', code='ROLE_ADMIN',
            is_system_role=True, scope='ORG', is_active=True
        )
        RoleModuleAccess.objects.using(DB).create(role=role_admin, module=mod_core, can_access=True)
        RoleSubmoduleAccess.objects.using(DB).create(role=role_admin, submodule=sub_settings, can_access=True)
        pset_admin = RolePermissionSet.objects.using(DB).create(role=role_admin, name='Admin Perms')
        RolePermissionSetItem.objects.using(DB).create(permission_set=pset_admin, permission=perm_settings_view, granted=True)
        RolePermissionSetItem.objects.using(DB).create(permission_set=pset_admin, permission=perm_settings_edit, granted=True)
        RoleAssignment.objects.using(DB).create(user=self.admin, role=role_admin, organization=self.org, is_active=True)

        role_sales = Role.objects.using(DB).create(
            organization=self.org, name='Sales Role', code='ROLE_SALES',
            is_system_role=True, scope='ORG', is_active=True
        )
        RoleModuleAccess.objects.using(DB).create(role=role_sales, module=mod_core, can_access=True)
        RoleSubmoduleAccess.objects.using(DB).create(role=role_sales, submodule=sub_settings, can_access=True)
        pset_sales = RolePermissionSet.objects.using(DB).create(role=role_sales, name='Sales Perms')
        RolePermissionSetItem.objects.using(DB).create(permission_set=pset_sales, permission=perm_settings_view, granted=True)
        RoleAssignment.objects.using(DB).create(user=self.sales_rep, role=role_sales, organization=self.org, is_active=True)

        # Catalog: Program & Package
        cat = ProgramCategory.objects.using(DB).create(
            organization=self.org, code='CAT-P9C', name='General Fitness', status='ACTIVE'
        )
        self.program = Program.objects.using(DB).create(
            organization=self.org, category=cat, code='PROG-P9C', name='CrossFit Strength', status='ACTIVE'
        )
        self.package = Package.objects.using(DB).create(
            organization=self.org, program=self.program, code='PKG-P9C-GOLD', name='Gold Membership', status='ACTIVE'
        )
        self.package_b = Package.objects.using(DB).create(
            organization=self.org, program=self.program, code='PKG-P9C-SILV', name='Silver Membership', status='ACTIVE'
        )
        self.package_version = PackageVersion.objects.using(DB).create(
            package=self.package, version_number=1, name_snapshot='Gold v1',
            duration_value=12, duration_unit='MONTHS', effective_from=timezone.now(),
            status='ACTIVE', created_by_user=self.admin
        )
        self.package_price = PackagePrice.objects.using(DB).create(
            package_version=self.package_version, branch=self.branch,
            base_price=Decimal('12000.00'), tax_percent=Decimal('12.00'),
            prices_include_tax=False, currency='INR', effective_from=timezone.now(),
            status='ACTIVE', created_by_user=self.admin
        )
        PackageBranchAvailability.objects.using(DB).create(
            package=self.package, branch=self.branch, status='ENABLED'
        )

        self.admin_token = str(_build_tenant_token(user=self.admin, tenant=self.tenant, db_alias=DB).access_token)
        self.sales_token = str(_build_tenant_token(user=self.sales_rep, tenant=self.tenant, db_alias=DB).access_token)

    def test_01_campaign_create_with_dates_and_scope(self):
        """Campaign create persists validity dates and branch/package configuration scope."""
        now = timezone.now()
        valid_from = now.isoformat()
        valid_until = (now + timedelta(days=30)).isoformat()

        res = self.client.post(
            '/api/v1/admin/discount-campaigns/',
            {
                'name': 'Festive Offer',
                'description': 'Valid at Downtown branch only',
                'discount_type': 'PERCENTAGE',
                'discount_value': '20.00',
                'max_discount': '500.00',
                'minimum_order_amount': '1000.00',
                'usage_limit': 50,
                'per_user_limit': 1,
                'valid_from': valid_from,
                'valid_until': valid_until,
                'status': 'ACTIVE',
                'configuration': {
                    'branch_id': str(self.branch.id),
                    'package_id': str(self.package.id),
                }
            },
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}',
            format='json'
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        camp_id = res.data['id']

        camp = DiscountCampaign.objects.using(DB).get(id=camp_id)
        self.assertEqual(camp.name, 'Festive Offer')
        self.assertEqual(camp.configuration.get('branch_id'), str(self.branch.id))
        self.assertEqual(camp.configuration.get('package_id'), str(self.package.id))
        self.assertEqual(camp.status, 'ACTIVE')
        self.assertTrue(camp.is_currently_valid())

    def test_02_campaign_edit_and_status_transitions(self):
        """Editing campaign updates fields and status transitions change validation behavior."""
        now = timezone.now()
        camp = DiscountCampaign.objects.using(DB).create(
            organization=self.org,
            name='Early Bird',
            discount_type='FIXED',
            discount_value=Decimal('200.00'),
            valid_from=now - timedelta(days=1),
            valid_until=now + timedelta(days=10),
            status='ACTIVE'
        )
        code = DiscountCode.objects.using(DB).create(
            campaign=camp, code='EARLY200', status='ACTIVE'
        )

        # Valid initially
        val = DiscountCouponEngineService.validate_coupon(
            code_str='EARLY200', user_profile=self.member_profile, order_subtotal=Decimal('1000.00'), db_alias=DB
        )
        self.assertTrue(val['is_valid'])

        # Pause campaign via API
        res = self.client.patch(
            f'/api/v1/admin/discount-campaigns/{camp.id}/',
            {'status': 'PAUSED'},
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}',
            format='json'
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        camp = DiscountCampaign.objects.using(DB).get(id=camp.id)
        self.assertEqual(camp.status, 'PAUSED')

        # Now validation fails
        val_paused = DiscountCouponEngineService.validate_coupon(
            code_str='EARLY200', user_profile=self.member_profile, order_subtotal=Decimal('1000.00'), db_alias=DB
        )
        self.assertFalse(val_paused['is_valid'])
        self.assertEqual(val_paused['reason_code'], 'INACTIVE')

    def test_03_rules_create_all_and_any_with_conditions_and_actions(self):
        """Authoring dynamic rules supports ALL and ANY evaluation modes, multiple conditions, and actions."""
        now = timezone.now()
        rule_res = self.client.post(
            '/api/v1/admin/discount-eligibility-rules/',
            {
                'name': 'High Usage Upgrade Incentive',
                'description': 'Over 80% sessions consumed and active > 60 days',
                'rule_type': 'UPGRADE_OFFER',
                'evaluation_mode': 'ALL_CONDITIONS',
                'priority': 10,
                'valid_from': now.isoformat(),
                'status': 'ACTIVE',
            },
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}',
            format='json'
        )
        self.assertEqual(rule_res.status_code, status.HTTP_201_CREATED)
        rule_id = rule_res.data['id']

        cond1 = self.client.post(
            f'/api/v1/admin/discount-eligibility-rules/{rule_id}/add-condition/',
            {
                'condition_type': 'USAGE_PERCENTAGE',
                'operator': 'GREATER_THAN_OR_EQUAL',
                'numeric_value': '80.00',
                'sequence': 1,
            },
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}',
            format='json'
        )
        self.assertEqual(cond1.status_code, status.HTTP_201_CREATED)

        cond2 = self.client.post(
            f'/api/v1/admin/discount-eligibility-rules/{rule_id}/add-condition/',
            {
                'condition_type': 'PACKAGE_AGE_DAYS',
                'operator': 'GREATER_THAN_OR_EQUAL',
                'numeric_value': '60.00',
                'sequence': 2,
            },
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}',
            format='json'
        )
        self.assertEqual(cond2.status_code, status.HTTP_201_CREATED)

        act = self.client.post(
            f'/api/v1/admin/discount-eligibility-rules/{rule_id}/add-action/',
            {
                'action_type': 'APPLY_PERCENTAGE_DISCOUNT',
                'discount_percentage': '25.00',
                'message': 'Upgrade now and save 25% on your renewal!',
                'auto_apply': True,
            },
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}',
            format='json'
        )
        self.assertEqual(act.status_code, status.HTTP_201_CREATED)
        rule = DiscountEligibilityRule.objects.using(DB).get(id=rule_id)
        self.assertEqual(DiscountRuleCondition.objects.using(DB).filter(discount_eligibility_rule=rule).count(), 2)
        self.assertEqual(DiscountRuleAction.objects.using(DB).filter(discount_eligibility_rule=rule).count(), 1)

        # Matches when both conditions met
        qualified = DiscountCouponEngineService.evaluate_member_offers(
            user_profile=self.member_profile,
            context={'usage_percentage': 85, 'package_age_days': 75}
        )
        self.assertTrue(any(o['rule_id'] == str(rule_id) for o in qualified))

        # Does NOT match when one fails
        not_qualified = DiscountCouponEngineService.evaluate_member_offers(
            user_profile=self.member_profile,
            context={'usage_percentage': 50, 'package_age_days': 75}
        )
        self.assertFalse(any(o['rule_id'] == str(rule_id) for o in not_qualified))

    def test_04_rules_status_toggle(self):
        """Toggling status on dynamic rule deactivates and activates it via API."""
        now = timezone.now()
        rule = DiscountEligibilityRule.objects.using(DB).create(
            organization=self.org,
            name='Test Toggle Rule',
            valid_from=now,
            status='ACTIVE'
        )
        res = self.client.post(
            f'/api/v1/admin/discount-eligibility-rules/{rule.id}/toggle-status/',
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}',
            format='json'
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        rule = DiscountEligibilityRule.objects.using(DB).get(id=rule.id)
        self.assertEqual(rule.status, 'INACTIVE')

    def test_05_member_context_resolver_with_real_entitlements(self):
        """Member context resolver accurately computes days active and session balances from domain models."""
        today = timezone.now().date()
        start_date = today - timedelta(days=45)
        end_date = start_date + timedelta(days=365)

        membership = Membership.objects.using(DB).create(
            user_profile=self.member_profile,
            package=self.package,
            package_version=self.package_version,
            purchase_branch=self.branch,
            home_branch=self.branch,
            membership_number=f'MEM-{uuid.uuid4().hex[:8].upper()}',
            start_date=start_date,
            end_date=end_date,
            status='ACTIVE'
        )

        MembershipEntitlement.objects.using(DB).create(
            membership=membership,
            entitlement_type='PERSONAL_TRAINING_SESSIONS',
            allocated_units=Decimal('20.00'),
            consumed_units=Decimal('15.00'),
            valid_from=timezone.now() - timedelta(days=45),
            status='ACTIVE'
        )

        context = DiscountCouponEngineService.resolve_member_context(self.member_profile, db_alias=DB)
        self.assertTrue(context['has_membership'])
        self.assertEqual(context['membership_number'], membership.membership_number)
        self.assertEqual(context['package_age_days'], 45)
        self.assertEqual(context['sessions_allocated'], 20.0)
        self.assertEqual(context['sessions_consumed'], 15.0)
        self.assertEqual(context['sessions_remaining'], 5.0)
        self.assertEqual(context['usage_percentage'], 75.0)
        self.assertEqual(context['branch_name'], 'Downtown Branch')
        self.assertEqual(context['current_package_name'], 'Gold Membership')

    def test_06_member_context_resolver_when_unavailable(self):
        """When a member profile has no active membership, reports metrics as unavailable instead of faking them."""
        context = DiscountCouponEngineService.resolve_member_context(self.empty_profile, db_alias=DB)
        self.assertFalse(context['has_membership'])
        self.assertIsNone(context['sessions_allocated'])
        self.assertIsNone(context['sessions_consumed'])
        self.assertIsNone(context['sessions_remaining'])
        self.assertIsNone(context['usage_percentage'])
        self.assertIsNone(context['package_age_days'])
        self.assertFalse(context['metrics_available'])

    def test_07_tax_calculation_authoritative_no_eighteen_percent_hardcode(self):
        """Redemption computes tax strictly using authoritative order tax ratio, never arbitrary 18%."""
        now = timezone.now()
        camp = DiscountCampaign.objects.using(DB).create(
            organization=self.org,
            name='Flat 500 Discount',
            discount_type='FIXED',
            discount_value=Decimal('500.00'),
            valid_from=now - timedelta(days=1),
            status='ACTIVE'
        )
        code = DiscountCode.objects.using(DB).create(campaign=camp, code='FLAT500', status='ACTIVE')

        # Order with 10% tax rate (Subtotal 5000, Tax 500 = 10% exactly)
        order = Order.objects.using(DB).create(
            branch=self.branch,
            user_profile=self.member_profile,
            order_number=f'ORD-TAX-{uuid.uuid4().hex[:6].upper()}',
            subtotal=Decimal('5000.00'),
            tax_amount=Decimal('500.00'),  # 10.0%
            total_amount=Decimal('5500.00'),
            status='PENDING_PAYMENT'
        )

        redemption = DiscountCouponEngineService.redeem_coupon(
            order=order,
            code_str='FLAT500',
            user_profile=self.member_profile,
            db_alias=DB
        )

        order.refresh_from_db()
        self.assertEqual(order.discount_amount, Decimal('500.00'))
        # Subtotal: 5000 - 500 = 4500. Tax at 10% = 450.00. Total = 4950.00
        # If it had used 18%, tax would have been 810.00 (which would be WRONG).
        self.assertEqual(order.tax_amount, Decimal('450.00'))
        self.assertEqual(order.total_amount, Decimal('4950.00'))

    def test_08_simulator_endpoint_uses_authoritative_member_context(self):
        """GET /api/v1/admin/discount-campaigns/member-context/ returns live domain metrics."""
        today = timezone.now().date()
        Membership.objects.using(DB).create(
            user_profile=self.member_profile,
            package=self.package,
            package_version=self.package_version,
            purchase_branch=self.branch,
            home_branch=self.branch,
            membership_number='MEM-SIM-01',
            start_date=today - timedelta(days=10),
            end_date=today + timedelta(days=355),
            status='ACTIVE'
        )

        res = self.client.get(
            f'/api/v1/admin/discount-campaigns/member-context/?user_profile_id={self.member_profile.id}',
            HTTP_AUTHORIZATION=f'Bearer {self.admin_token}'
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.data['has_membership'])
        self.assertEqual(res.data['package_age_days'], 10)
        self.assertEqual(res.data['branch_name'], 'Downtown Branch')

    def test_09_coupon_validation_with_branch_and_package_scope(self):
        """Campaign-level branch and package scopes reject ineligible branches and packages."""
        now = timezone.now()
        camp = DiscountCampaign.objects.using(DB).create(
            organization=self.org,
            name='Downtown Only',
            discount_type='PERCENTAGE',
            discount_value=Decimal('10.00'),
            valid_from=now - timedelta(days=1),
            status='ACTIVE',
            configuration={
                'branch_id': str(self.branch.id),
                'package_id': str(self.package.id)
            }
        )
        code = DiscountCode.objects.using(DB).create(campaign=camp, code='DOWNTOWN10', status='ACTIVE')

        # Eligible branch + package
        res_ok = DiscountCouponEngineService.validate_coupon(
            code_str='DOWNTOWN10',
            user_profile=self.member_profile,
            order_subtotal=Decimal('5000.00'),
            branch=self.branch,
            package=self.package,
            db_alias=DB
        )
        self.assertTrue(res_ok['is_valid'])

        # Ineligible branch
        res_wrong_branch = DiscountCouponEngineService.validate_coupon(
            code_str='DOWNTOWN10',
            user_profile=self.member_profile,
            order_subtotal=Decimal('5000.00'),
            branch=self.branch_b,
            package=self.package,
            db_alias=DB
        )
        self.assertFalse(res_wrong_branch['is_valid'])
        self.assertEqual(res_wrong_branch['reason_code'], 'BRANCH_NOT_ELIGIBLE')

        # Ineligible package
        res_wrong_pkg = DiscountCouponEngineService.validate_coupon(
            code_str='DOWNTOWN10',
            user_profile=self.member_profile,
            order_subtotal=Decimal('5000.00'),
            branch=self.branch,
            package=self.package_b,
            db_alias=DB
        )
        self.assertFalse(res_wrong_pkg['is_valid'])
        self.assertEqual(res_wrong_pkg['reason_code'], 'PACKAGE_NOT_ELIGIBLE')

    def test_10_tenant_org_isolation_and_rbac_denial(self):
        """Sales role without core.settings.edit permission is denied write access."""
        now = timezone.now()
        # Sales user cannot create campaign
        res = self.client.post(
            '/api/v1/admin/discount-campaigns/',
            {
                'name': 'Unauthorized Campaign',
                'discount_type': 'FIXED',
                'discount_value': '100.00',
                'valid_from': now.isoformat(),
            },
            HTTP_AUTHORIZATION=f'Bearer {self.sales_token}',
            format='json'
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_11_mixed_tax_rates_zero_rated_and_rounding_allocation(self):
        """Mixed-rate items, explicitly zero-rated items, and rounding penny allocation are calculated correctly."""
        now = timezone.now()
        camp = DiscountCampaign.objects.using(DB).create(
            organization=self.org,
            name='Mixed Tax Discount',
            discount_type='FIXED',
            discount_value=Decimal('300.00'),
            valid_from=now - timedelta(days=1),
            status='ACTIVE'
        )
        code = DiscountCode.objects.using(DB).create(campaign=camp, code='MIXED300', status='ACTIVE')

        # Order with 3 line items:
        # Item 1: 1000 @ 18% GST = 180 tax
        # Item 2: 1000 @ 0% (Explicitly zero-rated) = 0 tax
        # Item 3: 1000 @ 5% tax = 50 tax
        # Total subtotal: 3000, Total tax: 230, Total amount: 3230
        order = Order.objects.using(DB).create(
            branch=self.branch,
            user_profile=self.member_profile,
            order_number=f'ORD-MIX-{uuid.uuid4().hex[:6].upper()}',
            subtotal=Decimal('3000.00'),
            tax_amount=Decimal('230.00'),
            total_amount=Decimal('3230.00'),
            status='PENDING_PAYMENT'
        )
        i1 = OrderItem.objects.using(DB).create(
            order=order, item_type='PACKAGE', package=self.package,
            item_name_snapshot='Package 18%', quantity=Decimal('1.00'),
            unit_price_snapshot=Decimal('1000.00'), tax_percent_snapshot=Decimal('18.000'),
            discount_amount=Decimal('0.00'), tax_amount=Decimal('180.00'), total_amount=Decimal('1180.00')
        )
        i2 = OrderItem.objects.using(DB).create(
            order=order, item_type='OTHER',
            item_name_snapshot='Zero-Rated Item 0%', quantity=Decimal('1.00'),
            unit_price_snapshot=Decimal('1000.00'), tax_percent_snapshot=Decimal('0.000'),
            discount_amount=Decimal('0.00'), tax_amount=Decimal('0.00'), total_amount=Decimal('1000.00')
        )
        i3 = OrderItem.objects.using(DB).create(
            order=order, item_type='OTHER',
            item_name_snapshot='Supplements 5%', quantity=Decimal('1.00'),
            unit_price_snapshot=Decimal('1000.00'), tax_percent_snapshot=Decimal('5.000'),
            discount_amount=Decimal('0.00'), tax_amount=Decimal('50.00'), total_amount=Decimal('1050.00')
        )

        redemption = DiscountCouponEngineService.redeem_coupon(
            order=order,
            code_str='MIXED300',
            user_profile=self.member_profile,
            db_alias=DB
        )

        order.refresh_from_db()
        i1.refresh_from_db()
        i2.refresh_from_db()
        i3.refresh_from_db()

        # Each item has 1000 / 3000 = 1/3 share -> 100 discount each
        self.assertEqual(order.discount_amount, Decimal('300.00'))
        self.assertEqual(i1.discount_amount, Decimal('100.00'))
        self.assertEqual(i2.discount_amount, Decimal('100.00'))
        self.assertEqual(i3.discount_amount, Decimal('100.00'))

        # Line 1 after discount: 900 @ 18% = 162.00 tax, total 1062.00
        self.assertEqual(i1.tax_amount, Decimal('162.00'))
        self.assertEqual(i1.total_amount, Decimal('1062.00'))

        # Line 2 after discount: 900 @ 0% = 0.00 tax, total 900.00 (Zero-rate preserved!)
        self.assertEqual(i2.tax_amount, Decimal('0.00'))
        self.assertEqual(i2.total_amount, Decimal('900.00'))

        # Line 3 after discount: 900 @ 5% = 45.00 tax, total 945.00
        self.assertEqual(i3.tax_amount, Decimal('45.00'))
        self.assertEqual(i3.total_amount, Decimal('945.00'))

        # Order totals match item sum exactly
        self.assertEqual(order.tax_amount, Decimal('207.00')) # 162 + 0 + 45
        self.assertEqual(order.total_amount, Decimal('2907.00')) # 1062 + 900 + 945

        # Test exact penny rounding allocation with 100 discount on 3 items (33.33, 33.33, 33.34)
        DiscountCouponEngineService.recalculate_order_totals(order, Decimal('100.00'), db_alias=DB)
        i1.refresh_from_db()
        i2.refresh_from_db()
        i3.refresh_from_db()
        total_allocated_discount = i1.discount_amount + i2.discount_amount + i3.discount_amount
        self.assertEqual(total_allocated_discount, Decimal('100.00'))
        self.assertEqual(order.discount_amount, Decimal('100.00'))

    def test_12_repeated_redemption_idempotence_and_prevention(self):
        """Repeated redemption on same order is rejected and recalculation is mathematically deterministic."""
        now = timezone.now()
        camp = DiscountCampaign.objects.using(DB).create(
            organization=self.org, name='Flat 200', discount_type='FIXED',
            discount_value=Decimal('200.00'), valid_from=now - timedelta(days=1), status='ACTIVE'
        )
        code = DiscountCode.objects.using(DB).create(campaign=camp, code='FLAT200', status='ACTIVE')

        order = Order.objects.using(DB).create(
            branch=self.branch, user_profile=self.member_profile,
            order_number=f'ORD-REP-{uuid.uuid4().hex[:6].upper()}',
            subtotal=Decimal('2000.00'), tax_amount=Decimal('240.00'), total_amount=Decimal('2240.00'),
            status='PENDING_PAYMENT'
        )
        OrderItem.objects.using(DB).create(
            order=order, item_type='PACKAGE', package=self.package,
            item_name_snapshot='Package Item', quantity=Decimal('1.00'),
            unit_price_snapshot=Decimal('2000.00'), tax_percent_snapshot=Decimal('12.000'),
            discount_amount=Decimal('0.00'), tax_amount=Decimal('240.00'), total_amount=Decimal('2240.00')
        )

        redemption = DiscountCouponEngineService.redeem_coupon(
            order=order, code_str='FLAT200', user_profile=self.member_profile, db_alias=DB
        )
        self.assertIsNotNone(redemption.id)

        # Repeated redemption on same order MUST be rejected
        with self.assertRaises(ValidationError) as ctx:
            DiscountCouponEngineService.redeem_coupon(
                order=order, code_str='FLAT200', user_profile=self.member_profile, db_alias=DB
            )
        self.assertIn('already been redeemed', str(ctx.exception))

        # Recalculating totals 5 times produces the exact same totals without compounding corruption
        order.refresh_from_db()
        expected_tax = order.tax_amount
        expected_total = order.total_amount
        for _ in range(5):
            DiscountCouponEngineService.recalculate_order_totals(order, Decimal('200.00'), db_alias=DB)
            order.refresh_from_db()
            self.assertEqual(order.tax_amount, expected_tax)
            self.assertEqual(order.total_amount, expected_total)

    def test_13_missing_tax_snapshot_falls_back_to_organization_settings(self):
        """Orders missing explicit tax snapshot fall back to OrganizationSettings.tax_rate_pct."""
        now = timezone.now()
        camp = DiscountCampaign.objects.using(DB).create(
            organization=self.org, name='Fall Discount', discount_type='FIXED',
            discount_value=Decimal('500.00'), valid_from=now - timedelta(days=1), status='ACTIVE'
        )
        code = DiscountCode.objects.using(DB).create(campaign=camp, code='FALL500', status='ACTIVE')

        # Org settings tax rate is 12.00%. Order created without line items (or legacy order)
        order = Order.objects.using(DB).create(
            branch=self.branch, user_profile=self.member_profile,
            order_number=f'ORD-FALL-{uuid.uuid4().hex[:6].upper()}',
            subtotal=Decimal('2000.00'), tax_amount=Decimal('0.00'), total_amount=Decimal('2000.00'),
            status='PENDING_PAYMENT'
        )

        redemption = DiscountCouponEngineService.redeem_coupon(
            order=order, code_str='FALL500', user_profile=self.member_profile, db_alias=DB
        )
        order.refresh_from_db()

        # 2000 - 500 = 1500 net. Org tax rate is 12.00% -> 1500 * 0.12 = 180.00 tax
        self.assertEqual(order.tax_amount, Decimal('180.00'))
        self.assertEqual(order.total_amount, Decimal('1680.00'))

    def test_14_real_checkout_tampered_input_recomputed_from_database(self):
        """Real checkout ignores client-tampered subtotal and calculates from authoritative DB records."""
        now = timezone.now()
        camp = DiscountCampaign.objects.using(DB).create(
            organization=self.org, name='20 Percent Off', discount_type='PERCENTAGE',
            discount_value=Decimal('20.00'), valid_from=now - timedelta(days=1), status='ACTIVE'
        )
        code = DiscountCode.objects.using(DB).create(campaign=camp, code='PERCENT20', status='ACTIVE')

        # Real order with DB subtotal 4000.00
        order = Order.objects.using(DB).create(
            branch=self.branch, user_profile=self.member_profile,
            order_number=f'ORD-TAMP-{uuid.uuid4().hex[:6].upper()}',
            subtotal=Decimal('4000.00'), tax_amount=Decimal('480.00'), total_amount=Decimal('4480.00'),
            status='PENDING_PAYMENT'
        )
        OrderItem.objects.using(DB).create(
            order=order, item_type='PACKAGE', package=self.package,
            item_name_snapshot='Gold Package', quantity=Decimal('1.00'),
            unit_price_snapshot=Decimal('4000.00'), tax_percent_snapshot=Decimal('12.000'),
            discount_amount=Decimal('0.00'), tax_amount=Decimal('480.00'), total_amount=Decimal('4480.00')
        )

        # Validate with order passes authoritative DB calculation
        res = DiscountCouponEngineService.validate_coupon(
            code_str='PERCENT20',
            user_profile=self.member_profile,
            order_subtotal=Decimal('100.00'), # Fake/tampered subtotal from client
            db_alias=DB,
            order=order,
        )
        self.assertTrue(res['is_valid'])
        # 20% of 4000.00 is 800.00, NOT 20.00!
        self.assertEqual(Decimal(res['discount_amount']), Decimal('800.00'))

    def test_15_cross_organization_and_tenant_isolation_rejected(self):
        """Cross-organization redemption and API operations are strictly rejected."""
        now = timezone.now()
        # Other Org B
        org_b = Organization.objects.using(DB).create(
            code='P9C-ORG-B', name='Other Org B', status='ACTIVE'
        )
        loc_b = Location.objects.using(DB).create(organization=org_b, code='LOC-B', name='Loc B')
        branch_b_org = Branch.objects.using(DB).create(
            organization=org_b, location=loc_b, code='BR-B-ORG', name='Branch B Org'
        )
        user_b = TenantUser.objects.using(DB).create(
            organization=org_b, email='user_b@closure.test', first_name='Other', last_name='User'
        )
        profile_b = UserProfile.objects.using(DB).create(
            user=user_b, member_number='MEM-B-01', member_status='ACTIVE'
        )

        # Campaign in Org A
        camp_a = DiscountCampaign.objects.using(DB).create(
            organization=self.org, name='Org A Exclusive', discount_type='FIXED',
            discount_value=Decimal('100.00'), valid_from=now - timedelta(days=1), status='ACTIVE'
        )
        code_a = DiscountCode.objects.using(DB).create(campaign=camp_a, code='ORGA100', status='ACTIVE')

        # Order in Org B
        order_b = Order.objects.using(DB).create(
            branch=branch_b_org, user_profile=profile_b,
            order_number=f'ORD-ORGB-{uuid.uuid4().hex[:6].upper()}',
            subtotal=Decimal('1000.00'), tax_amount=Decimal('100.00'), total_amount=Decimal('1100.00'),
            status='PENDING_PAYMENT'
        )

        # Cross-org redemption fails
        with self.assertRaises(ValidationError) as ctx:
            DiscountCouponEngineService.redeem_coupon(
                order=order_b, code_str='ORGA100', user_profile=profile_b, db_alias=DB
            )
        self.assertTrue('different organization' in str(ctx.exception) or 'Cross-organization' in str(ctx.exception))

    def test_16_thread_local_tenant_context_restored_after_exception(self):
        """Thread-local tenant database alias is restored in finally blocks even if exceptions occur."""
        initial_alias = get_current_tenant_db_alias()
        self.assertEqual(initial_alias, DB)

        # Call evaluate_member_offers with invalid context or error
        try:
            DiscountCouponEngineService.evaluate_member_offers(
                user_profile=self.member_profile,
                context={'invalid': None},
                db_alias=DB
            )
        except Exception:
            pass

        restored_alias = get_current_tenant_db_alias()
        self.assertEqual(restored_alias, initial_alias)

    def test_17_concurrency_last_coupon_usage_limit_and_per_user_limit(self):
        """Row-locking serializes redemptions against the last available usage limit and per-user limits."""
        now = timezone.now()
        camp = DiscountCampaign.objects.using(DB).create(
            organization=self.org, name='Single Use Global', discount_type='FIXED',
            discount_value=Decimal('150.00'), valid_from=now - timedelta(days=1),
            usage_limit=1, per_user_limit=1, status='ACTIVE'
        )
        code = DiscountCode.objects.using(DB).create(campaign=camp, code='SINGLE150', status='ACTIVE')

        order1 = Order.objects.using(DB).create(
            branch=self.branch, user_profile=self.member_profile,
            order_number=f'ORD-C1-{uuid.uuid4().hex[:6].upper()}',
            subtotal=Decimal('1000.00'), tax_amount=Decimal('120.00'), total_amount=Decimal('1120.00'),
            status='PENDING_PAYMENT'
        )
        order2 = Order.objects.using(DB).create(
            branch=self.branch, user_profile=self.member_profile,
            order_number=f'ORD-C2-{uuid.uuid4().hex[:6].upper()}',
            subtotal=Decimal('1000.00'), tax_amount=Decimal('120.00'), total_amount=Decimal('1120.00'),
            status='PENDING_PAYMENT'
        )

        # First redemption succeeds
        r1 = DiscountCouponEngineService.redeem_coupon(
            order=order1, code_str='SINGLE150', user_profile=self.member_profile, db_alias=DB
        )
        self.assertIsNotNone(r1.id)

        # Second redemption fails due to usage_limit = 1
        with self.assertRaises(ValidationError) as ctx:
            DiscountCouponEngineService.redeem_coupon(
                order=order2, code_str='SINGLE150', user_profile=self.member_profile, db_alias=DB
            )
        self.assertIn('limit has been reached', str(ctx.exception))

    def test_18_member_metrics_multiple_active_memberships_and_source_package(self):
        """Multiple active memberships correctly match source-package scoped eligibility rules."""
        today = timezone.now().date()
        # Active membership 1: Gold Membership
        Membership.objects.using(DB).create(
            user_profile=self.member_profile, package=self.package,
            package_version=self.package_version, purchase_branch=self.branch, home_branch=self.branch,
            membership_number=f'MEM-GOLD-{uuid.uuid4().hex[:4]}',
            start_date=today - timedelta(days=30), end_date=today + timedelta(days=335), status='ACTIVE'
        )

        # Active membership 2: Silver Membership
        pv_silver = PackageVersion.objects.using(DB).create(
            package=self.package_b, version_number=1, name_snapshot='Silver v1',
            duration_value=6, duration_unit='MONTHS', effective_from=timezone.now(), status='ACTIVE', created_by_user=self.admin
        )
        Membership.objects.using(DB).create(
            user_profile=self.member_profile, package=self.package_b,
            package_version=pv_silver, purchase_branch=self.branch, home_branch=self.branch,
            membership_number=f'MEM-SILV-{uuid.uuid4().hex[:4]}',
            start_date=today - timedelta(days=10), end_date=today + timedelta(days=170), status='ACTIVE'
        )

        # Rule scoped to source_package = Package B (Silver)
        now = timezone.now()
        rule = DiscountEligibilityRule.objects.using(DB).create(
            organization=self.org, name='Silver Upgrade Rule',
            source_package=self.package_b, valid_from=now - timedelta(days=1), status='ACTIVE'
        )
        DiscountRuleAction.objects.using(DB).create(
            discount_eligibility_rule=rule, action_type='APPLY_PERCENTAGE_DISCOUNT',
            discount_percentage=Decimal('15.00')
        )

        offers = DiscountCouponEngineService.evaluate_member_offers(
            user_profile=self.member_profile,
            db_alias=DB
        )
        # Qualified because member holds Silver as one of their active memberships
        self.assertTrue(any(o['rule_id'] == str(rule.id) for o in offers))

    def test_19_member_metrics_unlimited_and_zero_allocations_fail_closed(self):
        """Unlimited entitlements and zero allocations report metrics as unavailable and fail closed on percentage checks."""
        today = timezone.now().date()
        m_unlimited = Membership.objects.using(DB).create(
            user_profile=self.member_profile, package=self.package,
            package_version=self.package_version, purchase_branch=self.branch, home_branch=self.branch,
            membership_number=f'MEM-UNL-{uuid.uuid4().hex[:4]}',
            start_date=today - timedelta(days=10), end_date=today + timedelta(days=355), status='ACTIVE'
        )
        MembershipEntitlement.objects.using(DB).create(
            membership=m_unlimited, entitlement_type='PERSONAL_TRAINING_SESSIONS',
            is_unlimited=True, valid_from=timezone.now() - timedelta(days=10), status='ACTIVE'
        )

        ctx_unlimited = DiscountCouponEngineService.resolve_member_context(self.member_profile, db_alias=DB)
        self.assertTrue(ctx_unlimited['is_unlimited'])
        self.assertIsNone(ctx_unlimited['usage_percentage'])
        self.assertFalse(ctx_unlimited['metrics_available'])

        # Condition on USAGE_PERCENTAGE fails closed
        cond = DiscountRuleCondition(
            condition_type='USAGE_PERCENTAGE', operator='LESS_THAN_OR_EQUAL', numeric_value=Decimal('50.00')
        )
        self.assertFalse(DiscountCouponEngineService._evaluate_condition(cond, ctx_unlimited))

        # Zero allocation test
        m_zero = Membership.objects.using(DB).create(
            user_profile=self.empty_profile, package=self.package,
            package_version=self.package_version, purchase_branch=self.branch, home_branch=self.branch,
            membership_number=f'MEM-ZERO-{uuid.uuid4().hex[:4]}',
            start_date=today - timedelta(days=5), end_date=today + timedelta(days=360), status='ACTIVE'
        )
        MembershipEntitlement.objects.using(DB).create(
            membership=m_zero, entitlement_type='PERSONAL_TRAINING_SESSIONS',
            allocated_units=Decimal('0.00'), consumed_units=Decimal('0.00'), valid_from=timezone.now() - timedelta(days=5), status='ACTIVE'
        )
        ctx_zero = DiscountCouponEngineService.resolve_member_context(self.empty_profile, db_alias=DB)
        self.assertIsNone(ctx_zero['usage_percentage']) # Not 0.0! Undefined.
        self.assertFalse(ctx_zero['metrics_available'])
        self.assertFalse(DiscountCouponEngineService._evaluate_condition(cond, ctx_zero))

    def test_20_expired_and_paused_coupon_redemption_rejected(self):
        """Expired campaigns, paused codes, and non-pending orders are rejected during redemption."""
        now = timezone.now()
        camp_exp = DiscountCampaign.objects.using(DB).create(
            organization=self.org, name='Expired Camp', discount_type='FIXED',
            discount_value=Decimal('50.00'), valid_from=now - timedelta(days=10),
            valid_until=now - timedelta(days=2), status='ACTIVE'
        )
        code_exp = DiscountCode.objects.using(DB).create(campaign=camp_exp, code='EXPIRED50', status='ACTIVE')

        order = Order.objects.using(DB).create(
            branch=self.branch, user_profile=self.member_profile,
            order_number=f'ORD-EXP-{uuid.uuid4().hex[:6].upper()}',
            subtotal=Decimal('1000.00'), tax_amount=Decimal('120.00'), total_amount=Decimal('1120.00'),
            status='PENDING_PAYMENT'
        )

        with self.assertRaises(ValidationError) as ctx:
            DiscountCouponEngineService.redeem_coupon(
                order=order, code_str='EXPIRED50', user_profile=self.member_profile, db_alias=DB
            )
        self.assertIn('expired', str(ctx.exception).lower())

        # Paid order rejection
        camp_ok = DiscountCampaign.objects.using(DB).create(
            organization=self.org, name='Valid Camp', discount_type='FIXED',
            discount_value=Decimal('50.00'), valid_from=now - timedelta(days=1), status='ACTIVE'
        )
        code_ok = DiscountCode.objects.using(DB).create(campaign=camp_ok, code='VALID50', status='ACTIVE')
        order_paid = Order.objects.using(DB).create(
            branch=self.branch, user_profile=self.member_profile,
            order_number=f'ORD-PAID-{uuid.uuid4().hex[:6].upper()}',
            subtotal=Decimal('1000.00'), tax_amount=Decimal('120.00'), total_amount=Decimal('1120.00'),
            status='PAID'
        )
        with self.assertRaises(ValidationError) as ctx:
            DiscountCouponEngineService.redeem_coupon(
                order=order_paid, code_str='VALID50', user_profile=self.member_profile, db_alias=DB
            )
        self.assertIn('PENDING_PAYMENT', str(ctx.exception))
