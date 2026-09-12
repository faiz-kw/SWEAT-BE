"""
Sprint 12 — Tenant IAM & RBAC Constraints & Hardening
Applies physical constraints, column defaults, non-deferrable FKs, and nullability:
1. permissions.source_permission_id NOT NULL and UNIQUE
2. role_module_access: permission_set_id, is_visible, created_at, updated_at NOT NULL
3. role_module_access: fk_role_module_access_permission_set ON DELETE RESTRICT non-deferrable
4. role_submodule_access: permission_set_id, is_visible, created_at, updated_at NOT NULL
5. role_submodule_access: fk_role_submodule_access_permission_set ON DELETE RESTRICT non-deferrable
6. Physical PostgreSQL column defaults:
   - users.is_mfa_enabled SET DEFAULT FALSE
   - user_branches.is_primary SET DEFAULT FALSE
   - user_departments.is_primary SET DEFAULT FALSE
   - permissions.version SET DEFAULT 1
   - roles.is_system_role SET DEFAULT FALSE
   - role_permission_sets.is_override SET DEFAULT FALSE
"""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0014_sprint12_iam_governance_backfill'),
    ]

    operations = [
        # 1. State Operations for Model Definitions
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name='permission',
                    name='source_permission_id',
                    field=models.UUIDField(help_text='Logical cross-DB ref to master.TenantPermissionCatalog.id', unique=True),
                ),
                migrations.AlterField(
                    model_name='rolemoduleaccess',
                    name='permission_set',
                    field=models.ForeignKey(
                        db_column='permission_set_id',
                        on_delete=django.db.models.deletion.RESTRICT,
                        related_name='module_access',
                        to='tenant_core.rolepermissionset',
                    ),
                ),
                migrations.AlterField(
                    model_name='rolemoduleaccess',
                    name='is_visible',
                    field=models.BooleanField(db_column='is_visible', default=True),
                ),
                migrations.AlterField(
                    model_name='rolemoduleaccess',
                    name='created_at',
                    field=models.DateTimeField(auto_now_add=True),
                ),
                migrations.AlterField(
                    model_name='rolemoduleaccess',
                    name='updated_at',
                    field=models.DateTimeField(auto_now=True),
                ),
                migrations.AlterField(
                    model_name='rolesubmoduleaccess',
                    name='permission_set',
                    field=models.ForeignKey(
                        db_column='permission_set_id',
                        on_delete=django.db.models.deletion.RESTRICT,
                        related_name='submodule_access',
                        to='tenant_core.rolepermissionset',
                    ),
                ),
                migrations.AlterField(
                    model_name='rolesubmoduleaccess',
                    name='is_visible',
                    field=models.BooleanField(db_column='is_visible', default=True),
                ),
                migrations.AlterField(
                    model_name='rolesubmoduleaccess',
                    name='created_at',
                    field=models.DateTimeField(auto_now_add=True),
                ),
                migrations.AlterField(
                    model_name='rolesubmoduleaccess',
                    name='updated_at',
                    field=models.DateTimeField(auto_now=True),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql="""
                    -- 1. permissions source_permission_id NOT NULL & UNIQUE
                    ALTER TABLE permissions ALTER COLUMN source_permission_id SET NOT NULL;
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint WHERE conname = 'permissions_source_permission_id_key'
                        ) THEN
                            ALTER TABLE permissions ADD CONSTRAINT permissions_source_permission_id_key UNIQUE (source_permission_id);
                        END IF;
                    END $$;

                    -- 2. role_module_access NOT NULL constraints
                    ALTER TABLE role_module_access ALTER COLUMN permission_set_id SET NOT NULL;
                    ALTER TABLE role_module_access ALTER COLUMN is_visible SET NOT NULL;
                    ALTER TABLE role_module_access ALTER COLUMN created_at SET NOT NULL;
                    ALTER TABLE role_module_access ALTER COLUMN updated_at SET NOT NULL;

                    -- 3. role_module_access FK constraint
                    ALTER TABLE role_module_access DROP CONSTRAINT IF EXISTS fk_role_module_access_permission_set;
                    ALTER TABLE role_module_access ADD CONSTRAINT fk_role_module_access_permission_set
                        FOREIGN KEY (permission_set_id) REFERENCES role_permission_sets(id)
                        ON DELETE RESTRICT;

                    -- 4. role_submodule_access NOT NULL constraints
                    ALTER TABLE role_submodule_access ALTER COLUMN permission_set_id SET NOT NULL;
                    ALTER TABLE role_submodule_access ALTER COLUMN is_visible SET NOT NULL;
                    ALTER TABLE role_submodule_access ALTER COLUMN created_at SET NOT NULL;
                    ALTER TABLE role_submodule_access ALTER COLUMN updated_at SET NOT NULL;

                    -- 5. role_submodule_access FK constraint
                    ALTER TABLE role_submodule_access DROP CONSTRAINT IF EXISTS fk_role_submodule_access_permission_set;
                    ALTER TABLE role_submodule_access ADD CONSTRAINT fk_role_submodule_access_permission_set
                        FOREIGN KEY (permission_set_id) REFERENCES role_permission_sets(id)
                        ON DELETE RESTRICT;

                    -- 6. Canonical physical defaults
                    ALTER TABLE users ALTER COLUMN is_mfa_enabled SET DEFAULT FALSE;
                    ALTER TABLE user_branches ALTER COLUMN is_primary SET DEFAULT FALSE;
                    ALTER TABLE user_departments ALTER COLUMN is_primary SET DEFAULT FALSE;
                    ALTER TABLE permissions ALTER COLUMN version SET DEFAULT 1;
                    ALTER TABLE roles ALTER COLUMN is_system_role SET DEFAULT FALSE;
                    ALTER TABLE role_permission_sets ALTER COLUMN is_override SET DEFAULT FALSE;
                    """,
                    reverse_sql="""
                    ALTER TABLE users ALTER COLUMN is_mfa_enabled DROP DEFAULT;
                    ALTER TABLE user_branches ALTER COLUMN is_primary DROP DEFAULT;
                    ALTER TABLE user_departments ALTER COLUMN is_primary DROP DEFAULT;
                    ALTER TABLE permissions ALTER COLUMN version DROP DEFAULT;
                    ALTER TABLE roles ALTER COLUMN is_system_role DROP DEFAULT;
                    ALTER TABLE role_permission_sets ALTER COLUMN is_override DROP DEFAULT;
                    ALTER TABLE role_submodule_access DROP CONSTRAINT IF EXISTS fk_role_submodule_access_permission_set;
                    ALTER TABLE role_module_access DROP CONSTRAINT IF EXISTS fk_role_module_access_permission_set;
                    ALTER TABLE permissions DROP CONSTRAINT IF EXISTS permissions_source_permission_id_key;
                    """
                ),
            ],
        ),
    ]
