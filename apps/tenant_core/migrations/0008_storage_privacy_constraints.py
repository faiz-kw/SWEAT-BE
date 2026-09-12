"""
Sprint 9 Phase 9C — Tenant Storage, Privacy & Audit Constraint Hardening & Alterations
Alters types, lengths, defaults, nullability, and FKs on Tenant tables:
  - files
  - processing_purposes
  - consent_records
  - privacy_requests
  - audit_events
"""

from django.db import migrations


def apply_tenant_constraints_and_hardening(apps, schema_editor):
    cursor = schema_editor.connection.cursor()

    # 1. Alter column types (using explicit USING where appropriate)
    cursor.execute("ALTER TABLE files ALTER COLUMN object_key TYPE text;")

    # 2. Alter VARCHAR lengths
    cursor.execute("ALTER TABLE files ALTER COLUMN mime_type TYPE varchar(150);")
    cursor.execute("ALTER TABLE files ALTER COLUMN classification TYPE varchar(50);")
    cursor.execute("ALTER TABLE processing_purposes ALTER COLUMN code TYPE varchar(100);")
    cursor.execute("ALTER TABLE processing_purposes ALTER COLUMN notice_version TYPE varchar(50);")
    cursor.execute("ALTER TABLE consent_records ALTER COLUMN status TYPE varchar(30);")
    cursor.execute("ALTER TABLE consent_records ALTER COLUMN notice_version TYPE varchar(50);")
    cursor.execute("ALTER TABLE privacy_requests ALTER COLUMN request_type TYPE varchar(40);")
    cursor.execute("ALTER TABLE privacy_requests ALTER COLUMN status TYPE varchar(40);")
    cursor.execute("ALTER TABLE audit_events ALTER COLUMN actor_type TYPE varchar(30);")

    # 3. Set physical DB column defaults
    cursor.execute("ALTER TABLE processing_purposes ALTER COLUMN is_mandatory SET DEFAULT FALSE;")

    # 4. Set NOT NULL on canonical non-nullable fields
    not_null_statements = [
        # files (0 rows)
        "ALTER TABLE files ALTER COLUMN owner_type SET NOT NULL;",
        "ALTER TABLE files ALTER COLUMN file_name SET NOT NULL;",
        "ALTER TABLE files ALTER COLUMN storage_provider SET NOT NULL;",
        "ALTER TABLE files ALTER COLUMN bucket_reference SET NOT NULL;",

        # processing_purposes (0 rows)
        "ALTER TABLE processing_purposes ALTER COLUMN status SET NOT NULL;",
        "ALTER TABLE processing_purposes ALTER COLUMN updated_at SET NOT NULL;",

        # consent_records (0 rows)
        "ALTER TABLE consent_records ALTER COLUMN user_id SET NOT NULL;",
        "ALTER TABLE consent_records ALTER COLUMN created_at SET NOT NULL;",

        # privacy_requests (0 rows)
        "ALTER TABLE privacy_requests ALTER COLUMN user_id SET NOT NULL;",

        # audit_events (19 rows - event_name backfilled from action)
        "ALTER TABLE audit_events ALTER COLUMN event_name SET NOT NULL;",
    ]
    for stmt in not_null_statements:
        cursor.execute(stmt)

    # 5. Harden Tenant Foreign Keys to ON DELETE RESTRICT
    tenant_fks_to_harden = [
        ('consent_records', 'user_id', 'users', 'RESTRICT'),
        ('consent_records', 'purpose_id', 'processing_purposes', 'RESTRICT'),
        ('privacy_requests', 'user_id', 'users', 'RESTRICT'),
    ]

    for tbl, col, tgt_tbl, on_delete in tenant_fks_to_harden:
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

    cursor.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'fk_privacy_requests_assigned_to'
            ) THEN
                ALTER TABLE privacy_requests
                ADD CONSTRAINT fk_privacy_requests_assigned_to
                FOREIGN KEY (assigned_to) REFERENCES users(id) ON DELETE SET NULL;
            END IF;
        END $$;
    """)


def rollback_tenant_constraints(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0007_storage_privacy_backfill'),
    ]

    operations = [
        migrations.RunPython(apply_tenant_constraints_and_hardening, rollback_tenant_constraints),
    ]
