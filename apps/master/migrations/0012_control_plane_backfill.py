"""
Sprint 9 Phase 9B — Master Control Plane Data Reconciliation & Backfill
Deterministic backfill for populated tables:
  - tenant_data_sources
  - tenant_data_hosting_policies
  - saas_plan_resource_limits
  - resource_metrics
  - tenant_resource_usage
"""

from django.db import migrations


def backfill_master_control_plane(apps, schema_editor):
    db_alias = schema_editor.connection.alias

    with schema_editor.connection.cursor() as cursor:
        # 1. Backfill tenant_data_sources
        cursor.execute("""
            UPDATE tenant_data_sources
            SET database_name = db_name,
                hosting_mode = source_type,
                database_engine = 'POSTGRESQL',
                provider = 'PLATFORM',
                network_mode = 'PRIVATE_NETWORK',
                ssl_mode = 'REQUIRE',
                secret_reference = db_password_secret_ref,
                schema_version = COALESCE(NULLIF(db_schema_version, ''), '1.0');
        """)

        # 2. Backfill tenant_data_hosting_policies
        cursor.execute("""
            UPDATE tenant_data_hosting_policies
            SET backup_owner = 'PLATFORM',
                restore_owner = 'PLATFORM',
                patching_owner = 'PLATFORM',
                monitoring_owner = 'PLATFORM',
                migration_owner = 'PLATFORM',
                encryption_owner = 'PLATFORM';
        """)

        # 3. Backfill saas_plan_resource_limits
        cursor.execute("""
            UPDATE saas_plan_resource_limits r
            SET is_unlimited = (r.limit_value = -1),
                warning_percent = r.soft_limit_pct::numeric(5,2),
                enforcement_mode = 'MONITOR_ONLY',
                created_at = p.created_at,
                updated_at = p.created_at
            FROM saas_plans p
            WHERE r.plan_id = p.id;
        """)

        # 4. Backfill resource_metrics
        cursor.execute("""
            UPDATE resource_metrics
            SET aggregation_period = 'REALTIME',
                status = CASE WHEN is_active THEN 'ACTIVE' ELSE 'INACTIVE' END,
                created_at = NOW(),
                updated_at = NOW();
        """)

        # 5. Backfill tenant_resource_usage
        cursor.execute("""
            UPDATE tenant_resource_usage
            SET usage_value = current_value::numeric(20,4),
                measured_at = last_calculated_at,
                created_at = last_calculated_at;
        """)


def reverse_backfill(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('master', '0011_control_plane_expand'),
    ]

    operations = [
        migrations.RunPython(backfill_master_control_plane, reverse_backfill),
    ]
