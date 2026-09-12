"""
Sprint 2 RBAC Runtime Verification Script.
Executes runtime tests against the centralized authorization engine,
DRF permission classes, multi-tenant DBs, and API endpoints.
"""

import os
import sys
import json
import django

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.test import Client
from rest_framework_simplejwt.tokens import RefreshToken
from apps.master.models_tenant import Tenant
from apps.master.models_saas import TenantModule, ProductModule, ProductSubmodule, TenantPermissionCatalog
from apps.master.models_iam import PlatformUser, PlatformRole, PlatformPermission, PlatformRolePermission, PlatformUserRole
from apps.tenant_core.models_org import Organization, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_rbac import (
    Role, RoleAssignment, Permission, RoleModuleAccess,
    RoleSubmoduleAccess, RolePermissionSet, RolePermissionSetItem, BranchModule,
    ModuleCatalog, SubmoduleCatalog
)
from apps.tenant_core.rbac_engine import RBACAuthorizationEngine
from config.routers import set_tenant_db_alias
from config.tenant_middleware import _register_tenant_connection

results = []

def record(test_name, passed, detail=""):
    results.append((test_name, passed, detail))
    status_label = "[PASS]" if passed else "[FAIL]"
    print(f"  {status_label} {test_name}" + (f" -> {detail}" if detail else ""))

print("=" * 75)
print("SPRINT 2 RBAC RUNTIME VERIFICATION")
print("=" * 75)

# Setup live connection to cult_fit
_register_tenant_connection('tenant_tenant_cult_fit', 'tenant_cult_fit')
_register_tenant_connection('tenant_cult_fit', 'tenant_cult_fit')
set_tenant_db_alias('tenant_cult_fit')

client = Client()

# Retrieve base live records
tenant = Tenant.objects.using('default').get(slug='cult-fit')
org = Organization.objects.using('tenant_cult_fit').first()
branch = Branch.objects.using('tenant_cult_fit').first()
admin_user = TenantUser.objects.using('tenant_cult_fit').get(email='admin@cultfit.in')
org_admin_role = Role.objects.using('tenant_cult_fit').get(code='ORG_ADMIN')

# Helper to generate tenant JWT token
def get_tenant_token(user):
    set_tenant_db_alias('tenant_cult_fit')
    refresh = RefreshToken()
    refresh['sub'] = str(user.id)
    refresh['user_type'] = 'tenant'
    refresh['roles'] = [
        ra.role.code for ra in RoleAssignment.objects.using('tenant_cult_fit')
        .filter(user=user, is_active=True).select_related('role')
    ]
    refresh['tid'] = str(tenant.id)
    refresh['tenant_slug'] = tenant.slug
    refresh['db_alias'] = 'tenant_tenant_cult_fit'
    refresh['email'] = user.email
    return str(refresh.access_token)

# Helper to generate platform JWT token
def get_platform_token(user):
    refresh = RefreshToken()
    refresh['sub'] = str(user.id)
    refresh['user_type'] = 'platform'
    refresh['roles'] = [
        ur.role.code for ur in PlatformUserRole.objects.using('default')
        .filter(user=user, is_active=True).select_related('role')
    ]
    refresh['tid'] = ''
    refresh['email'] = user.email
    return str(refresh.access_token)

admin_token = get_tenant_token(admin_user)

# ---------------------------------------------------------------------
# SECTION 1: 10-CHECK AUTHORIZATION ENGINE VALIDATION
# ---------------------------------------------------------------------
print("\n--- 1. 10-Check Central RBAC Engine Evaluation ---")

# Attach context to admin_user
admin_user._tenant_id = str(tenant.id)
admin_user._db_alias = 'tenant_cult_fit'
admin_user._auth_type = 'tenant'


engine = RBACAuthorizationEngine()

# Check 1: Inactive Tenant
orig_tenant_status = tenant.status
tenant.status = 'SUSPENDED'
tenant.save(using='default')

allowed1, reason1, code1 = engine.evaluate(
    user=admin_user,
    required_module='core',
    required_submodule='users',
    required_permission='core.users.view',
)
record("Check 1: Inactive tenant rejected", not allowed1 and code1 == "CHECK_1_TENANT_INACTIVE", f"Reason: {reason1}")

tenant.status = orig_tenant_status
tenant.save(using='default')

# Check 2: Inactive Organization
orig_org_status = org.status
org.status = 'SUSPENDED'
org.save(using='tenant_cult_fit')

allowed2, reason2, code2 = engine.evaluate(
    user=admin_user,
    required_module='core',
    required_submodule='users',
    required_permission='core.users.view',
)
record("Check 2: Inactive organization rejected", not allowed2 and code2 == "CHECK_2_ORG_INACTIVE", f"Reason: {reason2}")

org.status = orig_org_status
org.save(using='tenant_cult_fit')

# Check 3: Inactive Branch
orig_branch_status = branch.status
branch.status = 'INACTIVE'
branch.save(using='tenant_cult_fit')

allowed3, reason3, code3 = engine.evaluate(
    user=admin_user,
    required_module='core',
    required_submodule='users',
    required_permission='core.users.view',
    branch_id=str(branch.id),
)
record("Check 3: Inactive branch rejected", not allowed3 and code3 == "CHECK_3_BRANCH_INACTIVE", f"Reason: {reason3}")

branch.status = orig_branch_status
branch.save(using='tenant_cult_fit')

# Check 4: Disabled Tenant Module
allowed4, reason4, code4 = engine.evaluate(
    user=admin_user,
    required_module='non_existent_module',
    required_submodule='sub',
    required_permission='non_existent_module.sub.view',
)
record("Check 4: Disabled/unsubscribed tenant module rejected", not allowed4 and code4 == "CHECK_4_MODULE_NOT_ENTITLED", f"Reason: {reason4}")

# Check 5: Unavailable Branch Module (SELECTED_BRANCHES mode)
mod_crm_master = ProductModule.objects.using('default').get(code='crm')
tm_crm = TenantModule.objects.using('default').filter(tenant=tenant, module=mod_crm_master).first()
orig_mode = tm_crm.availability_mode if tm_crm else 'ALL_BRANCHES'

from apps.tenant_core.models_rbac import BranchModule
bm_crm = BranchModule.objects.using('tenant_cult_fit').filter(branch=branch, module_code='crm').first()
orig_bm_enabled = bm_crm.is_enabled if bm_crm else None
if bm_crm:
    bm_crm.is_enabled = False
    bm_crm.save(using='tenant_cult_fit')

if tm_crm:
    tm_crm.availability_mode = 'SELECTED_BRANCHES'
    tm_crm.save(using='default')

allowed5, reason5, code5 = engine.evaluate(
    user=admin_user,
    required_module='crm',
    required_submodule='leads',
    required_permission='crm.leads.view',
    branch_id=str(branch.id),
)
record("Check 5: Unavailable branch module rejected (SELECTED_BRANCHES)", not allowed5 and code5 == "CHECK_5_BRANCH_MODULE_UNAVAILABLE", f"Reason: {reason5}")

if tm_crm:
    tm_crm.availability_mode = orig_mode
    tm_crm.save(using='default')
if bm_crm and orig_bm_enabled is not None:
    bm_crm.is_enabled = orig_bm_enabled
    bm_crm.save(using='tenant_cult_fit')

# Check 6: Inactive User
user_inactive = TenantUser.objects.using('tenant_cult_fit').filter(email='disabled@cultfit.in').first()
if not user_inactive:
    user_inactive = TenantUser.objects.using('tenant_cult_fit').create(
        organization=org,
        email='disabled@cultfit.in',
        first_name='Disabled',
        last_name='User',
        status='DISABLED',
    )
user_inactive._tenant_id = str(tenant.id)
user_inactive._db_alias = 'tenant_cult_fit'
user_inactive._auth_type = 'tenant'

allowed6, reason6, code6 = engine.evaluate(
    user=user_inactive,
    required_module='crm',
    required_submodule='leads',
    required_permission='crm.leads.view',
)
record("Check 6: Inactive user rejected", not allowed6 and code6 == "CHECK_6_USER_INACTIVE", f"Reason: {reason6}")

# Check 7: Inactive / Missing Role Assignment
user_norole = TenantUser.objects.using('tenant_cult_fit').filter(email='user_norole@cultfit.in').first()
if not user_norole:
    user_norole = TenantUser.objects.using('tenant_cult_fit').create(
        organization=org,
        email='user_norole@cultfit.in',
        first_name='NoRole',
        last_name='User',
        status='ACTIVE'
    )
user_norole._tenant_id = str(tenant.id)
user_norole._db_alias = 'tenant_cult_fit'
user_norole._auth_type = 'tenant'

allowed7, reason7, code7 = engine.evaluate(
    user=user_norole,
    required_module='crm',
    required_submodule='leads',
    required_permission='crm.leads.view',
)
record("Check 7: User with no active role assignments rejected", not allowed7 and code7 == "CHECK_7_NO_ACTIVE_ROLE", f"Reason: {reason7}")

# Check 8: Role Module Access Denied
role_limited, _ = Role.objects.using('tenant_cult_fit').get_or_create(
    organization=org,
    code='FRONT_DESK',
    defaults={
        'name': 'Front Desk',
        'scope': 'BRANCH',
        'is_system': False,
        'is_active': True
    }
)
mod_finance = ModuleCatalog.objects.using('tenant_cult_fit').get(module_code='finance')
RoleModuleAccess.objects.using('tenant_cult_fit').update_or_create(
    role=role_limited,
    module=mod_finance,
    defaults={'can_access': False}
)
user_limited = TenantUser.objects.using('tenant_cult_fit').filter(email='reception@cultfit.in').first()
if not user_limited:
    user_limited = TenantUser.objects.using('tenant_cult_fit').create(
        organization=org,
        email='reception@cultfit.in',
        first_name='Front',
        last_name='Desk',
        status='ACTIVE',
        home_branch=branch
    )
user_limited._tenant_id = str(tenant.id)
user_limited._db_alias = 'tenant_cult_fit'
user_limited._auth_type = 'tenant'

RoleAssignment.objects.using('tenant_cult_fit').update_or_create(
    user=user_limited,
    role=role_limited,
    defaults={'branch': branch, 'is_active': True}
)

allowed8, reason8, code8 = engine.evaluate(
    user=user_limited,
    required_module='finance',
    required_submodule='invoices',
    required_permission='finance.invoices.view',
)
record("Check 8: Role module access denied", not allowed8 and code8 == "CHECK_8_MODULE_ACCESS_DENIED", f"Reason: {reason8}")

# Check 9: Role Submodule Access Denied
mod_crm = ModuleCatalog.objects.using('tenant_cult_fit').get(module_code='crm')
sub_campaigns = SubmoduleCatalog.objects.using('tenant_cult_fit').get(module=mod_crm, submodule_code='campaigns')
sub_leads = SubmoduleCatalog.objects.using('tenant_cult_fit').get(module=mod_crm, submodule_code='leads')

RoleModuleAccess.objects.using('tenant_cult_fit').update_or_create(
    role=role_limited,
    module=mod_crm,
    defaults={'can_access': True}
)
RoleSubmoduleAccess.objects.using('tenant_cult_fit').update_or_create(
    role=role_limited,
    submodule=sub_campaigns,
    defaults={'can_access': False}
)
allowed9, reason9, code9 = engine.evaluate(
    user=user_limited,
    required_module='crm',
    required_submodule='campaigns',
    required_permission='crm.campaigns.view',
)
record("Check 9: Role submodule access denied", not allowed9 and code9 == "CHECK_9_SUBMODULE_ACCESS_DENIED", f"Reason: {reason9}")

# Check 10: Action Permission Denied
RoleSubmoduleAccess.objects.using('tenant_cult_fit').update_or_create(
    role=role_limited,
    submodule=sub_leads,
    defaults={'can_access': True}
)
pset_limited, _ = RolePermissionSet.objects.using('tenant_cult_fit').get_or_create(
    role=role_limited,
    defaults={'name': 'Front Desk Leads PSet', 'is_active': True}
)
perm_leads_create = Permission.objects.using('tenant_cult_fit').get(permission_code='crm.leads.create')
RolePermissionSetItem.objects.using('tenant_cult_fit').update_or_create(
    permission_set=pset_limited,
    permission=perm_leads_create,
    defaults={'granted': False}
)
allowed10, reason10, code10 = engine.evaluate(
    user=user_limited,
    required_module='crm',
    required_submodule='leads',
    required_permission='crm.leads.create',
)
record("Check 10: Action permission denied", not allowed10 and code10 == "CHECK_10_PERMISSION_DENIED", f"Reason: {reason10}")

# ---------------------------------------------------------------------
# SECTION 2: CROSS-BRANCH ACCESS & PRIVILEGE ESCALATION
# ---------------------------------------------------------------------
print("\n--- 2. Branch & Horizontal Isolation ---")

branch_b = Branch.objects.using('tenant_cult_fit').filter(code='BLR-KOR').first()
if not branch_b:
    branch_b = Branch.objects.using('tenant_cult_fit').create(
        organization=org,
        location=branch.location,
        name='Koramangala Center',
        code='BLR-KOR',
        status='ACTIVE'
    )

user_b = TenantUser.objects.using('tenant_cult_fit').filter(email='user_b@cultfit.in').first()
if not user_b:
    user_b = TenantUser.objects.using('tenant_cult_fit').create(
        organization=org,
        email='user_b@cultfit.in',
        first_name='BranchB',
        last_name='User',
        status='ACTIVE',
        home_branch=branch_b
    )

# Grant core.users.view to role_limited so front desk staff can list users at their branch
mod_core = ModuleCatalog.objects.using('tenant_cult_fit').get(module_code='core')
sub_users = SubmoduleCatalog.objects.using('tenant_cult_fit').get(module=mod_core, submodule_code='users')
perm_users_view = Permission.objects.using('tenant_cult_fit').get(permission_code='core.users.view')

RoleModuleAccess.objects.using('tenant_cult_fit').update_or_create(
    role=role_limited, module=mod_core, defaults={'can_access': True}
)
RoleSubmoduleAccess.objects.using('tenant_cult_fit').update_or_create(
    role=role_limited, submodule=sub_users, defaults={'can_access': True}
)
RolePermissionSetItem.objects.using('tenant_cult_fit').update_or_create(
    permission_set=pset_limited, permission=perm_users_view, defaults={'granted': True}
)

token_limited = get_tenant_token(user_limited)

# Query /api/v1/tenant/users/ as branch-scoped user
resp_branch_list = client.get(
    '/api/v1/tenant/users/',
    HTTP_AUTHORIZATION=f'Bearer {token_limited}'
)
branch_users = resp_branch_list.json().get('results', []) if resp_branch_list.status_code == 200 else []
user_ids = [u.get('id') for u in branch_users]
record(
    "Branch isolation: Branch A user cannot list Branch B users",
    str(user_b.id) not in user_ids,
    f"Status={resp_branch_list.status_code}, VisibleCount={len(user_ids)}, UserBInResults={str(user_b.id) in user_ids}"
)

# Attempt direct read/write on Branch B user by Branch A staff
resp_cross = client.get(
    f'/api/v1/tenant/users/{user_b.id}/',
    HTTP_AUTHORIZATION=f'Bearer {token_limited}'
)
record(
    "Horizontal privilege escalation: Direct access to Branch B resource fails (404/403)",
    resp_cross.status_code in (403, 404),
    f"Status={resp_cross.status_code}"
)

# ---------------------------------------------------------------------
# SECTION 3: REAL-TIME DB REVOCATION (JWT ROLES NOT TRUSTED)
# ---------------------------------------------------------------------
print("\n--- 3. Database-Driven Role Revocation (JWT Claims Informational Only) ---")

token_to_revoke = get_tenant_token(user_limited)
resp_before = client.get(
    '/api/v1/tenant/users/',
    HTTP_AUTHORIZATION=f'Bearer {token_to_revoke}'
)
# Now revoke the role assignment in DB directly
RoleAssignment.objects.using('tenant_cult_fit').filter(user=user_limited).update(is_active=False)

resp_after = client.get(
    '/api/v1/tenant/users/',
    HTTP_AUTHORIZATION=f'Bearer {token_to_revoke}'
)
record(
    "JWT remains unexpired but revoked DB role loses access immediately (403)",
    resp_before.status_code == 200 and resp_after.status_code == 403,
    f"BeforeStatus={resp_before.status_code}, AfterStatus={resp_after.status_code}"
)

# Restore assignment
RoleAssignment.objects.using('tenant_cult_fit').filter(user=user_limited).update(is_active=True)

# ---------------------------------------------------------------------
# SECTION 4: SYSTEM ROLE IMMUTABILITY
# ---------------------------------------------------------------------
print("\n--- 4. System Role Protection (is_system=True) ---")

# Attempt to delete ORG_ADMIN system role
resp_del_sys = client.delete(
    f'/api/v1/tenant/roles/{org_admin_role.id}/',
    HTTP_AUTHORIZATION=f'Bearer {admin_token}'
)
record(
    "System role deletion blocked (403)",
    resp_del_sys.status_code == 403,
    f"Status={resp_del_sys.status_code}, Error={resp_del_sys.json().get('detail')}"
)

# Attempt to modify code/is_system of ORG_ADMIN
resp_patch_sys = client.patch(
    f'/api/v1/tenant/roles/{org_admin_role.id}/',
    data=json.dumps({'code': 'COMPROMISED', 'is_system': False}),
    content_type='application/json',
    HTTP_AUTHORIZATION=f'Bearer {admin_token}'
)
record(
    "System role code modification blocked (403)",
    resp_patch_sys.status_code == 403,
    f"Status={resp_patch_sys.status_code}, Error={resp_patch_sys.json().get('detail')}"
)

# ---------------------------------------------------------------------
# SECTION 5: PLATFORM RBAC PERMISSION ENFORCEMENT
# ---------------------------------------------------------------------
print("\n--- 5. Platform RBAC Enforcement ---")

pu_billing = PlatformUser.objects.using('default').filter(email='billing.verify@performanceos.io').first()
if not pu_billing:
    pu_billing = PlatformUser.objects.using('default').create(
        email='billing.verify@performanceos.io',
        first_name='Billing',
        last_name='Staff',
        status='ACTIVE',
        is_active=True
    )
else:
    pu_billing.status = 'ACTIVE'
    pu_billing.is_active = True
    pu_billing.save(using='default')
role_billing, _ = PlatformRole.objects.using('default').get_or_create(
    code='BILLING_SPECIALIST',
    defaults={'name': 'Billing Specialist', 'is_active': True}
)
PlatformUserRole.objects.using('default').update_or_create(
    user=pu_billing,
    role=role_billing,
    defaults={'is_active': True}
)

token_billing = get_platform_token(pu_billing)

# Accessing /api/v1/platform/tenants/ requires tenants.view, which BILLING_SPECIALIST lacks
resp_plat_denied = client.get(
    '/api/v1/platform/tenants/',
    HTTP_AUTHORIZATION=f'Bearer {token_billing}'
)
record(
    "Platform user without required permission gets 403",
    resp_plat_denied.status_code == 403,
    f"Status={resp_plat_denied.status_code}, Detail={resp_plat_denied.json().get('detail')}"
)

# ---------------------------------------------------------------------
# SECTION 6: DIRECT API ACCESS ENFORCEMENT
# ---------------------------------------------------------------------
print("\n--- 6. Direct API Calls Cannot Bypass Authorization ---")

# Unauthenticated request fails closed
resp_unauth = client.get('/api/v1/tenant/users/')
record(
    "Unauthenticated direct API call returns 401",
    resp_unauth.status_code == 401,
    f"Status={resp_unauth.status_code}"
)

# Direct access to roles endpoint with user lacking roles.view permission
resp_role_forbidden = client.get(
    '/api/v1/tenant/roles/',
    HTTP_AUTHORIZATION=f'Bearer {token_limited}'
)
record(
    "Unauthorized direct API call returns 403",
    resp_role_forbidden.status_code == 403,
    f"Status={resp_role_forbidden.status_code}"
)

# ---------------------------------------------------------------------
# SECTION 7: CANONICAL CORE MODULE & FOUNDATION HARDENING
# ---------------------------------------------------------------------
print("\n--- 7. Canonical Core Module & Foundation Endpoints Hardening ---")

# 1. Verify Master DB Core Catalog
master_core = ProductModule.objects.using('default').filter(code='core', is_core=True, is_active=True).first()
master_sub_count = ProductSubmodule.objects.using('default').filter(module__code='core').count()
master_perm_count = TenantPermissionCatalog.objects.using('default').filter(module__code='core').count()
record(
    "Master DB core module exists with canonical submodules and permissions",
    master_core is not None and master_sub_count >= 4 and master_perm_count >= 15,
    f"Module={getattr(master_core, 'code', None)}, Submodules={master_sub_count}, Permissions={master_perm_count}"
)

# 2. Verify Tenant DB Core Catalog Synchronization
tenant_core_mod = ModuleCatalog.objects.using('tenant_cult_fit').filter(module_code='core', is_enabled=True).first()
tenant_core_subs = SubmoduleCatalog.objects.using('tenant_cult_fit').filter(module__module_code='core').count()
tenant_core_perms = Permission.objects.using('tenant_cult_fit').filter(module__module_code='core').count()
record(
    "Tenant DB has synchronized core module, submodules, and permissions",
    tenant_core_mod is not None and tenant_core_subs >= 4 and tenant_core_perms >= 15,
    f"Module={getattr(tenant_core_mod, 'module_code', None)}, Submodules={tenant_core_subs}, Permissions={tenant_core_perms}"
)

# 3. Verify ORG_ADMIN has all core permissions
org_admin_ps = RolePermissionSet.objects.using('tenant_cult_fit').get(role=org_admin_role)
org_admin_core_count = RolePermissionSetItem.objects.using('tenant_cult_fit').filter(
    permission_set=org_admin_ps,
    permission__module__module_code='core',
    granted=True
).count()
record(
    "ORG_ADMIN role possesses Core action permissions in live DB",
    org_admin_core_count >= 15,
    f"GrantedCoreCount={org_admin_core_count}"
)

# 4. Verify Structural Tenant Endpoints are Strictly Read-Only (BranchModuleViewSet cannot mutate)
resp_branch_module_post = client.post(
    '/api/v1/tenant/branch-modules/',
    data=json.dumps({'branch_id': str(branch.id), 'module_code': 'crm'}),
    content_type='application/json',
    HTTP_AUTHORIZATION=f'Bearer {admin_token}'
)
record(
    "Branch modules endpoint is strictly Read-Only on tenant API (mutation returns 405)",
    resp_branch_module_post.status_code == 405,
    f"Status={resp_branch_module_post.status_code}"
)

# 5. Verify Tenant Admin CRUD: ORG_ADMIN can create and list roles via central RBAC
new_role_code = 'TEST_LEAD_COACH'
Role.objects.using('tenant_cult_fit').filter(organization=org, code=new_role_code).delete()
resp_create_role = client.post(
    '/api/v1/tenant/roles/',
    data=json.dumps({
        'organization': str(org.id),
        'name': 'Test Lead Coach',
        'code': new_role_code,
        'scope': 'BRANCH',
    }),
    content_type='application/json',
    HTTP_AUTHORIZATION=f'Bearer {admin_token}'
)
record(
    "Tenant Admin (ORG_ADMIN) creates custom role via RBAC-protected endpoint",
    resp_create_role.status_code == 201,
    f"Status={resp_create_role.status_code}"
)
if resp_create_role.status_code == 201:
    created_role_id = resp_create_role.json()['id']
    Role.objects.using('tenant_cult_fit').filter(id=created_role_id).delete()


# Summary
print("\n" + "=" * 75)
total_tests = len(results)
passed_tests = sum(1 for _, p, _ in results if p)
failed_tests = total_tests - passed_tests
print(f"RESULTS: Total={total_tests} | Passed={passed_tests} | Failed={failed_tests}")
print("=" * 75)

if failed_tests > 0:
    sys.exit(1)
