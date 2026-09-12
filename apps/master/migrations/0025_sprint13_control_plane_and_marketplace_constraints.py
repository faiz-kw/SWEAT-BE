"""
Sprint 13 — Control Plane & Marketplace Physical Constraints Hardening
Enforces NOT NULL, column defaults, and ON DELETE RESTRICT NOT DEFERRABLE foreign keys.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('master', '0024_sprint13_control_plane_and_marketplace_backfill'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name='saasplanintegration',
                    name='integration',
                    field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, to='master.marketplaceintegration'),
                ),
                migrations.AlterField(
                    model_name='saasplanintegration',
                    name='plan',
                    field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='bundled_integrations', to='master.saasplan'),
                ),
                migrations.AlterField(
                    model_name='tenantintegrationentitlement',
                    name='integration',
                    field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, to='master.marketplaceintegration'),
                ),
                migrations.AlterField(
                    model_name='tenantintegrationentitlement',
                    name='tenant',
                    field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='integration_entitlements', to='master.tenant'),
                ),
                migrations.AlterField(
                    model_name='tenantprovisioning',
                    name='tenant',
                    field=models.ForeignKey(default=None, on_delete=django.db.models.deletion.RESTRICT, related_name='provisioning_records', to='master.tenant'),
                    preserve_default=False,
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql="""
                    -- ==========================================================
                    -- 1. tenant_provisioning: nullability & FK restrict
                    -- ==========================================================
                    ALTER TABLE tenant_provisioning ALTER COLUMN tenant_id SET NOT NULL;
                    ALTER TABLE tenant_provisioning ALTER COLUMN provisioning_status SET NOT NULL;
                    ALTER TABLE tenant_provisioning ALTER COLUMN target_schema_version SET NOT NULL;

                    -- Drop legacy deferrable FK if exists
                    ALTER TABLE tenant_provisioning DROP CONSTRAINT IF EXISTS tenant_provisioning_tenant_id_bc6bae87_fk_tenants_id;
                    ALTER TABLE tenant_provisioning DROP CONSTRAINT IF EXISTS fk_tenant_provisioning_tenant;

                    -- Add hardened FK
                    ALTER TABLE tenant_provisioning
                        ADD CONSTRAINT fk_tenant_provisioning_tenant
                        FOREIGN KEY (tenant_id) REFERENCES tenants(id)
                        ON DELETE RESTRICT NOT DEFERRABLE;

                    -- ==========================================================
                    -- 2. tenant_data_source_health: tenant_id NOT NULL & FK restrict
                    -- ==========================================================
                    ALTER TABLE tenant_data_source_health ALTER COLUMN tenant_id SET NOT NULL;

                    ALTER TABLE tenant_data_source_health DROP CONSTRAINT IF EXISTS tenant_data_source_health_tenant_id_9edf2cb1_fk_tenants_id;
                    ALTER TABLE tenant_data_source_health DROP CONSTRAINT IF EXISTS fk_tenant_data_source_health_tenant;
                    ALTER TABLE tenant_data_source_health
                        ADD CONSTRAINT fk_tenant_data_source_health_tenant
                        FOREIGN KEY (tenant_id) REFERENCES tenants(id)
                        ON DELETE RESTRICT NOT DEFERRABLE;

                    -- ==========================================================
                    -- 3. marketplace_integrations: column defaults & constraints
                    -- ==========================================================
                    ALTER TABLE marketplace_integrations ALTER COLUMN name TYPE varchar(150);
                    ALTER TABLE marketplace_integrations ALTER COLUMN code TYPE varchar(100);
                    ALTER TABLE marketplace_integrations ALTER COLUMN status TYPE varchar(30);
                    ALTER TABLE marketplace_integrations ALTER COLUMN status SET DEFAULT 'DRAFT';
                    ALTER TABLE marketplace_integrations ALTER COLUMN category SET NOT NULL;

                    -- ==========================================================
                    -- 4. saas_plan_integrations: FK restrict & defaults
                    -- ==========================================================
                    ALTER TABLE saas_plan_integrations ALTER COLUMN is_included SET DEFAULT TRUE;
                    ALTER TABLE saas_plan_integrations ALTER COLUMN created_at SET NOT NULL;

                    ALTER TABLE saas_plan_integrations DROP CONSTRAINT IF EXISTS saas_plan_integrations_plan_id_b4da679a_fk_saas_plans_id;
                    ALTER TABLE saas_plan_integrations DROP CONSTRAINT IF EXISTS saas_plan_integratio_integration_id_8b94223c_fk_marketpla;
                    ALTER TABLE saas_plan_integrations DROP CONSTRAINT IF EXISTS fk_spi_plan_id;
                    ALTER TABLE saas_plan_integrations DROP CONSTRAINT IF EXISTS fk_spi_integration_id;

                    ALTER TABLE saas_plan_integrations
                        ADD CONSTRAINT fk_spi_plan_id
                        FOREIGN KEY (plan_id) REFERENCES saas_plans(id)
                        ON DELETE RESTRICT NOT DEFERRABLE;

                    ALTER TABLE saas_plan_integrations
                        ADD CONSTRAINT fk_spi_integration_id
                        FOREIGN KEY (integration_id) REFERENCES marketplace_integrations(id)
                        ON DELETE RESTRICT NOT DEFERRABLE;

                    -- ==========================================================
                    -- 5. tenant_integration_entitlements: FK restrict & defaults
                    -- ==========================================================
                    ALTER TABLE tenant_integration_entitlements ALTER COLUMN status TYPE varchar(30);
                    ALTER TABLE tenant_integration_entitlements ALTER COLUMN status SET DEFAULT 'AVAILABLE';
                    ALTER TABLE tenant_integration_entitlements ALTER COLUMN updated_at SET NOT NULL;

                    ALTER TABLE tenant_integration_entitlements DROP CONSTRAINT IF EXISTS tenant_integration_e_tenant_id_9c048d6e_fk_tenants_i;
                    ALTER TABLE tenant_integration_entitlements DROP CONSTRAINT IF EXISTS tenant_integration_e_integration_id_85dab7b4_fk_marketpla;
                    ALTER TABLE tenant_integration_entitlements DROP CONSTRAINT IF EXISTS fk_tie_tenant_id;
                    ALTER TABLE tenant_integration_entitlements DROP CONSTRAINT IF EXISTS fk_tie_integration_id;

                    ALTER TABLE tenant_integration_entitlements
                        ADD CONSTRAINT fk_tie_tenant_id
                        FOREIGN KEY (tenant_id) REFERENCES tenants(id)
                        ON DELETE RESTRICT NOT DEFERRABLE;

                    ALTER TABLE tenant_integration_entitlements
                        ADD CONSTRAINT fk_tie_integration_id
                        FOREIGN KEY (integration_id) REFERENCES marketplace_integrations(id)
                        ON DELETE RESTRICT NOT DEFERRABLE;
                    """,
                    reverse_sql="""
                    -- Reversible DDL fallback
                    ALTER TABLE tenant_provisioning DROP CONSTRAINT IF EXISTS fk_tenant_provisioning_tenant;
                    ALTER TABLE tenant_data_source_health DROP CONSTRAINT IF EXISTS fk_tenant_data_source_health_tenant;
                    ALTER TABLE saas_plan_integrations DROP CONSTRAINT IF EXISTS fk_spi_plan_id;
                    ALTER TABLE saas_plan_integrations DROP CONSTRAINT IF EXISTS fk_spi_integration_id;
                    ALTER TABLE tenant_integration_entitlements DROP CONSTRAINT IF EXISTS fk_tie_tenant_id;
                    ALTER TABLE tenant_integration_entitlements DROP CONSTRAINT IF EXISTS fk_tie_integration_id;
                    """
                )
            ]
        ),
    ]
