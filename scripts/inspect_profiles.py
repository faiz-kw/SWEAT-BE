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
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_memberships import Membership
from apps.tenant_core.models_crm import LeadConversion

t = Tenant.objects.get(slug='sweat')
ds = TenantDataSource.objects.get(tenant=t)
alias = build_tenant_db_alias(t.id)
_register_tenant_connection(alias, db_name=ds.db_name, data_source=ds, tenant_id=t.id)
set_tenant_db_alias(alias)

for p in UserProfile.objects.using(alias).select_related('user').all():
    ms = Membership.objects.using(alias).filter(user_profile=p).count()
    lc = LeadConversion.objects.using(alias).filter(user_profile=p).count()
    print(f"{p.user.full_name} ({p.user.email}): member_number={p.member_number!r}, member_status={p.member_status!r}, member_type={p.member_type!r}, acq={p.acquisition_source!r}, memberships={ms}, lead_conversions={lc}")
