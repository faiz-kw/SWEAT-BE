"""
Sprint 12 — Tenant IAM & RBAC Governance Expansion
1. Expands RoleModuleAccess with permission_set_id, is_visible, created_at, updated_at.
2. Expands RoleSubmoduleAccess with permission_set_id, is_visible, created_at, updated_at.
3. Aligns UserDepartment timestamps (joined_at -> assigned_at, left_at -> unassigned_at).
"""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0012_sprint11_tenant_hierarchy_constraints'),
    ]

    operations = [
        # 1. UserDepartment field alignments
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RenameField(
                    model_name='userdepartment',
                    old_name='joined_at',
                    new_name='assigned_at',
                ),
                migrations.RenameField(
                    model_name='userdepartment',
                    old_name='left_at',
                    new_name='unassigned_at',
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql="""
                    DO $$
                    BEGIN
                        IF EXISTS (
                            SELECT 1 FROM information_schema.columns 
                            WHERE table_schema = 'public' AND table_name = 'user_departments' AND column_name = 'joined_at'
                        ) THEN
                            ALTER TABLE user_departments RENAME COLUMN joined_at TO assigned_at;
                        END IF;
                        IF EXISTS (
                            SELECT 1 FROM information_schema.columns 
                            WHERE table_schema = 'public' AND table_name = 'user_departments' AND column_name = 'left_at'
                        ) THEN
                            ALTER TABLE user_departments RENAME COLUMN left_at TO unassigned_at;
                        END IF;
                    END $$;
                    """,
                    reverse_sql=migrations.RunSQL.noop,
                ),
            ],
        ),

        # 2. RoleModuleAccess schema expansion
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name='rolemoduleaccess',
                    name='permission_set',
                    field=models.ForeignKey(
                        blank=True,
                        db_column='permission_set_id',
                        null=True,
                        on_delete=django.db.models.deletion.RESTRICT,
                        related_name='module_access',
                        to='tenant_core.rolepermissionset',
                    ),
                ),
                migrations.AddField(
                    model_name='rolemoduleaccess',
                    name='is_visible',
                    field=models.BooleanField(db_column='is_visible', default=True, null=True),
                ),
                migrations.AddField(
                    model_name='rolemoduleaccess',
                    name='created_at',
                    field=models.DateTimeField(blank=True, db_column='created_at', null=True),
                ),
                migrations.AddField(
                    model_name='rolemoduleaccess',
                    name='updated_at',
                    field=models.DateTimeField(blank=True, db_column='updated_at', null=True),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql="""
                    ALTER TABLE role_module_access ADD COLUMN IF NOT EXISTS permission_set_id uuid;
                    ALTER TABLE role_module_access ADD COLUMN IF NOT EXISTS is_visible boolean;
                    ALTER TABLE role_module_access ADD COLUMN IF NOT EXISTS created_at timestamptz;
                    ALTER TABLE role_module_access ADD COLUMN IF NOT EXISTS updated_at timestamptz;
                    """,
                    reverse_sql="""
                    ALTER TABLE role_module_access DROP COLUMN IF EXISTS permission_set_id;
                    ALTER TABLE role_module_access DROP COLUMN IF EXISTS is_visible;
                    ALTER TABLE role_module_access DROP COLUMN IF EXISTS created_at;
                    ALTER TABLE role_module_access DROP COLUMN IF EXISTS updated_at;
                    """
                ),
            ],
        ),

        # 3. RoleSubmoduleAccess schema expansion
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name='rolesubmoduleaccess',
                    name='permission_set',
                    field=models.ForeignKey(
                        blank=True,
                        db_column='permission_set_id',
                        null=True,
                        on_delete=django.db.models.deletion.RESTRICT,
                        related_name='submodule_access',
                        to='tenant_core.rolepermissionset',
                    ),
                ),
                migrations.AddField(
                    model_name='rolesubmoduleaccess',
                    name='is_visible',
                    field=models.BooleanField(db_column='is_visible', default=True, null=True),
                ),
                migrations.AddField(
                    model_name='rolesubmoduleaccess',
                    name='created_at',
                    field=models.DateTimeField(blank=True, db_column='created_at', null=True),
                ),
                migrations.AddField(
                    model_name='rolesubmoduleaccess',
                    name='updated_at',
                    field=models.DateTimeField(blank=True, db_column='updated_at', null=True),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql="""
                    ALTER TABLE role_submodule_access ADD COLUMN IF NOT EXISTS permission_set_id uuid;
                    ALTER TABLE role_submodule_access ADD COLUMN IF NOT EXISTS is_visible boolean;
                    ALTER TABLE role_submodule_access ADD COLUMN IF NOT EXISTS created_at timestamptz;
                    ALTER TABLE role_submodule_access ADD COLUMN IF NOT EXISTS updated_at timestamptz;
                    """,
                    reverse_sql="""
                    ALTER TABLE role_submodule_access DROP COLUMN IF EXISTS permission_set_id;
                    ALTER TABLE role_submodule_access DROP COLUMN IF EXISTS is_visible;
                    ALTER TABLE role_submodule_access DROP COLUMN IF EXISTS created_at;
                    ALTER TABLE role_submodule_access DROP COLUMN IF EXISTS updated_at;
                    """
                ),
            ],
        ),
    ]
