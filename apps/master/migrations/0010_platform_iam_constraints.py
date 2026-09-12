# Generated for Sprint 8D: Master Platform IAM Constraint Hardening
from django.db import migrations


def harden_master_fks(apps, schema_editor):
    cursor = schema_editor.connection.cursor()
    fks_to_harden = [
        ('platform_submodules', 'module_id', 'platform_modules', 'RESTRICT'),
        ('platform_permissions', 'module_id', 'platform_modules', 'RESTRICT'),
        ('platform_permissions', 'submodule_id', 'platform_submodules', 'SET NULL'),
        ('platform_role_module_access', 'role_id', 'platform_roles', 'RESTRICT'),
        ('platform_role_module_access', 'module_id', 'platform_modules', 'RESTRICT'),
        ('platform_role_submodule_access', 'role_id', 'platform_roles', 'RESTRICT'),
        ('platform_role_submodule_access', 'submodule_id', 'platform_submodules', 'RESTRICT'),
        ('platform_role_permissions', 'role_id', 'platform_roles', 'RESTRICT'),
        ('platform_role_permissions', 'permission_id', 'platform_permissions', 'RESTRICT'),
        ('platform_user_roles', 'platform_user_id', 'platform_users', 'RESTRICT'),
        ('platform_user_roles', 'role_id', 'platform_roles', 'RESTRICT'),
        ('platform_user_roles', 'assigned_by_id', 'platform_users', 'SET NULL'),
        ('platform_user_departments', 'platform_user_id', 'platform_users', 'RESTRICT'),
        ('platform_user_departments', 'department_id', 'platform_departments', 'RESTRICT'),
        ('platform_roles', 'department_id', 'platform_departments', 'SET NULL'),
    ]

    for tbl, col, tgt_tbl, on_delete in fks_to_harden:
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


def rollback_master_fks(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('master', '0009_platform_iam_expand'),
    ]

    operations = [
        migrations.RunPython(harden_master_fks, rollback_master_fks),
    ]
