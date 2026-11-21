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
from apps.tenant_core.views_mobile import MobileDiscountCouponsView
from rest_framework.test import APIRequestFactory

factory = APIRequestFactory()

pkg_72 = Package.objects.using(alias).filter(name__icontains='Transform 72').first()
aarav_id = 'fb1a6dea-417a-4321-a016-aa26629c0518'

# Test 1: RENEWAL with Transform 72 for Aarav
request = factory.get(f'/api/v1/mobile/coupons/?package_id={pkg_72.id}&subtotal=41999&order_type=RENEWAL&member_id={aarav_id}')
view = MobileDiscountCouponsView.as_view()
res = view(request)
print("Test 1 - Aarav RENEWAL with Transform 72 status:", res.status_code)
print("Test 1 - Coupons returned:", [c['code'] for c in res.data])

# Test 2: UPGRADE with Transform + 144 for Aarav
pkg_144 = Package.objects.using(alias).filter(name__icontains='Transform + 144').first()
request = factory.get(f'/api/v1/mobile/coupons/?package_id={pkg_144.id}&subtotal=18000&order_type=UPGRADE&member_id={aarav_id}')
res = view(request)
print("\nTest 2 - Aarav UPGRADE with Transform + 144 status:", res.status_code)
print("Test 2 - Coupons returned:", [c['code'] for c in res.data])

# Test 3: NEW_PACKAGE with Transform + 144 for Aarav
request = factory.get(f'/api/v1/mobile/coupons/?package_id={pkg_144.id}&subtotal=59999&order_type=NEW_PACKAGE&member_id={aarav_id}')
res = view(request)
print("\nTest 3 - Aarav NEW_PACKAGE with Transform + 144 status:", res.status_code)
print("Test 3 - Coupons returned:", [c['code'] for c in res.data])
