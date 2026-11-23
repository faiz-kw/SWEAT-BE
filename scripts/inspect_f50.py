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

from apps.tenant_core.models_discounts import DiscountCampaign, DiscountCode
from apps.tenant_core.models_crm import Lead
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_workforce import UserProfile

print("=== ALL FOUNDING50 CODES ===")
for code in DiscountCode.objects.using(alias).filter(code__icontains='FOUNDING50'):
    c = code.campaign
    print(f"Code: {code.code} (ID: {code.id}, Status: {code.status})")
    print(f"  Campaign: {c.name} (ID: {c.id}, Status: {c.status})")
    print(f"  Config: {c.configuration}")

print("\n=== TestUser ProductionCheck ===")
lead = Lead.objects.using(alias).filter(first_name__icontains='TestUser').first()
if lead:
    print(f"Lead ID: {lead.id}, Phone: {lead.phone}, Phone norm: {lead.phone_normalized}")
    users = TenantUser.objects.using(alias).filter(phone=lead.phone_normalized)
    for u in users:
        print(f"  Matching User: {u.id} | {u.first_name} {u.last_name}")
        prof = UserProfile.objects.using(alias).filter(user=u).first()
        print(f"  Profile: {prof.id if prof else 'None'}")
        if prof:
            from apps.tenant_core.services_discounts import DiscountCouponEngineService
            state = DiscountCouponEngineService.get_member_lifecycle_state(prof, alias)
            print(f"  Lifecycle state: {state}")
