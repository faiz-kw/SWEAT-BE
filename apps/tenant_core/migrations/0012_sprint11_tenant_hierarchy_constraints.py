"""
Sprint 11 — Workstream A: Tenant Hierarchy Constraints & FK Hardening
Applies NOT NULL constraints, column defaults, and non-deferrable FK constraints:
- company_entities.legal_name NOT NULL
- branches.timezone NOT NULL
- module_catalog.source_module_id NOT NULL
- submodule_catalog.source_submodule_id NOT NULL
- module_catalog defaults (supports_branch_scope=FALSE, is_enabled=TRUE)
- organization_settings defaults (membership_config, booking_config, attendance_config, notification_config, ai_config = '{}'::jsonb)
- Hardened non-deferrable FK constraints with exact canonical ON DELETE actions
"""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0011_sprint11_tenant_hierarchy_backfill'),
    ]

    operations = [
        # 1. Nullability & Model Field Alterations
        migrations.AlterField(
            model_name='companyentity',
            name='legal_name',
            field=models.CharField(max_length=250),
        ),
        migrations.AlterField(
            model_name='branch',
            name='timezone',
            field=models.CharField(default='Asia/Kolkata', max_length=100),
        ),
        migrations.AlterField(
            model_name='modulecatalog',
            name='source_module_id',
            field=models.UUIDField(help_text='Logical cross-DB ref to master.ProductModule.id', unique=True),
        ),
        migrations.AlterField(
            model_name='submodulecatalog',
            name='source_submodule_id',
            field=models.UUIDField(help_text='Logical cross-DB ref to master.ProductSubmodule.id', unique=True),
        ),
        migrations.AlterField(
            model_name='companyentity',
            name='organization',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='company_entities', to='tenant_core.organization'),
        ),
        migrations.AlterField(
            model_name='location',
            name='organization',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='locations', to='tenant_core.organization'),
        ),
        migrations.AlterField(
            model_name='branch',
            name='organization',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='branches', to='tenant_core.organization'),
        ),
        migrations.AlterField(
            model_name='branch',
            name='company_entity',
            field=models.ForeignKey(blank=True, help_text='Optional: which legal entity operates this branch', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='branches', to='tenant_core.companyentity'),
        ),
        migrations.AlterField(
            model_name='branch',
            name='location',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='branches', to='tenant_core.location'),
        ),
        migrations.AlterField(
            model_name='organizationsettings',
            name='organization',
            field=models.OneToOneField(on_delete=django.db.models.deletion.RESTRICT, related_name='settings', to='tenant_core.organization'),
        ),
        migrations.AlterField(
            model_name='branchsettings',
            name='branch',
            field=models.OneToOneField(on_delete=django.db.models.deletion.RESTRICT, related_name='settings', to='tenant_core.branch'),
        ),

        # 2. Raw SQL for PostgreSQL column defaults & hardened FK constraints
        migrations.RunSQL(
            sql="""
            -- 1. Defaults on module_catalog
            ALTER TABLE module_catalog ALTER COLUMN supports_branch_scope SET DEFAULT FALSE;
            ALTER TABLE module_catalog ALTER COLUMN is_enabled SET DEFAULT TRUE;

            -- 2. Defaults on organization_settings JSONB configuration blocks
            ALTER TABLE organization_settings ALTER COLUMN membership_config SET DEFAULT '{}'::jsonb;
            ALTER TABLE organization_settings ALTER COLUMN booking_config SET DEFAULT '{}'::jsonb;
            ALTER TABLE organization_settings ALTER COLUMN attendance_config SET DEFAULT '{}'::jsonb;
            ALTER TABLE organization_settings ALTER COLUMN notification_config SET DEFAULT '{}'::jsonb;
            ALTER TABLE organization_settings ALTER COLUMN ai_config SET DEFAULT '{}'::jsonb;

            -- 3. Hardened FK constraints on company_entities
            ALTER TABLE company_entities DROP CONSTRAINT IF EXISTS company_entities_organization_id_13928614_fk_organizations_id;
            ALTER TABLE company_entities ADD CONSTRAINT fk_company_entities_organization_id
                FOREIGN KEY (organization_id) REFERENCES organizations(id)
                ON DELETE RESTRICT;

            -- 4. Hardened FK constraints on locations
            ALTER TABLE locations DROP CONSTRAINT IF EXISTS locations_organization_id_688bd0fd_fk_organizations_id;
            ALTER TABLE locations ADD CONSTRAINT fk_locations_organization_id
                FOREIGN KEY (organization_id) REFERENCES organizations(id)
                ON DELETE RESTRICT;

            -- 5. Hardened FK constraints on branches
            ALTER TABLE branches DROP CONSTRAINT IF EXISTS branches_organization_id_49dd869c_fk_organizations_id;
            ALTER TABLE branches ADD CONSTRAINT fk_branches_organization_id
                FOREIGN KEY (organization_id) REFERENCES organizations(id)
                ON DELETE RESTRICT;

            ALTER TABLE branches DROP CONSTRAINT IF EXISTS branches_company_entity_id_afad2bc8_fk_company_entities_id;
            ALTER TABLE branches ADD CONSTRAINT fk_branches_company_entity_id
                FOREIGN KEY (company_entity_id) REFERENCES company_entities(id)
                ON DELETE SET NULL;

            ALTER TABLE branches DROP CONSTRAINT IF EXISTS branches_location_id_1835a128_fk_locations_id;
            ALTER TABLE branches ADD CONSTRAINT fk_branches_location_id
                FOREIGN KEY (location_id) REFERENCES locations(id)
                ON DELETE RESTRICT;

            -- 6. Hardened FK constraints on organization_settings
            ALTER TABLE organization_settings DROP CONSTRAINT IF EXISTS organization_setting_organization_id_36340196_fk_organizat;
            ALTER TABLE organization_settings ADD CONSTRAINT fk_organization_settings_organization_id
                FOREIGN KEY (organization_id) REFERENCES organizations(id)
                ON DELETE RESTRICT;

            -- 7. Hardened FK constraints on branch_settings
            ALTER TABLE branch_settings DROP CONSTRAINT IF EXISTS branch_settings_branch_id_47a5cec5_fk_branches_id;
            ALTER TABLE branch_settings ADD CONSTRAINT fk_branch_settings_branch_id
                FOREIGN KEY (branch_id) REFERENCES branches(id)
                ON DELETE RESTRICT;
            """,
            reverse_sql="""
            ALTER TABLE module_catalog ALTER COLUMN supports_branch_scope DROP DEFAULT;
            ALTER TABLE module_catalog ALTER COLUMN is_enabled DROP DEFAULT;
            ALTER TABLE organization_settings ALTER COLUMN membership_config DROP DEFAULT;
            ALTER TABLE organization_settings ALTER COLUMN booking_config DROP DEFAULT;
            ALTER TABLE organization_settings ALTER COLUMN attendance_config DROP DEFAULT;
            ALTER TABLE organization_settings ALTER COLUMN notification_config DROP DEFAULT;
            ALTER TABLE organization_settings ALTER COLUMN ai_config DROP DEFAULT;
            """
        ),
    ]
