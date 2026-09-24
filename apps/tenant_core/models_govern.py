"""
Dedicated Tenant DB — Governance & Settings Models (3 Tables)
Tables: organization_settings, branch_settings, notification_templates
"""

import uuid
from django.db import models
from .models_org import Organization, Branch
from .models_users import TenantUser


class OrganizationSettings(models.Model):
    """
    Global settings and policies for the tenant organization.
    Currency, timezone, tax, booking policies, member lifecycle rules, etc.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField(Organization, on_delete=models.RESTRICT, related_name='settings')

    # Financial
    currency = models.CharField(max_length=10, default='INR')
    tax_rate_pct = models.DecimalField(max_digits=5, decimal_places=2, default=18.00)
    tax_id_number = models.CharField(max_length=64, blank=True, default='', help_text='GSTIN')

    # Booking & Cancellation
    booking_cancellation_window_hours = models.IntegerField(default=12)
    late_cancellation_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    allow_guest_passes = models.BooleanField(default=True)
    guest_passes_per_month = models.IntegerField(default=2)

    # Member Lifecycle
    membership_grace_period_days = models.IntegerField(default=7)
    allow_member_freeze = models.BooleanField(default=True)
    max_freeze_days_per_year = models.IntegerField(default=60)

    # Notifications
    send_booking_reminders = models.BooleanField(default=True)
    booking_reminder_hours_before = models.IntegerField(default=2)
    send_membership_expiry_alerts = models.BooleanField(default=True)
    membership_expiry_alert_days = models.IntegerField(default=7)

    # System & Localization
    default_timezone = models.CharField(max_length=100, default='Asia/Kolkata')
    language = models.CharField(max_length=20, default='en')
    date_format = models.CharField(max_length=30, default='YYYY-MM-DD')
    time_format = models.CharField(max_length=30, default='HH:mm')

    # Configuration Blocks
    membership_config = models.JSONField(default=dict, blank=True)
    booking_config = models.JSONField(default=dict, blank=True)
    attendance_config = models.JSONField(default=dict, blank=True)
    notification_config = models.JSONField(default=dict, blank=True)
    ai_config = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'organization_settings'
        verbose_name_plural = 'Organization settings'

    def __str__(self):
        return f"Settings — {self.organization.name}"


class BranchSettings(models.Model):
    """
    Branch-level overrides to organization settings.
    If a field is null, the organization default applies.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    branch = models.OneToOneField(Branch, on_delete=models.RESTRICT, related_name='settings')

    # Operating hours override
    business_open_time = models.CharField(max_length=10, blank=True, null=True)
    business_close_time = models.CharField(max_length=10, blank=True, null=True)

    # Capacity and booking overrides
    max_booking_capacity = models.IntegerField(null=True, blank=True)
    booking_cancellation_window_hours = models.IntegerField(null=True, blank=True)
    late_cancellation_fee = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    allow_guest_passes = models.BooleanField(null=True, blank=True)
    guest_passes_per_month = models.IntegerField(null=True, blank=True)

    # Configuration Overrides (null = inherit from organization)
    membership_config = models.JSONField(null=True, blank=True)
    booking_config = models.JSONField(null=True, blank=True)
    attendance_config = models.JSONField(null=True, blank=True)
    notification_config = models.JSONField(null=True, blank=True)
    ai_config = models.JSONField(null=True, blank=True)

    # Branch-specific contact
    contact_email = models.EmailField(blank=True, default='')
    contact_phone = models.CharField(max_length=30, blank=True, default='')
    whatsapp_number = models.CharField(max_length=30, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'branch_settings'
        verbose_name_plural = 'Branch settings'

    def __str__(self):
        return f"Settings — {self.branch.name}"


class NotificationTemplate(models.Model):
    """
    Configurable notification templates for SMS, WhatsApp, and email communications.
    Managed by Tenant Org Admin.
    """
    CHANNEL = [
        ('SMS', 'SMS'),
        ('WHATSAPP', 'WhatsApp'),
        ('EMAIL', 'Email'),
        ('PUSH', 'Push Notification'),
    ]
    EVENT_TYPE = [
        ('MEMBERSHIP_WELCOME', 'Membership Welcome'),
        ('MEMBERSHIP_EXPIRY', 'Membership Expiry Reminder'),
        ('MEMBERSHIP_RENEWAL', 'Membership Renewal Confirmation'),
        ('BOOKING_CONFIRMATION', 'Booking Confirmation'),
        ('BOOKING_REMINDER', 'Booking Reminder'),
        ('BOOKING_CANCELLATION', 'Booking Cancellation'),
        ('PAYMENT_RECEIVED', 'Payment Received'),
        ('PAYMENT_FAILED', 'Payment Failed'),
        ('LEAD_WELCOME', 'Lead Welcome'),
        ('TRIAL_SCHEDULED', 'Trial Scheduled'),
        ('CUSTOM', 'Custom'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.RESTRICT, related_name='notification_templates')
    branch = models.ForeignKey(Branch, on_delete=models.RESTRICT, null=True, blank=True, related_name='notification_templates')
    name = models.CharField(max_length=150)
    channel = models.CharField(max_length=30, choices=CHANNEL)
    event_type = models.CharField(max_length=50, choices=EVENT_TYPE)
    event_code = models.CharField(max_length=100, blank=True, default='')
    language = models.CharField(max_length=20, default='en')
    version = models.IntegerField(default=1)
    subject = models.CharField(max_length=300, blank=True, default='', help_text='Email subject line')
    body = models.TextField(help_text='Template body. Use {{first_name}}, {{branch_name}}, etc.')
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'notification_templates'
        unique_together = ('channel', 'event_type', 'is_default')
        ordering = ['channel', 'event_type']

    def save(self, *args, **kwargs):
        if not self.organization_id:
            from .models_org import Organization
            org = Organization.objects.first()
            if org:
                self.organization_id = org.id
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.channel} / {self.event_type})"


class BranchWorkingHours(models.Model):
    """
    Weekly operating schedule per branch (day-of-week 1=Monday to 7=Sunday).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    branch = models.ForeignKey(Branch, on_delete=models.RESTRICT, related_name='working_hours')
    day_of_week = models.SmallIntegerField()
    is_open = models.BooleanField(default=True)
    open_time = models.TimeField(null=True, blank=True)
    close_time = models.TimeField(null=True, blank=True)
    is_24_hours = models.BooleanField(default=False)
    has_split_shift = models.BooleanField(default=False)
    open_time_2 = models.TimeField(null=True, blank=True)
    close_time_2 = models.TimeField(null=True, blank=True)
    created_by = models.ForeignKey(TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_working_hours')
    updated_by = models.ForeignKey(TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='updated_working_hours')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'branch_working_hours'
        constraints = [
            models.UniqueConstraint(fields=['branch', 'day_of_week'], name='uq_branch_working_hours'),
            models.CheckConstraint(check=models.Q(day_of_week__gte=1, day_of_week__lte=7), name='chk_bwh_day_of_week_range'),
            models.CheckConstraint(
                check=~models.Q(is_open=True, is_24_hours=False, open_time__isnull=True) & ~models.Q(is_open=True, is_24_hours=False, close_time__isnull=True),
                name='chk_bwh_open_requires_times'
            ),
        ]
        indexes = [
            models.Index(fields=['branch'], name='idx_bwh_branch_id'),
        ]
        ordering = ['branch', 'day_of_week']

    def __str__(self):
        return f"{self.branch.name} — Day {self.day_of_week} ({'Open' if self.is_open else 'Closed'})"


class BranchOperatingException(models.Model):
    """
    Date-specific operating exceptions, closures, or modified hours per branch.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    branch = models.ForeignKey(Branch, on_delete=models.RESTRICT, related_name='operating_exceptions')
    exception_date = models.DateField()
    is_closed = models.BooleanField(default=False)
    open_time = models.TimeField(null=True, blank=True)
    close_time = models.TimeField(null=True, blank=True)
    reason = models.CharField(max_length=255, null=True, blank=True)
    created_by = models.ForeignKey(TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_operating_exceptions')
    updated_by = models.ForeignKey(TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='updated_operating_exceptions')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'branch_operating_exceptions'
        constraints = [
            models.UniqueConstraint(fields=['branch', 'exception_date'], name='uq_branch_operating_exceptions'),
            models.CheckConstraint(
                check=~models.Q(is_closed=False, open_time__isnull=True) & ~models.Q(is_closed=False, close_time__isnull=True),
                name='chk_boe_open_requires_times'
            ),
        ]
        indexes = [
            models.Index(fields=['branch', 'exception_date'], name='idx_boe_branch_date'),
        ]
        ordering = ['branch', 'exception_date']

    def __str__(self):
        return f"{self.branch.name} Exception on {self.exception_date} ({'Closed' if self.is_closed else 'Special Hours'})"
