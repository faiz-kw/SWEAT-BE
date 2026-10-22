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
from apps.master.services_auth_directory import compute_lookup_hash, normalize_identifier
tenant_users = list(TenantUser.objects.using(alias).all())
print(f"\nResetting passwords for {len(tenant_users)} Tenant Users in '{t.name}'...")
for u in tenant_users:
    u.set_password(NEW_PASSWORD)
    u.status = 'ACTIVE'
    u.is_login_allowed = True
    u.save(using=alias)
    
    # Align identity record
    if u.email:
        h = compute_lookup_hash(normalize_identifier(u.email))
        ai = AuthenticationIdentity.objects.using('default').filter(lookup_hash=h, account_type='TENANT', tenant_id=t.id).first()
        if ai:
            ai.subject_id = u.id
            ai.status = 'ACTIVE'
            ai.save(using='default')
        else:
            try:
                sync_tenant_user_identity(u, tenant_id=t.id, db=alias)
            except Exception as e:
                print(f"  [WARN] identity sync error for {u.email}: {e}")
    
    # Test that check_password works
    pw_ok = u.check_password(NEW_PASSWORD)
    print(f"  [OK] {u.email.ljust(26)} ({u.user_type}) -> Password Verified: {pw_ok}")

# 4. Configure Super Admin & Tenant Admin with requested credentials: admin / 123
SUPERADMIN_PW = '123'
print(f"\nSetting Super Admin credentials: Username 'admin', Password '{SUPERADMIN_PW}'...")

# Master DB: Platform Super Admin
pu = PlatformUser.objects.using('default').filter(email='admin.local@platform.local').first() or PlatformUser.objects.using('default').filter(username='admin').first()
if pu:
    pu.username = 'admin'
    pu.set_password(SUPERADMIN_PW)
    pu.status = 'ACTIVE'
    pu.is_active = True
    pu.is_staff = True
    pu.is_superuser = True
    pu.save(using='default')
    for ident_val in ['admin', pu.email]:
        h = compute_lookup_hash(normalize_identifier(ident_val))
        ai = AuthenticationIdentity.objects.using('default').filter(lookup_hash=h, account_type='PLATFORM', tenant_id=None).first()
        if not ai:
            ai = AuthenticationIdentity(lookup_hash=h, account_type='PLATFORM', identifier=ident_val, subject_id=pu.id, status='ACTIVE', identifier_type='USERNAME')
        ai.subject_id = pu.id
        ai.status = 'ACTIVE'
        ai.save(using='default')
    print(f"  [OK] Platform Superadmin '{pu.username}' / '{pu.email}' -> Password '123' Verified: {pu.check_password(SUPERADMIN_PW)}")

# Also set Master DB admin@sweat.com if exists
pu_sweat = PlatformUser.objects.using('default').filter(email='admin@sweat.com').first()
if pu_sweat:
    pu_sweat.set_password(SUPERADMIN_PW)
    pu_sweat.is_superuser = True
    pu_sweat.is_staff = True
    pu_sweat.save(using='default')
    h = compute_lookup_hash(normalize_identifier(pu_sweat.email))
    ai = AuthenticationIdentity.objects.using('default').filter(lookup_hash=h, account_type='PLATFORM', tenant_id=None).first()
    if not ai:
        ai = AuthenticationIdentity(lookup_hash=h, account_type='PLATFORM', identifier=pu_sweat.email, subject_id=pu_sweat.id, status='ACTIVE', identifier_type='EMAIL')
    ai.subject_id = pu_sweat.id
    ai.status = 'ACTIVE'
    ai.save(using='default')
    print(f"  [OK] Platform User '{pu_sweat.email}' -> Password '123' Verified: {pu_sweat.check_password(SUPERADMIN_PW)}")

# Tenant DB: admin@sweat.com
tu_admin = TenantUser.objects.using(alias).filter(email='admin@sweat.com').first()
if tu_admin:
    tu_admin.username = 'admin'
    tu_admin.set_password(SUPERADMIN_PW)
    tu_admin.status = 'ACTIVE'
    tu_admin.is_login_allowed = True
    tu_admin.save(using=alias)
    for ident_val in ['admin', 'admin@sweat.com']:
        h = compute_lookup_hash(normalize_identifier(ident_val))
        ai = AuthenticationIdentity.objects.using('default').filter(lookup_hash=h, account_type='TENANT', tenant_id=t.id).first()
        if not ai:
            ai = AuthenticationIdentity(lookup_hash=h, account_type='TENANT', tenant_id=t.id, identifier=ident_val, subject_id=tu_admin.id, status='ACTIVE', identifier_type='USERNAME')
        ai.subject_id = tu_admin.id
        ai.status = 'ACTIVE'
        ai.save(using='default')
    print(f"  [OK] Tenant Admin '{tu_admin.email}' (username '{tu_admin.username}') -> Password '123' Verified: {tu_admin.check_password(SUPERADMIN_PW)}")

print(f"\n=== CREDENTIALS CONFIGURED ===")
print("Super Admin / Django Admin: admin / 123")
print("SWEAT Tenant Admin: admin (or admin@sweat.com) / 123 (Org Code: sweat)")

