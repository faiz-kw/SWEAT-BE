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

from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.services_discounts import DiscountCouponEngineService

users = TenantUser.objects.using(alias).filter(first_name__icontains='Praveen')
for u in users:
    prof = UserProfile.objects.using(alias).filter(user=u).first()
    state = DiscountCouponEngineService.get_member_lifecycle_state(prof, alias) if prof else 'NO_PROFILE'
    print(f"User: {u.id} | {u.first_name} {u.last_name} | {u.email} | type={u.user_type} | lifecycle={state}")
