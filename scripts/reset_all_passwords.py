import os, sys, django
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.core.cache import cache
from apps.master.models import Tenant, TenantDataSource
from apps.master.models_iam import PlatformUser, AuthenticationIdentity
from apps.master.services_auth_directory import sync_tenant_user_identity, sync_platform_user_identity
from config.tenant_middleware import _register_tenant_connection
from config.routers import build_tenant_db_alias, set_tenant_db_alias
from apps.tenant_core.models_org import Organization, Branch
from apps.tenant_core.models_users import TenantUser, UserBranch
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_rbac import Role, RoleAssignment

NEW_PASSWORD = 'Sweat@2026!'

print(f"=== RESETTING ALL USERS' PASSWORDS TO: '{NEW_PASSWORD}' ===")

# Clear any login rate limit / lockout keys in cache
try:
    cache.clear()
    print("Authentication cache & lockout records cleared.")
except Exception as e:
    print(f"Cache clear warning: {e}")

# 1. Connect to Tenant DB
t = Tenant.objects.get(slug='sweat')
ds = TenantDataSource.objects.get(tenant=t)
alias = build_tenant_db_alias(t.id)
_register_tenant_connection(alias, db_name=ds.db_name, data_source=ds, tenant_id=t.id)
set_tenant_db_alias(alias)

org = Organization.objects.using(alias).filter(status='ACTIVE').first() or Organization.objects.using(alias).first()
home_branch = Branch.objects.using(alias).filter(code='SWEAT_GOREGAON').first() or Branch.objects.using(alias).first()

# 2. Ensure member@sweat.com exists (from the staff user list)
member_user = TenantUser.objects.using(alias).filter(email__iexact='member@sweat.com').first()
if not member_user:
    print("Recreating 'member@sweat.com' account...")
    member_user = TenantUser(
        organization=org,
        email='member@sweat.com',
        username='member@sweat.com',
        first_name='Member',
        last_name='User',
        user_type='STAFF',
        status='ACTIVE',
        is_login_allowed=True,
        home_branch=home_branch,
    )
    member_user.set_password(NEW_PASSWORD)
    member_user.save(using=alias)

    UserProfile.objects.using(alias).get_or_create(
        user=member_user,
        defaults={
            'first_name_snapshot': 'Member',
            'last_name_snapshot': 'User',
            'member_status': 'ACTIVE',
        }
    )
    for b in Branch.objects.using(alias).all():
        UserBranch.objects.using(alias).get_or_create(
            user=member_user,
            branch=b,
            defaults={'relationship_type': 'PRIMARY', 'is_primary': (b == home_branch), 'status': 'ACTIVE'}
        )

# 3. Reset password for ALL Tenant Users in 'sweat'
tenant_users = list(TenantUser.objects.using(alias).all())
print(f"\nResetting passwords for {len(tenant_users)} Tenant Users in '{t.name}'...")
for u in tenant_users:
    u.set_password(NEW_PASSWORD)
    u.status = 'ACTIVE'
    u.is_login_allowed = True
    u.save(using=alias)
    sync_tenant_user_identity(u, tenant_id=t.id, db=alias)
    
    # Test that check_password works
    pw_ok = u.check_password(NEW_PASSWORD)
    print(f"  [OK] {u.email.ljust(26)} ({u.user_type}) -> Password Verified: {pw_ok}")

# 4. Reset password for Platform Superadmin Users (Master DB)
platform_users = list(PlatformUser.objects.using('default').all())
print(f"\nResetting passwords for {len(platform_users)} Platform Users in Master DB...")
for pu in platform_users:
    pu.set_password(NEW_PASSWORD)
    pu.status = 'ACTIVE'
    pu.is_active = True
    pu.save(using='default')
    sync_platform_user_identity(pu)
    pw_ok = pu.check_password(NEW_PASSWORD)
    print(f"  [OK] {pu.email.ljust(30)} (Platform Superadmin) -> Password Verified: {pw_ok}")

print(f"\n=== ALL PASSWORDS SUCCESSFULLY RESET TO: {NEW_PASSWORD} ===")
