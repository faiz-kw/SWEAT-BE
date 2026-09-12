"""
Sprint 10 Phase 10C — Commercial Billing Constraint Hardening
Sets DB defaults, enforces NOT NULL, and hardens FKs to ON DELETE RESTRICT.
"""

from django.db import migrations


def apply_billing_constraints(apps, schema_editor):
    cursor = schema_editor.connection.cursor()

    # 1. DB Defaults
    defaults_sql = [
        "ALTER TABLE saas_plans ALTER COLUMN is_custom SET DEFAULT FALSE;",
        "ALTER TABLE saas_plans ALTER COLUMN is_featured SET DEFAULT FALSE;",
        "ALTER TABLE saas_plan_modules ALTER COLUMN is_included SET DEFAULT TRUE;",
        "ALTER TABLE tenant_subscriptions ALTER COLUMN autopay_enabled SET DEFAULT FALSE;",
        "ALTER TABLE tenant_billing_methods ALTER COLUMN is_default SET DEFAULT FALSE;",
        "ALTER TABLE tenant_billing_methods ALTER COLUMN status SET DEFAULT 'ACTIVE';",
    ]
    for stmt in defaults_sql:
        cursor.execute(stmt)

    # 2. NOT NULL constraints
    not_null_sql = [
        "ALTER TABLE saas_plan_prices ALTER COLUMN effective_from SET NOT NULL;",
        "ALTER TABLE saas_plan_prices ALTER COLUMN updated_at SET NOT NULL;",
        "ALTER TABLE saas_plan_modules ALTER COLUMN created_at SET NOT NULL;",
        "ALTER TABLE tenant_subscriptions ALTER COLUMN currency SET NOT NULL;",
        "ALTER TABLE tenant_subscriptions ALTER COLUMN billing_amount SET NOT NULL;",
        "ALTER TABLE tenant_subscriptions ALTER COLUMN started_at SET NOT NULL;",
        "ALTER TABLE tenant_subscriptions ALTER COLUMN current_period_start SET NOT NULL;",
        "ALTER TABLE tenant_subscriptions ALTER COLUMN current_period_end SET NOT NULL;",
        "ALTER TABLE subscription_invoices ALTER COLUMN total_amount SET NOT NULL;",
        "ALTER TABLE subscription_invoices ALTER COLUMN billing_period_start SET NOT NULL;",
        "ALTER TABLE subscription_invoices ALTER COLUMN billing_period_end SET NOT NULL;",
        "ALTER TABLE subscription_dunning_events ALTER COLUMN tenant_id SET NOT NULL;",
    ]
    for stmt in not_null_sql:
        cursor.execute(stmt)

    # 3. Harden Master Billing Foreign Keys to ON DELETE RESTRICT
    fks_to_harden = [
        ('saas_plan_prices', 'plan_id', 'saas_plans', 'RESTRICT'),
        ('saas_plan_modules', 'plan_id', 'saas_plans', 'RESTRICT'),
        ('saas_plan_modules', 'module_id', 'product_modules', 'RESTRICT'),
        ('tenant_subscriptions', 'tenant_id', 'tenants', 'RESTRICT'),
        ('tenant_subscriptions', 'plan_id', 'saas_plans', 'RESTRICT'),
        ('tenant_billing_methods', 'tenant_id', 'tenants', 'RESTRICT'),
        ('subscription_invoices', 'tenant_id', 'tenants', 'RESTRICT'),
        ('subscription_invoices', 'subscription_id', 'tenant_subscriptions', 'RESTRICT'),
        ('subscription_payments', 'tenant_id', 'tenants', 'RESTRICT'),
        ('subscription_payments', 'invoice_id', 'subscription_invoices', 'RESTRICT'),
        ('subscription_dunning_events', 'tenant_id', 'tenants', 'RESTRICT'),
        ('subscription_dunning_events', 'subscription_id', 'tenant_subscriptions', 'RESTRICT'),
    ]

    for tbl, col, tgt_tbl, on_delete in fks_to_harden:
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


def rollback_constraints(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('master', '0016_sprint10_commercial_billing_backfill'),
    ]

    operations = [
        migrations.RunPython(apply_billing_constraints, rollback_constraints),
    ]
