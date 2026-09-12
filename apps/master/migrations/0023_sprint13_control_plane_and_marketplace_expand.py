"""
Sprint 13 — Control Plane & Marketplace Schema Expansion (Additive Only)
Adds canonical columns to tenant_provisioning, tenant_data_source_health,
marketplace_integrations, saas_plan_integrations, and tenant_integration_entitlements.
"""

from django.db import migrations, models
import django.db.models.deletion
from django.utils import timezone


class Migration(migrations.Migration):

    dependencies = [
        ('master', '0022_sprint12_iam_governance'),
    ]

    operations = [
        # 1. tenant_provisioning
        migrations.AddField(
            model_name='tenantprovisioning',
            name='provisioning_status',
            field=models.CharField(default='PENDING', max_length=30),
        ),
        migrations.AddField(
            model_name='tenantprovisioning',
            name='target_schema_version',
            field=models.CharField(default='1.0', max_length=50),
        ),
        migrations.AddField(
            model_name='tenantprovisioning',
            name='database_created_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='tenantprovisioning',
            name='failed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='tenantprovisioning',
            name='failure_reason',
            field=models.TextField(blank=True, null=True),
        ),

        # 2. tenant_data_source_health
        migrations.AddField(
            model_name='tenantdatasourcehealth',
            name='tenant',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name='health_checks',
                to='master.tenant',
            ),
        ),
        migrations.AddField(
            model_name='tenantdatasourcehealth',
            name='detected_schema_version',
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
        migrations.AddField(
            model_name='tenantdatasourcehealth',
            name='error_code',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),

        # 3. marketplace_integrations
        migrations.AlterField(
            model_name='marketplaceintegration',
            name='code',
            field=models.CharField(help_text='e.g. RAZORPAY, TELECMI, GUPSHUP', max_length=100, unique=True),
        ),
        migrations.AlterField(
            model_name='marketplaceintegration',
            name='name',
            field=models.CharField(max_length=150),
        ),
        migrations.AlterField(
            model_name='marketplaceintegration',
            name='status',
            field=models.CharField(
                choices=[('ACTIVE', 'Active'), ('INACTIVE', 'Inactive'), ('DEPRECATED', 'Deprecated')],
                default='DRAFT',
                max_length=30,
            ),
        ),
        migrations.AddField(
            model_name='marketplaceintegration',
            name='category',
            field=models.CharField(default='OTHER', max_length=50),
        ),
        migrations.AddField(
            model_name='marketplaceintegration',
            name='configuration_schema',
            field=models.JSONField(blank=True, null=True),
        ),

        # 4. saas_plan_integrations
        migrations.AddField(
            model_name='saasplanintegration',
            name='created_at',
            field=models.DateTimeField(default=timezone.now),
        ),

        # 5. tenant_integration_entitlements
        migrations.AlterField(
            model_name='tenantintegrationentitlement',
            name='status',
            field=models.CharField(
                choices=[
                    ('AVAILABLE', 'Available'),
                    ('ENABLED', 'Enabled'),
                    ('ACTIVE', 'Active'),
                    ('DISABLED', 'Disabled'),
                    ('INACTIVE', 'Inactive'),
                    ('SUSPENDED', 'Suspended'),
                ],
                default='AVAILABLE',
                max_length=30,
            ),
        ),
        migrations.AddField(
            model_name='tenantintegrationentitlement',
            name='enabled_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='tenantintegrationentitlement',
            name='disabled_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='tenantintegrationentitlement',
            name='updated_at',
            field=models.DateTimeField(auto_now=True),
        ),
    ]
