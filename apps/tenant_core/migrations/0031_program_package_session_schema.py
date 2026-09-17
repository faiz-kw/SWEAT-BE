import uuid
from decimal import Decimal
import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


def backfill_program_types_and_packages(apps, schema_editor):
    ProgramType = apps.get_model('tenant_core', 'ProgramType')
    Program = apps.get_model('tenant_core', 'Program')
    Package = apps.get_model('tenant_core', 'Package')
    Organization = apps.get_model('tenant_core', 'Organization')
    db_alias = schema_editor.connection.alias

    # 1. Backfill ProgramType and link to Programs
    standard_types = [
        ('MEMBERSHIP', 'Membership', 1),
        ('FITNESS', 'Fitness', 2),
        ('PERSONAL_TRAINING', 'Personal Training', 3),
        ('PILATES', 'Pilates', 4),
        ('BOOTCAMP', 'Bootcamp', 5),
        ('TRANSFORMATION', 'Transformation', 6),
        ('ONLINE', 'Online', 7),
        ('HYBRID', 'Hybrid', 8),
        ('OTHER', 'Other', 9),
    ]

    for org in Organization.objects.using(db_alias).all():
        type_map = {}
        for code, name, order in standard_types:
            pt, _ = ProgramType.objects.using(db_alias).get_or_create(
                organization=org,
                code=code,
                defaults={
                    'name': name,
                    'display_order': order,
                    'status': 'ACTIVE',
                }
            )
            type_map[code] = pt

        # Link programs with legacy_program_type or program_type
        for prog in Program.objects.using(db_alias).filter(organization=org):
            legacy_code = getattr(prog, 'legacy_program_type', None) or 'MEMBERSHIP'
            legacy_code = legacy_code.upper().strip()
            if legacy_code not in type_map:
                pt, _ = ProgramType.objects.using(db_alias).get_or_create(
                    organization=org,
                    code=legacy_code,
                    defaults={
                        'name': legacy_code.replace('_', ' ').title(),
                        'display_order': 10,
                        'status': 'ACTIVE',
                    }
                )
                type_map[legacy_code] = pt
            prog.program_type = type_map[legacy_code]
            prog.save(using=db_alias, update_fields=['program_type'])

    # 2. Backfill null packages.program_id with a controlled legacy Program per organization
    null_packages = Package.objects.using(db_alias).filter(program__isnull=True)
    for pkg in null_packages:
        org = pkg.organization
        legacy_prog, _ = Program.objects.using(db_alias).get_or_create(
            organization=org,
            code='LEGACY-PROGRAM',
            defaults={
                'name': 'Legacy Program',
                'status': 'ACTIVE',
            }
        )
        pkg.program = legacy_prog
        pkg.save(using=db_alias, update_fields=['program'])


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0030_tenantuser_username'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProgramType',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('code', models.CharField(max_length=100)),
                ('name', models.CharField(max_length=150)),
                ('description', models.TextField(blank=True, null=True)),
                ('display_order', models.IntegerField(default=0)),
                ('status', models.CharField(choices=[('ACTIVE', 'Active'), ('INACTIVE', 'Inactive')], default='ACTIVE', max_length=20)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='program_types', to='tenant_core.organization')),
            ],
            options={
                'db_table': 'program_types',
                'unique_together': {('organization', 'code')},
            },
        ),
        migrations.AddIndex(
            model_name='programtype',
            index=models.Index(fields=['organization', 'status', 'display_order'], name='idx_progtyp_org_st_ord'),
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name='program',
                    name='legacy_program_type',
                    field=models.CharField(blank=True, db_column='program_type', max_length=40, null=True),
                ),
                migrations.AlterField(
                    model_name='program',
                    name='program_type',
                    field=models.ForeignKey(blank=True, db_column='program_type_id', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='programs', to='tenant_core.programtype'),
                ),
            ],
            database_operations=[
                migrations.AddField(
                    model_name='program',
                    name='program_type',
                    field=models.ForeignKey(blank=True, db_column='program_type_id', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='programs', to='tenant_core.programtype'),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name='program',
            index=models.Index(fields=['program_type', 'status'], name='idx_prog_type_status'),
        ),
        migrations.RunPython(
            backfill_program_types_and_packages,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name='package',
            name='program',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='packages', to='tenant_core.program'),
        ),
        migrations.AlterUniqueTogether(
            name='package',
            unique_together={('program', 'code'), ('organization', 'code')},
        ),
        migrations.AddField(
            model_name='packageversion',
            name='total_days',
            field=models.PositiveIntegerField(default=30),
        ),
        migrations.AddField(
            model_name='packageversion',
            name='is_trial',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='packageversion',
            name='only_for_trial',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='packageversion',
            name='show_on_web',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='packageversion',
            name='show_on_app',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='packageversion',
            name='published_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name='packageversion',
            constraint=models.CheckConstraint(condition=models.Q(('total_days__gt', 0)), name='chk_package_version_total_days_gt_zero'),
        ),
        migrations.AddField(
            model_name='packageprice',
            name='display_price',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True),
        ),
        migrations.AddField(
            model_name='packageprice',
            name='prices_include_tax',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='packageentitlementdefinition',
            name='extra_unit_price',
            field=models.DecimalField(blank=True, decimal_places=2, default=Decimal('0.00'), max_digits=14, null=True),
        ),
        migrations.AlterField(
            model_name='packageclassaccessrule',
            name='access_type',
            field=models.CharField(choices=[('INCLUDED', 'Included in Package'), ('EXCLUDED', 'Explicitly Excluded'), ('DISCOUNTED', 'Discounted Rate'), ('ADD_ON', 'Add-On'), ('PAY_PER_USE', 'Pay Per Use')], default='INCLUDED', max_length=20),
        ),
    ]
