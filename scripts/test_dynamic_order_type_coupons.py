import os
import sys

sys.path.insert(0, r'F:\fitness-command-center\backend')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from apps.tenant_core.services_discounts import DiscountCouponEngineService
from apps.tenant_core.services_crm import LeadConversionService
from apps.tenant_core.models_crm import Lead
from apps.tenant_core.models_catalog import Package, PackageVersion
from apps.tenant_core.models_org import Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.views_crm import LeadViewSet
from apps.tenant_core.context import tenant_database_context
from rest_framework.test import APIRequestFactory, force_authenticate

tenant_id = 'd778f252-6fe0-459f-86a6-b75fc395ff24'

print('=== TEST 1: Dynamic Order Type Matching ===')
assert DiscountCouponEngineService.matches_order_type('NEW_MEMBERSHIP', ['NEW_PACKAGE']) == True, 'NEW_MEMBERSHIP should match NEW_PACKAGE'
assert DiscountCouponEngineService.matches_order_type('LEAD_CONVERSION', ['NEW_MEMBERSHIP', 'NEW_PACKAGE']) == True, 'LEAD_CONVERSION should match'
assert DiscountCouponEngineService.matches_order_type('NEW_PACKAGE', ['NEW_MEMBERSHIP']) == True, 'NEW_PACKAGE should match NEW_MEMBERSHIP'
assert DiscountCouponEngineService.matches_order_type('RENEWAL', ['NEW_MEMBERSHIP']) == False, 'RENEWAL should not match NEW_MEMBERSHIP'
assert DiscountCouponEngineService.matches_order_type('RENEWAL', ['RENEWAL']) == True, 'RENEWAL should match RENEWAL'
assert DiscountCouponEngineService.matches_order_type('NEW_MEMBERSHIP', ['ALL']) == True, 'ALL should match all'
assert DiscountCouponEngineService.matches_order_type('NEW_MEMBERSHIP', []) == True, 'Empty should match all'
print('All 7 Dynamic Order Type assertions PASSED!')

with tenant_database_context(tenant_id) as alias:
    print('\n=== TEST 2: Validate FOUNDING50 on Elite 300 ===')
    pkg_300 = Package.objects.using(alias).filter(name__icontains='Elite 300').first()
    pv_300 = PackageVersion.objects.using(alias).filter(package=pkg_300, status='ACTIVE').first()
    branch = Branch.objects.using(alias).filter(status='ACTIVE').first()
    lead = Lead.objects.using(alias).first()

    # 2a. With explicit NEW_MEMBERSHIP
    res1 = DiscountCouponEngineService.validate_coupon(
        code_str='FOUNDING50',
        order_subtotal=89999,
        branch=branch,
        package=pkg_300,
        order_type='NEW_MEMBERSHIP',
        db_alias=alias,
    )
    print('res1 (order_type=NEW_MEMBERSHIP): is_valid=', res1.get('is_valid'), 'discount=', res1.get('discount_amount'), 'reason=', res1.get('reason'))
    assert res1['is_valid'] == True

    # 2b. With order_type omitted (Contextual inference)
    res2 = DiscountCouponEngineService.validate_coupon(
        code_str='FOUNDING50',
        order_subtotal=89999,
        branch=branch,
        package=pkg_300,
        db_alias=alias,
    )
    print('res2 (order_type=None, context inferred): is_valid=', res2.get('is_valid'), 'discount=', res2.get('discount_amount'), 'reason=', res2.get('reason'))
    assert res2['is_valid'] == True

    # 2c. With RENEWAL (should fail gracefully)
    res3 = DiscountCouponEngineService.validate_coupon(
        code_str='FOUNDING50',
        order_subtotal=89999,
        branch=branch,
        package=pkg_300,
        order_type='RENEWAL',
        db_alias=alias,
    )
    print('res3 (order_type=RENEWAL): is_valid=', res3.get('is_valid'), 'reason=', res3.get('reason'))
    assert res3['is_valid'] == False

    print('\n=== TEST 3: LeadConversionService.get_conversion_quote ===')
    quote = LeadConversionService.get_conversion_quote(
        lead=lead,
        package_version_id=str(pv_300.id),
        branch_id=str(branch.id),
        coupon_code='FOUNDING50',
        order_type='NEW_MEMBERSHIP',
        db_alias=alias,
    )
    print('Quote Subtotal:', quote['pricing']['subtotal'])
    print('Quote Discount:', quote['pricing']['discount_amount'])
    print('Quote Total Payable:', quote['pricing']['total_payable'])
    print('Quote Coupon Valid:', quote['coupon']['is_valid'])
    print('Quote Coupon Code:', quote['coupon'].get('code'))
    assert quote['coupon']['is_valid'] == True
    assert float(quote['pricing']['discount_amount']) == 44999.50
    assert float(quote['pricing']['total_payable']) == 44999.50

    print('\n=== TEST 4: LeadConversion Quote without explicit order_type (Default test) ===')
    quote_default = LeadConversionService.get_conversion_quote(
        lead=lead,
        package_version_id=str(pv_300.id),
        branch_id=str(branch.id),
        coupon_code='FOUNDING50',
        db_alias=alias,
    )
    assert quote_default['coupon']['is_valid'] == True
    assert float(quote_default['pricing']['discount_amount']) == 44999.50
    print('Quote with default order_type also valid and calculated ₹44,999.50!')

    print('\n=== TEST 5: HTTP API POST /api/v1/tenant/leads/{id}/conversion-quote/ ===')
    from apps.tenant_core.models_rbac import RoleAssignment
    ra = RoleAssignment.objects.using(alias).select_related('user').first()
    user = ra.user if ra else TenantUser.objects.using(alias).first()
    from apps.authentication.views import _build_tenant_token
    from apps.master.models_tenant import Tenant
    tenant_obj = Tenant.objects.get(id=tenant_id)
    tokens = _build_tenant_token(user, tenant_obj, alias)
    access_token = str(tokens.access_token)

    rf = APIRequestFactory()
    req = rf.post(
        f'/api/v1/tenant/leads/{lead.id}/conversion-quote/',
        {
            'package_version_id': str(pv_300.id),
            'branch_id': str(branch.id),
            'coupon_code': 'FOUNDING50',
            'order_type': 'NEW_MEMBERSHIP',
        },
        format='json',
        HTTP_AUTHORIZATION=f'Bearer {access_token}',
        HTTP_X_TENANT_ID=str(tenant_id),
    )
    req.tenant_db_alias = alias
    req._tenant_db_alias = alias

    view = LeadViewSet.as_view({'post': 'conversion_quote'})
    resp = view(req, pk=str(lead.id))
    print('HTTP Status:', resp.status_code)
    print('Response Data Pricing:', resp.data.get('pricing'))
    print('Response Coupon Valid:', resp.data.get('coupon', {}).get('is_valid'))
    print('Response Coupon Discount:', resp.data.get('coupon', {}).get('discount_amount'))
    assert resp.status_code == 200
    assert resp.data['coupon']['is_valid'] == True
    assert float(resp.data['pricing']['discount_amount']) == 44999.50
    assert float(resp.data['pricing']['total_payable']) == 44999.50
    print('HTTP API POST conversion-quote PASSED 100%!')

print('\n======================================================')
print('  ALL DYNAMIC ORDER TYPE & API TESTS PASSED 100%!     ')
print('======================================================')
