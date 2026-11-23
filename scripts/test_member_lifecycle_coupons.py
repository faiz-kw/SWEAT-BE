import os
import sys

sys.path.insert(0, r'F:\fitness-command-center\backend')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from apps.tenant_core.models_discounts import DiscountCampaign, DiscountCode
from apps.tenant_core.models_catalog import Package
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_memberships import Membership
from apps.tenant_core.services_discounts import DiscountCouponEngineService
from apps.tenant_core.views_mobile import MobileDiscountCouponsView, MobileValidateCouponView
from apps.tenant_core.context import tenant_database_context
from rest_framework.test import APIRequestFactory

tenant_id = 'd778f252-6fe0-459f-86a6-b75fc395ff24'
factory = APIRequestFactory()

with tenant_database_context(tenant_id) as alias:
    print('======================================================================')
    print('  MEMBER LIFECYCLE & DYNAMIC COUPON ELIGIBILITY VERIFICATION SUITE    ')
    print('======================================================================\n')

    # Resolve Aarav Sharma (Active member from user screenshot)
    aarav_profile = UserProfile.objects.using(alias).filter(id='fb1a6dea-417a-4321-a016-aa26629c0518').first()
    pkg_perf36 = Package.objects.using(alias).filter(name__icontains='Performance 36').first()

    assert aarav_profile is not None, "Aarav Sharma profile should exist"
    assert pkg_perf36 is not None, "Performance 36 package should exist"

    aarav_state = DiscountCouponEngineService.get_member_lifecycle_state(aarav_profile, alias)
    print(f"Aarav Sharma ({aarav_profile.user.email}): Current Lifecycle State = {aarav_state}")
    assert aarav_state == 'ACTIVE', "Aarav Sharma must have ACTIVE lifecycle state"

    print("\n----------------------------------------------------------------------")
    print("TEST 1: GET /api/tenant/mobile/coupons/ for Aarav Sharma (Renewal Modal)")
    print("----------------------------------------------------------------------")
    req = factory.get('/api/tenant/mobile/coupons/', {
        'package_id': str(pkg_perf36.id),
        'order_type': 'RENEWAL',
        'subtotal': '36999',
        'member_id': str(aarav_profile.id),
    })
    req.tenant_db_alias = alias
    req._tenant_db_alias = alias
    view_coupons = MobileDiscountCouponsView.as_view()
    resp = view_coupons(req)
    assert resp.status_code == 200, f"Expected 200 OK, got {resp.status_code}"

    coupon_codes = [c['code'] for c in resp.data]
    print(f"Coupons returned for Aarav Sharma: {coupon_codes}")
    assert 'LOYAL30' in coupon_codes, "LOYAL30 must be returned for Active Member Renewal"
    assert 'GRATITUDE25' not in coupon_codes, "CRITICAL: GRATITUDE25 must NOT be returned for Active Member!"
    print("-> PASS: GRATITUDE25 is correctly EXCLUDED for active member Aarav Sharma; ONLY LOYAL30 is returned!")

    print("\n----------------------------------------------------------------------")
    print("TEST 2: Direct Coupon Validation for Aarav Sharma (Renewal)")
    print("----------------------------------------------------------------------")
    # 2a. Validate LOYAL30 (Should be valid)
    val_loyal = DiscountCouponEngineService.validate_coupon(
        code_str='LOYAL30',
        user_profile=aarav_profile,
        order_subtotal=36999,
        package=pkg_perf36,
        order_type='RENEWAL',
        db_alias=alias,
    )
    print("LOYAL30 validation:", val_loyal.get('is_valid'), "| Discount:", val_loyal.get('discount_amount'))
    assert val_loyal['is_valid'] == True
    assert float(val_loyal['discount_amount']) == 11099.70
    print("-> PASS: LOYAL30 correctly gives 30% discount (₹11,099.70) for Aarav Sharma.")

    # 2b. Validate GRATITUDE25 (Must fail because Aarav has ACTIVE membership)
    val_gratitude = DiscountCouponEngineService.validate_coupon(
        code_str='GRATITUDE25',
        user_profile=aarav_profile,
        order_subtotal=36999,
        package=pkg_perf36,
        order_type='RENEWAL',
        db_alias=alias,
    )
    print("GRATITUDE25 validation:", val_gratitude.get('is_valid'), "| Reason:", val_gratitude.get('reason'))
    assert val_gratitude['is_valid'] == False
    print(f"-> PASS: GRATITUDE25 correctly rejected with reason: '{val_gratitude.get('reason')}'")

    print("\n----------------------------------------------------------------------")
    print("TEST 3: Expired Member in Rejoin Modal")
    print("----------------------------------------------------------------------")
    # Find or test an expired member
    from django.utils import timezone
    today = timezone.now().date()
    # Find a member with no active membership (expired)
    all_profiles = UserProfile.objects.using(alias).filter(memberships__isnull=False).distinct()
    expired_profile = None
    for p in all_profiles:
        if DiscountCouponEngineService.get_member_lifecycle_state(p, alias) == 'EXPIRED':
            expired_profile = p
            break

    if not expired_profile:
        # Create an expired mock context or test lifecycle directly
        print("Note: All existing profiles have active memberships; testing semantic state directly.")
        assert DiscountCouponEngineService.matches_member_type('EXPIRED', ['NEW', 'EXPIRED']) == True
        assert DiscountCouponEngineService.matches_member_type('ACTIVE', ['NEW', 'EXPIRED']) == False
        assert DiscountCouponEngineService.matches_order_type('REJOIN', ['NEW_MEMBERSHIP', 'REJOIN']) == True
        assert DiscountCouponEngineService.matches_order_type('RENEWAL', ['NEW_MEMBERSHIP', 'REJOIN']) == False
    else:
        print(f"Found expired profile: {expired_profile.user.email}")
        req_exp = factory.get('/api/tenant/mobile/coupons/', {
            'package_id': str(pkg_perf36.id),
            'order_type': 'REJOIN',
            'subtotal': '36999',
            'member_id': str(expired_profile.id),
        })
        req_exp.tenant_db_alias = alias
        req_exp._tenant_db_alias = alias
        resp_exp = view_coupons(req_exp)
        exp_codes = [c['code'] for c in resp_exp.data]
        print(f"Coupons returned for Expired Member (REJOIN): {exp_codes}")
        assert 'GRATITUDE25' in exp_codes, "GRATITUDE25 must be returned for Expired Member Rejoin"
        assert 'LOYAL30' not in exp_codes, "LOYAL30 must NOT be returned for Expired Member Rejoin"
        print("-> PASS: Expired member gets GRATITUDE25 and not LOYAL30!")

    print("\n----------------------------------------------------------------------")
    print("TEST 4: New Prospect / First-Time Buyer (Lead Conversion)")
    print("----------------------------------------------------------------------")
    req_new = factory.get('/api/tenant/mobile/coupons/', {
        'package_id': str(pkg_perf36.id),
        'order_type': 'NEW_MEMBERSHIP',
        'subtotal': '36999',
    })
    req_new.tenant_db_alias = alias
    req_new._tenant_db_alias = alias
    resp_new = view_coupons(req_new)
    new_codes = [c['code'] for c in resp_new.data]
    print(f"Coupons returned for New Prospect (NEW_MEMBERSHIP): {new_codes}")
    assert 'GRATITUDE25' in new_codes, "GRATITUDE25 must be returned for New Prospect"
    assert 'LOYAL30' not in new_codes, "LOYAL30 must NOT be returned for New Prospect"
    print("-> PASS: New prospect gets GRATITUDE25 and not LOYAL30!")

    print("\n======================================================================")
    print("  ALL LIFECYCLE & APPLICABILITY TESTS PASSED 100%!                     ")
    print("======================================================================")
