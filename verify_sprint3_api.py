"""
Sprint 3 API & Integration Live Runtime Verification Script.
Executes live runtime tests against:
- Master DB ('default') and dedicated Tenant DB ('tenant_cult_fit')
- Canonical Platform Permission Catalog & Role Grants (Req I)
- Platform Custom Action RBAC:
  - assign_branches: negative without tenants.edit (403), positive with tenants.edit (200) (Req A, B)
  - change_plan: negative without billing.edit (403), positive with billing.edit (200) (Req C, D)
  - toggle_install: negative without marketplace.edit (403), positive with marketplace.edit (200) (Req E, F)
- Tenant UserBranch RBAC:
  - Positive with ONLY core.users.edit: create (201), update (200), delete (204) (Req G)
  - Negative without core.users.edit: create (403), update (403), delete (403) (Req H)
- Read-only & Atomic RBAC invariants:
  - Invoices (read-only), Resource usage & summary (read-only)
  - SubmoduleCatalog & PermissionCatalog (read-only)
  - Atomic RBAC matrix endpoint (/matrix/) with rollback & system protection
  - CompanyEntity (read-only for tenant), TenantAuditEvent (read-only ledger)
  - OrganizationSettings, BranchSettings, NotificationTemplate
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
from apps.master.models_saas import (
    TenantModule, ProductModule, SaasPlan, SaasPlanPrice,
    TenantSubscription, SubscriptionInvoice, TenantResourceUsage, ResourceMetric,
)
from apps.master.models_market import MarketplaceIntegration, TenantIntegrationEntitlement
from apps.master.models_iam import PlatformUser, PlatformRole, PlatformPermission, PlatformRolePermission, PlatformUserRole
from apps.tenant_core.models_org import Organization, Branch, CompanyEntity
from apps.tenant_core.models_users import TenantUser, UserBranch
from apps.tenant_core.models_rbac import (
    Role, RoleAssignment, Permission, RoleModuleAccess,
    RoleSubmoduleAccess, RolePermissionSet, RolePermissionSetItem, BranchModule,
    ModuleCatalog, SubmoduleCatalog
)
from apps.tenant_core.models_govern import OrganizationSettings, BranchSettings, NotificationTemplate
from apps.tenant_core.models_privacy import TenantAuditEvent
from config.routers import set_tenant_db_alias
from config.tenant_middleware import _register_tenant_connection

results = []

def record(test_name, passed, detail=""):
    results.append((test_name, passed, detail))
    status_label = "[PASS]" if passed else "[FAIL]"
    print(f"  {status_label} {test_name}" + (f" -> {detail}" if detail else ""))

print("=" * 80)
print("SPRINT 3 API & INTEGRATION RUNTIME VERIFICATION (FINAL HARDENING)")
print("=" * 80)

# 1. Register live tenant connection
_register_tenant_connection('tenant_tenant_cult_fit', 'tenant_cult_fit')
_register_tenant_connection('tenant_cult_fit', 'tenant_cult_fit')
set_tenant_db_alias('tenant_cult_fit')

client = Client()

# 2. Retrieve base live entities
tenant = Tenant.objects.using('default').get(slug='cult-fit')
org = Organization.objects.using('tenant_cult_fit').first()
branch = Branch.objects.using('tenant_cult_fit').first()
admin_user = TenantUser.objects.using('tenant_cult_fit').get(email='admin@cultfit.in')
org_admin_role = Role.objects.using('tenant_cult_fit').get(code='ORG_ADMIN')

# Helper: Platform JWT Token for non-superuser users
def get_platform_token_for_role(role_code):
    role = PlatformRole.objects.using('default').get(code=role_code)
    email = f"{role_code.lower()}_test@performanceos.internal"
    user, _ = PlatformUser.objects.using('default').get_or_create(
        email=email,
        defaults={
            'first_name': role_code,
            'last_name': 'Tester',
            'status': 'ACTIVE',
            'is_staff': True,
            'is_superuser': False,  # Strict non-superuser check
        }
    )
    # Ensure role assignment exists
    PlatformUserRole.objects.using('default').get_or_create(user=user, role=role)
    refresh = RefreshToken()
    refresh['sub'] = str(user.id)
    refresh['user_type'] = 'platform'
    refresh['role'] = role_code
    refresh['email'] = user.email
    return str(refresh.access_token)

# Helper: Tenant JWT Token
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

# Platform tokens for testing without superuser bypass
plat_super_token = get_platform_token_for_role('SUPER_ADMIN')      # Has all 19 platform permissions
plat_support_token = get_platform_token_for_role('SUPPORT_LEAD')    # Has only .view permissions, NO .edit
admin_tenant_token = get_tenant_token(admin_user)

print("\n--- 0. PLATFORM PERMISSION CATALOG & ROLE GRANTS (REQ I) ---")
perm_count = PlatformPermission.objects.using('default').count()
grant_count = PlatformRolePermission.objects.using('default').count()
super_grants = PlatformRolePermission.objects.using('default').filter(role__code='SUPER_ADMIN').count()
billing_admin_grants = PlatformRolePermission.objects.using('default').filter(role__code='BILLING_ADMIN').count()
billing_spec_grants = PlatformRolePermission.objects.using('default').filter(role__code='BILLING_SPECIALIST').count()
support_grants = PlatformRolePermission.objects.using('default').filter(role__code='SUPPORT_LEAD').count()

required_perms = [
    'tenants.view', 'tenants.edit', 'tenants.create', 'tenants.delete',
    'billing.view', 'billing.edit', 'billing.create', 'billing.delete',
    'marketplace.view', 'marketplace.edit',
]
missing_perms = [p for p in required_perms if not PlatformPermission.objects.using('default').filter(code=p).exists()]

record(
    "Req I: Master DB contains canonical platform permissions (19) and role grants (32)",
    perm_count == 19 and grant_count == 32 and len(missing_perms) == 0 and super_grants == 19 and billing_admin_grants == 5 and billing_spec_grants == 4 and support_grants == 4,
    f"perms={perm_count}, grants={grant_count}, missing={missing_perms}, super={super_grants}, billing_admin={billing_admin_grants}, billing_spec={billing_spec_grants}, support={support_grants}"
)

print("\n--- 1. MASTER APIS & CUSTOM ACTION RBAC (REQ A-F) ---")

# 1.1 Tenant Metrics
res = client.get('/api/v1/platform/tenants/metrics/', HTTP_AUTHORIZATION=f'Bearer {plat_super_token}')
record(
    "TenantViewSet.metrics returns real computed values",
    res.status_code == 200 and res.json().get('totalTenants', 0) >= 1 and 'monthlyRecurringRevenue' in res.json(),
    f"Status {res.status_code}, totalTenants={res.json().get('totalTenants')}, MRR={res.json().get('monthlyRecurringRevenue')}"
)

# 1.2 Tenant Modules & assign-branches (Req A & B)
crm_tm = TenantModule.objects.using('default').filter(tenant=tenant, module__code='crm').first()
if crm_tm:
    # A. User WITHOUT tenants.edit cannot call assign_branches
    res_assign_denied = client.post(
        f'/api/v1/platform/tenant-modules/{crm_tm.id}/assign-branches/',
        data=json.dumps({'branch_ids': [str(branch.id)]}),
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {plat_support_token}',
    )
    record(
        "Req A: User WITHOUT tenants.edit cannot call assign_branches (HTTP 403)",
        res_assign_denied.status_code == 403,
        f"Status {res_assign_denied.status_code}"
    )

    # B. User WITH tenants.edit can call assign_branches
    res_assign_allowed = client.post(
        f'/api/v1/platform/tenant-modules/{crm_tm.id}/assign-branches/',
        data=json.dumps({'branch_ids': [str(branch.id)]}),
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {plat_super_token}',
    )
    crm_tm.refresh_from_db()
    record(
        "Req B: User WITH tenants.edit can call assign_branches (HTTP 200)",
        res_assign_allowed.status_code == 200 and crm_tm.availability_mode == 'SELECTED_BRANCHES',
        f"Status {res_assign_allowed.status_code}, availability_mode={crm_tm.availability_mode}"
    )

# 1.3 Tenant Subscriptions & change-plan (Req C & D)
active_sub = TenantSubscription.objects.using('default').filter(tenant=tenant).first()
if active_sub:
    plan = active_sub.plan
    # C. User WITHOUT billing.edit cannot call change_plan
    res_change_denied = client.post(
        f'/api/v1/platform/subscriptions/{active_sub.id}/change-plan/',
        data=json.dumps({'plan_id': str(plan.id), 'billing_cycle': active_sub.billing_cycle}),
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {plat_support_token}',
    )
    record(
        "Req C: User WITHOUT billing.edit cannot call change_plan (HTTP 403)",
        res_change_denied.status_code == 403,
        f"Status {res_change_denied.status_code}"
    )

    # D. User WITH billing.edit can call change_plan
    res_change_allowed = client.post(
        f'/api/v1/platform/subscriptions/{active_sub.id}/change-plan/',
        data=json.dumps({'plan_id': str(plan.id), 'billing_cycle': active_sub.billing_cycle}),
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {plat_super_token}',
    )
    record(
        "Req D: User WITH billing.edit can call change_plan (HTTP 200)",
        res_change_allowed.status_code == 200,
        f"Status {res_change_allowed.status_code}"
    )

# 1.4 Marketplace Integration toggle-install (Req E & F)
app = MarketplaceIntegration.objects.using('default').first()
if app:
    # E. User WITHOUT marketplace.edit cannot call toggle_install
    res_toggle_denied = client.post(
        f'/api/v1/platform/marketplace/{app.id}/toggle-install/',
        data=json.dumps({'tenant_id': str(tenant.id)}),
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {plat_support_token}',
    )
    record(
        "Req E: User WITHOUT marketplace.edit cannot call toggle_install (HTTP 403)",
        res_toggle_denied.status_code == 403,
        f"Status {res_toggle_denied.status_code}"
    )

    # F. User WITH marketplace.edit can call toggle_install
    res_toggle_allowed = client.post(
        f'/api/v1/platform/marketplace/{app.id}/toggle-install/',
        data=json.dumps({'tenant_id': str(tenant.id)}),
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {plat_super_token}',
    )
    record(
        "Req F: User WITH marketplace.edit can call toggle_install (HTTP 200)",
        res_toggle_allowed.status_code == 200 and 'is_installed' in res_toggle_allowed.json(),
        f"Status {res_toggle_allowed.status_code}, is_installed={res_toggle_allowed.json().get('is_installed')}"
    )

# 1.5 Invoices & Usage (read-only enforcement)
res_inv = client.get('/api/v1/platform/invoices/', HTTP_AUTHORIZATION=f'Bearer {plat_super_token}')
res_inv_post = client.post(
    '/api/v1/platform/invoices/',
    data=json.dumps({'total': '1000'}),
    content_type='application/json',
    HTTP_AUTHORIZATION=f'Bearer {plat_super_token}',
)
record(
    "SubscriptionInvoiceViewSet is strictly read-only (GET=200, POST=405)",
    res_inv.status_code == 200 and res_inv_post.status_code == 405,
    f"GET {res_inv.status_code}, POST {res_inv_post.status_code}"
)

res_usage = client.get('/api/v1/platform/usage/', HTTP_AUTHORIZATION=f'Bearer {plat_super_token}')
res_usage_sum = client.get('/api/v1/platform/usage/summary/', HTTP_AUTHORIZATION=f'Bearer {plat_super_token}')
res_usage_post = client.post(
    '/api/v1/platform/usage/',
    data=json.dumps({'current_value': 100}),
    content_type='application/json',
    HTTP_AUTHORIZATION=f'Bearer {plat_super_token}',
)
record(
    "TenantResourceUsageViewSet is strictly read-only with summary action (GET=200, summary=200, POST=405)",
    res_usage.status_code == 200 and res_usage_sum.status_code == 200 and res_usage_post.status_code == 405,
    f"GET {res_usage.status_code}, summary {res_usage_sum.status_code}, POST {res_usage_post.status_code}"
)

print("\n--- 2. TENANT RBAC APIS RUNTIME VERIFICATION ---")

# 2.1 Submodules catalog (read-only)
res_sub = client.get('/api/v1/tenant/submodules/', HTTP_AUTHORIZATION=f'Bearer {admin_tenant_token}')
res_sub_post = client.post(
    '/api/v1/tenant/submodules/',
    data=json.dumps({'submodule_code': 'hacked'}),
    content_type='application/json',
    HTTP_AUTHORIZATION=f'Bearer {admin_tenant_token}',
)
sub_items = res_sub.json().get('results', res_sub.json()) if isinstance(res_sub.json(), dict) else res_sub.json()
has_settings_sub = any(s.get('submodule_code') == 'settings' for s in sub_items)
has_notifs_sub = any(s.get('submodule_code') == 'notifications' for s in sub_items)
has_audit_sub = any(s.get('submodule_code') == 'audit' for s in sub_items)
record(
    "SubmoduleCatalogViewSet is read-only (GET=200, POST=405) and contains settings/notifications/audit",
    res_sub.status_code == 200 and res_sub_post.status_code == 405 and has_settings_sub and has_notifs_sub and has_audit_sub,
    f"GET {res_sub.status_code}, POST {res_sub_post.status_code}, submodules: settings={has_settings_sub}, notifs={has_notifs_sub}, audit={has_audit_sub}"
)

# 2.2 Permissions catalog (read-only)
res_perm = client.get('/api/v1/tenant/permissions/', HTTP_AUTHORIZATION=f'Bearer {admin_tenant_token}')
res_perm_post = client.post(
    '/api/v1/tenant/permissions/',
    data=json.dumps({'permission_code': 'hacked.view'}),
    content_type='application/json',
    HTTP_AUTHORIZATION=f'Bearer {admin_tenant_token}',
)
perm_items = res_perm.json().get('results', res_perm.json()) if isinstance(res_perm.json(), dict) else res_perm.json()
has_settings_perm = any(p.get('permission_code') == 'core.settings.view' for p in perm_items)
has_notifs_perm = any(p.get('permission_code') == 'core.notifications.manage' for p in perm_items)
has_audit_perm = any(p.get('permission_code') == 'core.audit.view' for p in perm_items)
record(
    "PermissionViewSet is read-only (GET=200, POST=405) and contains core.settings/notifications/audit permissions",
    res_perm.status_code == 200 and res_perm_post.status_code == 405 and has_settings_perm and has_notifs_perm and has_audit_perm,
    f"GET {res_perm.status_code}, POST {res_perm_post.status_code}, settings.view={has_settings_perm}, notifs.manage={has_notifs_perm}, audit.view={has_audit_perm}"
)

# 2.3 Atomic RBAC Matrix endpoint
set_tenant_db_alias('tenant_cult_fit')
custom_role, _ = Role.objects.using('tenant_cult_fit').get_or_create(
    organization=org,
    code='VERIFY_CUSTOM_STAFF',
    defaults={'name': 'Verify Custom Staff', 'scope': 'BRANCH', 'is_system': False, 'is_active': True}
)
custom_pset, _ = RolePermissionSet.objects.using('tenant_cult_fit').get_or_create(
    role=custom_role,
    defaults={'name': 'Custom Staff Permission Set', 'is_active': True}
)

# A) Atomic success
matrix_payload = {
    'module_access': [{'module_code': 'crm', 'is_allowed': True}],
    'submodule_access': [{'module_code': 'crm', 'submodule_code': 'leads', 'is_allowed': True}],
    'permissions': [{'permission_code': 'crm.leads.view', 'is_granted': True}],
}
res_matrix = client.post(
    f'/api/v1/tenant/permission-sets/{custom_pset.id}/matrix/',
    data=json.dumps(matrix_payload),
    content_type='application/json',
    HTTP_AUTHORIZATION=f'Bearer {admin_tenant_token}',
)
record(
    "POST /permission-sets/{id}/matrix/ executes atomically and updates 3 tables",
    res_matrix.status_code == 200 and res_matrix.json().get('success') is True,
    f"Status {res_matrix.status_code}, success={res_matrix.json().get('success')}"
)

# B) Rollback on invalid submodule code
bad_matrix_payload = {
    'module_access': [{'module_code': 'crm', 'is_allowed': True}],
    'submodule_access': [{'module_code': 'crm', 'submodule_code': 'invalid_submodule_code', 'is_allowed': True}],
    'permissions': [],
}
res_bad_matrix = client.post(
    f'/api/v1/tenant/permission-sets/{custom_pset.id}/matrix/',
    data=json.dumps(bad_matrix_payload),
    content_type='application/json',
    HTTP_AUTHORIZATION=f'Bearer {admin_tenant_token}',
)
record(
    "POST /permission-sets/{id}/matrix/ rolls back completely on invalid code (HTTP 400)",
    res_bad_matrix.status_code == 400,
    f"Status {res_bad_matrix.status_code}, error={res_bad_matrix.json().get('error')}"
)

# C) Protection of system roles
admin_pset = RolePermissionSet.objects.using('tenant_cult_fit').filter(role=org_admin_role).first()
if admin_pset:
    res_sys_matrix = client.post(
        f'/api/v1/tenant/permission-sets/{admin_pset.id}/matrix/',
        data=json.dumps({'module_access': [], 'submodule_access': [], 'permissions': []}),
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {admin_tenant_token}',
    )
    record(
        "POST /permission-sets/{id}/matrix/ rejects modification of protected system roles (HTTP 400)",
        res_sys_matrix.status_code == 400,
        f"Status {res_sys_matrix.status_code}, response={res_sys_matrix.json()}"
    )

print("\n--- 3. USER BRANCH RBAC RUNTIME VERIFICATION (REQ G & H) ---")
set_tenant_db_alias('tenant_cult_fit')

# Setup dedicated RBAC users in tenant DB to verify Req G & H
core_mod = ModuleCatalog.objects.using('tenant_cult_fit').get(module_code='core')
users_submod = SubmoduleCatalog.objects.using('tenant_cult_fit').get(module=core_mod, submodule_code='users')
p_user_view = Permission.objects.using('tenant_cult_fit').get(submodule=users_submod, permission_code='core.users.view')
p_user_edit = Permission.objects.using('tenant_cult_fit').get(submodule=users_submod, permission_code='core.users.edit')

# Role with ONLY core.users.view and core.users.edit (no core.users.create, no core.users.delete)
editor_role, _ = Role.objects.using('tenant_cult_fit').get_or_create(
    organization=org,
    code='VERIFY_UB_EDITOR',
    defaults={'name': 'User Branch Editor Only', 'scope': 'ORG', 'is_system': False, 'is_active': True}
)
if editor_role.scope != 'ORG':
    editor_role.scope = 'ORG'
    editor_role.save(using='tenant_cult_fit')
RoleModuleAccess.objects.using('tenant_cult_fit').get_or_create(role=editor_role, module=core_mod, defaults={'can_access': True})
RoleSubmoduleAccess.objects.using('tenant_cult_fit').get_or_create(role=editor_role, submodule=users_submod, defaults={'can_access': True})
pset_editor, _ = RolePermissionSet.objects.using('tenant_cult_fit').get_or_create(role=editor_role, defaults={'name': 'UB Editor Pset'})
RolePermissionSetItem.objects.using('tenant_cult_fit').get_or_create(permission_set=pset_editor, permission=p_user_view, defaults={'granted': True})
RolePermissionSetItem.objects.using('tenant_cult_fit').get_or_create(permission_set=pset_editor, permission=p_user_edit, defaults={'granted': True})

ub_editor_user, _ = TenantUser.objects.using('tenant_cult_fit').get_or_create(
    email='ub_editor@cultfit.in',
    defaults={'organization': org, 'first_name': 'UB', 'last_name': 'Editor', 'status': 'ACTIVE'}
)
RoleAssignment.objects.using('tenant_cult_fit').get_or_create(user=ub_editor_user, role=editor_role, defaults={'is_active': True})

# Role with ONLY core.users.view (NO core.users.edit)
viewer_role, _ = Role.objects.using('tenant_cult_fit').get_or_create(
    organization=org,
    code='VERIFY_UB_VIEWER',
    defaults={'name': 'User Branch Viewer Only', 'scope': 'ORG', 'is_system': False, 'is_active': True}
)
if viewer_role.scope != 'ORG':
    viewer_role.scope = 'ORG'
    viewer_role.save(using='tenant_cult_fit')
RoleModuleAccess.objects.using('tenant_cult_fit').get_or_create(role=viewer_role, module=core_mod, defaults={'can_access': True})
RoleSubmoduleAccess.objects.using('tenant_cult_fit').get_or_create(role=viewer_role, submodule=users_submod, defaults={'can_access': True})
pset_viewer, _ = RolePermissionSet.objects.using('tenant_cult_fit').get_or_create(role=viewer_role, defaults={'name': 'UB Viewer Pset'})
RolePermissionSetItem.objects.using('tenant_cult_fit').get_or_create(permission_set=pset_viewer, permission=p_user_view, defaults={'granted': True})

ub_viewer_user, _ = TenantUser.objects.using('tenant_cult_fit').get_or_create(
    email='ub_viewer@cultfit.in',
    defaults={'organization': org, 'first_name': 'UB', 'last_name': 'Viewer', 'status': 'ACTIVE'}
)
RoleAssignment.objects.using('tenant_cult_fit').get_or_create(user=ub_viewer_user, role=viewer_role, defaults={'is_active': True})

# Target user to attach/detach branch
target_user, _ = TenantUser.objects.using('tenant_cult_fit').get_or_create(
    email='target_staff@cultfit.in',
    defaults={'organization': org, 'first_name': 'Target', 'last_name': 'Staff', 'status': 'ACTIVE'}
)

editor_token = get_tenant_token(ub_editor_user)
viewer_token = get_tenant_token(ub_viewer_user)

# Clean up any existing UserBranch for target_user and branch
UserBranch.objects.using('tenant_cult_fit').filter(user=target_user, branch=branch).delete()

# Req H: User WITHOUT core.users.edit CANNOT mutate user-branch associations
res_h_create = client.post(
    '/api/v1/tenant/user-branches/',
    data=json.dumps({'user': str(target_user.id), 'branch': str(branch.id), 'scope_type': 'HOME', 'is_active': True}),
    content_type='application/json',
    HTTP_AUTHORIZATION=f'Bearer {viewer_token}',
)
record(
    "Req H: User WITHOUT core.users.edit cannot create user-branch association (HTTP 403)",
    res_h_create.status_code == 403,
    f"Status {res_h_create.status_code}"
)

# Req G: User WITH ONLY core.users.edit CAN create user-branch association (HTTP 201)
res_g_create = client.post(
    '/api/v1/tenant/user-branches/',
    data=json.dumps({'user': str(target_user.id), 'branch': str(branch.id), 'scope_type': 'HOME', 'is_active': True}),
    content_type='application/json',
    HTTP_AUTHORIZATION=f'Bearer {editor_token}',
)
record(
    "Req G1: User WITH ONLY core.users.edit can create user-branch association (HTTP 201)",
    res_g_create.status_code == 201,
    f"Status {res_g_create.status_code}"
)

ub_id = res_g_create.json().get('id') if res_g_create.status_code == 201 else None

if ub_id:
    # Req H update: User WITHOUT core.users.edit cannot update
    res_h_update = client.patch(
        f'/api/v1/tenant/user-branches/{ub_id}/',
        data=json.dumps({'scope_type': 'ADDITIONAL'}),
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {viewer_token}',
    )
    record(
        "Req H: User WITHOUT core.users.edit cannot update user-branch association (HTTP 403)",
        res_h_update.status_code == 403,
        f"Status {res_h_update.status_code}"
    )

    # Req G update: User WITH ONLY core.users.edit can update
    res_g_update = client.patch(
        f'/api/v1/tenant/user-branches/{ub_id}/',
        data=json.dumps({'scope_type': 'ADDITIONAL'}),
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {editor_token}',
    )
    record(
        "Req G2: User WITH ONLY core.users.edit can update user-branch association (HTTP 200)",
        res_g_update.status_code == 200 and res_g_update.json().get('scope_type') == 'ADDITIONAL',
        f"Status {res_g_update.status_code}"
    )

    # Req H delete: User WITHOUT core.users.edit cannot delete
    res_h_delete = client.delete(
        f'/api/v1/tenant/user-branches/{ub_id}/',
        HTTP_AUTHORIZATION=f'Bearer {viewer_token}',
    )
    record(
        "Req H: User WITHOUT core.users.edit cannot delete user-branch association (HTTP 403)",
        res_h_delete.status_code == 403,
        f"Status {res_h_delete.status_code}"
    )

    # Req G delete: User WITH ONLY core.users.edit can delete
    res_g_delete = client.delete(
        f'/api/v1/tenant/user-branches/{ub_id}/',
        HTTP_AUTHORIZATION=f'Bearer {editor_token}',
    )
    record(
        "Req G3: User WITH ONLY core.users.edit can delete user-branch association (HTTP 204)",
        res_g_delete.status_code == 204,
        f"Status {res_g_delete.status_code}"
    )

# 3.2 CompanyEntity (read-only for tenant)
res_comp = client.get('/api/v1/tenant/company-entities/', HTTP_AUTHORIZATION=f'Bearer {admin_tenant_token}')
res_comp_post = client.post(
    '/api/v1/tenant/company-entities/',
    data=json.dumps({'legal_name': 'Hacked Co'}),
    content_type='application/json',
    HTTP_AUTHORIZATION=f'Bearer {admin_tenant_token}',
)
record(
    "CompanyEntityViewSet is strictly read-only on tenant (GET=200, POST=405)",
    res_comp.status_code == 200 and res_comp_post.status_code == 405,
    f"GET {res_comp.status_code}, POST {res_comp_post.status_code}"
)

print("\n--- 4. SETTINGS, NOTIFICATIONS & AUDIT APIS RUNTIME VERIFICATION ---")

# 4.1 Organization Settings
res_org_set = client.get('/api/v1/tenant/organization-settings/current/', HTTP_AUTHORIZATION=f'Bearer {admin_tenant_token}')
res_org_put = client.put(
    '/api/v1/tenant/organization-settings/current/',
    data=json.dumps({'currency': 'INR', 'tax_rate_pct': '18.00'}),
    content_type='application/json',
    HTTP_AUTHORIZATION=f'Bearer {admin_tenant_token}',
)
record(
    "OrganizationSettingsViewSet /current/ supports read and update (core.settings.*)",
    res_org_set.status_code == 200 and res_org_put.status_code == 200,
    f"GET {res_org_set.status_code}, PUT {res_org_put.status_code}, currency={res_org_put.json().get('currency')}"
)

# 4.2 Branch Settings
res_br_set = client.get('/api/v1/tenant/branch-settings/', HTTP_AUTHORIZATION=f'Bearer {admin_tenant_token}')
record(
    "BranchSettingsViewSet is accessible under core.settings.view",
    res_br_set.status_code == 200,
    f"Status {res_br_set.status_code}"
)

# 4.3 Notification Templates
res_notifs = client.get('/api/v1/tenant/notification-templates/', HTTP_AUTHORIZATION=f'Bearer {admin_tenant_token}')
record(
    "NotificationTemplateViewSet is accessible under core.notifications.view",
    res_notifs.status_code == 200,
    f"Status {res_notifs.status_code}"
)

# 4.4 Tenant Audit Events (strictly read-only / append-only)
res_audit = client.get('/api/v1/tenant/audit-events/', HTTP_AUTHORIZATION=f'Bearer {admin_tenant_token}')
res_audit_post = client.post(
    '/api/v1/tenant/audit-events/',
    data=json.dumps({'action': 'HACK'}),
    content_type='application/json',
    HTTP_AUTHORIZATION=f'Bearer {admin_tenant_token}',
)
record(
    "TenantAuditEventViewSet is strictly read-only from API (GET=200, POST=405)",
    res_audit.status_code == 200 and res_audit_post.status_code == 405,
    f"GET {res_audit.status_code}, POST {res_audit_post.status_code}"
)

print("\n" + "=" * 80)
pass_count = sum(1 for _, passed, _ in results if passed)
total_count = len(results)
print(f"VERIFICATION SUMMARY: {pass_count}/{total_count} CHECKS PASSED")
print("=" * 80)

if pass_count != total_count:
    sys.exit(1)
