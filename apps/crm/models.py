"""
CRM Models for PerformanceOS (Phase 1).
Provides Lead and LeadActivity models with full tenant isolation.
"""

from django.db import models
from django.utils import timezone
from apps.tenants.models import TenantAwareModel, Location
from apps.users.models import User

class LeadStage(models.TextChoices):
    NEW = 'New', 'New'
    CONTACTED = 'Contacted', 'Contacted'
    QUALIFIED = 'Qualified', 'Qualified'
    TRIAL_BOOKED = 'Trial Booked', 'Trial Booked'
    TRIAL_ATTENDED = 'Trial Attended', 'Trial Attended'
    OFFER_SENT = 'Offer Sent', 'Offer Sent'
    NEGOTIATION = 'Negotiation', 'Negotiation'
    CONVERTED = 'Converted', 'Converted'
    LOST = 'Lost', 'Lost'


class LeadStatus(models.TextChoices):
    OPEN = 'Open', 'Open'
    WON = 'Won', 'Won'
    LOST = 'Lost', 'Lost'


class LeadSource(models.TextChoices):
    WEBSITE = 'Website', 'Website'
    INSTAGRAM = 'Instagram', 'Instagram'
    REFERRAL = 'Referral', 'Referral'
    WALK_IN = 'Walk-in', 'Walk-in'
    META_ADS = 'Meta Ads', 'Meta Ads'
    GOOGLE_ADS = 'Google Ads', 'Google Ads'
    PHONE = 'Phone Inquiry', 'Phone Inquiry'
    CORPORATE = 'Corporate', 'Corporate'


class Lead(TenantAwareModel):
    """
    Represents a prospective client/lead in the sales pipeline.
    Strictly isolated by Tenant and assigned to a specific Location.
    """
    id = models.CharField(
        max_length=64,
        primary_key=True,
        help_text="Unique Lead Identifier (e.g. LED-001)"
    )
    location = models.ForeignKey(
        Location,
        on_delete=models.CASCADE,
        related_name='leads',
        help_text="Location / Studio branch this lead is interested in"
    )
    name = models.CharField(max_length=255)
    phone = models.CharField(max_length=32)
    email = models.EmailField(blank=True, default='')

    source = models.CharField(
        max_length=64,
        choices=LeadSource.choices,
        default=LeadSource.WEBSITE
    )
    interested_service = models.CharField(
        max_length=128,
        default='Strength Training',
        help_text="Primary fitness service interested in (e.g. Strength, Pilates, PT)"
    )
    goal = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text="Customer primary fitness goal (e.g. Weight Loss, Muscle Gain)"
    )

    assigned_to = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_leads',
        help_text="Sales rep or trainer assigned to manage this lead"
    )

    stage = models.CharField(
        max_length=32,
        choices=LeadStage.choices,
        default=LeadStage.NEW,
        help_text="Sales pipeline stage"
    )
    status = models.CharField(
        max_length=16,
        choices=LeadStatus.choices,
        default=LeadStatus.OPEN,
        help_text="Overall status: Open, Won, or Lost"
    )

    score = models.IntegerField(
        default=50,
        help_text="AI / Lead Quality score (0 to 100)"
    )
    budget = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0.00,
        help_text="Customer monthly or package budget"
    )
    notes = models.TextField(blank=True, default='')

    last_contact_at = models.DateTimeField(null=True, blank=True)
    next_follow_up_at = models.DateTimeField(null=True, blank=True)
    trial_date = models.DateTimeField(null=True, blank=True, help_text="Scheduled trial session date/time")

    class Meta:
        db_table = 'crm_leads'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} - {self.stage} ({self.id})"


class ActivityType(models.TextChoices):
    CALL = 'Call', 'Call'
    WHATSAPP = 'WhatsApp', 'WhatsApp'
    EMAIL = 'Email', 'Email'
    TRIAL = 'Trial', 'Trial'
    NOTE = 'Note', 'Note'
    STAGE_CHANGE = 'Stage Change', 'Stage Change'


class LeadActivity(TenantAwareModel):
    """
    Audit and activity trail for a lead (calls logged, WhatsApp messages, trials, stage changes).
    """
    lead = models.ForeignKey(
        Lead,
        on_delete=models.CASCADE,
        related_name='activities'
    )
    activity_type = models.CharField(
        max_length=32,
        choices=ActivityType.choices,
        default=ActivityType.NOTE
    )
    summary = models.TextField()
    performed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='lead_activities'
    )

    class Meta:
        db_table = 'crm_lead_activities'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.activity_type} on {self.lead.id} at {self.created_at}"
