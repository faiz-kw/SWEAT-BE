# Generated manually for Sprint 2 Foundation RBAC Hardening
from django.db import migrations


def seed_core_catalog(apps, schema_editor):
    ProductModule = apps.get_model('master', 'ProductModule')
    ProductSubmodule = apps.get_model('master', 'ProductSubmodule')
    TenantPermissionCatalog = apps.get_model('master', 'TenantPermissionCatalog')
    Tenant = apps.get_model('master', 'Tenant')
    TenantModule = apps.get_model('master', 'TenantModule')
    SaasPlan = apps.get_model('master', 'SaasPlan')
    SaasPlanModule = apps.get_model('master', 'SaasPlanModule')

    db_alias = schema_editor.connection.alias

    # 1. Create or update canonical core product module
    core_mod, _ = ProductModule.objects.using(db_alias).update_or_create(
        code='core',
        defaults={
            'name': 'Core System & Administration',
            'description': 'Core tenant administration, identity, and access governance',
            'icon': 'Shield',
            'is_core': True,
            'is_active': True,
            'sort_order': 0,
        },
    )

    # 2. Approved core submodules
    submodules_data = [
        ('users', 'User Management', 'Tenant user accounts and profile management', 1),
        ('departments', 'Department Management', 'Internal organizational divisions and teams', 2),
        ('roles', 'Role Management', 'Tenant roles, scope definitions, and assignments', 3),
        ('permissions', 'Permission Matrix Management', 'Granular role permission matrices and overrides', 4),
    ]

    submod_objs = {}
    for code, name, desc, order in submodules_data:
        submod, _ = ProductSubmodule.objects.using(db_alias).update_or_create(
            module=core_mod,
            code=code,
            defaults={
                'name': name,
                'description': desc,
                'sort_order': order,
                'is_active': True,
            },
        )
        submod_objs[code] = submod

    # 3. Approved canonical administrative permissions
    permissions_data = [
        # users
        ('core.users.view', 'users', 'view', 'Can view tenant users', 'View tenant staff and member accounts'),
        ('core.users.create', 'users', 'create', 'Can create tenant users', 'Create and invite new tenant users'),
        ('core.users.edit', 'users', 'edit', 'Can edit tenant users', 'Update tenant user profiles and status'),
        ('core.users.delete', 'users', 'delete', 'Can delete tenant users', 'Deactivate or remove tenant users'),
        # departments
        ('core.departments.view', 'departments', 'view', 'Can view departments', 'View organizational departments'),
        ('core.departments.create', 'departments', 'create', 'Can create departments', 'Create new tenant departments'),
        ('core.departments.edit', 'departments', 'edit', 'Can edit departments', 'Update department details and hierarchy'),
        ('core.departments.delete', 'departments', 'delete', 'Can delete departments', 'Remove tenant departments'),
        # roles
        ('core.roles.view', 'roles', 'view', 'Can view tenant roles', 'View role catalog and scope assignments'),
        ('core.roles.create', 'roles', 'create', 'Can create tenant roles', 'Create new custom tenant roles'),
        ('core.roles.edit', 'roles', 'edit', 'Can edit tenant roles', 'Update role configuration and scopes'),
        ('core.roles.delete', 'roles', 'delete', 'Can delete tenant roles', 'Delete non-system custom roles'),
        ('core.roles.assign', 'roles', 'assign', 'Can assign and revoke roles', 'Assign roles to users or revoke assignments'),
        # permissions
        ('core.permissions.view', 'permissions', 'view', 'Can view role permission matrices', 'Inspect role permission sets'),
        ('core.permissions.manage', 'permissions', 'manage', 'Can manage role permission matrices', 'Modify role permission grants'),
    ]

    for perm_code, sub_code, action, label, desc in permissions_data:
        TenantPermissionCatalog.objects.using(db_alias).update_or_create(
            code=perm_code,
            defaults={
                'module': core_mod,
                'submodule': submod_objs[sub_code],
                'action': action,
                'label': label,
                'description': desc,
                'is_active': True,
                'catalog_version': '1.0',
            },
        )

    # 4. Automatically entitle core module for all existing tenants
    for tenant in Tenant.objects.using(db_alias).all():
        TenantModule.objects.using(db_alias).get_or_create(
            tenant=tenant,
            module=core_mod,
            defaults={
                'availability_mode': 'ALL_BRANCHES',
                'is_enabled': True,
            },
        )

    # 5. Automatically include core module in all SaaS plans
    for plan in SaasPlan.objects.using(db_alias).all():
        SaasPlanModule.objects.using(db_alias).get_or_create(
            plan=plan,
            module=core_mod,
            defaults={
                'is_included': True,
                'usage_limit': None,
            },
        )


def revert_core_catalog(apps, schema_editor):
    ProductModule = apps.get_model('master', 'ProductModule')
    TenantPermissionCatalog = apps.get_model('master', 'TenantPermissionCatalog')
    TenantModule = apps.get_model('master', 'TenantModule')
    SaasPlanModule = apps.get_model('master', 'SaasPlanModule')

    db_alias = schema_editor.connection.alias
    core_mod = ProductModule.objects.using(db_alias).filter(code='core').first()
    if core_mod:
        TenantPermissionCatalog.objects.using(db_alias).filter(module=core_mod).delete()
        TenantModule.objects.using(db_alias).filter(module=core_mod).delete()
        SaasPlanModule.objects.using(db_alias).filter(module=core_mod).delete()
        core_mod.submodules.all().delete()
        core_mod.delete()


class Migration(migrations.Migration):

    dependencies = [
        ('master', '0002_alter_tenantprovisioning_tenant'),
    ]

    operations = [
        migrations.RunPython(seed_core_catalog, revert_core_catalog),
    ]
