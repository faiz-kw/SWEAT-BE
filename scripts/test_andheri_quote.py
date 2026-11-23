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

from apps.tenant_core.models_catalog import Package, PackageVersion
from apps.tenant_core.models_crm import Lead
from apps.tenant_core.models_org import Branch
from apps.tenant_core.services_crm import LeadConversionService

lead = Lead.objects.using(alias).filter(first_name__icontains='TestUser').first()
branch = Branch.objects.using(alias).filter(name__icontains='Andheri').first()
pkg_300 = Package.objects.using(alias).filter(name__icontains='Elite 300').first()
pv = PackageVersion.objects.using(alias).filter(package=pkg_300).first()

print(f"Lead: {lead.id} | {lead.first_name} {lead.last_name}")
print(f"Branch: {branch.id} | {branch.name}")
print(f"PackageVersion: {pv.id}")

quote = LeadConversionService.get_conversion_quote(
    lead=lead,
    package_version_id=str(pv.id),
    branch_id=str(branch.id),
    coupon_code='FOUNDING50',
    order_type='NEW_MEMBERSHIP',
    db_alias=alias,
)
print("Quote coupon:", quote.get('coupon'))
