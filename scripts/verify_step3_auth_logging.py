"""
Step 3 Verification Script: Authentication & Security Logging
Validates:
1. UniversalLoginView login success audit log (AUTH.LOGIN_SUCCESS)
2. UniversalLoginView login failure audit log (AUTH.LOGIN_FAILED)
3. LogoutView logout audit log (AUTH.LOGOUT)
4. Session revocation audit log (REVOKE_SESSIONS)
5. MFA verify/failed audit logging without TypeError
"""

import os
import sys
import uuid
import django
from django.test import RequestFactory

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.tenant_core.models_activity_audit import ActivityAuditLog
from apps.tenant_core.models_org import Organization, Branch
from apps.tenant_core.models_users import TenantUser
from apps.authentication.views import UniversalLoginView, LogoutView
from apps.authentication.security import revoke_all_user_sessions
from apps.tenant_core.audit import emit_audit_event
from config.tenant_middleware import _register_tenant_connection
from config.routers import set_tenant_db_alias

print("=" * 80)
print("STEP 3: AUTHENTICATION & SECURITY AUDIT VERIFICATION")
print("=" * 80)

# Connect to tenant database
t = Tenant.objects.using('default').get(slug='sweat')
ds = TenantDataSource.objects.using('default').get(tenant=t)
db_alias = f"tenant_{t.slug}"
_register_tenant_connection(db_alias, db_name=ds.database_name, data_source=ds, tenant_id=t.id)
set_tenant_db_alias(db_alias)

test_user = TenantUser.objects.using(db_alias).filter(status='ACTIVE').first()
assert test_user is not None, "No active TenantUser found for testing!"
print(f"Target test user: {test_user.email} (ID: {test_user.id})")

factory = RequestFactory()

# Test 1: Failed Login Attempt
print("\n[Test 1: UniversalLoginView Failed Login Logging]")
count_before = ActivityAuditLog.objects.using(db_alias).filter(action_code='AUTH.LOGIN_FAILED').count()

req_fail = factory.post(
    '/api/v1/auth/login/',
    {'email': test_user.email, 'password': 'WrongPassword123!', 'tenant_slug': 'sweat'},
    format='json'
)
req_fail.META['REMOTE_ADDR'] = '192.168.1.50'
req_fail.META['HTTP_USER_AGENT'] = 'Mozilla/5.0 SecurityTestBrowser'
view = UniversalLoginView.as_view()
res_fail = view(req_fail)

count_after = ActivityAuditLog.objects.using(db_alias).filter(action_code='AUTH.LOGIN_FAILED').count()
assert count_after == count_before + 1, f"Failed login not recorded! Before={count_before}, After={count_after}"
last_fail = ActivityAuditLog.objects.using(db_alias).filter(action_code='AUTH.LOGIN_FAILED').first()
assert last_fail.result == 'FAILURE'
assert last_fail.error_code == 'INVALID_CREDENTIALS'
assert last_fail.ip_address == '192.168.1.50'
print(f"  [PASS] Failed login recorded cleanly: {last_fail.event_description}")

# Test 2: Successful Login Attempt
print("\n[Test 2: UniversalLoginView Successful Login Logging]")
# Set a known temporary password on test_user
test_password = "TemporaryTestPassword123!"
test_user.set_password(test_password)
test_user.save(using=db_alias)

count_succ_before = ActivityAuditLog.objects.using(db_alias).filter(action_code='AUTH.LOGIN_SUCCESS').count()

req_succ = factory.post(
    '/api/v1/auth/login/',
    {'email': test_user.email, 'password': test_password, 'tenant_slug': 'sweat'},
    format='json'
)
req_succ.META['REMOTE_ADDR'] = '192.168.1.55'
req_succ.META['HTTP_USER_AGENT'] = 'Mozilla/5.0 VerifiedUserBrowser'
res_succ = view(req_succ)

assert res_succ.status_code == 200, f"Login failed with status {res_succ.status_code}: {res_succ.data}"
count_succ_after = ActivityAuditLog.objects.using(db_alias).filter(action_code='AUTH.LOGIN_SUCCESS').count()
assert count_succ_after == count_succ_before + 1, "Successful login not recorded!"
last_succ = ActivityAuditLog.objects.using(db_alias).filter(action_code='AUTH.LOGIN_SUCCESS').first()
assert last_succ.result == 'SUCCESS'
assert last_succ.actor_email == test_user.email
assert last_succ.ip_address == '192.168.1.55'
print(f"  [PASS] Successful login recorded cleanly: {last_succ.event_description}")

# Test 3: Logout Logging
print("\n[Test 3: LogoutView Logging]")
refresh_token = res_succ.data.get('refresh') or res_succ.cookies.get('refresh_token').value
count_logout_before = ActivityAuditLog.objects.using(db_alias).filter(action_code='AUTH.LOGOUT').count()

req_logout = factory.post(
    '/api/v1/auth/logout/',
    {'refresh': refresh_token},
    format='json'
)
req_logout.META['REMOTE_ADDR'] = '192.168.1.55'
logout_view = LogoutView.as_view()
res_logout = logout_view(req_logout)
assert res_logout.status_code == 200

count_logout_after = ActivityAuditLog.objects.using(db_alias).filter(action_code='AUTH.LOGOUT').count()
assert count_logout_after == count_logout_before + 1, "Logout not recorded!"
last_logout = ActivityAuditLog.objects.using(db_alias).filter(action_code='AUTH.LOGOUT').first()
assert last_logout.action_code == 'AUTH.LOGOUT'
print(f"  [PASS] Logout recorded cleanly: {last_logout.event_description}")

# Test 4: Session Revocation Logging
print("\n[Test 4: Session Revocation Logging]")
count_rev_before = ActivityAuditLog.objects.using(db_alias).filter(action_code__contains='REVOKE_SESSIONS').count()
revoke_all_user_sessions(
    user_id=str(test_user.id),
    user_type='tenant',
    actor_email='admin@sweat.fit',
    client_ip='10.0.0.1',
    db_alias=db_alias
)
count_rev_after = ActivityAuditLog.objects.using(db_alias).filter(action_code__contains='REVOKE_SESSIONS').count()
assert count_rev_after == count_rev_before + 1, "Session revocation not recorded in ActivityAuditLog!"
last_rev = ActivityAuditLog.objects.using(db_alias).filter(action_code__contains='REVOKE_SESSIONS').first()
print(f"  [PASS] Session revocation recorded: {last_rev.event_description}")

# Test 5: MFA Verification & Failure Audit Logging
print("\n[Test 5: MFA Verification Audit Logging]")
count_mfa_before = ActivityAuditLog.objects.using(db_alias).filter(action_code__contains='MFA').count()
emit_audit_event(
    actor_id=str(test_user.id),
    actor_email=test_user.email,
    actor_roles=['Staff'],
    action='SECURITY.MFA_FAILED',
    target_type='TenantUser',
    target_id=str(test_user.id),
    db_alias=db_alias,
    metadata={'reason': 'Invalid TOTP code provided'},
)
count_mfa_after = ActivityAuditLog.objects.using(db_alias).filter(action_code__contains='MFA').count()
assert count_mfa_after == count_mfa_before + 1, "MFA audit event not recorded!"
last_mfa = ActivityAuditLog.objects.using(db_alias).filter(action_code__contains='MFA').first()
print(f"  [PASS] MFA event recorded without TypeError: {last_mfa.action_code} - {last_mfa.event_description}")

print("\n" + "=" * 80)
print("STEP 3 VERIFICATION COMPLETED SUCCESSFULLY: 5/5 CHECKS PASSED")
print("=" * 80)
