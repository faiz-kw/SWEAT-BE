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

from apps.tenant_core.models import DiscountCampaign
from apps.tenant_core.models_catalog import Package

c = DiscountCampaign.objects.using(alias).filter(name__icontains='Loyal 30%').first()
cfg = c.configuration or {}
loyal_pkg_ids = [str(x) for x in cfg.get('package_ids', [])]
print(f"Loyal 30% package_ids ({len(loyal_pkg_ids)}):")
for pid in loyal_pkg_ids:
    p = Package.objects.using(alias).filter(id=pid).first()
    print(f"  {pid} -> {p.name if p else 'NOT FOUND'}")

print("\nAll Packages with Transform in name:")
for p in Package.objects.using(alias).filter(name__icontains='Transform'):
    is_in_loyal = str(p.id) in loyal_pkg_ids
    print(f"  id={p.id}, name={p.name}, program_id={p.program_id}, IN_LOYAL_30={is_in_loyal}")
