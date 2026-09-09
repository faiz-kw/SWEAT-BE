"""
Administration Models: Services, TenantSettings, CustomForm, ApiKey, AuditLog, and SecurityPolicy.
"""

from django.db import models
from django.utils import timezone
from apps.tenants.models import TenantAwareModel, Tenant, Location
from apps.users.models import User


class ServiceCategory(models.TextChoices):
    FITNESS = 'Fitness', 'Fitness'
    PERSONAL_TRAINING = 'Personal Training', 'Personal Training'
    PILATES = 'Pilates', 'Pilates'
    YOGA = 'Yoga', 'Yoga'
    NUTRITION = 'Nutrition', 'Nutrition'
    WELLNESS_SPA = 'Wellness & Spa', 'Wellness & Spa'
    PHYSIOTHERAPY = 'Physiotherapy', 'Physiotherapy'
    RECOVERY = 'Recovery', 'Recovery'


class Service(TenantAwareModel):
    """
    Catalog of gym services, bookable amenities, and packages.
    """
    id = models.CharField(max_length=64, primary_key=True, help_text="e.g. SVC-001")
    location = models.ForeignKey(
        Location, on_delete=models.CASCADE, related_name='services',
        null=True, blank=True, help_text="Specific location or all locations if null"
    )
    name = models.CharField(max_length=255)
    category = models.CharField(max_length=64, choices=ServiceCategory.choices, default=ServiceCategory.FITNESS)
    description = models.TextField(blank=True, default='')
    duration_minutes = models.IntegerField(default=60)
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    capacity = models.IntegerField(default=1, help_text="Max concurrent clients")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'services'
        ordering = ['category', 'name']

    def __str__(self):
        return f"{self.name} ({self.category}) - ₹{self.price}"


class TenantSettings(TenantAwareModel):
    """
    Global system settings, booking policies, and tax rates per tenant.
    """
    currency = models.CharField(max_length=8, default='INR')
    tax_rate_gst = models.DecimalField(max_digits=5, decimal_places=2, default=18.00, help_text="GST / VAT Percentage")
    tax_id_number = models.CharField(max_length=64, blank=True, default='', help_text="GSTIN / Tax ID")
    
    # Booking & Cancellation policies
    booking_cancellation_window_hours = models.IntegerField(default=12, help_text="Hours before class when cancellation is penalty-free")
    late_cancellation_fee = models.DecimalField(max_digits=8, decimal_places=2, default=200.00)
    allow_guest_passes = models.BooleanField(default=True)
    guest_passes_per_month = models.IntegerField(default=2)
    
    # Member Lifecycle
    membership_grace_period_days = models.IntegerField(default=7)
    allow_member_freeze = models.BooleanField(default=True)
    max_freeze_days_per_year = models.IntegerField(default=60)
    
    # Operating Hours
    business_open_time = models.CharField(max_length=16, default='06:00')
    business_close_time = models.CharField(max_length=16, default='22:00')

    class Meta:
        db_table = 'tenant_settings'

    def __str__(self):
        return f"Settings - {self.tenant.name}"


class CustomForm(TenantAwareModel):
    """
    Dynamic forms, health questionnaires, and waivers (e.g. PAR-Q, COVID waiver).
    """
    id = models.CharField(max_length=64, primary_key=True, help_text="e.g. FORM-PARQ")
    title = models.CharField(max_length=255)
    code = models.CharField(max_length=64)
    description = models.TextField(blank=True, default='')
    is_mandatory = models.BooleanField(default=False)
    fields_schema = models.JSONField(default=list, help_text="List of field definitions (label, type, required, options)")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'custom_forms'
        ordering = ['title']

    def __str__(self):
        return f"{self.title} ({self.code})"


class ApiKey(TenantAwareModel):
    """
    Developer API Keys and Webhook configuration.
    """
    id = models.CharField(max_length=64, primary_key=True, help_text="e.g. KEY-001")
    name = models.CharField(max_length=128, help_text="e.g. Mobile App Gateway or Website Form")
    key_prefix = models.CharField(max_length=16, default='pk_live_')
    key_hash = models.CharField(max_length=255)
    permissions_scope = models.JSONField(default=list, blank=True)
    webhook_url = models.URLField(blank=True, default='')
    webhook_secret = models.CharField(max_length=64, blank=True, default='')
    is_active = models.BooleanField(default=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'api_keys'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.key_prefix}...)"


class AuditAction(models.TextChoices):
    CREATE = 'CREATE', 'CREATE'
    UPDATE = 'UPDATE', 'UPDATE'
    DELETE = 'DELETE', 'DELETE'
    LOGIN = 'LOGIN', 'LOGIN'
    LOGOUT = 'LOGOUT', 'LOGOUT'
    EXPORT = 'EXPORT', 'EXPORT'
    IMPERSONATE = 'IMPERSONATE', 'IMPERSONATE'


class AuditLog(TenantAwareModel):
    """
    Immutable audit trail recording every critical business action.
    """
    id = models.CharField(max_length=64, primary_key=True)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_entries')
    user_email = models.EmailField(blank=True, default='')
    action = models.CharField(max_length=32, choices=AuditAction.choices, default=AuditAction.UPDATE)
    module = models.CharField(max_length=64, help_text="e.g. CRM, Members, Finance, Users")
    entity_type = models.CharField(max_length=64, help_text="e.g. Member, Invoice, Lead")
    entity_id = models.CharField(max_length=64, blank=True, default='')
    description = models.CharField(max_length=500)
    diff_payload = models.JSONField(default=dict, blank=True, help_text="Previous vs New values diff")
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        db_table = 'audit_logs'
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.action}] {self.module} - {self.user_email} at {self.created_at}"


class SecurityPolicy(TenantAwareModel):
    """
    Tenant-specific security policies.
    """
    enforce_mfa = models.BooleanField(default=False)
    session_timeout_minutes = models.IntegerField(default=60)
    password_min_length = models.IntegerField(default=8)
    require_special_character = models.BooleanField(default=True)
    max_failed_attempts_lockout = models.IntegerField(default=5)
    ip_whitelist = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = 'security_policies'

    def __str__(self):
        return f"Security Policy - {self.tenant.name}"
