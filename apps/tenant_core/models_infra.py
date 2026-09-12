"""
Dedicated Tenant DB — Infrastructure Models (3 Tables)
Tables: files, integrations, legacy_entity_map
"""

import uuid
from django.db import models
from django.utils import timezone
from .models_users import TenantUser


class File(models.Model):
    """
    Metadata for all files stored in Zata.ai S3-compatible object storage.
    Actual files are stored in S3 — only metadata lives in PostgreSQL.
    Object keys are namespaced per tenant to prevent cross-tenant access.
    Frozen S3 Key Format: tenants/{tenant_uuid}/files/{file_uuid}
    """
    CLASSIFICATION = [
        ('PUBLIC', 'Public'),
        ('INTERNAL', 'Internal'),
        ('CONFIDENTIAL', 'Confidential'),
        ('RESTRICTED', 'Restricted'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Canonical metadata fields
    owner_type = models.CharField(max_length=100, default='TENANT_USER')
    owner_id = models.UUIDField(null=True, blank=True)
    file_name = models.CharField(max_length=255, default='')
    original_file_name = models.CharField(max_length=255, blank=True, null=True)
    storage_provider = models.CharField(max_length=50, default='ZATA_S3')
    bucket_reference = models.CharField(max_length=150, default='zata-private-storage')
    object_key = models.TextField(unique=True)
    mime_type = models.CharField(max_length=150, blank=True, default='')
    file_size = models.BigIntegerField(help_text='File size in bytes')
    checksum = models.CharField(max_length=128, help_text='SHA-256 or MD5 integrity hash')
    classification = models.CharField(max_length=50, choices=CLASSIFICATION, default='INTERNAL')
    uploaded_by = models.ForeignKey(TenantUser, on_delete=models.SET_NULL, null=True, blank=True)
    uploaded_at = models.DateTimeField(default=timezone.now)

    # Legacy fields maintained for backward compatibility
    bucket_name = models.CharField(max_length=255, default='zata-private-storage')
    original_filename = models.CharField(max_length=500, default='')
    entity_type = models.CharField(max_length=100, blank=True, default='', help_text='e.g. TenantUser, Lead, Member')
    entity_id = models.UUIDField(null=True, blank=True)
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'files'
        ordering = ['-uploaded_at']

    def __str__(self):
        return f"{self.file_name or self.original_filename} ({self.mime_type}) [{self.classification}]"

    def save(self, *args, **kwargs):
        if not self.file_name and self.original_filename:
            self.file_name = self.original_filename
        elif not self.original_filename and self.original_file_name:
            self.original_filename = self.original_file_name
        elif not self.original_filename and self.file_name:
            self.original_filename = self.file_name
        if not self.original_file_name and self.original_filename:
            self.original_file_name = self.original_filename
        if not self.bucket_reference and self.bucket_name:
            self.bucket_reference = self.bucket_name
        elif not self.bucket_name and self.bucket_reference:
            self.bucket_name = self.bucket_reference
        if not self.owner_type and self.entity_type:
            self.owner_type = self.entity_type
        elif not self.entity_type and self.owner_type:
            self.entity_type = self.owner_type
        if not self.owner_id and self.entity_id:
            self.owner_id = self.entity_id
        elif not self.entity_id and self.owner_id:
            self.entity_id = self.owner_id
        super().save(*args, **kwargs)


class Integration(models.Model):
    """
    Tenant-side configuration of enabled external integrations.
    Each row represents a configured integration provider (e.g. Razorpay, Gupshup).
    Secret credentials are stored in Vault — only the reference path is stored here.
    """
    INTEGRATION_TYPE = [
        ('PAYMENT', 'Payment Gateway'),
        ('CALLING', 'Voice Calling / IVR'),
        ('WHATSAPP', 'WhatsApp Messaging'),
        ('EMAIL', 'Email / SMTP'),
        ('LEADS', 'Lead Generation'),
        ('ACCESS_CONTROL', 'Access Control / Biometrics'),
        ('ACCOUNTING', 'Accounting / ERP'),
        ('ANALYTICS', 'Analytics'),
        ('OTHER', 'Other'),
    ]
    STATUS = [('ACTIVE', 'Active'), ('INACTIVE', 'Inactive'), ('ERROR', 'Error')]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    integration_type = models.CharField(max_length=50, choices=INTEGRATION_TYPE)
    provider = models.CharField(max_length=100, help_text='e.g. Razorpay, TeleCMI, Gupshup, SES, Meta, Timewatch')
    # Vault/Secret Manager path — actual credentials are never stored in DB
    secret_reference = models.CharField(max_length=255, blank=True, default='', help_text='Vault path to credentials')
    # Non-secret configuration (webhook URLs, API modes, etc.)
    configuration = models.JSONField(default=dict, help_text='Non-sensitive configuration JSON')
    status = models.CharField(max_length=30, choices=STATUS, default='INACTIVE')
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'integrations'
        unique_together = ('integration_type', 'provider')
        ordering = ['integration_type', 'provider']

    def __str__(self):
        return f"{self.provider} ({self.integration_type}) [{self.status}]"


class LegacyEntityMap(models.Model):
    """
    Migration traceability map — traces every migrated legacy record to its new entity.
    Created during data migration from the legacy MySQL system.
    Allows reverting or cross-referencing legacy data indefinitely.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_system = models.CharField(max_length=100, help_text='e.g. legacy_mysql, old_crm')
    legacy_entity_type = models.CharField(max_length=100, help_text='Legacy table/entity name')
    legacy_entity_id = models.CharField(max_length=255, help_text='Original primary key in legacy system')
    new_entity_type = models.CharField(max_length=100, help_text='New model name e.g. TenantUser')
    new_entity_id = models.UUIDField(help_text='UUID of new entity')
    migration_batch_id = models.UUIDField(help_text='Groups records from the same migration run')
    source_checksum = models.CharField(max_length=128, blank=True, default='', help_text='Fingerprint of source data at migration time')
    migrated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'legacy_entity_map'
        unique_together = ('source_system', 'legacy_entity_type', 'legacy_entity_id')
        ordering = ['-migrated_at']

    def __str__(self):
        return f"{self.source_system}/{self.legacy_entity_type}/{self.legacy_entity_id} → {self.new_entity_type}/{self.new_entity_id}"
