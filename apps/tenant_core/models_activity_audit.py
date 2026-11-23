"""
Centralized Canonical Activity & Audit Logging Model for SWEAT Fitness CRM.
Table: activity_audit_logs

Fulfills the 9-Point Contract:
1. Who performed the activity? (actor_name, actor_email, actor_role, actor_type, actor_user)
2. What exactly did they do? (action_code, action_verb, action_category)
3. Which CRM module/section was involved? (module, submodule)
4. Which customer/record was affected? (entity_type, entity_id, entity_name)
5. What was the previous value/status? (before_state, field_changes.old_value)
6. What is the new value/status? (after_state, field_changes.new_value)
7. When did it happen? (occurred_at)
8. Was it successful or failed? (result, error_code, error_message)
9. Additional details/reasons/remarks? (reason, remarks, metadata)

Security & Integrity:
- Append-only immutability enforced at Django model level and PostgreSQL trigger level.
- Multi-field changes broken down into structured field_changes array.
"""

import uuid
from django.db import models
from django.utils import timezone
from django.core.exceptions import PermissionDenied
from .models_org import Organization, Branch
from .models_users import TenantUser


class ActivityAuditLog(models.Model):
    """
    Unified, immutable, append-only activity & audit log capturing every action,
    event, mutation, and state transition across all CRM modules.
    """

    ACTOR_TYPE_CHOICES = [
        ('STAFF', 'Staff / Front Desk / Trainer / Sales'),
        ('ADMIN', 'Tenant Admin / Super Admin'),
        ('MEMBER', 'Member / Customer'),
        ('SYSTEM', 'Automated System Job / Celery'),
        ('API', 'API Client'),
        ('INTEGRATION', 'External Integration'),
        ('IOT_DEVICE', 'Turnstile / Access Control Hardware'),
        ('AI_AGENT', 'AI Voice / Chat Agent'),
    ]

    ACTION_CATEGORY_CHOICES = [
        ('AUTHENTICATION', 'Authentication & Security'),
        ('MEMBERSHIP', 'Memberships & Packages'),
        ('OPERATIONS', 'Classes, Bookings & Scheduling'),
        ('COMMERCE', 'Orders, Payments & Billing'),
        ('COMMUNICATION', 'Intake, Leads & Communications'),
        ('WORKFORCE', 'Staff, Trainers & Roster'),
        ('ACCESS_CONTROL', 'Permissions, Roles & IoT Access'),
        ('SETTINGS', 'Organization, Policies & Settings'),
        ('SYSTEM', 'Automated Workflows & Background Jobs'),
        ('GENERAL', 'General CRM Activity'),
    ]

    RESULT_CHOICES = [
        ('SUCCESS', 'Success'),
        ('FAILURE', 'Failure'),
        ('WARNING', 'Warning'),
    ]

    SOURCE_CHANNEL_CHOICES = [
        ('WEB_ADMIN', 'Web Admin Command Center'),
        ('MEMBER_PORTAL', 'Web Member Portal'),
        ('MOBILE_APP', 'Mobile App'),
        ('FRONT_DESK', 'Front Desk Terminal'),
        ('TRAINER_APP', 'Trainer App'),
        ('TURNSTILE_IOT', 'Turnstile IoT Device'),
        ('AI_VOICE_AGENT', 'AI Voice Agent / Telephony'),
        ('WEBHOOK', 'External Webhook (Razorpay / Meta)'),
        ('BACKGROUND_WORKER', 'Celery Worker / Scheduled Task'),
        ('API', 'Direct API Integration'),
    ]

    # Primary key
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # 1. Scope & Timing
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='activity_audit_logs',
        db_index=True,
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='activity_audit_logs',
        db_index=True,
    )
    branch_name = models.CharField(max_length=255, blank=True, default='')

    # 2. Who performed the activity?
    actor_type = models.CharField(
        max_length=30,
        choices=ACTOR_TYPE_CHOICES,
        default='STAFF',
        db_index=True,
    )
    actor_user = models.ForeignKey(
        TenantUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='activity_audit_actions',
    )
    actor_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    actor_email = models.CharField(max_length=320, blank=True, default='', db_index=True)
    actor_name = models.CharField(max_length=255, blank=True, default='')
    actor_role = models.CharField(max_length=100, blank=True, default='', db_index=True)

    # 3. What exactly did they do?
    action_code = models.CharField(max_length=100, db_index=True)
    action_category = models.CharField(
        max_length=50,
        choices=ACTION_CATEGORY_CHOICES,
        default='GENERAL',
        db_index=True,
    )
    action_verb = models.CharField(max_length=50, blank=True, default='', db_index=True)

    # 4. Which CRM module/section was involved?
    module = models.CharField(max_length=50, db_index=True)
    submodule = models.CharField(max_length=50, blank=True, default='', db_index=True)

    # 5. Which customer, record, or entity was affected?
    entity_type = models.CharField(max_length=100, db_index=True)
    entity_id = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    entity_name = models.CharField(max_length=255, blank=True, default='')

    # 6. What was the previous vs new value? (Detailed granular diffs)
    before_state = models.JSONField(null=True, blank=True)
    after_state = models.JSONField(null=True, blank=True)
    field_changes = models.JSONField(
        null=True,
        blank=True,
        help_text='Granular field-by-field array: [{field_name, field_label, old_value, new_value}]',
    )

    # 7. Narrative, reason, and remarks (Simple English 9-Point Contract)
    event_description = models.TextField(blank=True, default='')
    reason = models.TextField(blank=True, default='')
    remarks = models.TextField(blank=True, default='')

    # 8. Success or failure status
    result = models.CharField(
        max_length=20,
        choices=RESULT_CHOICES,
        default='SUCCESS',
        db_index=True,
    )
    error_code = models.CharField(max_length=100, blank=True, default='')
    error_message = models.TextField(blank=True, default='')

    # 9. Request metadata, telemetry & origin
    metadata = models.JSONField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default='')
    request_id = models.CharField(max_length=255, blank=True, default='')
    correlation_id = models.CharField(max_length=255, blank=True, default='')
    source_channel = models.CharField(
        max_length=50,
        choices=SOURCE_CHANNEL_CHOICES,
        default='WEB_ADMIN',
        db_index=True,
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'activity_audit_logs'
        ordering = ['-occurred_at']
        indexes = [
            models.Index(fields=['module', '-occurred_at']),
            models.Index(fields=['actor_type', '-occurred_at']),
            models.Index(fields=['action_code', '-occurred_at']),
            models.Index(fields=['entity_type', 'entity_id']),
            models.Index(fields=['branch', '-occurred_at']),
            models.Index(fields=['result', '-occurred_at']),
            models.Index(fields=['action_category', '-occurred_at']),
        ]

    def __str__(self):
        return f"[{self.occurred_at.strftime('%Y-%m-%d %H:%M:%S')}] {self.action_code} on {self.entity_type}({self.entity_name or self.entity_id}) by {self.actor_name or self.actor_email or self.actor_type}"

    def save(self, *args, **kwargs):
        # Enforce append-only immutability
        if self.pk and not self._state.adding:
            raise PermissionDenied("ActivityAuditLog records are append-only and cannot be updated.")

        # Ensure organization is populated from branch if missing
        if not self.organization_id and self.branch and getattr(self.branch, 'organization_id', None):
            self.organization_id = self.branch.organization_id

        # Ensure branch details are synchronized
        if self.branch:
            if not self.branch_name and hasattr(self.branch, 'name'):
                self.branch_name = self.branch.name

        # Ensure actor metadata is synchronized from TenantUser if available
        if self.actor_user:
            if not self.actor_id:
                self.actor_id = str(self.actor_user.id)
            if not self.actor_email and hasattr(self.actor_user, 'email'):
                self.actor_email = self.actor_user.email or ''
            if not self.actor_name:
                first = getattr(self.actor_user, 'first_name', '') or ''
                last = getattr(self.actor_user, 'last_name', '') or ''
                name = f"{first} {last}".strip()
                self.actor_name = name or self.actor_email or str(self.actor_user.id)

        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("ActivityAuditLog records are immutable and cannot be deleted.")

    @property
    def actor_display(self) -> str:
        if self.actor_name and self.actor_role:
            return f"{self.actor_name} ({self.actor_role})"
        if self.actor_name:
            return self.actor_name
        if self.actor_email:
            return self.actor_email
        return self.actor_type or "System"

    @property
    def occurred_at_ist(self) -> str:
        if not self.occurred_at:
            return ""
        ist_time = self.occurred_at.astimezone(timezone.get_fixed_timezone(330))
        return ist_time.strftime("%d %b %Y, %I:%M %p IST")

    @property
    def field_changes_count(self) -> int:
        if isinstance(self.field_changes, list):
            return len(self.field_changes)
        return 0
