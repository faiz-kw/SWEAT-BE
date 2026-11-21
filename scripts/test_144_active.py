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

from apps.tenant_core.models_catalog import Package
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.views_mobile import MobileDiscountCouponsView
from rest_framework.test import APIRequestFactory

factory = APIRequestFactory()

pkg_144 = Package.objects.using(alias).filter(name__icontains='Transform + 144').first()
active_user = TenantUser.objects.using(alias).filter(id='aa097919-16cd-4d5f-a584-5224865ea7e9').first()

req = factory.get(f'/api/v1/mobile/coupons/?package_id={pkg_144.id}&subtotal=59999&order_type=NEW_MEMBERSHIP')
req.user = active_user

view = MobileDiscountCouponsView.as_view()
res = view(req)
print(f"When logged-in user is ACTIVE: Status={res.status_code}")
print(f"Coupons returned: {[c['code'] for c in res.data]}")
