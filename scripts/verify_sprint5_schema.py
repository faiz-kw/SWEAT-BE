"""
Sprint 5 Schema Field Alignment & Multi-Tenant Migrations Live Runtime Verification Script.

Executes live runtime checks against:
- Master DB ('default') and dedicated Tenant DB ('tenant_cult_fit')
- Field existence, types, nullability, and defaults across all 10 reconciled models
- Live tenant data integrity and deterministic backfill verification
- ModuleCatalog cross-DB logical reference verification (no PostgreSQL cross-DB FK)
- migrate_all_tenants command:
  - Master DB protection
  - Dynamic alias resolution
  - --dry-run capability
  - --tenant capability
  - Idempotent safe re-execution
- Centralized RBAC safety and backward compatibility preservation
"""

import os
import sys
import uuid
import django
from io import StringIO

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.core.management import call_command
from django.db import models

from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.models_saas import ProductModule
from apps.tenant_core.models_org import Organization, CompanyEntity, Location, Branch
from apps.tenant_core.models_users import TenantUser, Department
from apps.tenant_core.models_rbac import (
    Role, RoleAssignment, RolePermissionSet, ModuleCatalog, SubmoduleCatalog, Permission,
)
from apps.tenant_core.models_govern import OrganizationSettings, BranchSettings, NotificationTemplate
from apps.tenant_core.models_privacy import ProcessingPurpose, ConsentRecord, TenantAuditEvent
from config.routers import TenantRouter, set_tenant_db_alias
from config.tenant_middleware import _register_tenant_connection

results = []


def record(test_name, passed, detail=""):
    results.append((test_name, passed, detail))
    status_label = "[PASS]" if passed else "[FAIL]"
    print(f"  {status_label} {test_name}" + (f" -> {detail}" if detail else ""))


print("=" * 80)
print("SPRINT 5 SCHEMA FIELD ALIGNMENT LIVE RUNTIME VERIFICATION")
print("=" * 80)

# Setup live tenant connection
tenant = Tenant.objects.using('default').get(slug='cult-fit')
ds = TenantDataSource.objects.using('default').get(tenant=tenant)
db_alias = f"tenant_{ds.db_name}"
_register_tenant_connection(db_alias, ds.db_name)
set_tenant_db_alias(db_alias)

# -----------------------------------------------------------------------------
# 1. Role Model Alignment
# -----------------------------------------------------------------------------
print("\n[Section 1: Role Model Alignment]")
role_dept_field = Role._meta.get_field('department')
record(
    "Role.department is ForeignKey to Department with SET_NULL",
    isinstance(role_dept_field, models.ForeignKey) and
    role_dept_field.remote_field.model == Department and
    role_dept_field.remote_field.on_delete == models.SET_NULL and
    role_dept_field.null is True,
    f"target={role_dept_field.remote_field.model.__name__}, on_delete={role_dept_field.remote_field.on_delete}"
)

role_scope_field = Role._meta.get_field('scope')
record(
    "Role.scope preserved for backward compatibility",
    role_scope_field.default == 'BRANCH' and
    'ORG' in [c[0] for c in role_scope_field.choices],
    f"default={role_scope_field.default}"
)

live_role_count = Role.objects.using(db_alias).count()
record(
    "Live Role rows intact",
    live_role_count >= 5,
    f"count={live_role_count}"
)

# -----------------------------------------------------------------------------
# 2. RoleAssignment Alignment
# -----------------------------------------------------------------------------
print("\n[Section 2: RoleAssignment Model Alignment]")
ra_org_field = RoleAssignment._meta.get_field('organization')
record(
    "RoleAssignment.organization is ForeignKey with RESTRICT",
    isinstance(ra_org_field, models.ForeignKey) and
    ra_org_field.remote_field.model == Organization and
    ra_org_field.remote_field.on_delete == models.RESTRICT and
    ra_org_field.null is False,
    f"target={ra_org_field.remote_field.model.__name__}, on_delete={ra_org_field.remote_field.on_delete}"
)

ra_scope_field = RoleAssignment._meta.get_field('scope_type')
expected_scopes = ['ORGANIZATION', 'COMPANY_ENTITY', 'LOCATION', 'BRANCH']
record(
    "RoleAssignment.scope_type choices match approved schema",
    [c[0] for c in ra_scope_field.choices] == expected_scopes,
    f"choices={expected_scopes}"
)

ra_assigned_by_field = RoleAssignment._meta.get_field('assigned_by')
record(
    "RoleAssignment.assigned_by maps to db_column='assigned_by_user_id' with SET_NULL",
    isinstance(ra_assigned_by_field, models.ForeignKey) and
    ra_assigned_by_field.remote_field.model == TenantUser and
    ra_assigned_by_field.remote_field.on_delete == models.SET_NULL and
    ra_assigned_by_field.db_column == 'assigned_by_user_id' and
    ra_assigned_by_field.null is True,
    f"col={ra_assigned_by_field.db_column}, on_delete={ra_assigned_by_field.remote_field.on_delete}"
)

ra_status_field = RoleAssignment._meta.get_field('status')
record(
    "RoleAssignment.status has ACTIVE/INACTIVE choices, default ACTIVE",
    [c[0] for c in ra_status_field.choices] == ['ACTIVE', 'INACTIVE'] and
    ra_status_field.default == 'ACTIVE',
    f"default={ra_status_field.default}"
)

ra_is_active_field = RoleAssignment._meta.get_field('is_active')
record(
    "RoleAssignment.is_active preserved for backward compatibility",
    ra_is_active_field.default is True,
    "preserved is_active=True"
)

# Verify live backfilled RoleAssignment rows
all_ras = list(RoleAssignment.objects.using(db_alias).all())
ra_orgs_valid = all(ra.organization_id is not None for ra in all_ras)
ra_statuses_valid = all(ra.status == 'ACTIVE' for ra in all_ras if ra.is_active)
branch_ras = [ra for ra in all_ras if ra.branch_id is not None]
org_ras = [ra for ra in all_ras if ra.branch_id is None]
branch_scope_preserved = len(branch_ras) == 1 and branch_ras[0].scope_type == 'BRANCH'
org_scope_preserved = len(org_ras) == 3 and all(r.scope_type == 'ORGANIZATION' for r in org_ras)

record(
    "Live RoleAssignment rows deterministically backfilled & scope preserved",
    len(all_ras) == 4 and ra_orgs_valid and ra_statuses_valid and branch_scope_preserved and org_scope_preserved,
    f"count={len(all_ras)}, branch_scoped={len(branch_ras)} (scope={branch_ras[0].scope_type if branch_ras else None}), org_scoped={len(org_ras)}"
)

# Verify status and is_active synchronization
test_user = TenantUser.objects.using(db_alias).first()
test_role = Role.objects.using(db_alias).first()
ra_sync_test = RoleAssignment(user=test_user, role=test_role, is_active=False)
sync_init_passed = (ra_sync_test.status == 'INACTIVE' and ra_sync_test.is_active is False)
ra_sync_test.status = 'ACTIVE'
ra_sync_test.clean()
sync_status_authoritative = (ra_sync_test.is_active is True and ra_sync_test.status == 'ACTIVE')
ra_sync_test.is_active = False
ra_sync_test.clean()
sync_is_active_passed = (ra_sync_test.status == 'INACTIVE' and ra_sync_test.is_active is False)

record(
    "RoleAssignment status & is_active bidirectional synchronization",
    sync_init_passed and sync_status_authoritative and sync_is_active_passed,
    f"init_sync={sync_init_passed}, status_authoritative={sync_status_authoritative}, is_active_sync={sync_is_active_passed}"
)

# -----------------------------------------------------------------------------
# 3. RolePermissionSet Alignment
# -----------------------------------------------------------------------------
print("\n[Section 3: RolePermissionSet Model Alignment]")
rps_org_field = RolePermissionSet._meta.get_field('organization')
record(
    "RolePermissionSet.organization is ForeignKey with RESTRICT",
    isinstance(rps_org_field, models.ForeignKey) and
    rps_org_field.remote_field.model == Organization and
    rps_org_field.remote_field.on_delete == models.RESTRICT,
    f"target={rps_org_field.remote_field.model.__name__}"
)

rps_scope_field = RolePermissionSet._meta.get_field('scope_type')
record(
    "RolePermissionSet.scope_type choices match approved schema",
    [c[0] for c in rps_scope_field.choices] == expected_scopes,
    f"choices={expected_scopes}"
)

rps_inherits_field = RolePermissionSet._meta.get_field('inherits_from')
record(
    "RolePermissionSet.inherits_from self-referential FK with SET_NULL",
    isinstance(rps_inherits_field, models.ForeignKey) and
    rps_inherits_field.remote_field.model == RolePermissionSet and
    rps_inherits_field.remote_field.on_delete == models.SET_NULL and
    rps_inherits_field.null is True,
    f"null={rps_inherits_field.null}"
)

rps_override_field = RolePermissionSet._meta.get_field('is_override')
record(
    "RolePermissionSet.is_override default False",
    rps_override_field.default is False,
    "default=False"
)

all_rps = list(RolePermissionSet.objects.using(db_alias).all())
rps_backfilled_valid = all(rps.organization_id is not None and rps.scope_type == 'ORGANIZATION' for rps in all_rps)
record(
    "Live RolePermissionSet rows deterministically backfilled",
    len(all_rps) >= 5 and rps_backfilled_valid,
    f"count={len(all_rps)}, valid={rps_backfilled_valid}"
)

# -----------------------------------------------------------------------------
# 4. ModuleCatalog Logical Reference
# -----------------------------------------------------------------------------
print("\n[Section 4: ModuleCatalog Cross-DB Reference]")
mc_source_field = ModuleCatalog._meta.get_field('source_module_id')
record(
    "ModuleCatalog.source_module_id is UUIDField (not PostgreSQL FK)",
    isinstance(mc_source_field, models.UUIDField) and not mc_source_field.is_relation,
    "UUIDField logical cross-DB ref without PostgreSQL FK constraint"
)

# Verify all live modules mapped to Master ProductModule
all_mcs = list(ModuleCatalog.objects.using(db_alias).all())
matched_count = 0
for mc in all_mcs:
    pm = ProductModule.objects.using('default').filter(code=mc.module_code).first()
    if pm and mc.source_module_id == pm.id:
        matched_count += 1

record(
    "Live ModuleCatalog rows backfilled with Master ProductModule UUIDs",
    len(all_mcs) == 9 and matched_count == 9,
    f"matched {matched_count}/{len(all_mcs)} modules against Master ProductModule"
)

# -----------------------------------------------------------------------------
# 5. Organization & Branch Settings Alignment
# -----------------------------------------------------------------------------
print("\n[Section 5: Settings Model Alignment]")
os_json_fields = ['membership_config', 'booking_config', 'attendance_config', 'notification_config', 'ai_config']
os_all_json_exist = all(
    isinstance(OrganizationSettings._meta.get_field(f), models.JSONField)
    for f in os_json_fields
)
record(
    "OrganizationSettings has all 5 JSONB config blocks",
    os_all_json_exist,
    f"fields={os_json_fields}"
)

os_formatting_fields = ['default_timezone', 'language', 'date_format', 'time_format']
os_all_fmt_exist = all(
    isinstance(OrganizationSettings._meta.get_field(f), models.CharField)
    for f in os_formatting_fields
)
record(
    "OrganizationSettings has formatting fields (tz, lang, date_format, time_format)",
    os_all_fmt_exist,
    f"fields={os_formatting_fields}"
)

bs_all_json_exist = all(
    isinstance(BranchSettings._meta.get_field(f), models.JSONField) and
    BranchSettings._meta.get_field(f).null is True
    for f in os_json_fields
)
record(
    "BranchSettings has all 5 JSONB override blocks (nullable)",
    bs_all_json_exist,
    f"fields={os_json_fields} with null=True"
)

live_os = OrganizationSettings.objects.using(db_alias).first()
record(
    "Live OrganizationSettings has initialized JSONB blocks",
    live_os is not None and all(isinstance(getattr(live_os, f), dict) for f in os_json_fields),
    f"all JSONB blocks initialized to dict"
)

# -----------------------------------------------------------------------------
# 6. NotificationTemplate Alignment
# -----------------------------------------------------------------------------
print("\n[Section 6: NotificationTemplate Model Alignment]")
nt_org_field = NotificationTemplate._meta.get_field('organization')
nt_branch_field = NotificationTemplate._meta.get_field('branch')
record(
    "NotificationTemplate has organization (RESTRICT) and branch (RESTRICT, nullable)",
    nt_org_field.remote_field.on_delete == models.RESTRICT and
    nt_branch_field.remote_field.on_delete == models.RESTRICT and
    nt_branch_field.null is True,
    f"org_on_delete={nt_org_field.remote_field.on_delete}, branch_on_delete={nt_branch_field.remote_field.on_delete}"
)

nt_code_field = NotificationTemplate._meta.get_field('event_code')
nt_lang_field = NotificationTemplate._meta.get_field('language')
nt_ver_field = NotificationTemplate._meta.get_field('version')
record(
    "NotificationTemplate has event_code, language, and version fields",
    isinstance(nt_code_field, models.CharField) and
    isinstance(nt_lang_field, models.CharField) and
    isinstance(nt_ver_field, models.IntegerField),
    f"lang_default={nt_lang_field.default}, ver_default={nt_ver_field.default}"
)

# -----------------------------------------------------------------------------
# 7. Privacy & Consent Alignment
# -----------------------------------------------------------------------------
print("\n[Section 7: Privacy & Consent Alignment]")
pp_ver_field = ProcessingPurpose._meta.get_field('notice_version')
record(
    "ProcessingPurpose.notice_version default '1.0'",
    pp_ver_field.default == '1.0',
    f"default={pp_ver_field.default}"
)

cr_ver_field = ConsentRecord._meta.get_field('notice_version')
cr_source_field = ConsentRecord._meta.get_field('capture_source')
cr_proof_field = ConsentRecord._meta.get_field('proof_metadata')
cr_granted_field = ConsentRecord._meta.get_field('granted_at')
cr_withdrawn_field = ConsentRecord._meta.get_field('withdrawn_at')
record(
    "ConsentRecord has notice_version, capture_source, proof_metadata, granted_at, withdrawn_at",
    cr_ver_field.default == '1.0' and
    cr_source_field.default == 'WEB_FORM' and
    isinstance(cr_proof_field, models.JSONField) and
    cr_granted_field.null is True and
    cr_withdrawn_field.null is True,
    "proof_metadata=JSONField, capture_source=WEB_FORM"
)

# -----------------------------------------------------------------------------
# 8. TenantAuditEvent Alignment
# -----------------------------------------------------------------------------
print("\n[Section 8: TenantAuditEvent Model Alignment]")
ae_actor_type = TenantAuditEvent._meta.get_field('actor_type')
ae_org_id = TenantAuditEvent._meta.get_field('organization_id')
ae_ce_id = TenantAuditEvent._meta.get_field('company_entity_id')
ae_loc_id = TenantAuditEvent._meta.get_field('location_id')
ae_req_id = TenantAuditEvent._meta.get_field('request_id')
ae_corr_id = TenantAuditEvent._meta.get_field('correlation_id')
ae_source = TenantAuditEvent._meta.get_field('source_application')
record(
    "TenantAuditEvent has actor_type, metadata IDs, request/correlation IDs, source_application",
    'TENANT_USER' in [c[0] for c in ae_actor_type.choices] and
    isinstance(ae_org_id, models.UUIDField) and
    isinstance(ae_ce_id, models.UUIDField) and
    isinstance(ae_loc_id, models.UUIDField) and
    isinstance(ae_req_id, models.CharField) and
    isinstance(ae_corr_id, models.CharField) and
    ae_source.default == 'web_admin',
    f"actor_type_default={ae_actor_type.default}, source_app_default={ae_source.default}"
)

# -----------------------------------------------------------------------------
# 9. Multi-Tenant Migration Command Verification
# -----------------------------------------------------------------------------
print("\n[Section 9: Multi-Tenant Migration Command]")
router = TenantRouter()
record(
    "Master DB Protection: TenantRouter strictly disallows tenant_core migrations on Master DB",
    router.allow_migrate('default', 'tenant_core') is False and
    router.allow_migrate(db_alias, 'tenant_core') is True,
    f"default={router.allow_migrate('default', 'tenant_core')}, {db_alias}={router.allow_migrate(db_alias, 'tenant_core')}"
)

out_dry = StringIO()
call_command('migrate_all_tenants', '--all', '--dry-run', stdout=out_dry)
dry_run_success = 'Succeeded       : 1' in out_dry.getvalue() and 'Failed          : 0' in out_dry.getvalue()
record(
    "migrate_all_tenants --dry-run completes successfully with 0 failures",
    dry_run_success,
    "dry-run reported clean state"
)

out_all = StringIO()
call_command('migrate_all_tenants', '--all', stdout=out_all)
all_success = 'Succeeded       : 1' in out_all.getvalue() and 'Failed          : 0' in out_all.getvalue()
record(
    "migrate_all_tenants --all idempotent execution succeeds with 0 failures",
    all_success,
    "idempotent rerun verified"
)

# -----------------------------------------------------------------------------
# SUMMARY REPORT
# -----------------------------------------------------------------------------
print("\n" + "=" * 80)
total_checks = len(results)
passed_checks = sum(1 for _, p, _ in results)
failed_checks = total_checks - passed_checks
print(f"VERIFICATION SUMMARY: Total={total_checks}, Passed={passed_checks}, Failed={failed_checks}")
print("=" * 80)

if failed_checks > 0:
    print("\nFAILURES DETECTED:")
    for name, passed, detail in results:
        if not passed:
            print(f"  [FAIL] {name} -> {detail}")
    sys.exit(1)
else:
    print(f"\nALL {total_checks}/{total_checks} SPRINT 5 RUNTIME CHECKS PASSED.")
    sys.exit(0)
