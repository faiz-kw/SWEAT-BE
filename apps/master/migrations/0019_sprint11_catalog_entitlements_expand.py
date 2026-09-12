"""
Sprint 11 — Workstream B: Product Module Catalog & Tenant Entitlements Schema Expansion
Alters column lengths and adds missing columns for:
- product_modules
- product_submodules
- tenant_permission_catalog
- tenant_modules
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('master', '0018_sprint10_reconcile_invoice_file_key'),
    ]

    operations = [
        # 1. Column length alterations
        migrations.AlterField(
            model_name='productmodule',
            name='code',
            field=models.CharField(help_text='e.g. CRM, BOOKINGS, NUTRITION, AI_VOICE', max_length=100, unique=True),
        ),
        migrations.AlterField(
            model_name='productmodule',
            name='name',
            field=models.CharField(max_length=150),
        ),
        migrations.AlterField(
            model_name='productsubmodule',
            name='code',
            field=models.CharField(max_length=100),
        ),
        migrations.AlterField(
            model_name='productsubmodule',
            name='name',
            field=models.CharField(max_length=150),
        ),

        # 2. product_modules missing columns
        migrations.AddField(
            model_name='productmodule',
            name='supports_branch_scope',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='productmodule',
            name='display_order',
            field=models.IntegerField(null=True),
        ),
        migrations.AddField(
            model_name='productmodule',
            name='status',
            field=models.CharField(max_length=30, null=True),
        ),
        migrations.AddField(
            model_name='productmodule',
            name='created_at',
            field=models.DateTimeField(null=True),
        ),
        migrations.AddField(
            model_name='productmodule',
            name='updated_at',
            field=models.DateTimeField(null=True),
        ),

        # 3. product_submodules missing columns
        migrations.AddField(
            model_name='productsubmodule',
            name='display_order',
            field=models.IntegerField(null=True),
        ),
        migrations.AddField(
            model_name='productsubmodule',
            name='status',
            field=models.CharField(max_length=30, null=True),
        ),
        migrations.AddField(
            model_name='productsubmodule',
            name='created_at',
            field=models.DateTimeField(null=True),
        ),
        migrations.AddField(
            model_name='productsubmodule',
            name='updated_at',
            field=models.DateTimeField(null=True),
        ),

        # 4. tenant_permission_catalog missing columns
        migrations.AddField(
            model_name='tenantpermissioncatalog',
            name='version',
            field=models.IntegerField(null=True),
        ),
        migrations.AddField(
            model_name='tenantpermissioncatalog',
            name='status',
            field=models.CharField(max_length=30, null=True),
        ),
        migrations.AddField(
            model_name='tenantpermissioncatalog',
            name='created_at',
            field=models.DateTimeField(null=True),
        ),
        migrations.AddField(
            model_name='tenantpermissioncatalog',
            name='updated_at',
            field=models.DateTimeField(null=True),
        ),

        # 5. tenant_modules missing columns
        migrations.AddField(
            model_name='tenantmodule',
            name='status',
            field=models.CharField(max_length=30, null=True),
        ),
        migrations.AddField(
            model_name='tenantmodule',
            name='configuration',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='tenantmodule',
            name='created_at',
            field=models.DateTimeField(null=True),
        ),
        migrations.AddField(
            model_name='tenantmodule',
            name='updated_at',
            field=models.DateTimeField(null=True),
        ),
    ]
