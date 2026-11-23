import os, sys
backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from config.tenant_middleware import _register_tenant_connection
from config.routers import build_tenant_db_alias, set_tenant_db_alias

tenant = Tenant.objects.using('default').filter(slug='sweat').first()
ds = TenantDataSource.objects.using('default').filter(tenant_id=tenant.id, status='ACTIVE').first()
alias = build_tenant_db_alias(tenant.id)
_register_tenant_connection(alias=alias, db_name=getattr(ds, 'database_name', None), data_source=ds, tenant_id=tenant.id)
set_tenant_db_alias(alias)

from apps.tenant_core.models_catalog import Program, Package
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_crm import Lead
from apps.tenant_core.models_memberships import Membership
from apps.tenant_core.services_discounts import DiscountCouponEngineService

print("\n=== ALL PROGRAMS & PACKAGES ===")
for p in Program.objects.using(alias).all():
    print(f"\nProgram: {p.name} (ID: {p.id})")
    for pkg in Package.objects.using(alias).filter(program=p):
        print(f"    Package: {pkg.name} | ID: {pkg.id}")

print("\n=== DEVANSHI SHAH ===")
dev_profiles = UserProfile.objects.using(alias).filter(user__first_name__icontains='Devanshi')
for prof in dev_profiles:
    print(f"Profile: {prof.id} | User: {prof.user.first_name} {prof.user.last_name} ({prof.user.id})")
    lifecycle = DiscountCouponEngineService.get_member_lifecycle_state(prof, alias)
    print(f"  Lifecycle state: {lifecycle}")
    mems = Membership.objects.using(alias).filter(user_profile=prof)
    for m in mems:
        print(f"  Membership: {m.id} | Status: {m.status} | Package: {getattr(m.package, 'name', 'None')} ({getattr(m.package, 'id', 'None')}) | End: {m.end_date}")

# Also check by email/name across User
from apps.tenant_core.models_users import TenantUser
users = TenantUser.objects.using(alias).filter(first_name__icontains='Devanshi')
for u in users:
    print(f"User: {u.id} | {u.first_name} {u.last_name} | email: {u.email} | is_member: {getattr(u, 'is_member', None)}")
    prof = UserProfile.objects.using(alias).filter(user=u).first()
    print(f"  Profile: {prof.id if prof else 'None'}")
