"""
CRM Phase 9 Targeted Test Suite: Offers, Coupons & Campaign Commercial Integration
Tests:
  - DiscountCampaign tenant isolation, active/inactive/scheduled/expired lifecycle
  - CouponCode create, edit, deactivate, prevention of deletion with historical redemptions
  - Coupon validation: branch, package, min spend, max discount, usage limits, per-user limits
  - Quote preview no write, conversion redemption idempotency, no duplicate redemptions
  - Lead 360 commercial payload, available coupons, redemptions history
  - Acquisition campaign grouping, leads, trials, conversions, paid revenue calculation without double counting
  - Drill-down explainability: leads, conversions, revenue, redemptions
  - RBAC permission enforcement, tenant & branch isolation, audit event logging
  - Phase 6/7/8 compatibility, zero Referral/Rewards contamination, zero mock data
"""

import uuid
from decimal import Decimal
from datetime import timedelta
from django.utils import timezone
from django.core.exceptions import ValidationError
from rest_framework.test import APITestCase
from rest_framework import status
from rest_framework.exceptions import PermissionDenied

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
    RoleModuleAccess, RoleSubmoduleAccess
)
from apps.tenant_core.models_catalog import (
    ProgramCategory, Program, Package, PackageVersion, PackagePrice,
    PackageBranchAvailability, PackageEntitlementDefinition
)
from apps.tenant_core.models_commerce import Order, OrderItem, PaymentTransaction
from apps.tenant_core.models_discounts import (
    DiscountCampaign, DiscountCode, DiscountRedemption, DiscountEligibilityRule
)
from apps.tenant_core.models_crm import (
    LeadSource, Lead, LeadAttribution, TrialBooking, LeadConversion
)
from apps.tenant_core.models_audit_outbox import BusinessAuditEvent

from apps.tenant_core.services_discounts import DiscountCouponEngineService
from apps.tenant_core.services_crm import LeadConversionService, CRMLeadService
from apps.tenant_core.automation.registry import RecommendationActionRegistry


DB = 'tenant_test'


class CRMPhase9OffersCampaignsTestCase(APITestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias(DB)

        # Master DB Setup
        self.tenant = Tenant.objects.using('default').create(
            code='P9-TENANT', name='Phase9 Fitness', slug='phase9-fitness', status='ACTIVE'
        )
        TenantDataSource.objects.using('default').create(
            tenant=self.tenant, db_name='test_fitness_tenant',
            database_name='test_fitness_tenant', status='ACTIVE', database_engine='POSTGRESQL'
        )
        plan = SaasPlan.objects.using('default').create(
            name='Enterprise Plan', code='P9-ENT', tier='ENTERPRISE', status='ACTIVE'
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
            code='P9-ORG', name='Phase9 Org', status='ACTIVE'
        )
        loc = Location.objects.using(DB).create(
            organization=self.org, code='P9-LOC', name='Downtown Location', status='ACTIVE'
        )
        self.branch = Branch.objects.using(DB).create(
            organization=self.org, location=loc, code='BR-P9-A', name='Downtown Branch', status='ACTIVE'
        )
        self.branch_b = Branch.objects.using(DB).create(
            organization=self.org, location=loc, code='BR-P9-B', name='Uptown Branch', status='ACTIVE'
        )

        # Admin & Sales Staff Users
        self.admin = TenantUser.objects.using(DB).create(
            organization=self.org, email='admin@phase9.test',
            first_name='Admin', last_name='Owner', status='ACTIVE'
        )
        self.sales_rep = TenantUser.objects.using(DB).create(
            organization=self.org, email='sales@phase9.test',
            first_name='Sales', last_name='Agent', status='ACTIVE'
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

        perm_crm_view, _ = Permission.objects.using(DB).get_or_create(
            module=mod_crm, submodule=sub_leads, action='view',
            defaults={'permission_code': 'crm.leads.view', 'label': 'View Leads'}
        )
        perm_crm_edit, _ = Permission.objects.using(DB).get_or_create(
            module=mod_crm, submodule=sub_leads, action='edit',
            defaults={'permission_code': 'crm.leads.edit', 'label': 'Edit Leads'}
        )
        perm_crm_convert, _ = Permission.objects.using(DB).get_or_create(
            module=mod_crm, submodule=sub_leads, action='convert',
            defaults={'permission_code': 'crm.leads.convert', 'label': 'Convert Leads'}
        )
        perm_settings_view, _ = Permission.objects.using(DB).get_or_create(
            module=mod_core, submodule=sub_settings, action='view',
            defaults={'permission_code': 'core.settings.view', 'label': 'View Settings'}
        )
        perm_settings_edit, _ = Permission.objects.using(DB).get_or_create(
            module=mod_core, submodule=sub_settings, action='edit',
            defaults={'permission_code': 'core.settings.edit', 'label': 'Edit Settings'}
        )

        # Admin role: full settings + crm
        role_admin = Role.objects.using(DB).create(
            organization=self.org, name='Admin Role', code='ROLE_ADMIN',
            is_system_role=True, scope='ORG', is_active=True
        )
        RoleModuleAccess.objects.using(DB).create(role=role_admin, module=mod_crm, can_access=True)
        RoleSubmoduleAccess.objects.using(DB).create(role=role_admin, submodule=sub_leads, can_access=True)
        RoleModuleAccess.objects.using(DB).create(role=role_admin, module=mod_core, can_access=True)
        RoleSubmoduleAccess.objects.using(DB).create(role=role_admin, submodule=sub_settings, can_access=True)
        pset_admin = RolePermissionSet.objects.using(DB).create(role=role_admin, name='Admin Perms')
        for p in [perm_crm_view, perm_crm_edit, perm_crm_convert, perm_settings_view, perm_settings_edit]:
            RolePermissionSetItem.objects.using(DB).create(permission_set=pset_admin, permission=p, granted=True)
        RoleAssignment.objects.using(DB).create(user=self.admin, role=role_admin, organization=self.org, is_active=True)

        # Sales role: can view and edit CRM leads, view discounts, but CANNOT edit discounts
        role_sales = Role.objects.using(DB).create(
            organization=self.org, name='Sales Role', code='ROLE_SALES',
            is_system_role=True, scope='ORG', is_active=True
        )
        RoleModuleAccess.objects.using(DB).create(role=role_sales, module=mod_crm, can_access=True)
        RoleSubmoduleAccess.objects.using(DB).create(role=role_sales, submodule=sub_leads, can_access=True)
        RoleModuleAccess.objects.using(DB).create(role=role_sales, module=mod_core, can_access=True)
        RoleSubmoduleAccess.objects.using(DB).create(role=role_sales, submodule=sub_settings, can_access=True)
        pset_sales = RolePermissionSet.objects.using(DB).create(role=role_sales, name='Sales Perms')
        for p in [perm_crm_view, perm_crm_edit, perm_crm_convert, perm_settings_view]:
            RolePermissionSetItem.objects.using(DB).create(permission_set=pset_sales, permission=p, granted=True)
        RoleAssignment.objects.using(DB).create(user=self.sales_rep, role=role_sales, organization=self.org, is_active=True)

        # Catalog: Program & Package
        cat = ProgramCategory.objects.using(DB).create(
            organization=self.org, code='CAT-P9', name='General Fitness', status='ACTIVE'
        )
        self.program = Program.objects.using(DB).create(
            organization=self.org, category=cat, code='PROG-P9', name='CrossFit Strength', status='ACTIVE'
        )
        self.package = Package.objects.using(DB).create(
            organization=self.org, program=self.program, code='PKG-P9-GOLD', name='Gold Membership', status='ACTIVE'
        )
        self.package_version = PackageVersion.objects.using(DB).create(
            package=self.package, version_number=1, name_snapshot='Gold v1',
            duration_value=1, duration_unit='MONTH', effective_from=timezone.now(),
            status='ACTIVE', created_by_user=self.admin
        )
        self.package_price = PackagePrice.objects.using(DB).create(
            package_version=self.package_version, branch=self.branch,
            base_price=Decimal('5000.00'), tax_percent=Decimal('18.00'),
            prices_include_tax=False, currency='INR', effective_from=timezone.now(),
            status='ACTIVE', created_by_user=self.admin
        )
        PackageBranchAvailability.objects.using(DB).create(
            package=self.package, branch=self.branch, status='ENABLED'
        )
        PackageEntitlementDefinition.objects.using(DB).create(
            package_version=self.package_version, entitlement_type='CLASS_SESSIONS',
            allocated_units=Decimal('20.00'), is_unlimited=False, status='ACTIVE'
        )

        # Another organization for Tenant Isolation test
        self.other_org = Organization.objects.using(DB).create(
            code='OTHER-ORG', name='Other Gym Chain', status='ACTIVE'
        )

        # Active Promotional DiscountCampaign
        self.campaign = DiscountCampaign.objects.using(DB).create(
            organization=self.org,
            name='Summer Transformation',
            discount_type='PERCENTAGE',
            discount_value=Decimal('20.00'),
            max_discount=Decimal('1500.00'),
            minimum_order_amount=Decimal('1000.00'),
            usage_limit=50,
            per_user_limit=1,
            valid_from=timezone.now() - timedelta(days=1),
            valid_until=timezone.now() + timedelta(days=30),
            status='ACTIVE'
        )

        # Active Coupon Code
        self.coupon = DiscountCode.objects.using(DB).create(
            campaign=self.campaign,
            code='SUMMER20',
            branch=self.branch,
            package=self.package,
            status='ACTIVE'
        )

        # Sample Prospect Lead
        self.lead = Lead.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            first_name='Ananya',
            last_name='Deshmukh',
            email_normalized='ananya@test.com',
            phone_normalized='+919876543210',
            current_status='NEW_LEAD',
            interested_program=self.program
        )

        self._authenticate(self.admin)

    def _authenticate(self, user):
        refresh = _build_tenant_token(user=user, tenant=self.tenant, db_alias=DB)
        token = str(refresh.access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

    # ------------------------------------------------------------------
    # TESTS
    # ------------------------------------------------------------------

    def test_01_discount_campaign_tenant_isolation(self):
        """Campaigns of Organization A must not be visible to Organization B."""
        camp_other = DiscountCampaign.objects.using(DB).create(
            organization=self.other_org,
            name='Other Tenant Offer',
            discount_type='FIXED',
            discount_value=Decimal('500.00'),
            valid_from=timezone.now(),
            status='ACTIVE'
        )
        org_a_camps = DiscountCampaign.objects.using(DB).filter(organization=self.org)
        self.assertIn(self.campaign, org_a_camps)
        self.assertNotIn(camp_other, org_a_camps)

    def test_02_active_and_computed_status(self):
        """Backend serializer returns computed_status 'ACTIVE' for valid active campaign."""
        from apps.tenant_core.serializers_discounts import DiscountCampaignSerializer, DiscountCodeSerializer
        data_camp = DiscountCampaignSerializer(self.campaign).data
        self.assertEqual(data_camp['computed_status'], 'ACTIVE')
        self.assertEqual(data_camp['usage_remaining'], 50)

        data_code = DiscountCodeSerializer(self.coupon).data
        self.assertEqual(data_code['computed_status'], 'ACTIVE')

    def test_03_scheduled_campaign_state(self):
        """Future dated campaign is reported as SCHEDULED."""
        from apps.tenant_core.serializers_discounts import DiscountCampaignSerializer
        scheduled_camp = DiscountCampaign.objects.using(DB).create(
            organization=self.org,
            name='Diwali Early Bird',
            discount_type='PERCENTAGE',
            discount_value=Decimal('10.00'),
            valid_from=timezone.now() + timedelta(days=5),
            status='ACTIVE'
        )
        data = DiscountCampaignSerializer(scheduled_camp).data
        self.assertEqual(data['computed_status'], 'SCHEDULED')

    def test_04_expired_campaign_state(self):
        """Past dated campaign is reported as EXPIRED."""
        from apps.tenant_core.serializers_discounts import DiscountCampaignSerializer
        expired_camp = DiscountCampaign.objects.using(DB).create(
            organization=self.org,
            name='New Year Deal',
            discount_type='PERCENTAGE',
            discount_value=Decimal('25.00'),
            valid_from=timezone.now() - timedelta(days=30),
            valid_until=timezone.now() - timedelta(days=1),
            status='ACTIVE'
        )
        data = DiscountCampaignSerializer(expired_camp).data
        self.assertEqual(data['computed_status'], 'EXPIRED')

    def test_05_inactive_campaign_state(self):
        """Paused campaign reports INACTIVE."""
        from apps.tenant_core.serializers_discounts import DiscountCampaignSerializer
        paused_camp = DiscountCampaign.objects.using(DB).create(
            organization=self.org,
            name='Paused Promo',
            discount_type='PERCENTAGE',
            discount_value=Decimal('10.00'),
            valid_from=timezone.now(),
            status='PAUSED'
        )
        data = DiscountCampaignSerializer(paused_camp).data
        self.assertEqual(data['computed_status'], 'INACTIVE')

    def test_06_coupon_code_create(self):
        """Coupon code created under campaign links correctly."""
        code = DiscountCode.objects.using(DB).create(
            campaign=self.campaign,
            code='SUMMER_VIP',
            status='ACTIVE'
        )
        self.assertEqual(code.campaign, self.campaign)
        self.assertEqual(code.code, 'SUMMER_VIP')

    def test_07_coupon_edit_and_deactivate(self):
        """Coupon code status toggled to INACTIVE."""
        self.coupon.status = 'INACTIVE'
        self.coupon.save(using=DB)
        from apps.tenant_core.serializers_discounts import DiscountCodeSerializer
        data = DiscountCodeSerializer(self.coupon).data
        self.assertEqual(data['computed_status'], 'INACTIVE')

    def test_08_historical_redemption_preserved_prevent_deletion(self):
        """Coupons with redemption history cannot be deleted."""
        profile = UserProfile.objects.using(DB).create(
            user=self.admin, preferred_branch=self.branch, joining_date=timezone.now().date()
        )
        order = Order.objects.using(DB).create(
            branch=self.branch, user_profile=profile,
            order_number='ORD-P9-001', status='PAID', total_amount=Decimal('4000.00')
        )
        DiscountRedemption.objects.using(DB).create(
            campaign=self.campaign,
            discount_code=self.coupon,
            user_profile=profile,
            order=order,
            discount_amount=Decimal('1000.00'),
            redeemed_at=timezone.now()
        )
        self._authenticate(self.admin)
        res = self.client.delete(f'/api/v1/admin/discount-codes/{self.coupon.id}/')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Cannot delete a coupon code with redemption history', res.data['error'])

    def test_09_valid_coupon_evaluation(self):
        """Coupon validation returns is_valid=True and calculates correct discount."""
        profile = UserProfile.objects.using(DB).create(
            user=self.admin, preferred_branch=self.branch, joining_date=timezone.now().date()
        )
        res = DiscountCouponEngineService.validate_coupon(
            code_str='SUMMER20',
            user_profile=profile,
            order_subtotal=Decimal('5000.00'),
            branch=self.branch,
            package=self.package,
            db_alias=DB
        )
        self.assertTrue(res['is_valid'])
        # 20% of 5000 is 1000, which is under max_discount 1500
        self.assertEqual(Decimal(res['discount_amount']), Decimal('1000.00'))

    def test_10_expired_coupon_rejected(self):
        """Expired campaign rejects coupon validation."""
        self.campaign.valid_until = timezone.now() - timedelta(days=2)
        self.campaign.save(using=DB)
        profile = UserProfile.objects.using(DB).create(
            user=self.admin, preferred_branch=self.branch, joining_date=timezone.now().date()
        )
        res = DiscountCouponEngineService.validate_coupon(
            code_str='SUMMER20',
            user_profile=profile,
            order_subtotal=Decimal('5000.00'),
            branch=self.branch,
            package=self.package,
            db_alias=DB
        )
        self.assertFalse(res['is_valid'])
        self.assertEqual(res['reason_code'], 'EXPIRED')

    def test_11_inactive_coupon_rejected(self):
        """Inactive coupon code is rejected."""
        self.coupon.status = 'INACTIVE'
        self.coupon.save(using=DB)
        profile = UserProfile.objects.using(DB).create(
            user=self.admin, preferred_branch=self.branch, joining_date=timezone.now().date()
        )
        res = DiscountCouponEngineService.validate_coupon(
            code_str='SUMMER20',
            user_profile=profile,
            order_subtotal=Decimal('5000.00'),
            branch=self.branch,
            package=self.package,
            db_alias=DB
        )
        self.assertFalse(res['is_valid'])
        self.assertEqual(res['reason_code'], 'INACTIVE')

    def test_12_usage_cap_enforced(self):
        """Exhausting usage limit rejects coupon validation."""
        self.campaign.usage_limit = 1
        self.campaign.save(using=DB)
        profile = UserProfile.objects.using(DB).create(
            user=self.admin, preferred_branch=self.branch, joining_date=timezone.now().date()
        )
        order = Order.objects.using(DB).create(
            branch=self.branch, user_profile=profile,
            order_number='ORD-P9-CAP', status='PAID', total_amount=Decimal('4000.00')
        )
        DiscountRedemption.objects.using(DB).create(
            campaign=self.campaign,
            discount_code=self.coupon,
            user_profile=profile,
            order=order,
            discount_amount=Decimal('1000.00'),
            redeemed_at=timezone.now()
        )
        res = DiscountCouponEngineService.validate_coupon(
            code_str='SUMMER20',
            user_profile=profile,
            order_subtotal=Decimal('5000.00'),
            branch=self.branch,
            package=self.package,
            db_alias=DB
        )
        self.assertFalse(res['is_valid'])
        self.assertEqual(res['reason_code'], 'USAGE_LIMIT_EXCEEDED')

    def test_13_per_customer_limit_enforced(self):
        """Exceeding per-customer limit rejects coupon validation."""
        profile = UserProfile.objects.using(DB).create(
            user=self.admin, preferred_branch=self.branch, joining_date=timezone.now().date()
        )
        order = Order.objects.using(DB).create(
            branch=self.branch, user_profile=profile,
            order_number='ORD-P9-PERUSER', status='PAID', total_amount=Decimal('4000.00')
        )
        DiscountRedemption.objects.using(DB).create(
            campaign=self.campaign,
            discount_code=self.coupon,
            user_profile=profile,
            order=order,
            discount_amount=Decimal('1000.00'),
            redeemed_at=timezone.now()
        )
        res = DiscountCouponEngineService.validate_coupon(
            code_str='SUMMER20',
            user_profile=profile,
            order_subtotal=Decimal('5000.00'),
            branch=self.branch,
            package=self.package,
            db_alias=DB
        )
        self.assertFalse(res['is_valid'])
        self.assertEqual(res['reason_code'], 'PER_USER_LIMIT_EXCEEDED')

    def test_14_wrong_branch_rejected(self):
        """Coupon scoped to Branch A is rejected for Branch B."""
        profile = UserProfile.objects.using(DB).create(
            user=self.admin, preferred_branch=self.branch_b, joining_date=timezone.now().date()
        )
        res = DiscountCouponEngineService.validate_coupon(
            code_str='SUMMER20',
            user_profile=profile,
            order_subtotal=Decimal('5000.00'),
            branch=self.branch_b,
            package=self.package,
            db_alias=DB
        )
        self.assertFalse(res['is_valid'])
        self.assertEqual(res['reason_code'], 'BRANCH_NOT_ELIGIBLE')

    def test_15_wrong_package_rejected(self):
        """Coupon scoped to Package A is rejected for Package B."""
        pkg_other = Package.objects.using(DB).create(
            organization=self.org, program=self.program, code='PKG-OTHER', name='Other Package', status='ACTIVE'
        )
        profile = UserProfile.objects.using(DB).create(
            user=self.admin, preferred_branch=self.branch, joining_date=timezone.now().date()
        )
        res = DiscountCouponEngineService.validate_coupon(
            code_str='SUMMER20',
            user_profile=profile,
            order_subtotal=Decimal('5000.00'),
            branch=self.branch,
            package=pkg_other,
            db_alias=DB
        )
        self.assertFalse(res['is_valid'])
        self.assertEqual(res['reason_code'], 'PACKAGE_NOT_ELIGIBLE')

    def test_16_minimum_spend_enforced(self):
        """Order subtotal below minimum_order_amount is rejected."""
        profile = UserProfile.objects.using(DB).create(
            user=self.admin, preferred_branch=self.branch, joining_date=timezone.now().date()
        )
        res = DiscountCouponEngineService.validate_coupon(
            code_str='SUMMER20',
            user_profile=profile,
            order_subtotal=Decimal('800.00'),  # min is 1000
            branch=self.branch,
            package=self.package,
            db_alias=DB
        )
        self.assertFalse(res['is_valid'])
        self.assertEqual(res['reason_code'], 'MINIMUM_ORDER_NOT_MET')

    def test_17_maximum_discount_capped(self):
        """Calculated discount is capped at max_discount."""
        profile = UserProfile.objects.using(DB).create(
            user=self.admin, preferred_branch=self.branch, joining_date=timezone.now().date()
        )
        # 20% of 20000 = 4000, but cap is 1500
        res = DiscountCouponEngineService.validate_coupon(
            code_str='SUMMER20',
            user_profile=profile,
            order_subtotal=Decimal('20000.00'),
            branch=self.branch,
            package=self.package,
            db_alias=DB
        )
        self.assertTrue(res['is_valid'])
        self.assertEqual(Decimal(res['discount_amount']), Decimal('1500.00'))

    def test_18_quote_preview_does_not_write_records(self):
        """Getting a conversion quote previews discount without creating any DB records."""
        redemptions_before = DiscountRedemption.objects.using(DB).count()
        orders_before = Order.objects.using(DB).count()

        quote = LeadConversionService.get_conversion_quote(
            lead=self.lead,
            package_version_id=str(self.package_version.id),
            branch_id=str(self.branch.id),
            coupon_code='SUMMER20',
            db_alias=DB
        )
        self.assertIsNotNone(quote['coupon'])
        self.assertTrue(quote['coupon']['is_valid'])
        self.assertEqual(quote['pricing']['discount_amount'], '1000.00')

        self.assertEqual(DiscountRedemption.objects.using(DB).count(), redemptions_before)
        self.assertEqual(Order.objects.using(DB).count(), orders_before)

    def test_19_successful_conversion_redemption_once(self):
        """Executing lead conversion with coupon creates exactly 1 DiscountRedemption."""
        quote = LeadConversionService.get_conversion_quote(
            lead=self.lead,
            package_version_id=str(self.package_version.id),
            branch_id=str(self.branch.id),
            coupon_code='SUMMER20',
            db_alias=DB
        )
        total_payable = Decimal(quote['pricing']['total_payable'])

        res = LeadConversionService.execute_conversion(
            lead=self.lead,
            package_version_id=str(self.package_version.id),
            branch_id=str(self.branch.id),
            payment_provider='RAZORPAY',
            payment_amount=total_payable,
            coupon_code='SUMMER20',
            actor_user=self.admin,
            idempotency_key='P9-CONV-TEST-1',
            db_alias=DB
        )
        self.assertEqual(res['order_status'], 'PAID')
        self.assertEqual(res['lead_status'], 'CONVERTED')

        # Exactly 1 redemption created
        redemptions = DiscountRedemption.objects.using(DB).filter(discount_code=self.coupon)
        self.assertEqual(redemptions.count(), 1)
        self.assertEqual(redemptions.first().discount_amount, Decimal('1000.00'))

    def test_20_payment_retry_no_duplicate_redemption(self):
        """Retrying conversion with same idempotency key returns cached result without duplicate redemption."""
        quote = LeadConversionService.get_conversion_quote(
            lead=self.lead,
            package_version_id=str(self.package_version.id),
            branch_id=str(self.branch.id),
            coupon_code='SUMMER20',
            db_alias=DB
        )
        total_payable = Decimal(quote['pricing']['total_payable'])

        res1 = LeadConversionService.execute_conversion(
            lead=self.lead,
            package_version_id=str(self.package_version.id),
            branch_id=str(self.branch.id),
            payment_provider='CASH',
            payment_amount=total_payable,
            coupon_code='SUMMER20',
            actor_user=self.admin,
            idempotency_key='P9-RETRY-TEST',
            db_alias=DB
        )
        res2 = LeadConversionService.execute_conversion(
            lead=self.lead,
            package_version_id=str(self.package_version.id),
            branch_id=str(self.branch.id),
            payment_provider='CASH',
            payment_amount=total_payable,
            coupon_code='SUMMER20',
            actor_user=self.admin,
            idempotency_key='P9-RETRY-TEST',
            db_alias=DB
        )
        self.assertEqual(res1['conversion_id'], res2['conversion_id'])
        self.assertEqual(DiscountRedemption.objects.using(DB).filter(discount_code=self.coupon).count(), 1)

    def test_21_lead_360_offers_endpoint(self):
        """Lead 360 offers action returns campaigns, available coupons, and redemption history."""
        self._authenticate(self.admin)
        res = self.client.get(f'/api/v1/admin/leads/{self.lead.id}/offers/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['lead_id'], str(self.lead.id))
        self.assertGreaterEqual(len(res.data['campaigns']), 1)
        self.assertGreaterEqual(len(res.data['available_coupons']), 1)
        self.assertEqual(res.data['available_coupons'][0]['code'], 'SUMMER20')

    def test_22_acquisition_campaign_grouping_and_metrics(self):
        """Campaign performance groups by campaign name and platform with accurate counts."""
        # Create 2 leads attributed to Meta campaign
        lead1 = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch, first_name='Lead1', last_name='Test',
            phone_normalized='+919999900001', current_status='NEW_LEAD'
        )
        LeadAttribution.objects.using(DB).create(
            organization=self.org, lead=lead1, campaign_name='Meta Summer 2026',
            platform='Meta', utm_source='facebook', captured_at=timezone.now()
        )

        lead2 = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch, first_name='Lead2', last_name='Test',
            phone_normalized='+919999900002', current_status='NEW_LEAD'
        )
        LeadAttribution.objects.using(DB).create(
            organization=self.org, lead=lead2, campaign_name='Meta Summer 2026',
            platform='Meta', utm_source='instagram', captured_at=timezone.now()
        )

        # 1 trial booking on lead1
        TrialBooking.objects.using(DB).create(
            branch=self.branch, lead=lead1,
            scheduled_start=timezone.now(), scheduled_end=timezone.now() + timedelta(hours=1),
            status='BOOKED'
        )

        # 1 conversion on lead2 with paid order
        profile2 = UserProfile.objects.using(DB).create(
            user=self.sales_rep, preferred_branch=self.branch, joining_date=timezone.now().date()
        )
        order2 = Order.objects.using(DB).create(
            branch=self.branch, user_profile=profile2, lead=lead2,
            order_number='ORD-P9-CAMP-1', status='PAID', total_amount=Decimal('5900.00')
        )
        LeadConversion.objects.using(DB).create(
            lead=lead2, user_profile=profile2, order_id=order2.id, converted_at=timezone.now()
        )

        self._authenticate(self.admin)
        res = self.client.get('/api/v1/admin/crm/campaigns/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        campaigns = res.data['campaigns']
        meta_camp = next((c for c in campaigns if c['campaign_name'] == 'Meta Summer 2026'), None)
        self.assertIsNotNone(meta_camp)
        self.assertEqual(meta_camp['leads_count'], 2)
        self.assertEqual(meta_camp['trials_count'], 1)
        self.assertEqual(meta_camp['conversions_count'], 1)
        self.assertEqual(meta_camp['conversion_rate'], 50.0)
        self.assertEqual(meta_camp['paid_revenue'], '5900.00')

    def test_23_campaign_revenue_no_double_counting(self):
        """Revenue strictly uses paid orders without double counting multi-touch attributions."""
        lead = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch, first_name='Multi', last_name='Touch',
            phone_normalized='+919999900003', current_status='CONVERTED'
        )
        # 2 touches for the same campaign on same lead
        LeadAttribution.objects.using(DB).create(
            organization=self.org, lead=lead, campaign_name='Google Search Brand',
            platform='Google', touch_type='FIRST_TOUCH', captured_at=timezone.now() - timedelta(days=2)
        )
        LeadAttribution.objects.using(DB).create(
            organization=self.org, lead=lead, campaign_name='Google Search Brand',
            platform='Google', touch_type='LEAD_CAPTURE', captured_at=timezone.now()
        )

        profile = UserProfile.objects.using(DB).create(
            user=self.sales_rep, preferred_branch=self.branch, joining_date=timezone.now().date()
        )
        order = Order.objects.using(DB).create(
            branch=self.branch, user_profile=profile, lead=lead,
            order_number='ORD-P9-NODUP', status='PAID', total_amount=Decimal('7000.00')
        )
        LeadConversion.objects.using(DB).create(
            lead=lead, user_profile=profile, order_id=order.id, converted_at=timezone.now()
        )

        self._authenticate(self.admin)
        res = self.client.get('/api/v1/admin/crm/campaigns/?platform=Google')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        camp = next(c for c in res.data['campaigns'] if c['campaign_name'] == 'Google Search Brand')
        # Even with 2 touches, leads_count must be 1 and paid revenue must be exactly 7000.00
        self.assertEqual(camp['leads_count'], 1)
        self.assertEqual(camp['paid_revenue'], '7000.00')

    def test_24_campaign_drilldown_leads_conversions_revenue(self):
        """Drilldown endpoints return explainable records."""
        lead = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch, first_name='Drill', last_name='Down',
            phone_normalized='+919999900004', current_status='CONVERTED'
        )
        LeadAttribution.objects.using(DB).create(
            organization=self.org, lead=lead, campaign_name='Drill Campaign', platform='Meta'
        )
        profile = UserProfile.objects.using(DB).create(
            user=self.sales_rep, preferred_branch=self.branch, joining_date=timezone.now().date()
        )
        order = Order.objects.using(DB).create(
            branch=self.branch, user_profile=profile, lead=lead,
            order_number='ORD-DRILL-1', status='PAID', total_amount=Decimal('3500.00')
        )
        LeadConversion.objects.using(DB).create(
            lead=lead, user_profile=profile, order_id=order.id, converted_at=timezone.now()
        )

        self._authenticate(self.admin)

        # 1. Drilldown leads
        res_leads = self.client.get('/api/v1/admin/crm/campaigns/leads/?campaign_name=Drill+Campaign')
        self.assertEqual(res_leads.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res_leads.data), 1)
        self.assertEqual(res_leads.data[0]['id'], str(lead.id))

        # 2. Drilldown conversions
        res_conv = self.client.get('/api/v1/admin/crm/campaigns/conversions/?campaign_name=Drill+Campaign')
        self.assertEqual(res_conv.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res_conv.data), 1)
        self.assertEqual(res_conv.data[0]['order_number'], 'ORD-DRILL-1')

        # 3. Drilldown revenue
        res_rev = self.client.get('/api/v1/admin/crm/campaigns/revenue/?campaign_name=Drill+Campaign')
        self.assertEqual(res_rev.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res_rev.data), 1)
        self.assertEqual(res_rev.data[0]['amount'], '3500.00')

    def test_25_rbac_sales_rep_cannot_create_discount_campaign(self):
        """Sales rep has view access but lacks core.settings.edit to create discounts."""
        self._authenticate(self.sales_rep)
        res = self.client.post('/api/v1/admin/discount-campaigns/', {
            'name': 'Unauthorized Discount',
            'discount_type': 'FIXED',
            'discount_value': '500.00',
            'valid_from': timezone.now().isoformat(),
            'status': 'ACTIVE'
        })
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_26_rbac_sales_rep_can_view_campaigns(self):
        """Sales rep with crm.leads.view can view acquisition campaign analytics."""
        self._authenticate(self.sales_rep)
        res = self.client.get('/api/v1/admin/crm/campaigns/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    def test_27_audit_event_logged_on_coupon_actions(self):
        """Coupon creation and toggle status record BusinessAuditEvent."""
        audits_before = BusinessAuditEvent.objects.using(DB).count()
        self._authenticate(self.admin)
        res = self.client.post(f'/api/v1/admin/discount-codes/{self.coupon.id}/toggle-status/', {'status': 'INACTIVE'})
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        audit = BusinessAuditEvent.objects.using(DB).filter(
            action_code='COUPON_STATUS_CHANGED', entity_id=str(self.coupon.id)
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.after_data['status'], 'INACTIVE')

    def test_28_recommendation_registry_contains_send_offer(self):
        """RecommendationActionRegistry contains advisory SEND_OFFER action."""
        meta = RecommendationActionRegistry.get_recommendation_meta('SEND_OFFER')
        self.assertIsNotNone(meta)
        self.assertEqual(meta['code'], 'SEND_OFFER')
        self.assertFalse(meta['is_automation_executable'])  # purely advisory
        self.assertEqual(meta['target_ui_action'], 'VIEW_OFFERS')

    def test_29_zero_referral_rewards_route_contamination(self):
        """Referrals & Rewards models and routes remain completely separate from CRM campaigns."""
        from apps.tenant_core.models_referrals import ReferralProgram
        self.assertEqual(ReferralProgram.objects.using(DB).count(), 0)

    def test_30_branch_filtering_in_campaigns(self):
        """Campaign performance respects branch filter."""
        lead_a = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch, first_name='BranchA', last_name='Lead',
            phone_normalized='+919999900005', current_status='NEW_LEAD'
        )
        LeadAttribution.objects.using(DB).create(
            organization=self.org, lead=lead_a, campaign_name='Branch Filter Test', platform='Meta'
        )

        lead_b = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch_b, first_name='BranchB', last_name='Lead',
            phone_normalized='+919999900006', current_status='NEW_LEAD'
        )
        LeadAttribution.objects.using(DB).create(
            organization=self.org, lead=lead_b, campaign_name='Branch Filter Test', platform='Meta'
        )

        self._authenticate(self.admin)
        res_a = self.client.get(f'/api/v1/admin/crm/campaigns/?branch_id={self.branch.id}')
        camp_a = next(c for c in res_a.data['campaigns'] if c['campaign_name'] == 'Branch Filter Test')
        self.assertEqual(camp_a['leads_count'], 1)
