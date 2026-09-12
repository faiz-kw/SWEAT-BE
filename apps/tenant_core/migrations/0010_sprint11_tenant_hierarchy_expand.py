"""
Sprint 11 — Workstream A: Tenant Hierarchy Schema Expansion
Adds missing columns to `branches` and expands varchar lengths on `organization_settings`.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0009_alter_consentrecord_consent_method_and_more'),
    ]

    operations = [
        # 1. branches missing columns
        migrations.AddField(
            model_name='branch',
            name='address_line_1',
            field=models.CharField(blank=True, max_length=250, null=True),
        ),
        migrations.AddField(
            model_name='branch',
            name='address_line_2',
            field=models.CharField(blank=True, max_length=250, null=True),
        ),
        migrations.AddField(
            model_name='branch',
            name='latitude',
            field=models.DecimalField(blank=True, decimal_places=7, max_digits=10, null=True),
        ),
        migrations.AddField(
            model_name='branch',
            name='longitude',
            field=models.DecimalField(blank=True, decimal_places=7, max_digits=10, null=True),
        ),
        migrations.AddField(
            model_name='branch',
            name='timezone',
            field=models.CharField(default='Asia/Kolkata', max_length=100),
        ),

        # 2. organization_settings varchar length reconciliations
        migrations.AlterField(
            model_name='organizationsettings',
            name='default_timezone',
            field=models.CharField(default='Asia/Kolkata', max_length=100),
        ),
        migrations.AlterField(
            model_name='organizationsettings',
            name='language',
            field=models.CharField(default='en', max_length=20),
        ),
        migrations.AlterField(
            model_name='organizationsettings',
            name='date_format',
            field=models.CharField(default='YYYY-MM-DD', max_length=30),
        ),
        migrations.AlterField(
            model_name='organizationsettings',
            name='time_format',
            field=models.CharField(default='HH:mm', max_length=30),
        ),
    ]
