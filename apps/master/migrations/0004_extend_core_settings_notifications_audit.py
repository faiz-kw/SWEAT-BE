# Generated manually for Sprint 3 Settings, Notifications, and Audit Core catalog extension
from django.db import migrations


def extend_core_catalog(apps, schema_editor):
    ProductModule = apps.get_model('master', 'ProductModule')
    ProductSubmodule = apps.get_model('master', 'ProductSubmodule')
    TenantPermissionCatalog = apps.get_model('master', 'TenantPermissionCatalog')

    db_alias = schema_editor.connection.alias

    core_mod = ProductModule.objects.using(db_alias).filter(code='core').first()
    if not core_mod:
        return

    # 1. New Core Submodules
    new_submodules = [
        ('settings', 'Settings & Governance', 'Organization defaults and branch configuration overrides', 5),
        ('notifications', 'Notification Templates', 'Communication templates and message event triggers', 6),
        ('audit', 'Audit Trail', 'Append-only tenant administrative audit ledger', 7),
    ]

    submod_objs = {}
    for code, name, desc, order in new_submodules:
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

    # 2. New Core Permissions
    new_permissions = [
        # settings
        ('core.settings.view', 'settings', 'view', 'Can view organization and branch settings', 'View business configuration and policies'),
        ('core.settings.edit', 'settings', 'edit', 'Can edit organization and branch settings', 'Update business configuration, tax, and policies'),
        # notifications
        ('core.notifications.view', 'notifications', 'view', 'Can view notification templates', 'View communication templates'),
        ('core.notifications.manage', 'notifications', 'manage', 'Can manage notification templates', 'Create, edit, or delete notification templates'),
        # audit
        ('core.audit.view', 'audit', 'view', 'Can view tenant audit logs', 'Inspect append-only tenant audit events'),
    ]

    for perm_code, sub_code, action, label, desc in new_permissions:
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


def rollback_extended_core_catalog(apps, schema_editor):
    ProductModule = apps.get_model('master', 'ProductModule')
    ProductSubmodule = apps.get_model('master', 'ProductSubmodule')
    TenantPermissionCatalog = apps.get_model('master', 'TenantPermissionCatalog')

    db_alias = schema_editor.connection.alias
    core_mod = ProductModule.objects.using(db_alias).filter(code='core').first()
    if not core_mod:
        return

    codes_to_remove = [
        'core.settings.view', 'core.settings.edit',
        'core.notifications.view', 'core.notifications.manage',
        'core.audit.view',
    ]
    TenantPermissionCatalog.objects.using(db_alias).filter(code__in=codes_to_remove).delete()
    ProductSubmodule.objects.using(db_alias).filter(module=core_mod, code__in=['settings', 'notifications', 'audit']).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('master', '0003_seed_core_module_catalog'),
    ]

    operations = [
        migrations.RunPython(extend_core_catalog, rollback_extended_core_catalog),
    ]
