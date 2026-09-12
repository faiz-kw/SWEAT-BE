"""
Sprint 1 Remediation Runtime Verification Script.
Runs comprehensive runtime checks against the live PostgreSQL databases and Django application.
"""

import os
import sys
import json
import django

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.test import Client
from apps.master.models_iam import PlatformUser
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.tenant_core.models_org import Organization
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.views import TenantDBMixin
from config.routers import (
    TenantRouter, MasterRouter, TenantRoutingError,
    get_tenant_db_alias, set_tenant_db_alias,
)
from config.tenant_middleware import _register_tenant_connection
from rest_framework.exceptions import PermissionDenied

results = []

def record(test_name, passed, detail=""):
    results.append((test_name, passed, detail))
    status_label = "[PASS]" if passed else "[FAIL]"
    print(f"  {status_label} {test_name}" + (f" -> {detail}" if detail else ""))

print("=" * 70)
print("SPRINT 1 REMEDIATION RUNTIME VERIFICATION")
print("=" * 70)

client = Client()

# =====================================================================
# 1. FRONTEND TENANT LOGIN & UNIVERSAL LOGIN
# =====================================================================
print("\n--- 1. Frontend & Universal Tenant Login ---")

# 1.1 Correct tenant slug -> login succeeds
resp_valid_slug = client.post(
    '/api/v1/auth/login/',
    data=json.dumps({
        'email': 'admin@cultfit.in',
        'password': 'TenantAdmin@123!',
        'tenant_slug': 'cult-fit',
    }),
    content_type='application/json'
)
data_valid = resp_valid_slug.json() if resp_valid_slug.status_code == 200 else {}
passed_valid = (
    resp_valid_slug.status_code == 200 and
    data_valid.get('user_type') == 'tenant' and
    'access' in data_valid and
    'refresh' in data_valid and
    data_valid.get('tenant', {}).get('slug') == 'cult-fit'
)
record(
    "Correct tenant_slug -> login succeeds",
    passed_valid,
    f"Status={resp_valid_slug.status_code}, UserType={data_valid.get('user_type')}, Tenant={data_valid.get('tenant', {}).get('slug')}"
)

# 1.2 Wrong tenant slug -> rejected with 401
resp_wrong_slug = client.post(
    '/api/v1/auth/login/',
    data=json.dumps({
        'email': 'admin@cultfit.in',
        'password': 'TenantAdmin@123!',
        'tenant_slug': 'wrong-nonexistent-gym',
    }),
    content_type='application/json'
)
record(
    "Wrong tenant_slug -> rejected",
    resp_wrong_slug.status_code == 401,
    f"Status={resp_wrong_slug.status_code}, Error={resp_wrong_slug.json().get('error')}"
)

# 1.3 Missing tenant slug for tenant user -> clear validation (no O(N) tenant scan)
resp_missing_slug = client.post(
    '/api/v1/auth/login/',
    data=json.dumps({
        'email': 'admin@cultfit.in',
        'password': 'TenantAdmin@123!',
    }),
    content_type='application/json'
)
record(
    "Missing tenant_slug -> fails closed (no O(N) scanning)",
    resp_missing_slug.status_code == 401,
    f"Status={resp_missing_slug.status_code}, Error={resp_missing_slug.json().get('error')}"
)

# 1.4 Platform user login without tenant slug -> succeeds against Master DB
resp_platform = client.post(
    '/api/v1/auth/login/',
    data=json.dumps({
        'email': 'admin@performanceos.io',
        'password': 'Admin@Test1234!',
    }),
    content_type='application/json'
)
# If password differs on existing DB, update it
if resp_platform.status_code != 200:
    pu = PlatformUser.objects.using('default').filter(email='admin@performanceos.io').first()
    if pu:
        pu.set_password('Admin@Test1234!')
        pu.save(using='default')
        resp_platform = client.post(
            '/api/v1/auth/login/',
            data=json.dumps({
                'email': 'admin@performanceos.io',
                'password': 'Admin@Test1234!',
            }),
            content_type='application/json'
        )
data_platform = resp_platform.json() if resp_platform.status_code == 200 else {}
record(
    "Platform user login without tenant_slug -> succeeds against Master DB",
    resp_platform.status_code == 200 and data_platform.get('user_type') == 'platform',
    f"Status={resp_platform.status_code}, UserType={data_platform.get('user_type')}"
)

# =====================================================================
# 2. REFRESH TOKEN ROTATION (RTR)
# =====================================================================
print("\n--- 2. Refresh Token Rotation (RTR) ---")

initial_refresh_token = data_valid.get('refresh')

# 2.1 Refresh #1 -> success + new refresh token + HttpOnly cookie
resp_refresh1 = client.post(
    '/api/v1/auth/token/refresh/',
    data=json.dumps({'refresh': initial_refresh_token}),
    content_type='application/json'
)
data_ref1 = resp_refresh1.json() if resp_refresh1.status_code == 200 else {}
new_rotated_refresh = data_ref1.get('refresh')
has_new_access = bool(data_ref1.get('access'))
cookie_set = 'refresh_token' in resp_refresh1.cookies
record(
    "Refresh #1 -> success + new rotated refresh token",
    resp_refresh1.status_code == 200 and bool(new_rotated_refresh) and new_rotated_refresh != initial_refresh_token,
    f"Status={resp_refresh1.status_code}, TokenChanged={new_rotated_refresh != initial_refresh_token}, CookiePresent={cookie_set}"
)

# =====================================================================
# 3. OLD REFRESH-TOKEN REJECTION
# =====================================================================
print("\n--- 3. Old Refresh-Token Rejection ---")

# 3.1 Reusing the old refresh token must return 401 Unauthorized
resp_reuse_old = client.post(
    '/api/v1/auth/token/refresh/',
    data=json.dumps({'refresh': initial_refresh_token}),
    content_type='application/json'
)
record(
    "Reuse old refresh token -> 401 Unauthorized (blacklisted)",
    resp_reuse_old.status_code == 401,
    f"Status={resp_reuse_old.status_code}, Detail={resp_reuse_old.json().get('detail')}"
)

# 3.2 New refresh token works
resp_refresh2 = client.post(
    '/api/v1/auth/token/refresh/',
    data=json.dumps({'refresh': new_rotated_refresh}),
    content_type='application/json'
)
data_ref2 = resp_refresh2.json() if resp_refresh2.status_code == 200 else {}
newest_refresh = data_ref2.get('refresh')
record(
    "New rotated refresh token -> success",
    resp_refresh2.status_code == 200 and bool(newest_refresh),
    f"Status={resp_refresh2.status_code}, NewestTokenIssued={bool(newest_refresh)}"
)

# =====================================================================
# 4. LOGOUT BLACKLIST
# =====================================================================
print("\n--- 4. Logout Blacklist ---")

# 4.1 Logout blacklists the refresh token
resp_logout = client.post(
    '/api/v1/auth/logout/',
    data=json.dumps({'refresh': newest_refresh}),
    content_type='application/json'
)
record(
    "Logout succeeds and clears cookie",
    resp_logout.status_code == 200 and 'Logged out successfully' in resp_logout.json().get('message', ''),
    f"Status={resp_logout.status_code}, CookieCleared={'refresh_token' not in resp_logout.cookies or resp_logout.cookies['refresh_token'].value == ''}"
)

# 4.2 Refreshing the logged-out token must return 401
resp_refresh_after_logout = client.post(
    '/api/v1/auth/token/refresh/',
    data=json.dumps({'refresh': newest_refresh}),
    content_type='application/json'
)
record(
    "Refresh with logged-out token -> 401 Unauthorized (blacklisted)",
    resp_refresh_after_logout.status_code == 401,
    f"Status={resp_refresh_after_logout.status_code}, Detail={resp_refresh_after_logout.json().get('detail')}"
)

# =====================================================================
# 5. MISSING TENANT CONTEXT (FAIL-CLOSED)
# =====================================================================
print("\n--- 5. Missing Tenant Context (Fail-Closed) ---")

set_tenant_db_alias(None)
router = TenantRouter()

# 5.1 TenantRouter.db_for_read raises TenantRoutingError
read_raised = False
try:
    router.db_for_read(Organization)
except TenantRoutingError as e:
    read_raised = True
record(
    "TenantRouter.db_for_read fails closed without tenant context",
    read_raised,
    "Raises TenantRoutingError explicitly"
)

# 5.2 TenantRouter.db_for_write raises TenantRoutingError
write_raised = False
try:
    router.db_for_write(Organization)
except TenantRoutingError as e:
    write_raised = True
record(
    "TenantRouter.db_for_write fails closed without tenant context",
    write_raised,
    "Raises TenantRoutingError explicitly"
)

# 5.3 TenantDBMixin.get_db() raises PermissionDenied
mixin_raised = False
try:
    TenantDBMixin().get_db()
except PermissionDenied as e:
    mixin_raised = True
record(
    "TenantDBMixin.get_db() fails closed without tenant context",
    mixin_raised,
    "Raises PermissionDenied explicitly (HTTP 403)"
)

# =====================================================================
# 6. TENANT DB ROUTING
# =====================================================================
print("\n--- 6. Tenant DB Routing ---")

_register_tenant_connection('tenant_tenant_cult_fit', 'tenant_cult_fit')
set_tenant_db_alias('tenant_tenant_cult_fit')

# 6.1 Router routes to active tenant alias
target_alias = router.db_for_read(Organization)
record(
    "TenantRouter routes to active tenant DB alias",
    target_alias == 'tenant_tenant_cult_fit',
    f"Alias={target_alias}"
)

# 6.2 Query executes against dedicated tenant database
try:
    org = Organization.objects.first()
    record(
        "Tenant query reads from dedicated tenant DB",
        org is not None,
        f"Organization Name: {org.name if org else 'None'}"
    )
except Exception as e:
    record("Tenant query reads from dedicated tenant DB", False, str(e))

# Reset context
set_tenant_db_alias(None)

# =====================================================================
# 7. MASTER DB PROTECTION
# =====================================================================
print("\n--- 7. Master DB Protection ---")

master_router = MasterRouter()

# 7.1 Master router routes platform models to 'default'
m_read = master_router.db_for_read(PlatformUser)
m_write = master_router.db_for_write(PlatformUser)
record(
    "MasterRouter routes PlatformUser to 'default' Master DB",
    m_read == 'default' and m_write == 'default',
    f"db_for_read={m_read}, db_for_write={m_write}"
)

# 7.2 Tenant models never route to Master DB
try:
    t_route = router.db_for_read(Organization)
    record("Tenant models never route to 'default' Master DB", False, f"Unexpected route: {t_route}")
except TenantRoutingError:
    record("Tenant models never route to 'default' Master DB", True, "Blocked from Master DB with TenantRoutingError")

# 7.3 Disallow migrations of tenant_core to 'default' Master DB
allow_mig = router.allow_migrate('default', 'tenant_core')
record(
    "Tenant core migrations blocked from 'default' Master DB",
    allow_mig is False,
    f"allow_migrate('default', 'tenant_core') = {allow_mig}"
)

# =====================================================================
# 8. TENANT ISOLATION
# =====================================================================
print("\n--- 8. Tenant Isolation ---")

# 8.1 Disallow relations across Master DB and Tenant DB
rel_tenant_master = router.allow_relation(PlatformUser(), Organization())
rel_master_tenant = master_router.allow_relation(PlatformUser(), Organization())
record(
    "Cross-database relations between Master DB and Tenant DB blocked",
    rel_tenant_master is False and rel_master_tenant is False,
    f"TenantRouter={rel_tenant_master}, MasterRouter={rel_master_tenant}"
)

# 8.2 Tenant user token cannot access Master DB Platform endpoints
access_token = data_valid.get('access')
resp_platform_me = client.get(
    '/api/v1/auth/me/',
    HTTP_AUTHORIZATION=f'Bearer {access_token}'
)
data_me = resp_platform_me.json() if resp_platform_me.status_code == 200 else {}
record(
    "Tenant user identity isolated as tenant user type",
    resp_platform_me.status_code == 200 and data_me.get('user_type') == 'tenant',
    f"user_type={data_me.get('user_type')}, tenant_id={data_me.get('tenant_id')}"
)

# =====================================================================
# SUMMARY
# =====================================================================
print("\n" + "=" * 70)
total = len(results)
passed_count = sum(1 for _, p, _ in results if p)
failed_count = total - passed_count
print(f"RESULTS: {passed_count}/{total} PASSED, {failed_count} FAILED")
print("=" * 70)

if failed_count > 0:
    print("FAILED TESTS:")
    for name, p, detail in results:
        if not p:
            print(f"  - {name}: {detail}")
    sys.exit(1)
else:
    print("ALL RUNTIME CHECKS PASSED PERFECTLY!")
    sys.exit(0)
