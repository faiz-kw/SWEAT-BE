# Generated for Sprint 8C: Data Backfill / Reconciliation
from django.db import migrations


def backfill_tenant_iam_data(apps, schema_editor):
    db_alias = schema_editor.connection.alias

    with schema_editor.connection.cursor() as cursor:
        # 1. Backfill module_catalog code and display_order
        cursor.execute("""
            UPDATE module_catalog 
            SET code = module_code 
            WHERE (code IS NULL OR code = '') AND module_code IS NOT NULL;
        """)
        cursor.execute("""
            UPDATE module_catalog 
            SET display_order = sort_order 
            WHERE display_order = 0 AND sort_order != 0;
        """)

        # 2. Backfill submodule_catalog code and display_order
        cursor.execute("""
            UPDATE submodule_catalog 
            SET code = submodule_code 
            WHERE (code IS NULL OR code = '') AND submodule_code IS NOT NULL;
        """)
        cursor.execute("""
            UPDATE submodule_catalog 
            SET display_order = sort_order 
            WHERE display_order = 0 AND sort_order != 0;
        """)

        # 3. Backfill permissions code
        cursor.execute("""
            UPDATE permissions 
            SET code = permission_code 
            WHERE (code IS NULL OR code = '') AND permission_code IS NOT NULL;
        """)

        # 4. Backfill branch_modules.module_id by matching module_code
        cursor.execute("""
            UPDATE branch_modules bm 
            SET module_id = mc.id 
            FROM module_catalog mc 
            WHERE (bm.module_code = mc.code OR bm.module_code = mc.module_code) 
              AND bm.module_id IS NULL;
        """)

        # 5. Backfill users display_name
        cursor.execute("""
            UPDATE users 
            SET display_name = TRIM(first_name || ' ' || last_name) 
            WHERE display_name = '' OR display_name IS NULL;
        """)

        # 6. Backfill user_branches relationship_type and is_primary
        cursor.execute("""
            UPDATE user_branches 
            SET relationship_type = 'PRIMARY', is_primary = TRUE 
            WHERE scope_type = 'HOME';
        """)

        # 7. Backfill roles is_system_role
        cursor.execute("""
            UPDATE roles 
            SET is_system_role = is_system 
            WHERE is_system_role IS FALSE AND is_system IS TRUE;
        """)

        # 8. Backfill role_permission_set_items is_allowed
        cursor.execute("""
            UPDATE role_permission_set_items 
            SET is_allowed = granted 
            WHERE is_allowed IS TRUE AND granted IS FALSE;
        """)


def rollback_tenant_iam_data(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0003_iam_rbac_expand'),
    ]

    operations = [
        migrations.RunPython(backfill_tenant_iam_data, rollback_tenant_iam_data),
    ]
