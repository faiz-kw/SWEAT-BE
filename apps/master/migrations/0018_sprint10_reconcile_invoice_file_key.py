"""
Sprint 10 Phase 10C Reconciliation — Reconcile subscription_invoices.invoice_file_key.
Canonical Specification: TEXT | NULL | Object storage reference
Resolves Class D architectural storage violation by aligning physical PostgreSQL column to text NULL.
"""

from django.db import migrations, models


def alter_invoice_file_key_sql(apps, schema_editor):
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        cursor.execute("UPDATE subscription_invoices SET invoice_file_key = NULL WHERE invoice_file_key = '';")
        cursor.execute("ALTER TABLE subscription_invoices ALTER COLUMN invoice_file_key TYPE text;")
        cursor.execute("ALTER TABLE subscription_invoices ALTER COLUMN invoice_file_key DROP NOT NULL;")
        cursor.execute("ALTER TABLE subscription_invoices ALTER COLUMN invoice_file_key DROP DEFAULT;")


def rollback_sql(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('master', '0017_sprint10_commercial_billing_constraints'),
    ]

    operations = [
        migrations.AlterField(
            model_name='subscriptioninvoice',
            name='invoice_file_key',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.RunPython(alter_invoice_file_key_sql, rollback_sql),
    ]
