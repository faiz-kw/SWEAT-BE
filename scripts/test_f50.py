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

from apps.tenant_core.models_catalog import Program, Package, PackageVersion
from apps.tenant_core.models_discounts import DiscountCampaign, DiscountCode
from apps.tenant_core.models_crm import Lead
from apps.tenant_core.models_org import Branch
from apps.tenant_core.services_crm import LeadConversionService
from apps.tenant_core.services_discounts import DiscountCouponEngineService

lead = Lead.objects.using(alias).filter(first_name__icontains='TestUser').first() or Lead.objects.using(alias).first()
print(f"Lead: {lead.id} | {lead.first_name} {lead.last_name}")

branch = Branch.objects.using(alias).first()
print(f"Branch: {branch.id} | {branch.name}")

f50_code = DiscountCode.objects.using(alias).filter(code='FOUNDING50').first()
print(f"FOUNDING50 code: {f50_code}")
if f50_code:
    print(f"Campaign: {f50_code.campaign.name}")
    print(f"Config: {f50_code.campaign.configuration}")

pkg_300 = Package.objects.using(alias).filter(name__icontains='Elite 300').first()
print(f"Elite 300 Package: {pkg_300.id if pkg_300 else 'None'}")
if pkg_300:
    pv = PackageVersion.objects.using(alias).filter(package=pkg_300).first()
    print(f"PackageVersion: {pv.id if pv else 'None'}")

    res = DiscountCouponEngineService.validate_coupon(
        code_str='FOUNDING50',
        user_profile=None,
        order_subtotal=89999,
        branch=branch,
        package=pkg_300,
        order_type='NEW_MEMBERSHIP',
        db_alias=alias,
    )
    print("validate_coupon result for NEW_MEMBERSHIP:", res)

    if pv:
        try:
            quote = LeadConversionService.get_conversion_quote(
                lead=lead,
                package_version_id=str(pv.id),
                branch_id=str(branch.id),
                coupon_code='FOUNDING50',
                order_type='NEW_MEMBERSHIP',
                db_alias=alias,
            )
            print("Quote result coupon:", quote.get('coupon'))
        except Exception as e:
            print("Quote exception:", e)
