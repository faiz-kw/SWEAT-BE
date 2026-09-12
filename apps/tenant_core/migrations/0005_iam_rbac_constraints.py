# Generated for Sprint 8D: Tenant Catalogs + Operational IAM/RBAC Constraint Hardening
from django.db import migrations


def harden_tenant_fks_and_constraints(apps, schema_editor):
    cursor = schema_editor.connection.cursor()

    tenant_fks_to_harden = [
        ('branch_modules', 'branch_id', 'branches', 'RESTRICT'),
        ('branch_modules', 'module_id', 'module_catalog', 'RESTRICT'),
        ('departments', 'organization_id', 'organizations', 'RESTRICT'),
        ('departments', 'created_by_id', 'users', 'SET NULL'),
        ('permissions', 'module_id', 'module_catalog', 'RESTRICT'),
        ('permissions', 'submodule_id', 'submodule_catalog', 'SET NULL'),
        ('role_assignments', 'branch_id', 'branches', 'RESTRICT'),
        ('role_assignments', 'company_entity_id', 'company_entities', 'RESTRICT'),
        ('role_assignments', 'department_id', 'departments', 'SET NULL'),
        ('role_assignments', 'location_id', 'locations', 'RESTRICT'),
        ('role_assignments', 'organization_id', 'organizations', 'RESTRICT'),
        ('role_assignments', 'role_id', 'roles', 'RESTRICT'),
        ('role_assignments', 'user_id', 'users', 'RESTRICT'),
        ('role_assignments', 'assigned_by_user_id', 'users', 'SET NULL'),
        ('role_permission_set_items', 'permission_id', 'permissions', 'RESTRICT'),
        ('role_permission_set_items', 'permission_set_id', 'role_permission_sets', 'RESTRICT'),
        ('role_permission_sets', 'branch_id', 'branches', 'SET NULL'),
        ('role_permission_sets', 'company_entity_id', 'company_entities', 'SET NULL'),
        ('role_permission_sets', 'created_by_id', 'users', 'SET NULL'),
        ('role_permission_sets', 'inherits_from_id', 'role_permission_sets', 'SET NULL'),
        ('role_permission_sets', 'location_id', 'locations', 'SET NULL'),
        ('role_permission_sets', 'organization_id', 'organizations', 'RESTRICT'),
        ('role_permission_sets', 'role_id', 'roles', 'RESTRICT'),
        ('roles', 'created_by_id', 'users', 'SET NULL'),
        ('roles', 'department_id', 'departments', 'SET NULL'),
        ('roles', 'organization_id', 'organizations', 'RESTRICT'),
        ('submodule_catalog', 'module_id', 'module_catalog', 'RESTRICT'),
        ('user_branches', 'branch_id', 'branches', 'RESTRICT'),
        ('user_branches', 'user_id', 'users', 'RESTRICT'),
        ('user_departments', 'department_id', 'departments', 'RESTRICT'),
        ('user_departments', 'user_id', 'users', 'RESTRICT'),
    ]

    for tbl, col, tgt_tbl, on_delete in tenant_fks_to_harden:
        cursor.execute(f"""
            SELECT tc.constraint_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name
            WHERE tc.table_name = '{tbl}' AND kcu.column_name = '{col}' AND tc.constraint_type = 'FOREIGN KEY';
        """)
        rows = cursor.fetchall()
        for (cname,) in rows:
            cursor.execute(f"ALTER TABLE {tbl} DROP CONSTRAINT {cname};")
            cursor.execute(f"ALTER TABLE {tbl} ADD CONSTRAINT {cname} FOREIGN KEY ({col}) REFERENCES {tgt_tbl}(id) ON DELETE {on_delete};")

    not_null_sqls = [
        'ALTER TABLE module_catalog ALTER COLUMN code SET NOT NULL;',
        'ALTER TABLE module_catalog ALTER COLUMN display_order SET NOT NULL;',
        'ALTER TABLE submodule_catalog ALTER COLUMN code SET NOT NULL;',
        'ALTER TABLE submodule_catalog ALTER COLUMN display_order SET NOT NULL;',
        'ALTER TABLE permissions ALTER COLUMN code SET NOT NULL;',
        'ALTER TABLE branch_modules ALTER COLUMN module_id SET NOT NULL;',
        'ALTER TABLE users ALTER COLUMN display_name SET NOT NULL;',
        'ALTER TABLE user_branches ALTER COLUMN relationship_type SET NOT NULL;',
        'ALTER TABLE user_branches ALTER COLUMN is_primary SET NOT NULL;',
        'ALTER TABLE user_departments ALTER COLUMN is_primary SET NOT NULL;',
        'ALTER TABLE role_permission_set_items ALTER COLUMN is_allowed SET NOT NULL;',
    ]
    for sql in not_null_sqls:
        cursor.execute(sql)

    for tbl, conname, cols in [
        ('branch_modules', 'branch_modules_branch_id_module_id_uniq', '(branch_id, module_id)'),
        ('submodule_catalog', 'submodule_catalog_module_id_code_uniq', '(module_id, code)'),
    ]:
        cursor.execute(f"SELECT 1 FROM pg_constraint WHERE conname = '{conname}';")
        if not cursor.fetchone():
            cursor.execute(f"ALTER TABLE {tbl} ADD CONSTRAINT {conname} UNIQUE {cols};")


def rollback_tenant_fks_and_constraints(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0004_iam_rbac_backfill'),
    ]

    operations = [
        migrations.RunPython(harden_tenant_fks_and_constraints, rollback_tenant_fks_and_constraints),
    ]
