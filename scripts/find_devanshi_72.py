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

from apps.tenant_core.models import Membership

mems = Membership.objects.using(alias).filter(end_date='2027-05-09')
print(f"Memberships with end_date 2027-05-09: {mems.count()}")
for m in mems:
    up = m.user_profile
    print(f"Mem id={m.id}, package={m.package.name if m.package else 'None'}, user={up.first_name_snapshot if up else ''} {up.last_name_snapshot if up else ''}, up_id={up.id if up else ''}")
