"""
Sprint 9 Phase 9A — Master Control Plane Schema Expansion (Additive Only)
Tables expanded:
  - tenant_data_sources
  - tenant_data_hosting_policies
  - resource_metrics
  - saas_plan_resource_limits
  - tenant_resource_limits
  - tenant_resource_usage
  - tenant_usage_alerts
  - platform_audit_events
  - platform_settings
"""

from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('master', '0010_platform_iam_constraints'),
    ]

    operations = [
        # 1. tenant_data_sources
        migrations.AddField(
            model_name='tenantdatasource',
            name='hosting_mode',
            field=models.CharField(default='PLATFORM_MANAGED', max_length=30),
        ),
        migrations.AddField(
            model_name='tenantdatasource',
            name='database_engine',
            field=models.CharField(default='POSTGRESQL', max_length=30),
        ),
        migrations.AddField(
            model_name='tenantdatasource',
            name='database_name',
            field=models.CharField(blank=True, default='', max_length=150),
        ),
        migrations.AddField(
            model_name='tenantdatasource',
            name='provider',
            field=models.CharField(default='PLATFORM', max_length=50),
        ),
        migrations.AddField(
            model_name='tenantdatasource',
            name='region',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name='tenantdatasource',
            name='network_mode',
            field=models.CharField(default='PRIVATE_NETWORK', max_length=40),
        ),
        migrations.AddField(
            model_name='tenantdatasource',
            name='ssl_mode',
            field=models.CharField(default='REQUIRE', max_length=30),
        ),
        migrations.AddField(
            model_name='tenantdatasource',
            name='secret_reference',
            field=models.CharField(blank=True, default='', max_length=255, null=True),
        ),
        migrations.AddField(
            model_name='tenantdatasource',
            name='schema_version',
            field=models.CharField(default='1.0', max_length=50),
        ),

        # 2. tenant_data_hosting_policies
        migrations.AddField(
            model_name='tenantdatahostingpolicy',
            name='backup_owner',
            field=models.CharField(default='PLATFORM', max_length=30),
        ),
        migrations.AddField(
            model_name='tenantdatahostingpolicy',
            name='restore_owner',
            field=models.CharField(default='PLATFORM', max_length=30),
        ),
        migrations.AddField(
            model_name='tenantdatahostingpolicy',
            name='patching_owner',
            field=models.CharField(default='PLATFORM', max_length=30),
        ),
        migrations.AddField(
            model_name='tenantdatahostingpolicy',
            name='monitoring_owner',
            field=models.CharField(default='PLATFORM', max_length=30),
        ),
        migrations.AddField(
            model_name='tenantdatahostingpolicy',
            name='migration_owner',
            field=models.CharField(default='PLATFORM', max_length=30),
        ),
        migrations.AddField(
            model_name='tenantdatahostingpolicy',
            name='encryption_owner',
            field=models.CharField(default='PLATFORM', max_length=30),
        ),
        migrations.AddField(
            model_name='tenantdatahostingpolicy',
            name='availability_sla',
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
        migrations.AddField(
            model_name='tenantdatahostingpolicy',
            name='backup_policy_reference',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='tenantdatahostingpolicy',
            name='dr_policy_reference',
            field=models.TextField(blank=True, null=True),
        ),

        # 3. resource_metrics
        migrations.AddField(
            model_name='resourcemetric',
            name='aggregation_period',
            field=models.CharField(default='REALTIME', max_length=30),
        ),
        migrations.AddField(
            model_name='resourcemetric',
            name='status',
            field=models.CharField(default='ACTIVE', max_length=30),
        ),
        migrations.AddField(
            model_name='resourcemetric',
            name='created_at',
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.AddField(
            model_name='resourcemetric',
            name='updated_at',
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),

        # 4. saas_plan_resource_limits
        migrations.AddField(
            model_name='saasplanresourcelimit',
            name='is_unlimited',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='saasplanresourcelimit',
            name='warning_percent',
            field=models.DecimalField(decimal_places=2, default=80.0, max_digits=5),
        ),
        migrations.AddField(
            model_name='saasplanresourcelimit',
            name='enforcement_mode',
            field=models.CharField(default='MONITOR_ONLY', max_length=30),
        ),
        migrations.AddField(
            model_name='saasplanresourcelimit',
            name='created_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='saasplanresourcelimit',
            name='updated_at',
            field=models.DateTimeField(blank=True, null=True),
        ),

        # 5. tenant_resource_limits
        migrations.AddField(
            model_name='tenantresourcelimit',
            name='is_unlimited',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='tenantresourcelimit',
            name='warning_threshold_percent',
            field=models.DecimalField(decimal_places=2, default=80.0, max_digits=5),
        ),
        migrations.AddField(
            model_name='tenantresourcelimit',
            name='effective_from',
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.AddField(
            model_name='tenantresourcelimit',
            name='effective_until',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='tenantresourcelimit',
            name='override_reason',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='tenantresourcelimit',
            name='created_at',
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),

        # 6. tenant_resource_usage
        migrations.AddField(
            model_name='tenantresourceusage',
            name='period_start',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='tenantresourceusage',
            name='period_end',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='tenantresourceusage',
            name='usage_value',
            field=models.DecimalField(decimal_places=4, default=0.0, max_digits=20),
        ),
        migrations.AddField(
            model_name='tenantresourceusage',
            name='measured_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='tenantresourceusage',
            name='created_at',
            field=models.DateTimeField(blank=True, null=True),
        ),

        # 7. tenant_usage_alerts
        migrations.AddField(
            model_name='tenantusagealert',
            name='usage_value',
            field=models.DecimalField(decimal_places=4, default=0.0, max_digits=20),
        ),
        migrations.AddField(
            model_name='tenantusagealert',
            name='limit_value',
            field=models.DecimalField(decimal_places=4, default=0.0, max_digits=20),
        ),
        migrations.AddField(
            model_name='tenantusagealert',
            name='threshold_percent',
            field=models.DecimalField(decimal_places=2, default=80.0, max_digits=5),
        ),

        # 8. platform_audit_events
        migrations.AddField(
            model_name='platformauditevent',
            name='platform_user_id',
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='platformauditevent',
            name='tenant_id',
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='platformauditevent',
            name='event_name',
            field=models.CharField(default='SYSTEM_EVENT', max_length=150),
        ),
        migrations.AddField(
            model_name='platformauditevent',
            name='before_data',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='platformauditevent',
            name='after_data',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='platformauditevent',
            name='reason',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='platformauditevent',
            name='source_application',
            field=models.CharField(default='control_plane', max_length=100),
        ),
        migrations.AddField(
            model_name='platformauditevent',
            name='request_id',
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='platformauditevent',
            name='correlation_id',
            field=models.UUIDField(blank=True, null=True),
        ),

        # 9. platform_settings
        migrations.AddField(
            model_name='platformsetting',
            name='category',
            field=models.CharField(default='GENERAL', max_length=100),
        ),
        migrations.AddField(
            model_name='platformsetting',
            name='is_active',
            field=models.BooleanField(default=True),
        ),
    ]
