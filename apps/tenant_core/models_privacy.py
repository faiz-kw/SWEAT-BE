"""
Dedicated Tenant DB — Privacy, Consent & Compliance Models (4 Tables)
Tables: processing_purposes, consent_records, privacy_requests, audit_events
"""

import uuid
from django.db import models
from django.utils import timezone
from .models_users import TenantUser


class ProcessingPurpose(models.Model):
    """
    Documented data processing purposes (DPDP / GDPR compliance).
    Defines why data is collected and how long it is retained.
    """
    BASIS = [
        ('CONSENT', 'Consent'),
        ('CONTRACT', 'Contract'),
        ('LEGAL_OBLIGATION', 'Legal Obligation'),
        ('VITAL_INTERESTS', 'Vital Interests'),
        ('PUBLIC_TASK', 'Public Task'),
        ('LEGITIMATE_INTERESTS', 'Legitimate Interests'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=200)
    description = models.TextField()
    notice_version = models.CharField(max_length=50, default='1.0')
    is_mandatory = models.BooleanField(default=False, help_text='Cannot be opted out of')
    status = models.CharField(max_length=30, default='ACTIVE')
    legal_basis = models.CharField(max_length=30, choices=BASIS, default='CONSENT')
    retention_days = models.IntegerField(default=365, help_text='Data retention period in days. -1 = indefinite')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'processing_purposes'
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.code}) — {self.legal_basis}"

    def save(self, *args, **kwargs):
        if self.status == 'ACTIVE':
            self.is_active = True
        elif self.status == 'INACTIVE':
            self.is_active = False
        super().save(*args, **kwargs)


class ConsentRecord(models.Model):
    """
    Records when and how a user consented to each processing purpose.
    Append-only — new row created on each consent change.
    """
    STATUS = [
        ('GRANTED', 'Granted'),
        ('WITHDRAWN', 'Withdrawn'),
        ('PENDING', 'Pending'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(TenantUser, on_delete=models.RESTRICT, related_name='consents')
    purpose = models.ForeignKey(ProcessingPurpose, on_delete=models.RESTRICT, related_name='consents')
    status = models.CharField(max_length=30, choices=STATUS, default='GRANTED')
    notice_version = models.CharField(max_length=50, default='1.0')
    granted_at = models.DateTimeField(null=True, blank=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    capture_source = models.CharField(max_length=50, default='WEB_FORM')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    proof_metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    # Legacy fields
    subject_type = models.CharField(max_length=50, blank=True, default='', help_text='e.g. user, lead, member')
    subject_id = models.UUIDField(null=True, blank=True)
    subject_email = models.EmailField(blank=True, default='')
    consent_method = models.CharField(max_length=50, default='WEB_FORM', help_text='e.g. WEB_FORM, PAPER, SMS_OTP, VERBAL')
    user_agent = models.TextField(blank=True, default='')
    notes = models.TextField(blank=True, default='')
    recorded_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'consent_records'
        ordering = ['-recorded_at']

    def __str__(self):
        return f"{self.user_id} — {self.purpose.code}: {self.status}"

    def save(self, *args, **kwargs):
        if not self.capture_source and self.consent_method:
            self.capture_source = self.consent_method
        elif not self.consent_method and self.capture_source:
            self.consent_method = self.capture_source
        super().save(*args, **kwargs)


class PrivacyRequest(models.Model):
    """
    DPDP/GDPR data subject requests (access, deletion, portability, objection).
    """
    REQUEST_TYPE = [
        ('ACCESS', 'Right of Access'),
        ('ERASURE', 'Right to Erasure / Deletion'),
        ('PORTABILITY', 'Right to Data Portability'),
        ('RECTIFICATION', 'Right to Rectification'),
        ('RESTRICTION', 'Right to Restrict Processing'),
        ('OBJECTION', 'Right to Object'),
    ]
    STATUS = [
        ('RECEIVED', 'Received'),
        ('IN_PROGRESS', 'In Progress'),
        ('COMPLETED', 'Completed'),
        ('REJECTED', 'Rejected'),
        ('EXTENDED', 'Extended (Timeline Extended)'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(TenantUser, on_delete=models.RESTRICT)
    request_type = models.CharField(max_length=40, choices=REQUEST_TYPE)
    status = models.CharField(max_length=40, choices=STATUS, default='RECEIVED')
    identity_verified_at = models.DateTimeField(null=True, blank=True)
    assigned_to = models.ForeignKey(TenantUser, on_delete=models.SET_NULL, null=True, blank=True, db_column='assigned_to', related_name='assigned_privacy_requests')
    received_at = models.DateTimeField(null=True, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    resolution = models.TextField(blank=True, null=True)
    evidence = models.JSONField(blank=True, null=True)

    # Legacy fields
    subject_email = models.EmailField(blank=True, default='')
    subject_name = models.CharField(max_length=200, blank=True, default='')
    details = models.TextField(blank=True, default='')
    due_date = models.DateField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True, default='')
    handled_by_user_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'privacy_requests'
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.request_type}] {self.user_id} — {self.status}"

    def save(self, *args, **kwargs):
        if not self.resolution and self.rejection_reason:
            self.resolution = self.rejection_reason
        super().save(*args, **kwargs)


class TenantAuditEvent(models.Model):
    """
    Append-only tenant-side audit log. Records every significant action by any user.
    Sensitive data should be redacted before writing. Never update or delete rows.
    """
    ACTOR_TYPE = [
        ('TENANT_USER', 'Tenant User'),
        ('SYSTEM_JOB', 'System Job'),
        ('INTEGRATION', 'Integration'),
        ('SUPER_ADMIN', 'Super Admin'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization_id = models.UUIDField(null=True, blank=True)
    company_entity_id = models.UUIDField(null=True, blank=True)
    location_id = models.UUIDField(null=True, blank=True)
    branch_id = models.UUIDField(null=True, blank=True, help_text='Branch context at time of action')
    actor_type = models.CharField(max_length=30, choices=ACTOR_TYPE, default='TENANT_USER')
    actor = models.ForeignKey(TenantUser, on_delete=models.SET_NULL, null=True, blank=True, db_column='actor_id', related_name='audit_events')
    actor_role = models.CharField(max_length=100, blank=True, null=True)
    event_name = models.CharField(max_length=150, default='')
    action = models.CharField(max_length=50, help_text='e.g. CREATE, UPDATE, DELETE, LOGIN, EXPORT')
    resource_type = models.CharField(max_length=100, help_text='e.g. TenantUser, Role, RoleAssignment')
    resource_id = models.CharField(max_length=36, null=True, blank=True)
    before_state = models.JSONField(null=True, blank=True)
    after_state = models.JSONField(null=True, blank=True)
    reason = models.TextField(blank=True, null=True)
    source_application = models.CharField(max_length=100, default='web_admin')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default='')
    request_id = models.CharField(max_length=255, blank=True, null=True)
    correlation_id = models.CharField(max_length=255, blank=True, null=True)

    # Legacy fields
    actor_email = models.CharField(max_length=320, blank=True, default='')
    description = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    # Canonical aliases matching Class B mapping
    @property
    def before_data(self):
        return self.before_state

    @before_data.setter
    def before_data(self, val):
        self.before_state = val

    @property
    def after_data(self):
        return self.after_state

    @after_data.setter
    def after_data(self, val):
        self.after_state = val

    class Meta:
        app_label = 'tenant_core'
        db_table = 'audit_events'
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        # Allow initial insert; prevent subsequent modification
        if self.pk and not self._state.adding:
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied("TenantAuditEvent records are append-only and cannot be updated.")
        if not self.event_name and self.action:
            self.event_name = self.action
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied("TenantAuditEvent records are immutable and cannot be deleted.")

    def __str__(self):
        return f"[{self.action}] {self.resource_type}/{self.resource_id} by {self.actor_email}"
