"""
Dedicated Tenant DB — Layer 2 Module N: Audit, Reliability & Outbox Models
Tables:
  - business_audit_events
  - idempotency_records
  - domain_outbox_events
  - reason_codes
"""

import uuid
from django.db import models
from django.utils import timezone
from .models_org import Organization, Branch
from .models_users import TenantUser


class BusinessAuditEvent(models.Model):
    """
    Central append-only business audit trail answering who did what, when, from where, and why.
    """
    ACTOR_TYPE_CHOICES = [
        ('USER', 'User'),
        ('EMPLOYEE', 'Employee'),
        ('MEMBER', 'Member'),
        ('SYSTEM', 'System'),
        ('API', 'API'),
        ('INTEGRATION', 'Integration'),
    ]

    SOURCE_CHANNEL_CHOICES = [
        ('ADMIN_PANEL', 'Admin Panel'),
        ('WEB', 'Web'),
        ('MOBILE_APP', 'Mobile App'),
        ('FRONT_DESK', 'Front Desk'),
        ('TRAINER_PANEL', 'Trainer Panel'),
        ('SALES_PANEL', 'Sales Panel'),
        ('API', 'API'),
        ('SYSTEM', 'System'),
        ('INTEGRATION', 'Integration'),
        ('MIGRATION', 'Migration'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name='business_audit_events')
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='business_audit_events')
    actor_type = models.CharField(max_length=20, choices=ACTOR_TYPE_CHOICES, default='USER')
    actor_user = models.ForeignKey(TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_actions')
    actor_employee_profile_id = models.UUIDField(null=True, blank=True, help_text='FK to employee_profiles once created')
    source_channel = models.CharField(max_length=30, choices=SOURCE_CHANNEL_CHOICES, default='ADMIN_PANEL')
    module = models.CharField(max_length=50)
    action_code = models.CharField(max_length=100)
    entity_type = models.CharField(max_length=100)
    entity_id = models.UUIDField(null=True, blank=True)
    parent_entity_type = models.CharField(max_length=100, null=True, blank=True)
    parent_entity_id = models.UUIDField(null=True, blank=True)
    event_description = models.TextField(null=True, blank=True)
    before_data = models.JSONField(null=True, blank=True)
    after_data = models.JSONField(null=True, blank=True)
    metadata = models.JSONField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(null=True, blank=True)
    request_id = models.UUIDField(null=True, blank=True)
    session_id = models.CharField(max_length=255, null=True, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'business_audit_events'
        ordering = ['-occurred_at']
        indexes = [
            models.Index(fields=['organization', '-occurred_at'], name='biz_audit_org_occurred_idx'),
            models.Index(fields=['entity_type', 'entity_id', 'occurred_at'], name='biz_audit_entity_idx'),
            models.Index(fields=['actor_user', 'occurred_at'], name='biz_audit_actor_idx'),
            models.Index(fields=['request_id'], name='biz_audit_req_idx'),
        ]

    def __str__(self):
        return f"[{self.occurred_at}] {self.action_code} on {self.entity_type} ({self.entity_id}) by {self.actor_user or self.actor_type}"


class IdempotencyRecord(models.Model):
    """
    Guarantees retry-safe executions for sensitive mutations (booking, payment, checkout, etc.).
    """
    STATUS_CHOICES = [
        ('PROCESSING', 'Processing'),
        ('COMPLETED', 'Completed'),
        ('FAILED', 'Failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name='idempotency_records')
    idempotency_key = models.CharField(max_length=255)
    operation_type = models.CharField(max_length=50)
    actor_user = models.ForeignKey(TenantUser, on_delete=models.SET_NULL, null=True, blank=True)
    request_hash = models.CharField(max_length=128, null=True, blank=True)
    resource_type = models.CharField(max_length=100, null=True, blank=True)
    resource_id = models.UUIDField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PROCESSING')
    response_snapshot = models.JSONField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'idempotency_records'
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'operation_type', 'idempotency_key'],
                name='uq_idempotency_org_op_key'
            )
        ]
        indexes = [
            models.Index(fields=['organization', 'operation_type', 'idempotency_key'], name='idemp_org_op_key_idx'),
            models.Index(fields=['expires_at'], name='idemp_expires_at_idx'),
        ]

    def __str__(self):
        return f"{self.operation_type}:{self.idempotency_key} [{self.status}]"


class DomainOutboxEvent(models.Model):
    """
    Transactional outbox for reliable async event delivery (WhatsApp, email, push, integrations).
    """
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('PROCESSING', 'Processing'),
        ('PUBLISHED', 'Published'),
        ('FAILED', 'Failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name='outbox_events')
    event_type = models.CharField(max_length=100)
    aggregate_type = models.CharField(max_length=100)
    aggregate_id = models.UUIDField()
    payload = models.JSONField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    attempt_count = models.IntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'domain_outbox_events'
        indexes = [
            models.Index(fields=['status', 'next_attempt_at', 'created_at'], name='outbox_sched_idx'),
            models.Index(fields=['aggregate_type', 'aggregate_id'], name='outbox_agg_idx'),
        ]

    def __str__(self):
        return f"Outbox:{self.event_type} on {self.aggregate_type}:{self.aggregate_id} [{self.status}]"


class ReasonCode(models.Model):
    """
    Configurable structured reasons for auditable business actions (cancellations, refunds, freezes, etc.).
    """
    STATUS_CHOICES = [
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, null=True, blank=True, related_name='reason_codes')
    module = models.CharField(max_length=50)
    action_type = models.CharField(max_length=100)
    code = models.CharField(max_length=100)
    label = models.CharField(max_length=200)
    description = models.TextField(null=True, blank=True)
    requires_comment = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'reason_codes'
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'module', 'action_type', 'code'],
                name='uq_reason_org_mod_act_code'
            )
        ]
        indexes = [
            models.Index(fields=['organization', 'module', 'action_type', 'status'], name='reason_lookup_idx'),
        ]

    def __str__(self):
        return f"{self.module}.{self.action_type}:{self.code} ({self.label})"
