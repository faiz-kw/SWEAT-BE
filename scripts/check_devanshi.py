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

from django.apps import apps
m_id = "837e5f72-b7de-4b2e-ade8-15baba664a7d"

print("Checking in default DB:")
for model in apps.get_models():
    try:
        obj = model.objects.using('default').filter(id=m_id).first()
        if obj:
            print(f"DEFAULT DB MATCH in {model.__name__}: {obj}")
    except Exception:
        pass

print("Checking in tenant DB across all models for string 837e5f72:")
for model in apps.get_models():
    try:
        for f in model._meta.fields:
            if f.get_internal_type() in ('CharField', 'UUIDField'):
                kwargs = {f.name: m_id}
                obj = model.objects.using(alias).filter(**kwargs).first()
                if obj:
                    print(f"TENANT DB MATCH in {model.__name__}.{f.name}: {obj}")
    except Exception:
        pass
