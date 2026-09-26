import os, sys
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(SCRIPT_DIR)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()

from apps.master.models import Tenant, TenantDataSource
from config.tenant_middleware import _register_tenant_connection
from config.routers import build_tenant_db_alias, set_tenant_db_alias
from apps.tenant_core.models_org import Branch, Organization
from apps.tenant_core.models_workforce import TenantUser
from apps.tenant_core.models_rbac import Role
from apps.tenant_core.models_catalog import Program

t = Tenant.objects.get(slug='sweat')
ds = TenantDataSource.objects.get(tenant=t)
alias = build_tenant_db_alias(t.id)
_register_tenant_connection(alias, db_name=ds.db_name, data_source=ds, tenant_id=t.id)
set_tenant_db_alias(alias)

print('=== BRANCHES ===')
for b in Branch.objects.using(alias).all():
    print(f"ID: {b.id}, Code: {b.code}, Name: {b.name}, Status: {b.status}, OrgID: {b.organization_id}")

print('\n=== ROLES ===')
for r in Role.objects.using(alias).all():
    print(f"ID: {r.id}, Code: {r.code}, Name: {r.name}, Scope: {r.scope}, System: {r.is_system_role}, OrgID: {r.organization_id}, Desc: {r.description}")

print('\n=== PROGRAMS ===')
for p in Program.objects.using(alias).all():
    print(f"ID: {p.id}, Code: {p.code}, Name: {p.name}, Status: {p.status}, OrgID: {p.organization_id}")

print('\n=== TENANT USER FIELDS ===')
for f in TenantUser._meta.get_fields():
    if not f.is_relation or f.many_to_one:
        print(f.name, type(f).__name__)
