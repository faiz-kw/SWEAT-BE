"""
Sprint 13 — Control Plane & Marketplace Deterministic Data Backfill
Non-destructive backfill from authoritative existing relational data.
"""

from django.db import migrations


def backfill_sprint13_master_data(apps, schema_editor):
    TenantProvisioning = apps.get_model('master', 'TenantProvisioning')
    MarketplaceIntegration = apps.get_model('master', 'MarketplaceIntegration')
    TenantIntegrationEntitlement = apps.get_model('master', 'TenantIntegrationEntitlement')
    TenantDataSourceHealth = apps.get_model('master', 'TenantDataSourceHealth')

    db_alias = schema_editor.connection.alias

    # 1. Backfill tenant_provisioning
    for rec in TenantProvisioning.objects.using(db_alias).all():
        updated_fields = []
        if not rec.provisioning_status or rec.provisioning_status == 'PENDING':
            rec.provisioning_status = rec.status.upper() if rec.status else 'PENDING'
            updated_fields.append('provisioning_status')
        if not rec.target_schema_version:
            rec.target_schema_version = '1.0'
            updated_fields.append('target_schema_version')
        if not rec.database_created_at and rec.status in ('SUCCESS', 'COMPLETED'):
            rec.database_created_at = rec.completed_at or rec.created_at
            updated_fields.append('database_created_at')
        if not rec.failed_at and rec.status == 'FAILED':
            rec.failed_at = rec.completed_at or rec.updated_at
            updated_fields.append('failed_at')
        if updated_fields:
            rec.save(update_fields=updated_fields)

    # 2. Backfill marketplace_integrations categories
    category_map = {
        'INT-WHATSAPP': 'MESSAGING',
        'INT-RAZORPAY': 'PAYMENTS',
        'INT-STRIPE': 'PAYMENTS',
        'INT-ZOOM': 'OTHER',
        'INT-GCAL': 'OTHER',
    }
    for integ in MarketplaceIntegration.objects.using(db_alias).all():
        target_cat = category_map.get(integ.code, 'OTHER')
        if integ.category != target_cat:
            integ.category = target_cat
            integ.save(update_fields=['category'])

    # 3. Backfill tenant_integration_entitlements
    for ent in TenantIntegrationEntitlement.objects.using(db_alias).all():
        updated_fields = []
        if not ent.enabled_at and ent.status in ('ACTIVE', 'ENABLED'):
            ent.enabled_at = getattr(ent, 'activated_at', None) or getattr(ent, 'created_at', None)
            updated_fields.append('enabled_at')
        if not ent.disabled_at and ent.status in ('INACTIVE', 'DISABLED'):
            ent.disabled_at = getattr(ent, 'deactivated_at', None)
            updated_fields.append('disabled_at')
        if updated_fields:
            ent.save(update_fields=updated_fields)

    # 4. Backfill tenant_data_source_health if any records exist
    for h in TenantDataSourceHealth.objects.using(db_alias).all():
        if not h.tenant_id and h.data_source and getattr(h.data_source, 'tenant_id', None):
            h.tenant_id = h.data_source.tenant_id
            h.save(update_fields=['tenant_id'])


def reverse_backfill(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('master', '0023_sprint13_control_plane_and_marketplace_expand'),
    ]

    operations = [
        migrations.RunPython(backfill_sprint13_master_data, reverse_backfill),
    ]
