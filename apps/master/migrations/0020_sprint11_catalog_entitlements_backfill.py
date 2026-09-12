"""
Sprint 11 — Workstream B: Product Module Catalog & Tenant Entitlements Data Backfill
Deterministic, idempotent backfill for:
- product_modules: display_order, status, created_at, updated_at
- product_submodules: display_order, status, created_at, updated_at
- tenant_permission_catalog: version, status, created_at, updated_at
- tenant_modules: status, created_at, updated_at
"""

from django.db import migrations
from django.utils import timezone


def backfill_catalog_and_entitlements(apps, schema_editor):
    db_alias = schema_editor.connection.alias

    ProductModule = apps.get_model('master', 'ProductModule')
    ProductSubmodule = apps.get_model('master', 'ProductSubmodule')
    TenantPermissionCatalog = apps.get_model('master', 'TenantPermissionCatalog')
    TenantModule = apps.get_model('master', 'TenantModule')

    now = timezone.now()

    # 1. Backfill product_modules
    for pm in ProductModule.objects.using(db_alias).all():
        pm.supports_branch_scope = getattr(pm, 'supports_branch_scope', False) or False
        pm.display_order = getattr(pm, 'sort_order', 0) if pm.display_order is None else pm.display_order
        if not pm.status:
            pm.status = 'ACTIVE' if getattr(pm, 'is_active', True) else 'INACTIVE'
        if not pm.created_at:
            pm.created_at = now
        if not pm.updated_at:
            pm.updated_at = now
        pm.save(using=db_alias)

    # 2. Backfill product_submodules
    for ps in ProductSubmodule.objects.using(db_alias).all():
        ps.display_order = getattr(ps, 'sort_order', 0) if ps.display_order is None else ps.display_order
        if not ps.status:
            ps.status = 'ACTIVE' if getattr(ps, 'is_active', True) else 'INACTIVE'
        if not ps.created_at:
            ps.created_at = now
        if not ps.updated_at:
            ps.updated_at = now
        ps.save(using=db_alias)

    # 3. Backfill tenant_permission_catalog
    for tpc in TenantPermissionCatalog.objects.using(db_alias).all():
        if tpc.version is None:
            tpc.version = 1
        if not tpc.status:
            tpc.status = 'ACTIVE' if getattr(tpc, 'is_active', True) else 'INACTIVE'
        if not tpc.created_at:
            tpc.created_at = now
        if not tpc.updated_at:
            tpc.updated_at = now
        tpc.save(using=db_alias)

    # 4. Backfill tenant_modules
    for tm in TenantModule.objects.using(db_alias).all():
        if not tm.status:
            tm.status = 'ENABLED' if getattr(tm, 'is_enabled', True) else 'DISABLED'
        if not tm.created_at:
            tm.created_at = getattr(tm, 'enabled_at', now) or now
        if not tm.updated_at:
            tm.updated_at = now
        tm.save(using=db_alias)


class Migration(migrations.Migration):

    dependencies = [
        ('master', '0019_sprint11_catalog_entitlements_expand'),
    ]

    operations = [
        migrations.RunPython(backfill_catalog_and_entitlements, migrations.RunPython.noop),
    ]
