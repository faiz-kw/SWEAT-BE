"""
Sprint 11 — Workstream B: Product Module Catalog & Tenant Entitlements Constraints & FK Hardening
Enforces NOT NULL, column defaults, and non-deferrable FK constraints:
- NOT NULL on display_order, status, created_at, updated_at
- PostgreSQL column defaults on status, supports_branch_scope, version
- Hardened non-deferrable FK constraints on product_submodules, tenant_modules, tenant_permission_catalog
"""

from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('master', '0020_sprint11_catalog_entitlements_backfill'),
    ]

    operations = [
        # 1. NOT NULL alterations on product_modules
        migrations.AlterField(
            model_name='productmodule',
            name='display_order',
            field=models.IntegerField(default=0),
        ),
        migrations.AlterField(
            model_name='productmodule',
            name='status',
            field=models.CharField(default='ACTIVE', max_length=30),
        ),
        migrations.AlterField(
            model_name='productmodule',
            name='created_at',
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.AlterField(
            model_name='productmodule',
            name='updated_at',
            field=models.DateTimeField(auto_now=True),
        ),

        # 2. NOT NULL alterations on product_submodules
        migrations.AlterField(
            model_name='productsubmodule',
            name='display_order',
            field=models.IntegerField(default=0),
        ),
        migrations.AlterField(
            model_name='productsubmodule',
            name='status',
            field=models.CharField(default='ACTIVE', max_length=30),
        ),
        migrations.AlterField(
            model_name='productsubmodule',
            name='created_at',
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.AlterField(
            model_name='productsubmodule',
            name='updated_at',
            field=models.DateTimeField(auto_now=True),
        ),

        # 3. NOT NULL alterations on tenant_permission_catalog
        migrations.AlterField(
            model_name='tenantpermissioncatalog',
            name='version',
            field=models.IntegerField(default=1),
        ),
        migrations.AlterField(
            model_name='tenantpermissioncatalog',
            name='status',
            field=models.CharField(default='ACTIVE', max_length=30),
        ),
        migrations.AlterField(
            model_name='tenantpermissioncatalog',
            name='created_at',
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.AlterField(
            model_name='tenantpermissioncatalog',
            name='updated_at',
            field=models.DateTimeField(auto_now=True),
        ),

        # 4. NOT NULL alterations on tenant_modules
        migrations.AlterField(
            model_name='tenantmodule',
            name='status',
            field=models.CharField(default='ENABLED', max_length=30),
        ),
        migrations.AlterField(
            model_name='tenantmodule',
            name='created_at',
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.AlterField(
            model_name='tenantmodule',
            name='updated_at',
            field=models.DateTimeField(auto_now=True),
        ),

        # 5. Model Meta & FK Alterations
        migrations.AlterModelOptions(
            name='productmodule',
            options={'ordering': ['display_order', 'sort_order', 'name']},
        ),
        migrations.AlterModelOptions(
            name='productsubmodule',
            options={'ordering': ['module', 'display_order', 'sort_order']},
        ),
        migrations.AlterField(
            model_name='productsubmodule',
            name='module',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='submodules', to='master.productmodule'),
        ),
        migrations.AlterField(
            model_name='tenantpermissioncatalog',
            name='module',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='tenant_permissions', to='master.productmodule'),
        ),
        migrations.AlterField(
            model_name='tenantpermissioncatalog',
            name='submodule',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='master.productsubmodule'),
        ),
        migrations.AlterField(
            model_name='tenantmodule',
            name='tenant',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='enabled_modules_set', to='master.tenant'),
        ),
        migrations.AlterField(
            model_name='tenantmodule',
            name='module',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, to='master.productmodule'),
        ),

        # 6. Raw SQL for PostgreSQL column defaults & hardened FK constraints
        migrations.RunSQL(
            sql="""
            -- 1. Defaults on product_modules
            ALTER TABLE product_modules ALTER COLUMN supports_branch_scope SET DEFAULT FALSE;
            ALTER TABLE product_modules ALTER COLUMN status SET DEFAULT 'ACTIVE';

            -- 2. Defaults on product_submodules
            ALTER TABLE product_submodules ALTER COLUMN status SET DEFAULT 'ACTIVE';

            -- 3. Defaults on tenant_permission_catalog
            ALTER TABLE tenant_permission_catalog ALTER COLUMN version SET DEFAULT 1;
            ALTER TABLE tenant_permission_catalog ALTER COLUMN status SET DEFAULT 'ACTIVE';

            -- 4. Defaults on tenant_modules
            ALTER TABLE tenant_modules ALTER COLUMN status SET DEFAULT 'ENABLED';

            -- 5. Hardened FK constraints on product_submodules
            ALTER TABLE product_submodules DROP CONSTRAINT IF EXISTS product_submodules_module_id_cdedfbf7_fk_product_modules_id;
            ALTER TABLE product_submodules ADD CONSTRAINT fk_product_submodules_module_id
                FOREIGN KEY (module_id) REFERENCES product_modules(id)
                ON DELETE RESTRICT;

            -- 6. Hardened FK constraints on tenant_modules
            ALTER TABLE tenant_modules DROP CONSTRAINT IF EXISTS tenant_modules_tenant_id_d27d8a8b_fk_tenants_id;
            ALTER TABLE tenant_modules ADD CONSTRAINT fk_tenant_modules_tenant_id
                FOREIGN KEY (tenant_id) REFERENCES tenants(id)
                ON DELETE RESTRICT;

            ALTER TABLE tenant_modules DROP CONSTRAINT IF EXISTS tenant_modules_module_id_d8a8f63a_fk_product_modules_id;
            ALTER TABLE tenant_modules ADD CONSTRAINT fk_tenant_modules_module_id
                FOREIGN KEY (module_id) REFERENCES product_modules(id)
                ON DELETE RESTRICT;

            -- 7. Hardened FK constraints on tenant_permission_catalog
            ALTER TABLE tenant_permission_catalog DROP CONSTRAINT IF EXISTS tenant_permission_ca_module_id_3dd1f873_fk_product_m;
            ALTER TABLE tenant_permission_catalog ADD CONSTRAINT fk_tenant_permission_catalog_module_id
                FOREIGN KEY (module_id) REFERENCES product_modules(id)
                ON DELETE RESTRICT;

            ALTER TABLE tenant_permission_catalog DROP CONSTRAINT IF EXISTS tenant_permission_ca_submodule_id_f4e66a74_fk_product_s;
            ALTER TABLE tenant_permission_catalog ADD CONSTRAINT fk_tenant_permission_catalog_submodule_id
                FOREIGN KEY (submodule_id) REFERENCES product_submodules(id)
                ON DELETE SET NULL;
            """,
            reverse_sql="""
            ALTER TABLE product_modules ALTER COLUMN supports_branch_scope DROP DEFAULT;
            ALTER TABLE product_modules ALTER COLUMN status DROP DEFAULT;
            ALTER TABLE product_submodules ALTER COLUMN status DROP DEFAULT;
            ALTER TABLE tenant_permission_catalog ALTER COLUMN version DROP DEFAULT;
            ALTER TABLE tenant_permission_catalog ALTER COLUMN status DROP DEFAULT;
            ALTER TABLE tenant_modules ALTER COLUMN status DROP DEFAULT;
            """
        ),
    ]
