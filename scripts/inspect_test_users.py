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
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_workforce import UserProfile

t = Tenant.objects.get(slug='sweat')
ds = TenantDataSource.objects.get(tenant=t)
alias = build_tenant_db_alias(t.id)
_register_tenant_connection(alias, db_name=ds.db_name, data_source=ds, tenant_id=t.id)
set_tenant_db_alias(alias)

for email in ['member@sweat.com', 'member_uat@sweat.test']:
    u = TenantUser.objects.using(alias).filter(email=email).first()
    roles = [r.role.code for r in u.role_assignments.all()] if u else []
    p = UserProfile.objects.using(alias).filter(user=u).first() if u else None
    print(email, "user_type:", u.user_type if u else None, "Roles:", roles, "Profile:", p.member_number if p else None, p.acquisition_source if p else None)
