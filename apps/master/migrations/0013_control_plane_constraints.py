"""
Sprint 9 Phase 9C — Master Control Plane Constraint Hardening & Alterations
Alters types, lengths, defaults, nullability, and FKs on Master control plane tables.
"""

from django.db import migrations


def apply_master_constraints_and_hardening(apps, schema_editor):
    cursor = schema_editor.connection.cursor()

    # 1. Alter column types (using explicit USING where appropriate)
    cursor.execute("ALTER TABLE saas_plan_resource_limits ALTER COLUMN limit_value TYPE numeric(20,4) USING limit_value::numeric(20,4);")
    cursor.execute("ALTER TABLE tenant_resource_limits ALTER COLUMN limit_value TYPE numeric(20,4) USING limit_value::numeric(20,4);")
    cursor.execute("ALTER TABLE platform_audit_events ALTER COLUMN resource_id TYPE uuid USING NULLIF(resource_id, '')::uuid;")
    cursor.execute("ALTER TABLE platform_settings ALTER COLUMN value TYPE jsonb USING value::jsonb;")

    # 2. Alter VARCHAR lengths
    cursor.execute("ALTER TABLE tenant_data_sources ALTER COLUMN status TYPE varchar(30);")
    cursor.execute("ALTER TABLE resource_metrics ALTER COLUMN code TYPE varchar(100);")
    cursor.execute("ALTER TABLE resource_metrics ALTER COLUMN name TYPE varchar(150);")
    cursor.execute("ALTER TABLE tenant_usage_alerts ALTER COLUMN status TYPE varchar(30);")
    cursor.execute("ALTER TABLE platform_settings ALTER COLUMN key TYPE varchar(150);")

    # 3. Set physical DB column defaults
    cursor.execute("ALTER TABLE resource_metrics ALTER COLUMN is_billable SET DEFAULT FALSE;")
    cursor.execute("ALTER TABLE tenant_resource_usage ALTER COLUMN is_platform_billable SET DEFAULT TRUE;")
    cursor.execute("ALTER TABLE saas_plan_resource_limits ALTER COLUMN is_unlimited SET DEFAULT FALSE;")
    cursor.execute("ALTER TABLE saas_plan_resource_limits ALTER COLUMN warning_percent SET DEFAULT 80.00;")
    cursor.execute("ALTER TABLE tenant_resource_limits ALTER COLUMN is_unlimited SET DEFAULT FALSE;")
    cursor.execute("ALTER TABLE tenant_resource_limits ALTER COLUMN warning_threshold_percent SET DEFAULT 80.00;")
    cursor.execute("ALTER TABLE platform_settings ALTER COLUMN is_active SET DEFAULT TRUE;")

    # 4. Set NOT NULL on canonical non-nullable fields
    not_null_statements = [
        # tenant_data_sources (secret_reference intentionally left nullable per Sprint 9 baseline)
        "ALTER TABLE tenant_data_sources ALTER COLUMN hosting_mode SET NOT NULL;",
        "ALTER TABLE tenant_data_sources ALTER COLUMN database_engine SET NOT NULL;",
        "ALTER TABLE tenant_data_sources ALTER COLUMN database_name SET NOT NULL;",
        "ALTER TABLE tenant_data_sources ALTER COLUMN provider SET NOT NULL;",
        "ALTER TABLE tenant_data_sources ALTER COLUMN network_mode SET NOT NULL;",
        "ALTER TABLE tenant_data_sources ALTER COLUMN ssl_mode SET NOT NULL;",
        "ALTER TABLE tenant_data_sources ALTER COLUMN schema_version SET NOT NULL;",

        # tenant_data_hosting_policies
        "ALTER TABLE tenant_data_hosting_policies ALTER COLUMN backup_owner SET NOT NULL;",
        "ALTER TABLE tenant_data_hosting_policies ALTER COLUMN restore_owner SET NOT NULL;",
        "ALTER TABLE tenant_data_hosting_policies ALTER COLUMN patching_owner SET NOT NULL;",
        "ALTER TABLE tenant_data_hosting_policies ALTER COLUMN monitoring_owner SET NOT NULL;",
        "ALTER TABLE tenant_data_hosting_policies ALTER COLUMN migration_owner SET NOT NULL;",
        "ALTER TABLE tenant_data_hosting_policies ALTER COLUMN encryption_owner SET NOT NULL;",

        # saas_plan_resource_limits
        "ALTER TABLE saas_plan_resource_limits ALTER COLUMN is_unlimited SET NOT NULL;",
        "ALTER TABLE saas_plan_resource_limits ALTER COLUMN warning_percent SET NOT NULL;",
        "ALTER TABLE saas_plan_resource_limits ALTER COLUMN enforcement_mode SET NOT NULL;",
        "ALTER TABLE saas_plan_resource_limits ALTER COLUMN created_at SET NOT NULL;",
        "ALTER TABLE saas_plan_resource_limits ALTER COLUMN updated_at SET NOT NULL;",

        # tenant_resource_limits (0 rows)
        "ALTER TABLE tenant_resource_limits ALTER COLUMN is_unlimited SET NOT NULL;",
        "ALTER TABLE tenant_resource_limits ALTER COLUMN warning_threshold_percent SET NOT NULL;",
        "ALTER TABLE tenant_resource_limits ALTER COLUMN created_at SET NOT NULL;",

        # resource_metrics
        "ALTER TABLE resource_metrics ALTER COLUMN aggregation_period SET NOT NULL;",
        "ALTER TABLE resource_metrics ALTER COLUMN status SET NOT NULL;",
        "ALTER TABLE resource_metrics ALTER COLUMN created_at SET NOT NULL;",
        "ALTER TABLE resource_metrics ALTER COLUMN updated_at SET NOT NULL;",

        # tenant_resource_usage (period_start / period_end intentionally left nullable per Sprint 9 baseline)
        "ALTER TABLE tenant_resource_usage ALTER COLUMN usage_value SET NOT NULL;",
        "ALTER TABLE tenant_resource_usage ALTER COLUMN measured_at SET NOT NULL;",
        "ALTER TABLE tenant_resource_usage ALTER COLUMN created_at SET NOT NULL;",

        # tenant_usage_alerts (0 rows)
        "ALTER TABLE tenant_usage_alerts ALTER COLUMN usage_value SET NOT NULL;",
        "ALTER TABLE tenant_usage_alerts ALTER COLUMN limit_value SET NOT NULL;",
        "ALTER TABLE tenant_usage_alerts ALTER COLUMN threshold_percent SET NOT NULL;",

        # platform_audit_events (0 rows)
        "ALTER TABLE platform_audit_events ALTER COLUMN event_name SET NOT NULL;",
        "ALTER TABLE platform_audit_events ALTER COLUMN source_application SET NOT NULL;",

        # platform_settings (0 rows)
        "ALTER TABLE platform_settings ALTER COLUMN category SET NOT NULL;",
        "ALTER TABLE platform_settings ALTER COLUMN is_active SET NOT NULL;",
    ]
    for stmt in not_null_statements:
        cursor.execute(stmt)

    # 5. Harden Master Foreign Keys to ON DELETE RESTRICT
    # List of (table_name, column_name, target_table, on_delete)
    master_fks_to_harden = [
        ('tenant_data_sources', 'tenant_id', 'tenants', 'RESTRICT'),
        ('tenant_data_hosting_policies', 'tenant_id', 'tenants', 'RESTRICT'),
        ('saas_plan_resource_limits', 'plan_id', 'saas_plans', 'RESTRICT'),
        ('saas_plan_resource_limits', 'metric_id', 'resource_metrics', 'RESTRICT'),
        ('tenant_resource_limits', 'tenant_id', 'tenants', 'RESTRICT'),
        ('tenant_resource_limits', 'metric_id', 'resource_metrics', 'RESTRICT'),
        ('tenant_resource_usage', 'tenant_id', 'tenants', 'RESTRICT'),
        ('tenant_resource_usage', 'metric_id', 'resource_metrics', 'RESTRICT'),
        ('tenant_usage_alerts', 'tenant_id', 'tenants', 'RESTRICT'),
        ('tenant_usage_alerts', 'metric_id', 'resource_metrics', 'RESTRICT'),
    ]

    for tbl, col, tgt_tbl, on_delete in master_fks_to_harden:
        cursor.execute(f"""
            SELECT tc.constraint_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name
            WHERE tc.table_name = '{tbl}' AND kcu.column_name = '{col}' AND tc.constraint_type = 'FOREIGN KEY';
        """)
        rows = cursor.fetchall()
        for (cname,) in rows:
            cursor.execute(f"ALTER TABLE {tbl} DROP CONSTRAINT {cname};")
            cursor.execute(f"ALTER TABLE {tbl} ADD CONSTRAINT {cname} FOREIGN KEY ({col}) REFERENCES {tgt_tbl}(id) ON DELETE {on_delete};")


def rollback_master_constraints(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('master', '0012_control_plane_backfill'),
    ]

    operations = [
        migrations.RunPython(apply_master_constraints_and_hardening, rollback_master_constraints),
    ]
