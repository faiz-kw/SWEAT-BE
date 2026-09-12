"""Sprint 1 verification script."""
import os, sys, django
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.master.models_iam import PlatformUser, PlatformUserRole
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.tenant_core.models_users import TenantUser
from apps.authentication.views import _build_platform_token, _build_tenant_token
from config.tenant_middleware import _register_tenant_connection
from config.routers import set_tenant_db_alias
import jwt as pyjwt
from django.conf import settings
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken

print("=== SPRINT 1 VERIFICATION ===\n")

# --- Platform token ---
platform_user = PlatformUser.objects.using('default').first()
print(f"Platform user: {platform_user.email}")
token = _build_platform_token(platform_user)
payload = dict(token.payload)
roles = payload.get('roles')
old_role = payload.get('role', 'REMOVED')
print(f"  roles claim: {roles} (type={type(roles).__name__})")
print(f"  old 'role' claim: {old_role}")
assert isinstance(roles, list), "FAIL: roles must be a list"
assert old_role == 'REMOVED', "FAIL: old hardcoded 'role' claim still present"
print("  [PASS] Platform token: roles is list, no hardcoded 'role' claim\n")

# --- Tenant token ---
tenant = Tenant.objects.using('default').first()
ds = TenantDataSource.objects.using('default').filter(tenant=tenant, status='ACTIVE').first()
db_alias = f"tenant_{ds.db_name}"
_register_tenant_connection(db_alias, ds.db_name)
set_tenant_db_alias(db_alias)

t_user = TenantUser.objects.using(db_alias).first()
print(f"Tenant user: {t_user.email}, tenant: {tenant.slug}")
token2 = _build_tenant_token(t_user, tenant, db_alias)
payload2 = dict(token2.payload)
roles2 = payload2.get('roles')
old_role2 = payload2.get('role', 'REMOVED')
print(f"  roles claim: {roles2} (type={type(roles2).__name__})")
print(f"  old 'role' claim: {old_role2}")
print(f"  tid: {payload2.get('tid')}")
print(f"  db_alias: {payload2.get('db_alias')}")
assert isinstance(roles2, list), "FAIL: roles must be a list"
assert old_role2 == 'REMOVED', "FAIL: old hardcoded 'role' claim still present"
assert payload2.get('tid') == str(tenant.id), "FAIL: tid mismatch"
print("  [PASS] Tenant token: roles is list, no hardcoded 'role' claim, tid correct\n")

# --- JWT Signature Verification ---
print("=== JWT SIGNATURE VERIFICATION ===")
access_str = str(token2.access_token)

try:
    decoded = pyjwt.decode(access_str, settings.SECRET_KEY, algorithms=['HS256'])
    print(f"  Valid key decode: SUCCESS (tid={decoded.get('tid')})")
except Exception as e:
    print(f"  Valid key decode: FAIL - {e}")

try:
    pyjwt.decode(access_str, 'wrong-key', algorithms=['HS256'])
    print("  Wrong key decode: SECURITY BREACH - should have been rejected!")
except pyjwt.exceptions.InvalidSignatureError:
    print("  Wrong key decode: CORRECTLY REJECTED [PASS]")
except Exception as e:
    print(f"  Wrong key decode: {type(e).__name__}: {e}")

# --- Logout Blacklist ---
print("\n=== LOGOUT BLACKLIST TEST ===")
test_refresh = _build_platform_token(platform_user)
refresh_str = str(test_refresh)
before = BlacklistedToken.objects.count()
try:
    rt = RefreshToken(refresh_str)
    rt.blacklist()
    after = BlacklistedToken.objects.count()
    print(f"  Blacklisted before={before}, after={after}")
    if after > before:
        print("  [PASS] Token blacklisting works")
    else:
        print("  [FAIL] Blacklist count did not increase")
except Exception as e:
    print(f"  [FAIL] Blacklisting error: {e}")

print("\n=== SPRINT 1 VERIFICATION COMPLETE ===")
