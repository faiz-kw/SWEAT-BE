import os, sys
backend_dir = r'F:\fitness-command-center\backend'
sys.path.insert(0, backend_dir)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django; django.setup()
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from config.tenant_middleware import _register_tenant_connection
from config.routers import build_tenant_db_alias, set_tenant_db_alias
tenant = Tenant.objects.using('default').filter(slug='sweat').first()
ds = TenantDataSource.objects.using('default').filter(tenant_id=tenant.id, status='ACTIVE').first()
alias = build_tenant_db_alias(tenant.id)
_register_tenant_connection(alias=alias, db_name=getattr(ds, 'database_name', None), data_source=ds, tenant_id=tenant.id)
set_tenant_db_alias(alias)

from apps.tenant_core.models_catalog import Package
from apps.tenant_core.models_discounts import DiscountCampaign, DiscountCode
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_memberships import Membership
from apps.tenant_core.services_discounts import DiscountCouponEngineService
from django.utils import timezone
from django.db import models

# Find Devanshi Shah's profile
prof = UserProfile.objects.using(alias).filter(user__first_name__icontains='Devanshi').first()
print("Devanshi Profile:", prof)
if prof:
    lifecycle = DiscountCouponEngineService.get_member_lifecycle_state(prof, alias)
    print("Devanshi Lifecycle state:", lifecycle)

# Find Transform 72
pkg_72 = Package.objects.using(alias).filter(name__icontains='Transform 72').first()
print("Transform 72 pkg:", pkg_72.id, pkg_72.name, "Program:", pkg_72.program.name, pkg_72.program.id)

# Now simulate MobileDiscountCouponsView filtering step by step!
req_package_id = str(pkg_72.id)
req_subtotal = '41999'
req_order_type = 'RENEWAL'
req_member_id = str(prof.id) if prof else None

codes = (
    DiscountCode.objects.using(alias)
    .filter(
        status='ACTIVE',
        campaign__status='ACTIVE',
    )
    .select_related('campaign', 'branch', 'package')
)

print(f"\nChecking {codes.count()} active codes for RENEWAL with Transform 72:")
for c in codes:
    camp = c.campaign
    _cfg = camp.configuration if isinstance(camp.configuration, dict) else {}
    print(f"\nEvaluating Code: {c.code} | Campaign: {camp.name}")
    
    # 1. Package check
    _code_pkg_id = str(c.package_id) if c.package_id else None
    _cfg_pkg_ids = [str(x) for x in _cfg.get('package_ids', []) if x] if isinstance(_cfg.get('package_ids'), list) else []
    _cfg_single_pkg_id = str(_cfg.get('package_id')) if (_cfg.get('package_id') and not _cfg_pkg_ids) else None
    _allowed_pkgs = [_code_pkg_id] if _code_pkg_id else (_cfg_pkg_ids if _cfg_pkg_ids else ([_cfg_single_pkg_id] if _cfg_single_pkg_id else []))
    
    if _allowed_pkgs:
        match_pkg = str(req_package_id) in _allowed_pkgs
        print(f"  Package check: allowed={_allowed_pkgs}, req={req_package_id} -> Match={match_pkg}")
        if not match_pkg:
            print("  -> SKIPPED at package check")
            continue
    
    # 2. Order type check
    _aots = _cfg.get('order_types') if isinstance(_cfg.get('order_types'), list) else None
    if _aots:
        match_ot = DiscountCouponEngineService.matches_order_type(req_order_type, _aots)
        print(f"  Order type check: allowed={_aots}, req={req_order_type} -> Match={match_ot}")
        if not match_ot:
            print("  -> SKIPPED at order_type check")
            continue
            
    # 3. Member lifecycle check
    _amt = _cfg.get('allowed_member_types') or _cfg.get('member_type')
    if _amt and lifecycle:
        match_life = DiscountCouponEngineService.matches_member_type(lifecycle, _amt)
        print(f"  Lifecycle check: allowed={_amt}, member_state={lifecycle} -> Match={match_life}")
        if not match_life:
            print("  -> SKIPPED at lifecycle check")
            continue

    print("  ===> ELIGIBLE! Would be returned in results!")
