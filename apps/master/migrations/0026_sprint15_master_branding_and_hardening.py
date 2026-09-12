"""
Sprint 15 — Master DB Branding, Resource Usage & Foreign Key Hardening
Implements:
1. tenant_domains: tenant_id FK RESTRICT NOT DEFERRABLE, is_primary DEFAULT false, is_verified DEFAULT false
2. tenant_branding: tenant_id FK RESTRICT NOT DEFERRABLE, widen branding_mode to varchar(30),
   add canonical brand_name, secondary_color, theme_preset_code, theme_tokens, support_phone,
   logo_storage_key, favicon_storage_key, login_logo_key, login_background_key, email_logo_key,
   deterministic backfill from app_name and accent_color
3. tenant_resource_usage: deterministic backfill from measured_at then SET NOT NULL for period_start/period_end
4. subscription_dunning_events: invoice_id FK SET NULL NOT DEFERRABLE
5. tenant_data_source_health: widen status to varchar(30)
6. platform_branding: canonical brand_name (default PerformanceOS), secondary_color (default #f59e0b),
   login_title, support_phone, logo_storage_key, favicon_storage_key, login_logo_key, login_background_key
7. marketplace_integrations: logo_storage_key
"""

import django.db.models.deletion
from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('master', '0025_sprint13_control_plane_and_marketplace_constraints'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name='marketplaceintegration',
                    name='logo_storage_key',
                    field=models.TextField(blank=True, null=True),
                ),
                migrations.AddField(
                    model_name='platformbranding',
                    name='brand_name',
                    field=models.CharField(default='PerformanceOS', max_length=200),
                ),
                migrations.AddField(
                    model_name='platformbranding',
                    name='secondary_color',
                    field=models.CharField(blank=True, default='#f59e0b', max_length=20, null=True),
                ),
                migrations.AddField(
                    model_name='platformbranding',
                    name='login_title',
                    field=models.CharField(blank=True, max_length=250, null=True),
                ),
                migrations.AddField(
                    model_name='platformbranding',
                    name='support_phone',
                    field=models.CharField(blank=True, max_length=50, null=True),
                ),
                migrations.AddField(
                    model_name='platformbranding',
                    name='logo_storage_key',
                    field=models.TextField(blank=True, null=True),
                ),
                migrations.AddField(
                    model_name='platformbranding',
                    name='favicon_storage_key',
                    field=models.TextField(blank=True, null=True),
                ),
                migrations.AddField(
                    model_name='platformbranding',
                    name='login_logo_key',
                    field=models.TextField(blank=True, null=True),
                ),
                migrations.AddField(
                    model_name='platformbranding',
                    name='login_background_key',
                    field=models.TextField(blank=True, null=True),
                ),
                migrations.AddField(
                    model_name='tenantbranding',
                    name='brand_name',
                    field=models.CharField(blank=True, max_length=200, null=True),
                ),
                migrations.AddField(
                    model_name='tenantbranding',
                    name='secondary_color',
                    field=models.CharField(blank=True, max_length=20, null=True),
                ),
                migrations.AddField(
                    model_name='tenantbranding',
                    name='theme_preset_code',
                    field=models.CharField(blank=True, default='titanium-teal', max_length=100, null=True),
                ),
                migrations.AddField(
                    model_name='tenantbranding',
                    name='theme_tokens',
                    field=models.JSONField(blank=True, null=True),
                ),
                migrations.AddField(
                    model_name='tenantbranding',
                    name='support_phone',
                    field=models.CharField(blank=True, max_length=50, null=True),
                ),
                migrations.AddField(
                    model_name='tenantbranding',
                    name='logo_storage_key',
                    field=models.TextField(blank=True, null=True),
                ),
                migrations.AddField(
                    model_name='tenantbranding',
                    name='favicon_storage_key',
                    field=models.TextField(blank=True, null=True),
                ),
                migrations.AddField(
                    model_name='tenantbranding',
                    name='login_logo_key',
                    field=models.TextField(blank=True, null=True),
                ),
                migrations.AddField(
                    model_name='tenantbranding',
                    name='login_background_key',
                    field=models.TextField(blank=True, null=True),
                ),
                migrations.AddField(
                    model_name='tenantbranding',
                    name='email_logo_key',
                    field=models.TextField(blank=True, null=True),
                ),
                migrations.AlterField(
                    model_name='tenantbranding',
                    name='branding_mode',
                    field=models.CharField(choices=[('PLATFORM', 'Show Platform Branding'), ('CUSTOM', 'Full White-Label Custom Branding'), ('CO_BRANDED', 'Co-branded (Tenant + Platform)')], default='PLATFORM', max_length=30),
                ),
                migrations.AlterField(
                    model_name='tenantbranding',
                    name='tenant',
                    field=models.OneToOneField(on_delete=django.db.models.deletion.RESTRICT, related_name='branding', to='master.tenant'),
                ),
                migrations.AlterField(
                    model_name='tenantdomain',
                    name='tenant',
                    field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='domains', to='master.tenant'),
                ),
                migrations.AlterField(
                    model_name='tenantdatasourcehealth',
                    name='status',
                    field=models.CharField(choices=[('HEALTHY', 'Healthy'), ('DEGRADED', 'Degraded'), ('UNREACHABLE', 'Unreachable'), ('SCHEMA_DRIFT', 'Schema Version Drift')], default='HEALTHY', max_length=30),
                ),
                migrations.AlterField(
                    model_name='subscriptiondunningevent',
                    name='invoice',
                    field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='dunning_events', to='master.subscriptioninvoice'),
                ),
                migrations.AlterField(
                    model_name='tenantresourceusage',
                    name='period_start',
                    field=models.DateTimeField(default=django.utils.timezone.now),
                ),
                migrations.AlterField(
                    model_name='tenantresourceusage',
                    name='period_end',
                    field=models.DateTimeField(default=django.utils.timezone.now),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql="""
                    -- ==========================================================
                    -- 1. tenant_domains: column defaults and FK restrict
                    -- ==========================================================
                    ALTER TABLE tenant_domains ALTER COLUMN is_primary SET DEFAULT false;
                    ALTER TABLE tenant_domains ALTER COLUMN is_verified SET DEFAULT false;

                    ALTER TABLE tenant_domains DROP CONSTRAINT IF EXISTS tenant_domains_tenant_id_6d1ef00b_fk_tenants_id;
                    ALTER TABLE tenant_domains DROP CONSTRAINT IF EXISTS fk_tenant_domains_tenant;
                    ALTER TABLE tenant_domains
                        ADD CONSTRAINT fk_tenant_domains_tenant
                        FOREIGN KEY (tenant_id) REFERENCES tenants(id)
                        ON DELETE RESTRICT NOT DEFERRABLE;

                    -- ==========================================================
                    -- 2. tenant_branding: FK restrict, widen branding_mode, add columns, backfill
                    -- ==========================================================
                    ALTER TABLE tenant_branding ALTER COLUMN branding_mode TYPE varchar(30);

                    ALTER TABLE tenant_branding DROP CONSTRAINT IF EXISTS tenant_branding_tenant_id_7058c70d_fk_tenants_id;
                    ALTER TABLE tenant_branding DROP CONSTRAINT IF EXISTS fk_tenant_branding_tenant;
                    ALTER TABLE tenant_branding
                        ADD CONSTRAINT fk_tenant_branding_tenant
                        FOREIGN KEY (tenant_id) REFERENCES tenants(id)
                        ON DELETE RESTRICT NOT DEFERRABLE;

                    ALTER TABLE tenant_branding ADD COLUMN IF NOT EXISTS brand_name varchar(200);
                    ALTER TABLE tenant_branding ADD COLUMN IF NOT EXISTS secondary_color varchar(20);
                    ALTER TABLE tenant_branding ADD COLUMN IF NOT EXISTS theme_preset_code varchar(100);
                    ALTER TABLE tenant_branding ADD COLUMN IF NOT EXISTS theme_tokens jsonb;
                    ALTER TABLE tenant_branding ADD COLUMN IF NOT EXISTS support_phone varchar(50);
                    ALTER TABLE tenant_branding ADD COLUMN IF NOT EXISTS logo_storage_key text;
                    ALTER TABLE tenant_branding ADD COLUMN IF NOT EXISTS favicon_storage_key text;
                    ALTER TABLE tenant_branding ADD COLUMN IF NOT EXISTS login_logo_key text;
                    ALTER TABLE tenant_branding ADD COLUMN IF NOT EXISTS login_background_key text;
                    ALTER TABLE tenant_branding ADD COLUMN IF NOT EXISTS email_logo_key text;

                    -- Deterministic backfill from existing approved sources
                    UPDATE tenant_branding SET brand_name = app_name WHERE brand_name IS NULL AND app_name IS NOT NULL AND app_name != '';
                    UPDATE tenant_branding SET secondary_color = accent_color WHERE secondary_color IS NULL AND accent_color IS NOT NULL AND accent_color != '';
                    UPDATE tenant_branding SET theme_preset_code = 'titanium-teal' WHERE theme_preset_code IS NULL;

                    -- ==========================================================
                    -- 3. tenant_resource_usage: deterministic backfill from measured_at then SET NOT NULL
                    -- ==========================================================
                    UPDATE tenant_resource_usage SET period_start = measured_at WHERE period_start IS NULL;
                    UPDATE tenant_resource_usage SET period_end = measured_at WHERE period_end IS NULL;

                    ALTER TABLE tenant_resource_usage ALTER COLUMN period_start SET NOT NULL;
                    ALTER TABLE tenant_resource_usage ALTER COLUMN period_end SET NOT NULL;

                    -- ==========================================================
                    -- 4. subscription_dunning_events: invoice_id FK SET NULL NOT DEFERRABLE
                    -- ==========================================================
                    ALTER TABLE subscription_dunning_events DROP CONSTRAINT IF EXISTS subscription_dunning_invoice_id_950236c6_fk_subscript;
                    ALTER TABLE subscription_dunning_events DROP CONSTRAINT IF EXISTS fk_subscription_dunning_invoice;
                    ALTER TABLE subscription_dunning_events
                        ADD CONSTRAINT fk_subscription_dunning_invoice
                        FOREIGN KEY (invoice_id) REFERENCES subscription_invoices(id)
                        ON DELETE SET NULL NOT DEFERRABLE;

                    -- ==========================================================
                    -- 5. tenant_data_source_health: widen status to varchar(30)
                    -- ==========================================================
                    ALTER TABLE tenant_data_source_health ALTER COLUMN status TYPE varchar(30);

                    -- ==========================================================
                    -- 6. platform_branding: canonical fields with defaults
                    -- ==========================================================
                    ALTER TABLE platform_branding ADD COLUMN IF NOT EXISTS brand_name varchar(200) NOT NULL DEFAULT 'PerformanceOS';
                    ALTER TABLE platform_branding ADD COLUMN IF NOT EXISTS secondary_color varchar(20) DEFAULT '#f59e0b';
                    ALTER TABLE platform_branding ADD COLUMN IF NOT EXISTS login_title varchar(250);
                    ALTER TABLE platform_branding ADD COLUMN IF NOT EXISTS support_phone varchar(50);
                    ALTER TABLE platform_branding ADD COLUMN IF NOT EXISTS logo_storage_key text;
                    ALTER TABLE platform_branding ADD COLUMN IF NOT EXISTS favicon_storage_key text;
                    ALTER TABLE platform_branding ADD COLUMN IF NOT EXISTS login_logo_key text;
                    ALTER TABLE platform_branding ADD COLUMN IF NOT EXISTS login_background_key text;

                    -- ==========================================================
                    -- 7. marketplace_integrations: add logo_storage_key
                    -- ==========================================================
                    ALTER TABLE marketplace_integrations ADD COLUMN IF NOT EXISTS logo_storage_key text;
                    """,
                    reverse_sql="""
                    ALTER TABLE tenant_domains DROP CONSTRAINT IF EXISTS fk_tenant_domains_tenant;
                    ALTER TABLE tenant_branding DROP CONSTRAINT IF EXISTS fk_tenant_branding_tenant;
                    ALTER TABLE subscription_dunning_events DROP CONSTRAINT IF EXISTS fk_subscription_dunning_invoice;
                    ALTER TABLE tenant_resource_usage ALTER COLUMN period_start DROP NOT NULL;
                    ALTER TABLE tenant_resource_usage ALTER COLUMN period_end DROP NOT NULL;
                    ALTER TABLE marketplace_integrations DROP COLUMN IF EXISTS logo_storage_key;
                    ALTER TABLE platform_branding DROP COLUMN IF EXISTS brand_name;
                    ALTER TABLE platform_branding DROP COLUMN IF EXISTS secondary_color;
                    ALTER TABLE platform_branding DROP COLUMN IF EXISTS login_title;
                    ALTER TABLE platform_branding DROP COLUMN IF EXISTS support_phone;
                    ALTER TABLE platform_branding DROP COLUMN IF EXISTS logo_storage_key;
                    ALTER TABLE platform_branding DROP COLUMN IF EXISTS favicon_storage_key;
                    ALTER TABLE platform_branding DROP COLUMN IF EXISTS login_logo_key;
                    ALTER TABLE platform_branding DROP COLUMN IF EXISTS login_background_key;
                    ALTER TABLE tenant_branding DROP COLUMN IF EXISTS brand_name;
                    ALTER TABLE tenant_branding DROP COLUMN IF EXISTS secondary_color;
                    ALTER TABLE tenant_branding DROP COLUMN IF EXISTS theme_preset_code;
                    ALTER TABLE tenant_branding DROP COLUMN IF EXISTS theme_tokens;
                    ALTER TABLE tenant_branding DROP COLUMN IF EXISTS support_phone;
                    ALTER TABLE tenant_branding DROP COLUMN IF EXISTS logo_storage_key;
                    ALTER TABLE tenant_branding DROP COLUMN IF EXISTS favicon_storage_key;
                    ALTER TABLE tenant_branding DROP COLUMN IF EXISTS login_logo_key;
                    ALTER TABLE tenant_branding DROP COLUMN IF EXISTS login_background_key;
                    ALTER TABLE tenant_branding DROP COLUMN IF EXISTS email_logo_key;
                    """
                )
            ]
        ),
    ]
