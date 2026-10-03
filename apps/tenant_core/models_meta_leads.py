"""Tenant-owned Meta lead connections, mappings, and durable import history.

Zero Meta credentials belong in plaintext. OAuth tokens are Fernet-encrypted.
"""
import uuid
from django.db import models


class MetaConnection(models.Model):
    STATUSES = [
        ('NOT_CONNECTED', 'Not Connected'),
        ('CONNECTED', 'Connected'),
        ('TOKEN_EXPIRED', 'Token Expired'),
        ('REVOKED', 'Revoked'),
        ('DISCONNECTED', 'Disconnected'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey('tenant_core.Organization', on_delete=models.PROTECT)
    status = models.CharField(max_length=30, choices=STATUSES, default='NOT_CONNECTED')
    meta_user_id = models.CharField(max_length=100, blank=True, default='')
    meta_user_name = models.CharField(max_length=200, blank=True, default='')
    encrypted_user_access_token = models.TextField(blank=True, default='')
    token_expires_at = models.DateTimeField(null=True, blank=True)
    scopes = models.JSONField(default=list, blank=True)
    last_connected_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'crm_meta_connections'
        constraints = [models.UniqueConstraint(fields=['organization'], name='uq_meta_connection_org')]

    def __str__(self):
        return f"MetaConnection ({self.status}) for Org {self.organization_id}"


class MetaPageConnection(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey('tenant_core.Organization', on_delete=models.PROTECT)
    connection = models.ForeignKey(MetaConnection, on_delete=models.CASCADE, related_name='pages')
    page_id = models.CharField(max_length=100)
    page_name = models.CharField(max_length=255)
    encrypted_page_access_token = models.TextField(blank=True, default='')
    is_subscribed_to_webhooks = models.BooleanField(default=False)
    subscribed_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'crm_meta_page_connections'
        constraints = [models.UniqueConstraint(fields=['organization', 'page_id'], name='uq_meta_page_connection_org_page')]
        ordering = ['page_name', 'page_id']

    def __str__(self):
        return f"{self.page_name} ({self.page_id})"


class MetaLeadMapping(models.Model):
    UNMATCHED_BRANCH_POLICIES = [
        ('HOLD', 'Hold for review (Needs branch assignment)'),
        ('FALLBACK_BRANCH', 'Route to fallback branch'),
    ]
    ASSIGNMENT_MODES = [
        ('TENANT_POLICY', 'Follow tenant assignment policy'),
        ('SPECIFIC_USER', 'Assign to specific staff member'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey('tenant_core.Organization', on_delete=models.PROTECT)
    page_id = models.CharField(max_length=100)
    form_id = models.CharField(max_length=100)
    name = models.CharField(max_length=200)
    is_active = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    field_mappings = models.JSONField(default=dict)
    field_defaults = models.JSONField(default=dict, blank=True)
    branch_mode = models.CharField(max_length=10, choices=[('FIXED', 'Fixed branch'), ('ANSWER', 'Form answer')], default='FIXED')
    branch = models.ForeignKey('tenant_core.Branch', null=True, blank=True, on_delete=models.PROTECT)
    branch_field = models.CharField(max_length=100, blank=True, default='')
    branch_answers = models.JSONField(default=dict)
    unmatched_branch_policy = models.CharField(max_length=20, choices=UNMATCHED_BRANCH_POLICIES, default='HOLD')
    fallback_branch = models.ForeignKey('tenant_core.Branch', null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    lead_source = models.ForeignKey('tenant_core.LeadSource', on_delete=models.PROTECT)
    initial_stage = models.CharField(max_length=40, default='NEW_LEAD')
    repeat_policy = models.CharField(max_length=20, choices=[('REVIEW', 'Hold for review'), ('CREATE_NEW', 'Create a new enquiry')], default='REVIEW')
    assignment_mode = models.CharField(max_length=20, choices=ASSIGNMENT_MODES, default='TENANT_POLICY')
    assigned_sales_user = models.ForeignKey('tenant_core.TenantUser', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    create_followup_task = models.BooleanField(default=False)
    followup_task_type = models.CharField(max_length=30, default='CALL')
    followup_due_hours = models.PositiveIntegerField(default=24)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'crm_meta_lead_mappings'
        ordering = ['name', 'id']
        constraints = [models.UniqueConstraint(fields=['organization', 'page_id', 'form_id'], name='uq_meta_mapping_org_page_form')]


class MetaLeadImport(models.Model):
    STATUSES = [
        ('PENDING', 'Pending'),
        ('IMPORTED', 'Imported'),
        ('NEEDS_MAPPING', 'Needs mapping'),
        ('NEEDS_ASSIGNMENT', 'Needs branch assignment'),
        ('NEEDS_REVIEW', 'Repeat enquiry review'),
        ('FAILED', 'Failed'),
    ]
    MODES = [
        ('SIMULATOR', 'Simulator'),
        ('LIVE', 'Live Webhook'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey('tenant_core.Organization', on_delete=models.PROTECT)
    mode = models.CharField(max_length=20, choices=MODES, default='SIMULATOR')
    page_id = models.CharField(max_length=100)
    form_id = models.CharField(max_length=100)
    external_lead_id = models.CharField(max_length=100)
    payload_hash = models.CharField(max_length=64)
    field_data = models.JSONField(default=list)
    mapping_snapshot = models.JSONField(default=dict)
    mapping_version = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=30, choices=STATUSES, default='PENDING')
    lead = models.ForeignKey('tenant_core.Lead', on_delete=models.PROTECT, null=True, blank=True)
    attempt_count = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=50, blank=True, default='')
    error_message = models.CharField(max_length=500, blank=True, default='')

    # Campaign attribution captured from Meta Graph API
    campaign_id = models.CharField(max_length=100, blank=True, default='')
    campaign_name = models.CharField(max_length=255, blank=True, default='')
    adset_id = models.CharField(max_length=100, blank=True, default='')
    adset_name = models.CharField(max_length=255, blank=True, default='')
    ad_id = models.CharField(max_length=100, blank=True, default='')
    ad_name = models.CharField(max_length=255, blank=True, default='')
    is_organic = models.BooleanField(default=False)

    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'crm_meta_lead_imports'
        ordering = ['-received_at', '-id']
        constraints = [models.UniqueConstraint(fields=['organization', 'mode', 'external_lead_id'], name='uq_meta_import_org_mode_ext')]
        indexes = [models.Index(fields=['organization', 'status', '-received_at'], name='meta_import_org_status_idx')]
