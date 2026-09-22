"""
apps/tenant_core/models_communication.py — Normalized Omnichannel Communication Domain.

Provides provider-agnostic, tenant-isolated message persistence for:
- WhatsApp, Email, SMS (and future Voice/AI/Push)
- Full delivery lifecycle: QUEUED -> SUBMITTED -> SENT -> DELIVERED -> READ -> REPLIED
- Inbound webhook tracking with deterministic Lead resolution (RESOLVED, UNRESOLVED, AMBIGUOUS)
- Append-only status event history with database-enforced idempotency
"""

import uuid
from django.db import models
from django.utils import timezone

from .models_org import Organization
from .models_crm import Lead, TrialBooking, SalesFollowupTask
from .models_govern import NotificationTemplate
from .models_users import TenantUser


class CommunicationMessage(models.Model):
    """
    Authoritative record of all customer-facing communications (Outbound and Inbound).
    Preserves exact rendered content snapshot and delivery state transitions.
    """
    CHANNELS = [
        ('WHATSAPP', 'WhatsApp'),
        ('EMAIL', 'Email'),
        ('SMS', 'SMS'),
        ('VOICE', 'Voice Call'),
        ('AI_CALL', 'AI Call'),
        ('PUSH', 'Push Notification'),
        ('IN_APP', 'In-App Message'),
    ]

    DIRECTIONS = [
        ('OUTBOUND', 'Outbound (System / Agent to Customer)'),
        ('INBOUND', 'Inbound (Customer to System)'),
    ]

    PURPOSES = [
        ('TRANSACTIONAL', 'Transactional / Service Alert'),
        ('MARKETING', 'Marketing / Promotional'),
    ]

    STATUSES = [
        ('QUEUED', 'Queued for Dispatch'),
        ('SUBMITTED', 'Submitted to Provider'),
        ('SENT', 'Sent by Provider'),
        ('DELIVERED', 'Delivered to Recipient Device'),
        ('READ', 'Read by Recipient'),
        ('RECEIVED', 'Received Inbound Message'),
        ('REPLIED', 'Customer Replied'),
        ('FAILED', 'Delivery Failed'),
        ('CANCELLED', 'Dispatch Cancelled'),
    ]

    RESOLUTION_STATUSES = [
        ('RESOLVED', 'Resolved to a single Lead'),
        ('UNRESOLVED', 'Unknown sender phone/email (Lead is null)'),
        ('AMBIGUOUS', 'Multiple matching Leads found (Lead is null)'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='communication_messages',
        db_column='organization_id',
    )
    # Historical protection: PROTECT prevents deletion of Lead from silently destroying communication history
    lead = models.ForeignKey(
        Lead,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='communications',
        db_column='lead_id',
    )
    resolution_status = models.CharField(
        max_length=20,
        choices=RESOLUTION_STATUSES,
        default='RESOLVED',
    )
    sender_identifier = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text='Sender phone number or email address',
    )
    raw_sender_data = models.JSONField(
        default=dict,
        blank=True,
        help_text='Safe sender payload metadata for unresolved or ambiguous inbound messages',
    )

    channel = models.CharField(max_length=30, choices=CHANNELS)
    direction = models.CharField(max_length=20, choices=DIRECTIONS, default='OUTBOUND')
    purpose = models.CharField(max_length=30, choices=PURPOSES, default='TRANSACTIONAL')

    recipient = models.CharField(max_length=255, help_text='Destination phone or email')
    sender = models.CharField(max_length=255, blank=True, default='', help_text='Sender ID or from address')

    template = models.ForeignKey(
        NotificationTemplate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='used_in_communications',
        db_column='template_id',
    )
    template_reference = models.CharField(max_length=100, blank=True, default='')

    subject = models.CharField(max_length=300, blank=True, default='', help_text='Email subject line')
    body_snapshot = models.TextField(help_text='Immutable rendered snapshot of sent/received content')

    provider = models.CharField(max_length=100, blank=True, default='', help_text='e.g. Meta, Gupshup, SMTP, SES, Twilio')
    provider_message_id = models.CharField(max_length=255, null=True, blank=True, db_index=True)

    status = models.CharField(max_length=30, choices=STATUSES, default='QUEUED')
    idempotency_key = models.CharField(max_length=255, null=True, blank=True, db_index=True)

    queued_at = models.DateTimeField(default=timezone.now)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    replied_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)

    failure_code = models.CharField(max_length=100, blank=True, default='')
    failure_reason = models.TextField(blank=True, default='')

    trigger_type = models.CharField(
        max_length=50,
        blank=True,
        default='MANUAL',
        help_text='e.g. MANUAL, TRIAL_CONFIRMATION, TRIAL_REMINDER, LEAD_WELCOME, FOLLOWUP_TASK',
    )
    related_trial = models.ForeignKey(
        TrialBooking,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='communications',
        db_column='related_trial_id',
    )
    related_followup = models.ForeignKey(
        SalesFollowupTask,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='communications',
        db_column='related_followup_id',
    )
    in_reply_to = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='replies',
        db_column='in_reply_to_id',
    )

    created_by_user = models.ForeignKey(
        TenantUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sent_communications',
        db_column='created_by_user_id',
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'communication_messages'
        indexes = [
            models.Index(fields=['organization', 'channel', 'status'], name='idx_comm_org_chan_stat'),
            models.Index(fields=['lead', 'created_at'], name='idx_comm_lead_created'),
            models.Index(fields=['provider', 'provider_message_id'], name='idx_comm_prov_msg_id'),
            models.Index(fields=['organization', 'trigger_type'], name='idx_comm_org_trigger'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'idempotency_key'],
                condition=models.Q(idempotency_key__isnull=False),
                name='uq_comm_org_idempotency_key',
            ),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"Comm({self.channel} {self.direction} to {self.recipient} [{self.status}])"


class CommunicationStatusEvent(models.Model):
    """
    Append-only audit trail of provider status webhooks and state progression events.
    Protects against out-of-order webhook delivery and maintains strict delivery evidence.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message = models.ForeignKey(
        CommunicationMessage,
        on_delete=models.CASCADE,
        related_name='status_events',
        db_column='message_id',
    )
    provider_event_id = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    from_status = models.CharField(max_length=30, blank=True, default='')
    to_status = models.CharField(max_length=30)
    occurred_at = models.DateTimeField(default=timezone.now)
    raw_metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text='Sanitized, safely redacted provider webhook metadata',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'communication_status_events'
        constraints = [
            models.UniqueConstraint(
                fields=['message', 'provider_event_id'],
                condition=models.Q(provider_event_id__isnull=False),
                name='uq_comm_stat_msg_event',
            ),
        ]
        ordering = ['occurred_at']

    def __str__(self):
        return f"StatusEvent({self.message_id}: {self.from_status}->{self.to_status} at {self.occurred_at})"
