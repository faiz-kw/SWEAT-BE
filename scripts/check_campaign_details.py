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

from apps.tenant_core.models_discounts import DiscountCampaign, DiscountCode

for c in DiscountCampaign.objects.using(alias).filter(codes__code__in=['LOYAL30', 'GRATITUDE25', 'FOUNDING50']):
    print(f"\nCampaign: {c.name}")
    print(f"Status: {c.status}")
    print(f"Valid from: {c.valid_from} | Valid until: {c.valid_until}")
    print(f"Usage limit: {c.usage_limit} | Per user limit: {c.per_user_limit}")
    print(f"Min order: {c.minimum_order_amount} | Max discount: {c.max_discount}")
    print(f"Codes: {list(c.codes.values('code', 'status'))}")
    print(f"Config: {c.configuration}")
