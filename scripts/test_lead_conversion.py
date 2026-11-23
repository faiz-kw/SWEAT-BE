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
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_workforce import UserProfile

lead = Lead.objects.using(alias).filter(first_name__icontains='TestUser').first()
print(f"Lead: {lead.id} | {lead.first_name} {lead.last_name}")

# Find matching user profile for lead
matching_user = TenantUser.objects.using(alias).filter(
    organization=lead.organization,
    phone=lead.phone_normalized
).first() if getattr(lead, 'phone_normalized', None) else None

print(f"Matching user by phone_normalized: {matching_user}")

# What branches have Sweat Elite?
from apps.tenant_core.models_catalog import ProgramBranchAvailability
p = Program.objects.using(alias).filter(name__icontains='Sweat Elite').first()
avail_branches = ProgramBranchAvailability.objects.using(alias).filter(program=p, is_active=True)
for av in avail_branches:
    print(f"Sweat Elite available at branch: {av.branch.name} ({av.branch.id})")

branch = avail_branches.first().branch

pkg_300 = Package.objects.using(alias).filter(name__icontains='Elite 300').first()
pv = PackageVersion.objects.using(alias).filter(package=pkg_300).first()

print(f"\nTesting get_conversion_quote for lead {lead.id} at branch {branch.name}:")
try:
    quote = LeadConversionService.get_conversion_quote(
        lead=lead,
        package_version_id=str(pv.id),
        branch_id=str(branch.id),
        coupon_code='FOUNDING50',
        order_type='NEW_MEMBERSHIP',
        db_alias=alias,
    )
    print("Quote coupon:", quote.get('coupon'))
except Exception as e:
    print("Exception:", e)
    import traceback; traceback.print_exc()
