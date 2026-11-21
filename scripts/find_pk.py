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

from apps.tenant_core.views_members import MemberViewSet
from apps.tenant_core.models_org import Organization
from django.db import connections

org = Organization.objects.using(alias).first()
pk = '837e5f72-b7de-4b2e-ade8-15baba664a7d'
mv = MemberViewSet()
prof = mv.get_member_profile(alias, org, pk)
print('MemberViewSet profile for pk:', prof)

# Search in tenant DB
conn = connections[alias]
with conn.cursor() as cursor:
    cursor.execute("""
        SELECT table_name, column_name 
        FROM information_schema.columns 
        WHERE data_type = 'uuid' AND table_schema = 'public'
    """)
    uuid_cols = cursor.fetchall()

found = []
for t, c in uuid_cols:
    try:
        with conn.cursor() as cur:
            cur.execute(f'SELECT count(*) FROM "{t}" WHERE "{c}" = %s', [pk])
            cnt = cur.fetchone()[0]
            if cnt > 0:
                found.append((t, c))
    except Exception as e:
        pass

print('Found pk in tenant DB tables:', found)
