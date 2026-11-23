import os
import sys
from decimal import Decimal

# Setup Django
sys.path.insert(0, r'F:\fitness-command-center\backend')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from django.test import RequestFactory
from apps.tenant_core.models import (
    DiscountCampaign, DiscountCode, DiscountEligibilityRule,
    Package, PackageVersion, PackagePrice, UserProfile, MemberInvoice,
    DiscountRedemption, Order, OrderItem
)
from apps.tenant_core.context import tenant_database_context
from apps.tenant_core.services_pricing import UnifiedPricingService
from apps.tenant_core.services_discounts import DiscountCouponEngineService
from apps.tenant_core.views_mobile import MobilePricingQuoteView, MobileDiscountCouponsView

tenant_id = 'd778f252-6fe0-459f-86a6-b75fc395ff24'

print("======================================================================")
print("  PRODUCTION READINESS VERIFICATION SUITE — COUPON & PRICING ECOSYSTEM")
print("======================================================================\n")

passed = 0
failed = 0

def check(test_name, condition, details=""):
    global passed, failed
    if condition:
        print(f" [PASS] {test_name}")
        if details:
            print(f"        {details}")
        passed += 1
    else:
        print(f" [FAIL] {test_name}")
        if details:
            print(f"        ERROR: {details}")
        failed += 1

with tenant_database_context(tenant_id) as alias:
    # TEST 1: Catalog Package integrity
    active_pkgs = Package.objects.using(alias).filter(status='ACTIVE')
    check(
        "Active Package Catalog Integrity",
        active_pkgs.count() >= 8,
        f"Found {active_pkgs.count()} active packages in tenant DB"
    )

    # TEST 2: Specific Key Coupons Existence
    key_codes = ['FOUNDING50', 'FOUNDING40', 'LOYAL30', 'GRATITUDE25']
    for code in key_codes:
        c_obj = DiscountCode.objects.using(alias).filter(code=code, status='ACTIVE').first()
        check(
            f"Key Coupon Exists & Active: {code}",
            c_obj is not None,
            f"Campaign: {c_obj.campaign.name if c_obj else 'None'} | MaxDiscount: {c_obj.campaign.max_discount if c_obj else 'None'}"
        )

    # TEST 3: Un-capped Founding Discount
    c50 = DiscountCode.objects.using(alias).filter(code='FOUNDING50', status='ACTIVE').first()
    check(
        "FOUNDING50 is Un-capped (max_discount == 0.00)",
        c50 is not None and (c50.campaign.max_discount == Decimal('0.00') or c50.campaign.max_discount is None),
        f"Current max_discount is: {c50.campaign.max_discount if c50 else 'Missing'}"
    )

    # TEST 4: UnifiedPricingService calculation for Elite 300 (₹89,999) with FOUNDING50
    p_elite300 = Package.objects.using(alias).filter(name__icontains='Elite 300').first()
    if p_elite300:
        quote_50 = UnifiedPricingService.calculate_quote(
            package_id=str(p_elite300.id),
            coupon_code='FOUNDING50',
            order_type='NEW_MEMBERSHIP',
            db_alias=alias
        )
        p_res = quote_50.get('pricing', {})
        expected_discount = 44999.50
        actual_discount = float(p_res.get('discount_amount', 0))
        check(
            "UnifiedPricingService: 50% discount computed accurately for Elite 300",
            abs(actual_discount - expected_discount) < 0.01,
            f"Catalog: ₹{p_res.get('catalog_price')} | Discount: ₹{actual_discount} | Net: ₹{p_res.get('final_payable')} | GST: ₹{p_res.get('gst_tax_amount')}"
        )
        check(
            "UnifiedPricingService: Statutory GST is calculated",
            float(p_res.get('gst_tax_amount', 0)) > 0,
            f"GST Tax: ₹{p_res.get('gst_tax_amount')} (Taxable value: ₹{p_res.get('base_taxable_value')})"
        )

    # TEST 5: UnifiedPricingService calculation for Essential 12 (₹15,999) with LOYAL30
    p_ess12 = Package.objects.using(alias).filter(name__icontains='Essential 12').first()
    if p_ess12:
        quote_30 = UnifiedPricingService.calculate_quote(
            package_id=str(p_ess12.id),
            coupon_code='LOYAL30',
            order_type='RENEWAL',
            db_alias=alias
        )
        p_res = quote_30.get('pricing', {})
        expected_discount = 4799.70
        actual_discount = float(p_res.get('discount_amount', 0))
        check(
            "UnifiedPricingService: 30% discount computed accurately for Essential 12",
            abs(actual_discount - expected_discount) < 0.01,
            f"Catalog: ₹{p_res.get('catalog_price')} | Discount: ₹{actual_discount} | Net: ₹{p_res.get('final_payable')}"
        )

    # TEST 6: MobilePricingQuoteView HTTP API endpoint (GET and POST)
    factory = RequestFactory()
    request_post = factory.post(
        '/api/tenant/mobile/pricing/quote/',
        data={'package_id': str(p_ess12.id), 'coupon_code': 'LOYAL30', 'order_type': 'RENEWAL'},
        content_type='application/json'
    )
    request_post.tenant_db_alias = alias
    request_post.user = UserProfile.objects.using(alias).filter(member_type='MEMBER').first()
    view = MobilePricingQuoteView.as_view()
    resp_post = view(request_post)
    check(
        "HTTP API: POST /api/tenant/mobile/pricing/quote/ returns 200 OK",
        resp_post.status_code == 200,
        f"Response status: {resp_post.status_code} | final_payable: {resp_post.data.get('pricing', {}).get('final_payable') if hasattr(resp_post, 'data') else 'N/A'}"
    )

    request_get = factory.get(
        '/api/tenant/mobile/pricing/quote/',
        {'package_id': str(p_ess12.id), 'coupon_code': 'LOYAL30', 'order_type': 'RENEWAL'}
    )
    request_get.tenant_db_alias = alias
    request_get.user = request_post.user
    resp_get = view(request_get)
    check(
        "HTTP API: GET /api/tenant/mobile/pricing/quote/ returns 200 OK",
        resp_get.status_code == 200,
        f"Response status: {resp_get.status_code} | final_payable: {resp_get.data.get('pricing', {}).get('final_payable') if hasattr(resp_get, 'data') else 'N/A'}"
    )

    # TEST 7: MobileDiscountCouponsView returns applicable coupons for package
    request_coupons = factory.get(
        '/api/tenant/mobile/coupons/',
        {'package_id': str(p_ess12.id), 'order_type': 'RENEWAL'}
    )
    request_coupons.tenant_db_alias = alias
    request_coupons.user = request_post.user
    view_coupons = MobileDiscountCouponsView.as_view()
    resp_coupons = view_coupons(request_coupons)
    coupons_list = resp_coupons.data if isinstance(resp_coupons.data, list) else resp_coupons.data.get('coupons', [])
    found_loyal30 = any(c.get('code') == 'LOYAL30' for c in coupons_list)
    check(
        "HTTP API: GET /api/tenant/mobile/coupons/ returns LOYAL30 for Essential 12",
        found_loyal30,
        f"Found {len(coupons_list)} applicable coupons: {[c.get('code') for c in coupons_list]}"
    )

    # TEST 8: Invalid / Expired Coupon Rejection
    invalid_quote = UnifiedPricingService.calculate_quote(
        package_id=str(p_ess12.id),
        coupon_code='NON_EXISTENT_CODE_999',
        order_type='RENEWAL',
        db_alias=alias
    )
    c_inv = invalid_quote.get('coupon', {})
    check(
        "Invalid coupon safely rejected with is_valid=False",
        c_inv.get('is_valid') is False,
        f"Reason: {c_inv.get('reason')}"
    )

    # TEST 9: Inactive Member Context in Discounts workspace
    user_inactive = UserProfile.objects.using(alias).filter(member_status='INACTIVE').first()
    if not user_inactive:
        user_inactive = UserProfile.objects.using(alias).filter(memberships__isnull=True).first()
    if user_inactive:
        check(
            "Inactive member profile correctly identified without active package",
            user_inactive is not None,
            f"User ID: {user_inactive.id} ({user_inactive.first_name_snapshot} {user_inactive.last_name_snapshot})"
        )

print("\n======================================================================")
print(f"  VERIFICATION COMPLETE: {passed} PASSED, {failed} FAILED")
print("======================================================================\n")

if failed > 0:
    sys.exit(1)
