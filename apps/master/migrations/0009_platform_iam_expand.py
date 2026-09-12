# Generated for Sprint 8A: Master Platform IAM Expand
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('master', '0008_extend_core_files_catalog'),
    ]

    operations = [
        # 1. PlatformUser
        migrations.AlterField(
            model_name='platformuser',
            name='phone',
            field=models.CharField(blank=True, default='', max_length=30, null=True),
        ),

        # 2. PlatformDepartment
        migrations.AlterField(
            model_name='platformdepartment',
            name='name',
            field=models.CharField(max_length=150),
        ),
        migrations.AlterField(
            model_name='platformdepartment',
            name='description',
            field=models.TextField(blank=True, default='', null=True),
        ),
        migrations.AddField(
            model_name='platformdepartment',
            name='status',
            field=models.CharField(default='ACTIVE', max_length=30),
        ),

        # 3. PlatformRole
        migrations.AlterField(
            model_name='platformrole',
            name='is_system',
            field=models.BooleanField(db_column='is_system_role', default=False, help_text='System roles cannot be deleted'),
        ),
        migrations.AlterField(
            model_name='platformrole',
            name='code',
            field=models.CharField(max_length=100, unique=True),
        ),
        migrations.AlterField(
            model_name='platformrole',
            name='name',
            field=models.CharField(max_length=150),
        ),
        migrations.AlterField(
            model_name='platformrole',
            name='description',
            field=models.TextField(blank=True, default='', null=True),
        ),
        migrations.AddField(
            model_name='platformrole',
            name='department',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='roles', to='master.platformdepartment'),
        ),
        migrations.AddField(
            model_name='platformrole',
            name='status',
            field=models.CharField(default='ACTIVE', max_length=30),
        ),

        # 4. PlatformModule
        migrations.AlterModelOptions(
            name='platformmodule',
            options={'ordering': ['sort_order', 'name']},
        ),
        migrations.AlterField(
            model_name='platformmodule',
            name='sort_order',
            field=models.IntegerField(db_column='display_order', default=0),
        ),
        migrations.AlterField(
            model_name='platformmodule',
            name='code',
            field=models.CharField(max_length=100, unique=True),
        ),
        migrations.AlterField(
            model_name='platformmodule',
            name='name',
            field=models.CharField(max_length=150),
        ),
        migrations.AlterField(
            model_name='platformmodule',
            name='description',
            field=models.TextField(blank=True, default='', null=True),
        ),
        migrations.AddField(
            model_name='platformmodule',
            name='status',
            field=models.CharField(default='ACTIVE', max_length=30),
        ),
        migrations.AddField(
            model_name='platformmodule',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='platformmodule',
            name='updated_at',
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),

        # 5. PlatformSubmodule
        migrations.AlterModelOptions(
            name='platformsubmodule',
            options={'ordering': ['module', 'sort_order', 'name']},
        ),
        migrations.AlterField(
            model_name='platformsubmodule',
            name='sort_order',
            field=models.IntegerField(db_column='display_order', default=0),
        ),
        migrations.AlterField(
            model_name='platformsubmodule',
            name='code',
            field=models.CharField(max_length=100),
        ),
        migrations.AlterField(
            model_name='platformsubmodule',
            name='name',
            field=models.CharField(max_length=150),
        ),
        migrations.AlterField(
            model_name='platformsubmodule',
            name='description',
            field=models.TextField(blank=True, default='', null=True),
        ),
        migrations.AlterField(
            model_name='platformsubmodule',
            name='module',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='submodules', to='master.platformmodule'),
        ),
        migrations.AddField(
            model_name='platformsubmodule',
            name='status',
            field=models.CharField(default='ACTIVE', max_length=30),
        ),
        migrations.AddField(
            model_name='platformsubmodule',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='platformsubmodule',
            name='updated_at',
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),

        # 6. PlatformPermission
        migrations.AlterField(
            model_name='platformpermission',
            name='code',
            field=models.CharField(help_text='e.g. tenants.create', max_length=150, unique=True),
        ),
        migrations.AlterField(
            model_name='platformpermission',
            name='description',
            field=models.TextField(blank=True, default='', null=True),
        ),
        migrations.AlterField(
            model_name='platformpermission',
            name='label',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
        migrations.AlterField(
            model_name='platformpermission',
            name='module',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='permissions', to='master.platformmodule'),
        ),
        migrations.AlterField(
            model_name='platformpermission',
            name='submodule',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='permissions', to='master.platformsubmodule'),
        ),
        migrations.AddField(
            model_name='platformpermission',
            name='status',
            field=models.CharField(default='ACTIVE', max_length=30),
        ),
        migrations.AddField(
            model_name='platformpermission',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='platformpermission',
            name='updated_at',
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),

        # 7. PlatformRoleModuleAccess
        migrations.AlterField(
            model_name='platformrolemoduleaccess',
            name='can_access',
            field=models.BooleanField(db_column='is_visible', default=False),
        ),
        migrations.AlterField(
            model_name='platformrolemoduleaccess',
            name='module',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='role_access', to='master.platformmodule'),
        ),
        migrations.AlterField(
            model_name='platformrolemoduleaccess',
            name='role',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='module_access', to='master.platformrole'),
        ),
        migrations.AddField(
            model_name='platformrolemoduleaccess',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='platformrolemoduleaccess',
            name='updated_at',
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),

        # 8. PlatformRoleSubmoduleAccess
        migrations.AlterField(
            model_name='platformrolesubmoduleaccess',
            name='can_access',
            field=models.BooleanField(db_column='is_visible', default=False),
        ),
        migrations.AlterField(
            model_name='platformrolesubmoduleaccess',
            name='role',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='submodule_access', to='master.platformrole'),
        ),
        migrations.AlterField(
            model_name='platformrolesubmoduleaccess',
            name='submodule',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='role_access', to='master.platformsubmodule'),
        ),
        migrations.AddField(
            model_name='platformrolesubmoduleaccess',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='platformrolesubmoduleaccess',
            name='updated_at',
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),

        # 9. PlatformRolePermission
        migrations.AlterField(
            model_name='platformrolepermission',
            name='granted',
            field=models.BooleanField(db_column='is_allowed', default=True),
        ),
        migrations.AlterField(
            model_name='platformrolepermission',
            name='permission',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='role_grants', to='master.platformpermission'),
        ),
        migrations.AlterField(
            model_name='platformrolepermission',
            name='role',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='permissions', to='master.platformrole'),
        ),
        migrations.AddField(
            model_name='platformrolepermission',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='platformrolepermission',
            name='updated_at',
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),

        # 10. PlatformUserRole
        migrations.AlterField(
            model_name='platformuserrole',
            name='user',
            field=models.ForeignKey(db_column='platform_user_id', on_delete=django.db.models.deletion.RESTRICT, related_name='role_assignments', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AlterField(
            model_name='platformuserrole',
            name='role',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='user_assignments', to='master.platformrole'),
        ),
        migrations.AddField(
            model_name='platformuserrole',
            name='status',
            field=models.CharField(default='ACTIVE', max_length=30),
        ),
        migrations.AddField(
            model_name='platformuserrole',
            name='expires_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='platformuserrole',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='platformuserrole',
            name='updated_at',
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),

        # 11. PlatformUserDepartment
        migrations.AlterField(
            model_name='platformuserdepartment',
            name='user',
            field=models.ForeignKey(db_column='platform_user_id', on_delete=django.db.models.deletion.RESTRICT, related_name='department_memberships', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AlterField(
            model_name='platformuserdepartment',
            name='joined_at',
            field=models.DateTimeField(db_column='assigned_at', default=django.utils.timezone.now),
        ),
        migrations.AlterField(
            model_name='platformuserdepartment',
            name='left_at',
            field=models.DateTimeField(blank=True, db_column='unassigned_at', null=True),
        ),
        migrations.AlterField(
            model_name='platformuserdepartment',
            name='department',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='members', to='master.platformdepartment'),
        ),
        migrations.AddField(
            model_name='platformuserdepartment',
            name='is_primary',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='platformuserdepartment',
            name='status',
            field=models.CharField(default='ACTIVE', max_length=30),
        ),
        migrations.AddField(
            model_name='platformuserdepartment',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='platformuserdepartment',
            name='updated_at',
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
    ]
