import os, sys, json
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

from apps.tenant_core.models import DiscountCampaign, DiscountCode

for c in DiscountCampaign.objects.using(alias).filter(status='ACTIVE'):
    print(f"\n==========================================")
    print(f"Campaign: {c.name}")
    print(f"ID: {c.id}")
    print(f"Status: {c.status}")
    print(f"Discount: {c.discount_type} {c.discount_value}")
    codes = DiscountCode.objects.using(alias).filter(campaign=c)
    print(f"Codes: {[cd.code for cd in codes]}")
    print("Configuration JSON:")
    print(json.dumps(c.configuration, indent=2))
