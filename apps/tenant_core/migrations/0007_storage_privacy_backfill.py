"""
Sprint 9 Phase 9B — Tenant Storage, Privacy & Audit Data Reconciliation & Backfill
1. Drops NOT NULL constraint on request_id and correlation_id in audit_events (per canonical spec where request_id and correlation_id are nullable UUIDs).
2. Sanitizes empty string UUIDs in audit_events to NULL so they can be cast to UUID.
3. Backfills audit_events.event_name = action where empty.
"""

from django.db import migrations


def backfill_tenant_storage_privacy_audit(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        # 1. Drop NOT NULL on request_id and correlation_id (canonical spec allows NULL)
        cursor.execute("ALTER TABLE audit_events ALTER COLUMN request_id DROP NOT NULL;")
        cursor.execute("ALTER TABLE audit_events ALTER COLUMN correlation_id DROP NOT NULL;")

        # 2. Sanitize empty strings in audit_events for UUID columns
        cursor.execute("""
            UPDATE audit_events
            SET request_id = NULL
            WHERE request_id = '';
        """)
        cursor.execute("""
            UPDATE audit_events
            SET correlation_id = NULL
            WHERE correlation_id = '';
        """)
        cursor.execute("""
            UPDATE audit_events
            SET event_name = action
            WHERE event_name = '' OR event_name IS NULL;
        """)


def reverse_backfill(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0006_storage_privacy_expand'),
    ]

    operations = [
        migrations.RunPython(backfill_tenant_storage_privacy_audit, reverse_backfill),
    ]
