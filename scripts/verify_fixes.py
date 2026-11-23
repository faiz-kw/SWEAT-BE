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
from apps.tenant_core.models_discounts import DiscountCampaign, DiscountCode
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_crm import Lead
from apps.tenant_core.services_discounts import DiscountCouponEngineService
from apps.tenant_core.views_mobile import MobileDiscountCouponsView, MobileValidateCouponView
from rest_framework.test import APIRequestFactory

factory = APIRequestFactory()

praveen_user = TenantUser.objects.using(alias).filter(first_name__icontains='Praveen').first()
print(f"Logged in user (Staff): {praveen_user.email} (user_type={praveen_user.user_type})")

lead = Lead.objects.using(alias).filter(first_name__icontains='TestUser').first()
pkg_300 = Package.objects.using(alias).filter(name__icontains='Elite 300').first()

print("\n--- TEST: Lead Conversion with FOUNDING50 ---")
# Currently, staff user is logged in, no member_id passed
req = factory.post('/api/v1/mobile/coupons/validate/', {
    'code': 'FOUNDING50',
    'package_id': str(pkg_300.id),
    'subtotal': '89999',
    'order_type': 'NEW_MEMBERSHIP',
}, format='json')
req.user = praveen_user

view = MobileValidateCouponView.as_view()
res = view(req)
print(f"Validate FOUNDING50 for Lead: Status={res.status_code}, Data={res.data}")
