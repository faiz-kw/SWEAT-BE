# Generated for Sprint 8B: Tenant Catalogs + Operational IAM/RBAC Expand
import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0002_schema_field_alignment'),
    ]

    operations = [
        # 1. Department
        migrations.AlterField(
            model_name='department',
            name='organization',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='departments', to='tenant_core.organization'),
        ),
        migrations.AlterField(
            model_name='department',
            name='name',
            field=models.CharField(max_length=150),
        ),
        migrations.AlterField(
            model_name='department',
            name='description',
            field=models.TextField(blank=True, default='', null=True),
        ),
        migrations.AddField(
            model_name='department',
            name='status',
            field=models.CharField(default='ACTIVE', max_length=30),
        ),
        migrations.AddField(
            model_name='department',
            name='created_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_departments', to='tenant_core.tenantuser'),
        ),

        # 2. TenantUser
        migrations.AlterField(
            model_name='tenantuser',
            name='organization',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='users', to='tenant_core.organization'),
        ),
        migrations.AlterField(
            model_name='tenantuser',
            name='phone',
            field=models.CharField(blank=True, default='', max_length=30, null=True),
        ),
        migrations.AddField(
            model_name='tenantuser',
            name='display_name',
            field=models.CharField(blank=True, default='', max_length=150),
        ),
        migrations.AddField(
            model_name='tenantuser',
            name='gender',
            field=models.CharField(blank=True, max_length=20, null=True),
        ),
        migrations.AddField(
            model_name='tenantuser',
            name='date_of_birth',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='tenantuser',
            name='profile_file_id',
            field=models.ForeignKey(blank=True, db_column='profile_file_id', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='profile_users', to='tenant_core.file'),
        ),
        migrations.AddField(
            model_name='tenantuser',
            name='user_type',
            field=models.CharField(default='STAFF', max_length=30),
        ),
        migrations.AddField(
            model_name='tenantuser',
            name='email_verified_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='tenantuser',
            name='phone_verified_at',
            field=models.DateTimeField(blank=True, null=True),
        ),

        # 3. UserBranch
        migrations.AlterField(
            model_name='userbranch',
            name='branch',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='user_assignments', to='tenant_core.branch'),
        ),
        migrations.AlterField(
            model_name='userbranch',
            name='user',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='branch_assignments', to='tenant_core.tenantuser'),
        ),
        migrations.AddField(
            model_name='userbranch',
            name='relationship_type',
            field=models.CharField(default='PRIMARY', max_length=30),
        ),
        migrations.AddField(
            model_name='userbranch',
            name='is_primary',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='userbranch',
            name='status',
            field=models.CharField(default='ACTIVE', max_length=30),
        ),
        migrations.AddField(
            model_name='userbranch',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='userbranch',
            name='updated_at',
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),

        # 4. UserDepartment
        migrations.AlterField(
            model_name='userdepartment',
            name='joined_at',
            field=models.DateTimeField(db_column='assigned_at', default=django.utils.timezone.now),
        ),
        migrations.AlterField(
            model_name='userdepartment',
            name='left_at',
            field=models.DateTimeField(blank=True, db_column='unassigned_at', null=True),
        ),
        migrations.AlterField(
            model_name='userdepartment',
            name='department',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='members', to='tenant_core.department'),
        ),
        migrations.AlterField(
            model_name='userdepartment',
            name='user',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='department_memberships', to='tenant_core.tenantuser'),
        ),
        migrations.AddField(
            model_name='userdepartment',
            name='is_primary',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='userdepartment',
            name='status',
            field=models.CharField(default='ACTIVE', max_length=30),
        ),
        migrations.AddField(
            model_name='userdepartment',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='userdepartment',
            name='updated_at',
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),

        # 5. ModuleCatalog
        migrations.AlterModelOptions(
            name='modulecatalog',
            options={'ordering': ['display_order', 'sort_order', 'name']},
        ),
        migrations.AddField(
            model_name='modulecatalog',
            name='code',
            field=models.CharField(blank=True, max_length=100, null=True, unique=True),
        ),
        migrations.AlterField(
            model_name='modulecatalog',
            name='module_code',
            field=models.CharField(blank=True, help_text='Matches master.ProductModule.code', max_length=50, null=True, unique=True),
        ),
        migrations.AlterField(
            model_name='modulecatalog',
            name='name',
            field=models.CharField(max_length=150),
        ),
        migrations.AlterField(
            model_name='modulecatalog',
            name='description',
            field=models.TextField(blank=True, default='', null=True),
        ),
        migrations.AddField(
            model_name='modulecatalog',
            name='supports_branch_scope',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='modulecatalog',
            name='display_order',
            field=models.IntegerField(default=0),
        ),
        migrations.RunSQL(
            sql="UPDATE module_catalog SET catalog_version = split_part(catalog_version, '.', 1) WHERE catalog_version LIKE '%.%';",
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.AlterField(
            model_name='modulecatalog',
            name='catalog_version',
            field=models.IntegerField(default=1),
        ),
        migrations.AddField(
            model_name='modulecatalog',
            name='status',
            field=models.CharField(default='ACTIVE', max_length=30),
        ),
        migrations.AddField(
            model_name='modulecatalog',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='modulecatalog',
            name='updated_at',
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),

        # 6. SubmoduleCatalog
        migrations.AlterModelOptions(
            name='submodulecatalog',
            options={'ordering': ['module', 'display_order', 'sort_order']},
        ),
        migrations.AlterUniqueTogether(
            name='submodulecatalog',
            unique_together=set(),
        ),
        migrations.AddField(
            model_name='submodulecatalog',
            name='source_submodule_id',
            field=models.UUIDField(blank=True, help_text='Logical cross-DB ref to master.ProductSubmodule.id', null=True, unique=True),
        ),
        migrations.AddField(
            model_name='submodulecatalog',
            name='code',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AlterField(
            model_name='submodulecatalog',
            name='submodule_code',
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
        migrations.AlterField(
            model_name='submodulecatalog',
            name='name',
            field=models.CharField(max_length=150),
        ),
        migrations.AlterField(
            model_name='submodulecatalog',
            name='description',
            field=models.TextField(blank=True, default='', null=True),
        ),
        migrations.AlterField(
            model_name='submodulecatalog',
            name='module',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='submodules', to='tenant_core.modulecatalog'),
        ),
        migrations.AddField(
            model_name='submodulecatalog',
            name='display_order',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='submodulecatalog',
            name='catalog_version',
            field=models.IntegerField(default=1),
        ),
        migrations.AddField(
            model_name='submodulecatalog',
            name='status',
            field=models.CharField(default='ACTIVE', max_length=30),
        ),
        migrations.AddField(
            model_name='submodulecatalog',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='submodulecatalog',
            name='updated_at',
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),

        # 7. Permission
        migrations.AddField(
            model_name='permission',
            name='source_permission_id',
            field=models.UUIDField(blank=True, help_text='Logical cross-DB ref to master.TenantPermissionCatalog.id', null=True, unique=True),
        ),
        migrations.AddField(
            model_name='permission',
            name='code',
            field=models.CharField(blank=True, max_length=150, null=True, unique=True),
        ),
        migrations.AlterField(
            model_name='permission',
            name='permission_code',
            field=models.CharField(blank=True, help_text='e.g. crm.leads.view', max_length=150, null=True, unique=True),
        ),
        migrations.AlterField(
            model_name='permission',
            name='label',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
        migrations.AlterField(
            model_name='permission',
            name='description',
            field=models.TextField(blank=True, default='', null=True),
        ),
        migrations.AlterField(
            model_name='permission',
            name='module',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='permissions', to='tenant_core.modulecatalog'),
        ),
        migrations.AlterField(
            model_name='permission',
            name='submodule',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='tenant_core.submodulecatalog'),
        ),
        migrations.AddField(
            model_name='permission',
            name='version',
            field=models.IntegerField(default=1),
        ),
        migrations.AddField(
            model_name='permission',
            name='status',
            field=models.CharField(default='ACTIVE', max_length=30),
        ),
        migrations.AddField(
            model_name='permission',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='permission',
            name='updated_at',
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),

        # 8. Role
        migrations.AlterField(
            model_name='role',
            name='organization',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='roles', to='tenant_core.organization'),
        ),
        migrations.AlterField(
            model_name='role',
            name='name',
            field=models.CharField(max_length=150),
        ),
        migrations.AlterField(
            model_name='role',
            name='code',
            field=models.CharField(help_text='Stable code unique per org', max_length=100),
        ),
        migrations.AlterField(
            model_name='role',
            name='description',
            field=models.TextField(blank=True, default='', null=True),
        ),
        migrations.AddField(
            model_name='role',
            name='is_system_role',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='role',
            name='status',
            field=models.CharField(default='ACTIVE', max_length=30),
        ),
        migrations.AddField(
            model_name='role',
            name='created_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_roles', to='tenant_core.tenantuser'),
        ),

        # 9. RolePermissionSet
        migrations.AlterField(
            model_name='rolepermissionset',
            name='scope_type',
            field=models.CharField(choices=[('ORGANIZATION', 'Organization'), ('COMPANY_ENTITY', 'Company Entity'), ('LOCATION', 'Location'), ('BRANCH', 'Branch')], default='ORGANIZATION', max_length=30),
        ),
        migrations.AlterField(
            model_name='rolepermissionset',
            name='branch',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='role_permission_sets', to='tenant_core.branch'),
        ),
        migrations.AlterField(
            model_name='rolepermissionset',
            name='company_entity',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='role_permission_sets', to='tenant_core.companyentity'),
        ),
        migrations.AlterField(
            model_name='rolepermissionset',
            name='location',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='role_permission_sets', to='tenant_core.location'),
        ),
        migrations.AlterField(
            model_name='rolepermissionset',
            name='role',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='permission_sets', to='tenant_core.role'),
        ),
        migrations.AlterField(
            model_name='rolepermissionset',
            name='name',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
        migrations.AlterField(
            model_name='rolepermissionset',
            name='description',
            field=models.TextField(blank=True, default='', null=True),
        ),
        migrations.AddField(
            model_name='rolepermissionset',
            name='status',
            field=models.CharField(default='ACTIVE', max_length=30),
        ),
        migrations.AddField(
            model_name='rolepermissionset',
            name='created_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_permission_sets', to='tenant_core.tenantuser'),
        ),
        migrations.AddField(
            model_name='rolepermissionset',
            name='updated_at',
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),

        # 10. RolePermissionSetItem
        migrations.AlterField(
            model_name='rolepermissionsetitem',
            name='permission',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, to='tenant_core.permission'),
        ),
        migrations.AlterField(
            model_name='rolepermissionsetitem',
            name='permission_set',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='items', to='tenant_core.rolepermissionset'),
        ),
        migrations.AddField(
            model_name='rolepermissionsetitem',
            name='is_allowed',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='rolepermissionsetitem',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='rolepermissionsetitem',
            name='updated_at',
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),

        # 11. RoleAssignment
        migrations.AlterField(
            model_name='roleassignment',
            name='branch',
            field=models.ForeignKey(blank=True, help_text='NULL = org-wide scope', null=True, on_delete=django.db.models.deletion.RESTRICT, related_name='role_assignments', to='tenant_core.branch'),
        ),
        migrations.AlterField(
            model_name='roleassignment',
            name='company_entity',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.RESTRICT, related_name='role_assignments', to='tenant_core.companyentity'),
        ),
        migrations.AlterField(
            model_name='roleassignment',
            name='department',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='tenant_core.department'),
        ),
        migrations.AlterField(
            model_name='roleassignment',
            name='location',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.RESTRICT, related_name='role_assignments', to='tenant_core.location'),
        ),
        migrations.AlterField(
            model_name='roleassignment',
            name='role',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='user_assignments', to='tenant_core.role'),
        ),
        migrations.AlterField(
            model_name='roleassignment',
            name='user',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='role_assignments', to='tenant_core.tenantuser'),
        ),
        migrations.AlterField(
            model_name='roleassignment',
            name='scope_type',
            field=models.CharField(choices=[('ORGANIZATION', 'Organization'), ('COMPANY_ENTITY', 'Company Entity'), ('LOCATION', 'Location'), ('BRANCH', 'Branch')], default='ORGANIZATION', max_length=30),
        ),
        migrations.AlterField(
            model_name='roleassignment',
            name='status',
            field=models.CharField(choices=[('ACTIVE', 'Active'), ('INACTIVE', 'Inactive')], default='ACTIVE', max_length=30),
        ),
        migrations.AlterField(
            model_name='roleassignment',
            name='assigned_by',
            field=models.ForeignKey(blank=True, db_column='assigned_by_user_id', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='assigned_roles', to='tenant_core.tenantuser'),
        ),
        migrations.AddField(
            model_name='roleassignment',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='roleassignment',
            name='updated_at',
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),

        # 12. BranchModule
        migrations.AlterField(
            model_name='branchmodule',
            name='branch',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='enabled_modules', to='tenant_core.branch'),
        ),
        migrations.AlterField(
            model_name='branchmodule',
            name='enabled_at',
            field=models.DateTimeField(blank=True, default=django.utils.timezone.now, null=True),
        ),
        migrations.AddField(
            model_name='branchmodule',
            name='module',
            field=models.ForeignKey(blank=True, db_column='module_id', null=True, on_delete=django.db.models.deletion.RESTRICT, related_name='branch_modules', to='tenant_core.modulecatalog'),
        ),
        migrations.AddField(
            model_name='branchmodule',
            name='status',
            field=models.CharField(default='ENABLED', max_length=30),
        ),
        migrations.AddField(
            model_name='branchmodule',
            name='assigned_by_type',
            field=models.CharField(default='PLATFORM_USER', max_length=30),
        ),
        migrations.AddField(
            model_name='branchmodule',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='branchmodule',
            name='updated_at',
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
    ]
