"""
Sprint 9 Phase 9A — Tenant Storage, Privacy & Audit Schema Expansion (Additive Only)
Tables expanded:
  - files
  - processing_purposes
  - consent_records
  - privacy_requests
  - audit_events
"""

from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0005_iam_rbac_constraints'),
    ]

    operations = [
        # 1. files
        migrations.AddField(
            model_name='file',
            name='owner_type',
            field=models.CharField(default='TENANT_USER', max_length=100),
        ),
        migrations.AddField(
            model_name='file',
            name='owner_id',
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='file',
            name='file_name',
            field=models.CharField(default='', max_length=255),
        ),
        migrations.AddField(
            model_name='file',
            name='original_file_name',
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name='file',
            name='storage_provider',
            field=models.CharField(default='ZATA_S3', max_length=50),
        ),
        migrations.AddField(
            model_name='file',
            name='bucket_reference',
            field=models.CharField(default='zata-private-storage', max_length=150),
        ),

        # 2. processing_purposes
        migrations.AddField(
            model_name='processingpurpose',
            name='status',
            field=models.CharField(default='ACTIVE', max_length=30),
        ),
        migrations.AddField(
            model_name='processingpurpose',
            name='updated_at',
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),

        # 3. consent_records
        migrations.AddField(
            model_name='consentrecord',
            name='created_at',
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),

        # 4. privacy_requests
        migrations.AddField(
            model_name='privacyrequest',
            name='identity_verified_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='privacyrequest',
            name='assigned_to',
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='privacyrequest',
            name='received_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='privacyrequest',
            name='due_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='privacyrequest',
            name='resolution',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='privacyrequest',
            name='evidence',
            field=models.JSONField(blank=True, null=True),
        ),

        # 5. audit_events
        migrations.AddField(
            model_name='tenantauditevent',
            name='actor_role',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name='tenantauditevent',
            name='event_name',
            field=models.CharField(default='', max_length=150),
        ),
        migrations.AddField(
            model_name='tenantauditevent',
            name='reason',
            field=models.TextField(blank=True, null=True),
        ),
    ]
