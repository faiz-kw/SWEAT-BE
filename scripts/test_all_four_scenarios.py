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

from apps.tenant_core.models import DiscountCampaign, DiscountCode
from apps.tenant_core.models_catalog import Package
from apps.tenant_core.views_mobile import MobileDiscountCouponsView, MobileValidateCouponView
from rest_framework.test import APIRequestFactory

# Update Loyal 30% to include RENEWAL, UPGRADE, NEW_PACKAGE in order_types
c = DiscountCampaign.objects.using(alias).filter(name__icontains='Loyal 30%').first()
cfg = dict(c.configuration or {})
cfg['order_types'] = ['RENEWAL', 'UPGRADE', 'NEW_PACKAGE']
c.configuration = cfg
c.save(using=alias)
print(f"Updated Campaign '{c.name}' order_types to: {cfg['order_types']}")

factory = APIRequestFactory()
coupons_view = MobileDiscountCouponsView.as_view()
validate_view = MobileValidateCouponView.as_view()

aarav_id = 'fb1a6dea-417a-4321-a016-aa26629c0518'
pkg_72 = Package.objects.using(alias).filter(name__icontains='Transform 72').first()
pkg_144 = Package.objects.using(alias).filter(name__icontains='Transform + 144').first()

print("\n=======================================================")
print("TEST 1: Active Member (Aarav) - RENEW with Transform 72")
print("=======================================================")
req = factory.get(f'/api/v1/mobile/coupons/?package_id={pkg_72.id}&subtotal=41999&order_type=RENEWAL&member_id={aarav_id}')
res = coupons_view(req)
print("Status:", res.status_code)
print("Available Coupons:", [c['code'] for c in res.data])

val_req = factory.post('/api/v1/mobile/coupons/validate/', {
    'code': 'LOYAL30',
    'package_id': str(pkg_72.id),
    'subtotal': 41999,
    'order_type': 'RENEWAL',
    'member_id': aarav_id
}, format='json')
val_res = validate_view(val_req)
print("Validate LOYAL30 Status:", val_res.status_code, val_res.data)

print("\n=======================================================")
print("TEST 2: Active Member (Aarav) - UPGRADE with Transform + 144")
print("=======================================================")
req = factory.get(f'/api/v1/mobile/coupons/?package_id={pkg_144.id}&subtotal=18000&order_type=UPGRADE&member_id={aarav_id}')
res = coupons_view(req)
print("Status:", res.status_code)
print("Available Coupons:", [c['code'] for c in res.data])

val_req = factory.post('/api/v1/mobile/coupons/validate/', {
    'code': 'LOYAL30',
    'package_id': str(pkg_144.id),
    'subtotal': 18000,
    'order_type': 'UPGRADE',
    'member_id': aarav_id
}, format='json')
val_res = validate_view(val_req)
print("Validate LOYAL30 Status:", val_res.status_code, val_res.data)

print("\n=======================================================")
print("TEST 3: Active Member (Aarav) - NEW_PACKAGE with Transform + 144")
print("=======================================================")
req = factory.get(f'/api/v1/mobile/coupons/?package_id={pkg_144.id}&subtotal=59999&order_type=NEW_PACKAGE&member_id={aarav_id}')
res = coupons_view(req)
print("Status:", res.status_code)
print("Available Coupons:", [c['code'] for c in res.data])

val_req = factory.post('/api/v1/mobile/coupons/validate/', {
    'code': 'LOYAL30',
    'package_id': str(pkg_144.id),
    'subtotal': 59999,
    'order_type': 'NEW_PACKAGE',
    'member_id': aarav_id
}, format='json')
val_res = validate_view(val_req)
print("Validate LOYAL30 Status:", val_res.status_code, val_res.data)

print("\n=======================================================")
print("TEST 4: Lead Conversion (New Lead) - Elite 300 with FOUNDING50")
print("=======================================================")
pkg_elite300 = Package.objects.using(alias).filter(name__icontains='Elite 300').first()
req = factory.get(f'/api/v1/mobile/coupons/?package_id={pkg_elite300.id}&subtotal=89999&order_type=NEW_MEMBERSHIP')
res = coupons_view(req)
print("Available Coupons:", [c['code'] for c in res.data])

val_req = factory.post('/api/v1/mobile/coupons/validate/', {
    'code': 'FOUNDING50',
    'package_id': str(pkg_elite300.id),
    'subtotal': 89999,
    'order_type': 'NEW_MEMBERSHIP'
}, format='json')
val_res = validate_view(val_req)
print("Validate FOUNDING50 Status:", val_res.status_code, val_res.data)
