# Generated manually for Phase 2 Architecture Final Cleanup: ProgramCategory backfill and delivery_mode normalization

import re
from django.db import migrations, models


def run_program_category_backfill(apps, schema_editor):
    from config.routers import set_tenant_db_alias, get_tenant_db_alias
    db_alias = schema_editor.connection.alias
    prev_alias = get_tenant_db_alias()
    set_tenant_db_alias(db_alias)
    try:
        Program = apps.get_model('tenant_core', 'Program')
        ProgramCategory = apps.get_model('tenant_core', 'ProgramCategory')

        # 1. Backfill missing ProgramCategory from ProgramType
        missing_cat_programs = Program.objects.using(db_alias).filter(category__isnull=True, program_type__isnull=False)
        for prog in missing_cat_programs:
            pt = prog.program_type
            org_id = prog.organization_id
            cat = ProgramCategory.objects.using(db_alias).filter(organization_id=org_id, name__iexact=pt.name).first()
            if not cat:
                cat = ProgramCategory.objects.using(db_alias).filter(organization_id=org_id, code__iexact=pt.code).first()
            if not cat:
                cleaned = re.sub(r'[^A-Za-z0-9]+', '_', str(pt.name).strip().upper()).strip('_')
                base_code = cleaned[:80] if cleaned else 'CATEGORY'
                candidate = base_code
                counter = 1
                while ProgramCategory.objects.using(db_alias).filter(organization_id=org_id, code=candidate).exists():
                    counter += 1
                    suffix = f"_{counter}"
                    candidate = f"{base_code[:100 - len(suffix)]}{suffix}"

                cat = ProgramCategory.objects.using(db_alias).create(
                    organization_id=org_id,
                    name=pt.name,
                    code=candidate,
                    description=pt.description,
                    display_order=pt.display_order,
                    status=pt.status,
                )
            prog.category = cat
            prog.save(using=db_alias, update_fields=['category'])

        # 2. Normalize delivery_mode to engine service structures
        Program.objects.using(db_alias).filter(delivery_mode='GROUP').update(delivery_mode='GROUP_CLASS')
        Program.objects.using(db_alias).filter(delivery_mode='PERSONAL_TRAINING').update(delivery_mode='INDIVIDUAL_SERVICE')
        Program.objects.using(db_alias).filter(delivery_mode='OPEN_GYM').update(delivery_mode='OPEN_ACCESS')
        Program.objects.using(db_alias).filter(delivery_mode='HYBRID').update(delivery_mode='GROUP_CLASS')
    finally:
        set_tenant_db_alias(prev_alias)


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0044_phase2_program_branch_availability'),
    ]

    operations = [
        migrations.AlterField(
            model_name='program',
            name='delivery_mode',
            field=models.CharField(
                blank=True,
                choices=[
                    ('GROUP_CLASS', 'Group Class'),
                    ('INDIVIDUAL_SERVICE', 'Individual Service / PT'),
                    ('OPEN_ACCESS', 'Open Access / Gym Floor'),
                    ('GROUP', 'Group Class (Legacy)'),
                    ('PERSONAL_TRAINING', 'Personal Training (Legacy)'),
                    ('OPEN_GYM', 'Open Gym Access (Legacy)'),
                    ('HYBRID', 'Hybrid / Online (Legacy)')
                ],
                default='GROUP_CLASS',
                max_length=30
            ),
        ),
        migrations.RunPython(run_program_category_backfill, migrations.RunPython.noop),
    ]
