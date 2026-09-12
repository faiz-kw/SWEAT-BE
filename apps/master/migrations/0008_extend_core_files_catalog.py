# Generated for Sprint 7 File Subsystem Catalog Extension
from django.db import migrations


def extend_core_catalog(apps, schema_editor):
    ProductModule = apps.get_model('master', 'ProductModule')
    ProductSubmodule = apps.get_model('master', 'ProductSubmodule')
    TenantPermissionCatalog = apps.get_model('master', 'TenantPermissionCatalog')

    db_alias = schema_editor.connection.alias

    core_mod = ProductModule.objects.using(db_alias).filter(code='core').first()
    if not core_mod:
        return

    # 1. New Core Submodule: files
    submod_files, _ = ProductSubmodule.objects.using(db_alias).update_or_create(
        module=core_mod,
        code='files',
        defaults={
            'name': 'File & Storage Management',
            'description': 'Tenant private S3 file storage, presigned upload/download, and metadata governance',
            'sort_order': 8,
            'is_active': True,
        },
    )

    # 2. Approved Canonical Core File Permissions
    new_permissions = [
        ('core.files.view', 'files', 'view', 'Can view and download files', 'View file metadata and generate presigned download URLs'),
        ('core.files.create', 'files', 'create', 'Can upload and confirm files', 'Generate presigned upload URLs and confirm uploads'),
        ('core.files.delete', 'files', 'delete', 'Can delete files', 'Soft-delete tenant files'),
    ]

    for perm_code, sub_code, action, label, desc in new_permissions:
        TenantPermissionCatalog.objects.using(db_alias).update_or_create(
            code=perm_code,
            defaults={
                'module': core_mod,
                'submodule': submod_files,
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

    codes_to_remove = ['core.files.view', 'core.files.create', 'core.files.delete']
    TenantPermissionCatalog.objects.using(db_alias).filter(code__in=codes_to_remove).delete()
    ProductSubmodule.objects.using(db_alias).filter(module=core_mod, code='files').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('master', '0007_tenantprovisioning_celery_task_id'),
    ]

    operations = [
        migrations.RunPython(extend_core_catalog, rollback_extended_core_catalog),
    ]
