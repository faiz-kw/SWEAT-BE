"""
Sprint 12 — Master IAM & RBAC Governance
Enforces canonical Master IAM schema:
1. platform_users.is_mfa_enabled: physical default FALSE
2. platform_user_departments.is_primary: physical default FALSE
3. platform_user_departments.platform_user_id: ForeignKey to platform_users
4. platform_user_departments.assigned_at: canonical history timestamp
5. platform_user_departments.unassigned_at: canonical history timestamp
6. platform_roles.is_system_role: physical default FALSE
7. platform_modules.display_order: navigation ordering
8. platform_submodules.display_order: navigation ordering
9. platform_role_module_access.is_visible: physical default FALSE
10. platform_role_submodule_access.is_visible: physical default FALSE
11. platform_role_permissions.is_allowed: physical default TRUE
12. platform_user_roles.platform_user_id: ForeignKey to platform_users
"""

from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('master', '0021_sprint11_catalog_entitlements_constraints'),
    ]

    operations = [
        # 1. PlatformUser alterations
        migrations.AlterField(
            model_name='platformuser',
            name='is_mfa_enabled',
            field=models.BooleanField(default=False),
        ),

        # 2. PlatformUserDepartment field alignments
        migrations.RenameField(
            model_name='platformuserdepartment',
            old_name='user',
            new_name='platform_user',
        ),
        migrations.RenameField(
            model_name='platformuserdepartment',
            old_name='joined_at',
            new_name='assigned_at',
        ),
        migrations.RenameField(
            model_name='platformuserdepartment',
            old_name='left_at',
            new_name='unassigned_at',
        ),
        migrations.AlterUniqueTogether(
            name='platformuserdepartment',
            unique_together={('platform_user', 'department')},
        ),
        migrations.AlterField(
            model_name='platformuserdepartment',
            name='is_primary',
            field=models.BooleanField(default=False),
        ),

        # 3. PlatformRole field alignments
        migrations.RenameField(
            model_name='platformrole',
            old_name='is_system',
            new_name='is_system_role',
        ),
        migrations.AlterField(
            model_name='platformrole',
            name='is_system_role',
            field=models.BooleanField(db_column='is_system_role', default=False, help_text='System roles cannot be deleted'),
        ),

        # 4. PlatformModule & Submodule field alignments
        migrations.RenameField(
            model_name='platformmodule',
            old_name='sort_order',
            new_name='display_order',
        ),
        migrations.AlterModelOptions(
            name='platformmodule',
            options={'ordering': ['display_order', 'name']},
        ),
        migrations.RenameField(
            model_name='platformsubmodule',
            old_name='sort_order',
            new_name='display_order',
        ),
        migrations.AlterModelOptions(
            name='platformsubmodule',
            options={'ordering': ['module', 'display_order', 'name']},
        ),

        # 5. PlatformRoleModuleAccess & SubmoduleAccess field alignments
        migrations.RenameField(
            model_name='platformrolemoduleaccess',
            old_name='can_access',
            new_name='is_visible',
        ),
        migrations.AlterField(
            model_name='platformrolemoduleaccess',
            name='is_visible',
            field=models.BooleanField(db_column='is_visible', default=False),
        ),
        migrations.RenameField(
            model_name='platformrolesubmoduleaccess',
            old_name='can_access',
            new_name='is_visible',
        ),
        migrations.AlterField(
            model_name='platformrolesubmoduleaccess',
            name='is_visible',
            field=models.BooleanField(db_column='is_visible', default=False),
        ),

        # 6. PlatformRolePermission field alignments
        migrations.RenameField(
            model_name='platformrolepermission',
            old_name='granted',
            new_name='is_allowed',
        ),
        migrations.AlterField(
            model_name='platformrolepermission',
            name='is_allowed',
            field=models.BooleanField(db_column='is_allowed', default=True),
        ),

        # 7. PlatformUserRole field alignments
        migrations.RenameField(
            model_name='platformuserrole',
            old_name='user',
            new_name='platform_user',
        ),
        migrations.AlterUniqueTogether(
            name='platformuserrole',
            unique_together={('platform_user', 'role')},
        ),

        # 8. PostgreSQL physical defaults
        migrations.RunSQL(
            sql="""
            ALTER TABLE platform_users ALTER COLUMN is_mfa_enabled SET DEFAULT FALSE;
            ALTER TABLE platform_user_departments ALTER COLUMN is_primary SET DEFAULT FALSE;
            ALTER TABLE platform_roles ALTER COLUMN is_system_role SET DEFAULT FALSE;
            ALTER TABLE platform_role_module_access ALTER COLUMN is_visible SET DEFAULT FALSE;
            ALTER TABLE platform_role_submodule_access ALTER COLUMN is_visible SET DEFAULT FALSE;
            ALTER TABLE platform_role_permissions ALTER COLUMN is_allowed SET DEFAULT TRUE;
            """,
            reverse_sql="""
            ALTER TABLE platform_users ALTER COLUMN is_mfa_enabled DROP DEFAULT;
            ALTER TABLE platform_user_departments ALTER COLUMN is_primary DROP DEFAULT;
            ALTER TABLE platform_roles ALTER COLUMN is_system_role DROP DEFAULT;
            ALTER TABLE platform_role_module_access ALTER COLUMN is_visible DROP DEFAULT;
            ALTER TABLE platform_role_submodule_access ALTER COLUMN is_visible DROP DEFAULT;
            ALTER TABLE platform_role_permissions ALTER COLUMN is_allowed DROP DEFAULT;
            """
        ),
    ]
