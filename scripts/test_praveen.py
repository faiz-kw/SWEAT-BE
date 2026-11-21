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
from apps.tenant_core.models_memberships import Membership

praveen = TenantUser.objects.using(alias).filter(first_name__icontains='Praveen').first()
print(f"Praveen User: {praveen.id} | {praveen.first_name} {praveen.last_name}")
prof = UserProfile.objects.using(alias).filter(user=praveen).first()
print(f"Praveen Profile: {prof.id if prof else 'None'}")
if prof:
    state = DiscountCouponEngineService.get_member_lifecycle_state(prof, alias)
    print(f"Praveen Lifecycle State: {state}")
    mems = Membership.objects.using(alias).filter(user_profile=prof)
    for m in mems:
        print(f"  Membership: {m.id} | Status: {m.status} | End: {m.end_date}")

# Now test validate_coupon when user_profile is Praveen!
from apps.tenant_core.models_catalog import Package
pkg_300 = Package.objects.using(alias).filter(name__icontains='Elite 300').first()
res = DiscountCouponEngineService.validate_coupon(
    code_str='FOUNDING50',
    user_profile=prof,
    order_subtotal=89999,
    package=pkg_300,
    order_type='NEW_MEMBERSHIP',
    db_alias=alias,
)
print("\nvalidate_coupon with Praveen's profile and order_type=NEW_MEMBERSHIP:")
print(res)
