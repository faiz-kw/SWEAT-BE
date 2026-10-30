import uuid
from django.db import models
from django.utils import timezone


class SupportTicket(models.Model):
    """
    Support & operational tickets raised by members (or staff on member's behalf),
    including Passport Session purchase requests, membership inquiries, and billing issues.
    """
    class TicketCategory(models.TextChoices):
        PASSPORT_PURCHASE = 'PASSPORT_PURCHASE', 'Passport Purchase Request'
        MEMBERSHIP_INQUIRY = 'MEMBERSHIP_INQUIRY', 'Membership Inquiry'
        CLASS_BOOKING = 'CLASS_BOOKING', 'Class Booking Issue'
        BILLING_PAYMENT = 'BILLING_PAYMENT', 'Billing & Payments'
        GENERAL_INQUIRY = 'GENERAL_INQUIRY', 'General Inquiry'

    class TicketPriority(models.TextChoices):
        LOW = 'LOW', 'Low'
        MEDIUM = 'MEDIUM', 'Medium'
        HIGH = 'HIGH', 'High'
        URGENT = 'URGENT', 'Urgent'

    class TicketStatus(models.TextChoices):
        OPEN = 'OPEN', 'Open'
        IN_PROGRESS = 'IN_PROGRESS', 'In Progress'
        RESOLVED = 'RESOLVED', 'Resolved'
        CLOSED = 'CLOSED', 'Closed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ticket_number = models.CharField(max_length=50, unique=True, db_index=True)
    organization = models.ForeignKey(
        'tenant_core.Organization',
        on_delete=models.CASCADE,
        related_name='support_tickets',
        db_column='organization_id'
    )
    branch = models.ForeignKey(
        'tenant_core.Branch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='support_tickets',
        db_column='branch_id'
    )
    user_profile = models.ForeignKey(
        'tenant_core.UserProfile',
        on_delete=models.CASCADE,
        related_name='support_tickets',
        db_column='user_profile_id'
    )
    category = models.CharField(
        max_length=50,
        choices=TicketCategory.choices,
        default=TicketCategory.GENERAL_INQUIRY,
        db_index=True
    )
    priority = models.CharField(
        max_length=20,
        choices=TicketPriority.choices,
        default=TicketPriority.MEDIUM,
        db_index=True
    )
    status = models.CharField(
        max_length=25,
        choices=TicketStatus.choices,
        default=TicketStatus.OPEN,
        db_index=True
    )
    subject = models.CharField(max_length=255)
    description = models.TextField(blank=True, default='')

    assigned_to = models.ForeignKey(
        'tenant_core.TenantUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_tickets',
        db_column='assigned_to_id'
    )
    resolution_notes = models.TextField(blank=True, default='')
    resolved_by = models.ForeignKey(
        'tenant_core.TenantUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='resolved_tickets',
        db_column='resolved_by_id'
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'support_tickets'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['organization', 'status', 'priority'], name='idx_tkt_org_st_pr'),
            models.Index(fields=['organization', 'category', 'status'], name='idx_tkt_org_cat_st'),
            models.Index(fields=['user_profile', 'status'], name='idx_tkt_user_st'),
        ]

    def __str__(self):
        return f"[{self.ticket_number}] {self.subject} ({self.status})"
