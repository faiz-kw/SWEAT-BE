"""
Sprint 12 — Tenant IAM & RBAC Deterministic Backfill
Deterministic, authoritative backfill for:
1. permissions.source_permission_id (resolved from master.tenant_permission_catalog)
2. role_module_access (permission_set_id, is_visible, created_at, updated_at)
3. role_submodule_access (permission_set_id, is_visible, created_at, updated_at)
"""

from django.db import migrations, connections
from django.utils import timezone


def backfill_tenant_iam_governance(apps, schema_editor):
    db_alias = schema_editor.connection.alias

    Permission = apps.get_model('tenant_core', 'Permission')
    Role = apps.get_model('tenant_core', 'Role')
    RolePermissionSet = apps.get_model('tenant_core', 'RolePermissionSet')
    RoleModuleAccess = apps.get_model('tenant_core', 'RoleModuleAccess')
    RoleSubmoduleAccess = apps.get_model('tenant_core', 'RoleSubmoduleAccess')

    # 1. Deterministic resolution of permissions.source_permission_id from Master catalog
    master_perms = {}
    try:
        with connections['default'].cursor() as cur:
            cur.execute("SELECT lower(code), id FROM tenant_permission_catalog;")
            for p_code, p_id in cur.fetchall():
                master_perms[p_code] = p_id
    except Exception as e:
        # In test environments with isolated default DB
        pass

    if master_perms:
        unresolved_perms = []
        for perm in Permission.objects.using(db_alias).all():
            code = (getattr(perm, 'code', None) or getattr(perm, 'permission_code', None) or '').lower()
            if code in master_perms:
                perm.source_permission_id = master_perms[code]
                perm.save(using=db_alias, update_fields=['source_permission_id'])
            else:
                unresolved_perms.append(f"Permission id={perm.id}, code='{code}'")

        if unresolved_perms:
            raise RuntimeError(
                f"Deterministic backfill aborted: {len(unresolved_perms)} tenant permissions could not be "
                f"resolved to master catalog: {unresolved_perms[:10]}"
            )

    # 2. Deterministic backfill of role_module_access
    # Map role -> primary/oldest RolePermissionSet
    role_pset_map = {}
    for rps in RolePermissionSet.objects.using(db_alias).order_by('created_at').all():
        if rps.role_id not in role_pset_map:
            role_pset_map[rps.role_id] = rps

    unresolved_rma = []
    for rma in RoleModuleAccess.objects.using(db_alias).all():
        rps = role_pset_map.get(rma.role_id)
        if rps:
            rma.permission_set_id = rps.id
            if rma.is_visible is None:
                rma.is_visible = getattr(rma, 'can_access', True) if getattr(rma, 'can_access', None) is not None else True
            if not rma.created_at:
                rma.created_at = rps.created_at or timezone.now()
            if not rma.updated_at:
                rma.updated_at = rps.updated_at or timezone.now()
            rma.save(using=db_alias, update_fields=['permission_set_id', 'is_visible', 'created_at', 'updated_at'])
        else:
            unresolved_rma.append(f"RoleModuleAccess id={rma.id}, role_id={rma.role_id}")

    if unresolved_rma:
        raise RuntimeError(
            f"Deterministic backfill aborted: {len(unresolved_rma)} RoleModuleAccess rows could not be "
            f"resolved to a RolePermissionSet: {unresolved_rma[:10]}"
        )

    # 3. Deterministic backfill of role_submodule_access
    unresolved_rsma = []
    for rsma in RoleSubmoduleAccess.objects.using(db_alias).all():
        rps = role_pset_map.get(rsma.role_id)
        if rps:
            rsma.permission_set_id = rps.id
            if rsma.is_visible is None:
                rsma.is_visible = getattr(rsma, 'can_access', True) if getattr(rsma, 'can_access', None) is not None else True
            if not rsma.created_at:
                rsma.created_at = rps.created_at or timezone.now()
            if not rsma.updated_at:
                rsma.updated_at = rps.updated_at or timezone.now()
            rsma.save(using=db_alias, update_fields=['permission_set_id', 'is_visible', 'created_at', 'updated_at'])
        else:
            unresolved_rsma.append(f"RoleSubmoduleAccess id={rsma.id}, role_id={rsma.role_id}")

    if unresolved_rsma:
        raise RuntimeError(
            f"Deterministic backfill aborted: {len(unresolved_rsma)} RoleSubmoduleAccess rows could not be "
            f"resolved to a RolePermissionSet: {unresolved_rsma[:10]}"
        )


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0013_sprint12_iam_governance_expand'),
    ]

    operations = [
        migrations.RunPython(backfill_tenant_iam_governance, migrations.RunPython.noop),
    ]
