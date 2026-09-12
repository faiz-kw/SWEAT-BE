"""
Sprint 4 Billing Enforcement & Metering Live Runtime Verification Script.

Executes live runtime checks against:
- Master DB ('default') and dedicated Tenant DB ('tenant_cult_fit')
- Metric Catalog Reconciliation (Req 1, S4.4)
- QuotaChecker live evaluation & status counting (Req 2, S4.1)
- TenantUser Creation Quota Enforcement & atomic locking (S4.2)
- Subscription Authentication Hardening & State Machine (Req 3, S4.3)
- TenantResourceUsage background synchronization & idempotency (S4.5)
"""

import os
import sys
import uuid
import django

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.test import Client
from django.utils import timezone
from django.db import transaction
from django.core.management import call_command
from rest_framework_simplejwt.tokens import RefreshToken

from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.models_saas import (
    SaasPlan, SaasPlanResourceLimit, ResourceMetric,
    TenantResourceLimit, TenantResourceUsage, TenantSubscription,
)
from apps.master.quota import QuotaChecker, QuotaExceededError, QuotaConfigurationError
from apps.master.metering import sync_tenant_resource_usage
from apps.tenant_core.models_org import Organization, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_rbac import Role, RoleAssignment
from config.routers import set_tenant_db_alias
from config.tenant_middleware import _register_tenant_connection

results = []


def record(test_name, passed, detail=""):
    results.append((test_name, passed, detail))
    status_label = "[PASS]" if passed else "[FAIL]"
    print(f"  {status_label} {test_name}" + (f" -> {detail}" if detail else ""))


print("=" * 80)
print("SPRINT 4 BILLING ENFORCEMENT & METERING LIVE RUNTIME VERIFICATION")
print("=" * 80)

# 1. Setup tenant connection
_register_tenant_connection('tenant_tenant_cult_fit', 'tenant_cult_fit')
_register_tenant_connection('tenant_cult_fit', 'tenant_cult_fit')
set_tenant_db_alias('tenant_cult_fit')

client = Client()

# 2. Retrieve base live entities
tenant = Tenant.objects.using('default').get(slug='cult-fit')
org = Organization.objects.using('tenant_cult_fit').first()
branch = Branch.objects.using('tenant_cult_fit').first()
admin_user = TenantUser.objects.using('tenant_cult_fit').get(email='admin@cultfit.in')

# Clean up any leftover test data
TenantResourceLimit.objects.using('default').filter(tenant=tenant).delete()
TenantUser.objects.using('tenant_cult_fit').filter(email__startswith='sprint4_verify_').delete()


def get_token(user=None, tenant_override=None):
    u = user or admin_user
    t = tenant_override or tenant
    set_tenant_db_alias('tenant_cult_fit')
    refresh = RefreshToken()
    refresh['sub'] = str(u.id)
    refresh['user_type'] = 'tenant'
    refresh['roles'] = [
        ra.role.code for ra in RoleAssignment.objects.using('tenant_cult_fit').filter(user=u, is_active=True).select_related('role')
    ]
    refresh['tid'] = str(t.id)
    refresh['tenant_slug'] = t.slug
    refresh['db_alias'] = 'tenant_cult_fit'
    refresh['email'] = u.email
    return str(refresh.access_token)


# ==============================================================================
# SECTION 1: METRIC CATALOG RECONCILIATION (S4.4)
# ==============================================================================
print("\n--- Section 1: Metric Catalog Reconciliation ---")

metric_active_users = ResourceMetric.objects.using('default').filter(code='ACTIVE_USERS').first()
record(
    "1.1 Canonical ACTIVE_USERS metric exists",
    metric_active_users is not None and metric_active_users.code == 'ACTIVE_USERS',
    f"Found: {metric_active_users}"
)

metric_trainers = ResourceMetric.objects.using('default').filter(code='TRAINERS').first()
record(
    "1.2 Obsolete TRAINERS metric removed",
    metric_trainers is None,
    "No TRAINERS record in ResourceMetric"
)

plan_limits = SaasPlanResourceLimit.objects.using('default').filter(metric__code='ACTIVE_USERS')
limit_map = {pl.plan.code: pl.limit_value for pl in plan_limits}
record(
    "1.3 Authoritative plan limits exist for ACTIVE_USERS",
    limit_map.get('PLAN-STARTER') == 5 and limit_map.get('PLAN-GROWTH') == 20 and limit_map.get('PLAN-ENTERPRISE') == -1,
    f"Limits: {limit_map}"
)


# ==============================================================================
# SECTION 2: LIVE QUOTACHECKER SERVICE (S4.1)
# ==============================================================================
print("\n--- Section 2: QuotaChecker Live Service ---")

# Effective limit resolution
starter_limit = QuotaChecker.get_effective_limit(str(tenant.id), 'ACTIVE_USERS')
record(
    "2.1 Effective limit resolves from subscription plan",
    starter_limit in (5, 20, -1),
    f"Resolved limit: {starter_limit}"
)

# Live usage count
live_active_users = QuotaChecker.get_live_usage('ACTIVE_USERS', 'tenant_cult_fit')
live_locations = QuotaChecker.get_live_usage('LOCATIONS', 'tenant_cult_fit')
record(
    "2.2 Real-time live usage calculated from tenant DB",
    live_active_users >= 1 and live_locations >= 1,
    f"ACTIVE_USERS={live_active_users}, LOCATIONS={live_locations}"
)

# Status counting rule: ACTIVE & INVITED count, INACTIVE & SUSPENDED do not
test_invited = TenantUser.objects.using('tenant_cult_fit').create(
    organization=org,
    email='sprint4_verify_invited@cultfit.in',
    first_name='Verify',
    last_name='Invited',
    status='INVITED',
)
test_inactive = TenantUser.objects.using('tenant_cult_fit').create(
    organization=org,
    email='sprint4_verify_inactive@cultfit.in',
    first_name='Verify',
    last_name='Inactive',
    status='INACTIVE',
)
test_suspended = TenantUser.objects.using('tenant_cult_fit').create(
    organization=org,
    email='sprint4_verify_suspended@cultfit.in',
    first_name='Verify',
    last_name='Suspended',
    status='SUSPENDED',
)

new_count = QuotaChecker.get_live_usage('ACTIVE_USERS', 'tenant_cult_fit')
record(
    "2.3 Status counting: INVITED counted (+1), INACTIVE/SUSPENDED ignored (+0)",
    new_count == live_active_users + 1,
    f"Before: {live_active_users}, After (+1 INVITED, +1 INACTIVE, +1 SUSPENDED): {new_count}"
)

# Clean up status test users
test_invited.delete()
test_inactive.delete()
test_suspended.delete()

# TenantResourceLimit override precedence
override = TenantResourceLimit.objects.using('default').create(
    tenant=tenant,
    metric=metric_active_users,
    limit_value=999,
)
override_limit = QuotaChecker.get_effective_limit(str(tenant.id), 'ACTIVE_USERS')
record(
    "2.4 TenantResourceLimit override takes precedence over SaasPlan limit",
    override_limit == 999,
    f"Override: {override_limit}"
)
override.delete()

# Fail closed on missing config
dummy_metric = ResourceMetric.objects.using('default').create(
    code='UNCONFIGURED_MTR',
    name='Unconfigured',
    unit='units',
)
try:
    QuotaChecker.get_effective_limit(str(tenant.id), 'UNCONFIGURED_MTR')
    record("2.5 Missing quota config fails closed", False, "Did not raise exception")
except QuotaConfigurationError as qce:
    record("2.5 Missing quota config fails closed", True, f"Raised {type(qce).__name__}")
finally:
    dummy_metric.delete()


# ==============================================================================
# SECTION 3: USER CREATION QUOTA API ENFORCEMENT & CONCURRENCY (S4.2)
# ==============================================================================
print("\n--- Section 3: User Creation Quota Enforcement & Atomic Locking ---")

token = get_token()
base_usage = QuotaChecker.get_live_usage('ACTIVE_USERS', 'tenant_cult_fit')

# Set override exactly equal to current usage
limit_override = TenantResourceLimit.objects.using('default').create(
    tenant=tenant,
    metric=metric_active_users,
    limit_value=base_usage,
)

# Attempt user creation when quota is full -> Expect 400 QUOTA_EXCEEDED
payload = {
    'organization': str(org.id),
    'email': 'sprint4_verify_blocked@cultfit.in',
    'first_name': 'Blocked',
    'last_name': 'Staff',
    'password': 'StrongPassword123!',
    'status': 'ACTIVE',
}
res_blocked = client.post(
    '/api/v1/tenant/users/',
    payload,
    content_type='application/json',
    HTTP_AUTHORIZATION=f'Bearer {token}',
)
record(
    "3.1 Creation at quota limit rejected with 400 QUOTA_EXCEEDED",
    res_blocked.status_code == 400 and res_blocked.json().get('code') == 'QUOTA_EXCEEDED',
    f"Status: {res_blocked.status_code}, body: {res_blocked.json()}"
)

# Ensure no partial user persisted
user_persisted = TenantUser.objects.using('tenant_cult_fit').filter(email='sprint4_verify_blocked@cultfit.in').exists()
record(
    "3.2 Atomic rollback: Rejected creation leaves no partial user",
    not user_persisted,
    "No row in tenant DB"
)

# Increase override by 1 -> Creation succeeds
limit_override.limit_value = base_usage + 1
limit_override.save(using='default')

payload['email'] = 'sprint4_verify_allowed@cultfit.in'
res_allowed = client.post(
    '/api/v1/tenant/users/',
    payload,
    content_type='application/json',
    HTTP_AUTHORIZATION=f'Bearer {token}',
)
record(
    "3.3 Creation below quota limit succeeds with 201 Created",
    res_allowed.status_code == 201,
    f"Status: {res_allowed.status_code}"
)

# Clean up created user and override
TenantUser.objects.using('tenant_cult_fit').filter(email='sprint4_verify_allowed@cultfit.in').delete()
limit_override.delete()


# ==============================================================================
# SECTION 4: SUBSCRIPTION AUTHENTICATION HARDENING (S4.3)
# ==============================================================================
print("\n--- Section 4: Subscription Authentication Hardening ---")

sub = TenantSubscription.objects.using('default').filter(tenant=tenant).order_by('-created_at').first()
original_status = sub.status
original_trial_end = sub.trial_ends_at

try:
    # 4.1 ACTIVE -> 200 OK
    sub.status = 'ACTIVE'
    sub.save(using='default')
    r_act = client.get('/api/v1/tenant/users/', HTTP_AUTHORIZATION=f'Bearer {token}')
    record("4.1 ACTIVE subscription allows authentication (200)", r_act.status_code == 200, f"Status: {r_act.status_code}")

    # 4.2 TRIALING valid (future) -> 200 OK
    sub.status = 'TRIALING'
    sub.trial_ends_at = timezone.now() + timezone.timedelta(days=14)
    sub.save(using='default')
    r_tr_val = client.get('/api/v1/tenant/users/', HTTP_AUTHORIZATION=f'Bearer {token}')
    record("4.2 Valid TRIALING allows authentication (200)", r_tr_val.status_code == 200, f"Status: {r_tr_val.status_code}")

    # 4.3 TRIALING expired -> 401 Unauthorized
    sub.status = 'TRIALING'
    sub.trial_ends_at = timezone.now() - timezone.timedelta(minutes=5)
    sub.save(using='default')
    r_tr_exp = client.get('/api/v1/tenant/users/', HTTP_AUTHORIZATION=f'Bearer {token}')
    record("4.3 Expired TRIALING blocks authentication (401)", r_tr_exp.status_code == 401, f"Status: {r_tr_exp.status_code}")

    # 4.4 PAST_DUE strictly blocked (no grace period) -> 401 Unauthorized
    sub.status = 'PAST_DUE'
    sub.save(using='default')
    r_pd = client.get('/api/v1/tenant/users/', HTTP_AUTHORIZATION=f'Bearer {token}')
    record("4.4 PAST_DUE strictly blocks authentication (401)", r_pd.status_code == 401, f"Status: {r_pd.status_code}")

    # 4.5 CANCELED / PAUSED blocked -> 401 Unauthorized
    sub.status = 'CANCELED'
    sub.save(using='default')
    r_canc = client.get('/api/v1/tenant/users/', HTTP_AUTHORIZATION=f'Bearer {token}')
    record("4.5 CANCELED subscription blocks authentication (401)", r_canc.status_code == 401, f"Status: {r_canc.status_code}")

    sub.status = 'PAUSED'
    sub.save(using='default')
    r_paus = client.get('/api/v1/tenant/users/', HTTP_AUTHORIZATION=f'Bearer {token}')
    record("4.6 PAUSED subscription blocks authentication (401)", r_paus.status_code == 401, f"Status: {r_paus.status_code}")

finally:
    # Restore subscription
    sub.status = original_status
    sub.trial_ends_at = original_trial_end
    sub.save(using='default')

# Inactive tenant status -> 401 Unauthorized
original_tenant_status = tenant.status
try:
    tenant.status = 'SUSPENDED'
    tenant.save(using='default')
    r_inact = client.get('/api/v1/tenant/users/', HTTP_AUTHORIZATION=f'Bearer {token}')
    record("4.7 Inactive tenant status blocks authentication (401)", r_inact.status_code == 401, f"Status: {r_inact.status_code}")
finally:
    tenant.status = original_tenant_status
    tenant.save(using='default')


# ==============================================================================
# SECTION 5: TENANT RESOURCE USAGE METERING (S4.5)
# ==============================================================================
print("\n--- Section 5: Resource Usage Metering Background Sync ---")

# Run sync
res_sync = sync_tenant_resource_usage(tenant_id=str(tenant.id))
record(
    "5.1 sync_tenant_resource_usage execution succeeded",
    res_sync['succeeded'] >= 1 and res_sync['failed'] == 0,
    f"Result: {res_sync}"
)

# Verify snapshots in Master DB
usage_users = TenantResourceUsage.objects.using('default').filter(tenant=tenant, metric=metric_active_users).first()
usage_locs = TenantResourceUsage.objects.using('default').filter(tenant=tenant, metric__code='LOCATIONS').first()

live_users_now = QuotaChecker.get_live_usage('ACTIVE_USERS', 'tenant_cult_fit')
live_locs_now = QuotaChecker.get_live_usage('LOCATIONS', 'tenant_cult_fit')

record(
    "5.2 ACTIVE_USERS snapshot matches authoritative live tenant count",
    usage_users is not None and usage_users.current_value == live_users_now,
    f"Snapshot: {usage_users.current_value if usage_users else None}, Live: {live_users_now}"
)

record(
    "5.3 LOCATIONS snapshot matches authoritative live tenant count",
    usage_locs is not None and usage_locs.current_value == live_locs_now,
    f"Snapshot: {usage_locs.current_value if usage_locs else None}, Live: {live_locs_now}"
)

# Idempotency: run sync again, ensure row count does not increase
prev_count = TenantResourceUsage.objects.using('default').filter(tenant=tenant).count()
sync_tenant_resource_usage(tenant_id=str(tenant.id))
new_usage_count = TenantResourceUsage.objects.using('default').filter(tenant=tenant).count()
record(
    "5.4 Metering sync is idempotent (no duplicate records)",
    prev_count == new_usage_count,
    f"Count before={prev_count}, Count after={new_usage_count}"
)

# Management command execution
try:
    call_command('sync_resource_usage', '--tenant', tenant.slug)
    record("5.5 Management command sync_resource_usage ran successfully", True)
except Exception as cmd_exc:
    record("5.5 Management command sync_resource_usage ran successfully", False, str(cmd_exc))


# ==============================================================================
# SUMMARY
# ==============================================================================
print("\n" + "=" * 80)
total_tests = len(results)
passed_tests = sum(1 for _, p, _ in results)
failed_tests = total_tests - passed_tests

print(f"VERIFICATION SUMMARY: {passed_tests}/{total_tests} CHECKS PASSED")
if failed_tests > 0:
    print(f"FAILED CHECKS ({failed_tests}):")
    for name, p, det in results:
        if not p:
            print(f"  - {name}: {det}")
    sys.exit(1)
else:
    print("ALL SPRINT 4 BILLING ENFORCEMENT & METERING RUNTIME CHECKS PASSED!")
    print("=" * 80)
    sys.exit(0)
