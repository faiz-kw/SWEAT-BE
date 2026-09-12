"""
Sprint 11 — Workstream A: Tenant Hierarchy Data Backfill
Deterministic, idempotent backfill for:
- branches.timezone and branches.address_line_1
- submodule_catalog.source_submodule_id (from master.ProductSubmodule)
- company_entities.legal_name
- organization_settings JSONB configurations
"""

from django.db import migrations, connections


def backfill_tenant_hierarchy(apps, schema_editor):
    db_alias = schema_editor.connection.alias

    Branch = apps.get_model('tenant_core', 'Branch')
    Organization = apps.get_model('tenant_core', 'Organization')
    CompanyEntity = apps.get_model('tenant_core', 'CompanyEntity')
    OrganizationSettings = apps.get_model('tenant_core', 'OrganizationSettings')
    SubmoduleCatalog = apps.get_model('tenant_core', 'SubmoduleCatalog')
    ModuleCatalog = apps.get_model('tenant_core', 'ModuleCatalog')

    # 1. Backfill branches
    for branch in Branch.objects.using(db_alias).all():
        updated = False
        if not branch.timezone:
            org = Organization.objects.using(db_alias).filter(id=branch.organization_id).first()
            branch.timezone = org.timezone if (org and org.timezone) else 'Asia/Kolkata'
            updated = True
        if not branch.address_line_1 and branch.address:
            branch.address_line_1 = branch.address[:250]
            updated = True
        if updated:
            branch.save(using=db_alias)

    # 2. Backfill company_entities legal_name
    for ce in CompanyEntity.objects.using(db_alias).all():
        if not ce.legal_name:
            ce.legal_name = ce.name[:250]
            ce.save(using=db_alias)

    # 3. Backfill organization_settings JSON configurations
    for settings in OrganizationSettings.objects.using(db_alias).all():
        updated = False
        for cfg_field in ['membership_config', 'booking_config', 'attendance_config', 'notification_config', 'ai_config']:
            val = getattr(settings, cfg_field)
            if val is None:
                setattr(settings, cfg_field, {})
                updated = True
        if updated:
            settings.save(using=db_alias)

    # 4. Deterministic backfill of submodule_catalog.source_submodule_id from master.ProductSubmodule
    # Query default Master DB for product_submodules
    master_submodules = {}
    try:
        with connections['default'].cursor() as cur:
            cur.execute('''
                SELECT lower(pm.code), lower(ps.code), ps.id
                FROM product_submodules ps
                JOIN product_modules pm ON pm.id = ps.module_id;
            ''')
            for m_code, s_code, ps_id in cur.fetchall():
                master_submodules[(m_code, s_code)] = ps_id
    except Exception:
        pass

    if master_submodules:
        for sc in SubmoduleCatalog.objects.using(db_alias).filter(source_submodule_id__isnull=True):
            mc = ModuleCatalog.objects.using(db_alias).filter(id=sc.module_id).first()
            if mc:
                m_code = (mc.code or mc.module_code or '').lower()
                s_code = (sc.code or sc.submodule_code or '').lower()
                key = (m_code, s_code)
                if key in master_submodules:
                    sc.source_submodule_id = master_submodules[key]
                    sc.save(using=db_alias)


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0010_sprint11_tenant_hierarchy_expand'),
    ]

    operations = [
        migrations.RunPython(backfill_tenant_hierarchy, migrations.RunPython.noop),
    ]
