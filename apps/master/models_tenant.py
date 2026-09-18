"""
Master DB — Tenant Registry Models (4 Tables)
Tables: tenants, tenant_domains, platform_branding, tenant_branding
"""

import uuid
from django.db import models
from django.utils import timezone


class Tenant(models.Model):
    """
    Top-level SaaS customer record (a gym brand/organization).
    One row per paying customer. Created by platform team only.
    """
    TENANT_STATUS = [
        ('DRAFT', 'Draft'),
        ('PENDING_ACTIVATION', 'Pending Activation'),
        ('ACTIVE', 'Active'),
        ('SUSPENDED', 'Suspended'),
        ('DEACTIVATED', 'Deactivated'),
        ('TERMINATED', 'Terminated'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=50, unique=True, help_text='Stable tenant code e.g. ELEVATE-FIT')
    name = models.CharField(max_length=200)
    legal_name = models.CharField(max_length=250, blank=True, null=True)
    slug = models.SlugField(max_length=150, unique=True, help_text='URL-friendly identifier / subdomain base')
    status = models.CharField(max_length=30, choices=TENANT_STATUS, default='DRAFT')

    country = models.CharField(max_length=10, default='IN')
    currency = models.CharField(max_length=10, default='INR')
    timezone = models.CharField(max_length=64, default='Asia/Kolkata')
    default_language = models.CharField(max_length=20, default='en')

    # Lifecycle timestamps
    activated_at = models.DateTimeField(null=True, blank=True)
    suspended_at = models.DateTimeField(null=True, blank=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    terminated_at = models.DateTimeField(null=True, blank=True)
    deactivation_reason = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'master'
        db_table = 'tenants'
        ordering = ['name']

    def __str__(self):
        return f"{self.name} [{self.status}]"

    @property
    def is_accessible(self):
        """Tenant-side login/API access is allowed ONLY when status = ACTIVE."""
        return self.status == 'ACTIVE'

    def activate(self):
        self.status = 'ACTIVE'
        self.activated_at = timezone.now()
        self.save(update_fields=['status', 'activated_at', 'updated_at'])

    def suspend(self, reason=''):
        self.status = 'SUSPENDED'
        self.suspended_at = timezone.now()
        if reason:
            self.deactivation_reason = reason
        self.save(update_fields=['status', 'suspended_at', 'deactivation_reason', 'updated_at'])

    def deactivate(self, reason=''):
        self.status = 'DEACTIVATED'
        self.deactivated_at = timezone.now()
        if reason:
            self.deactivation_reason = reason
        self.save(update_fields=['status', 'deactivated_at', 'deactivation_reason', 'updated_at'])


class TenantDomain(models.Model):
    """
    Maps platform/custom domains to tenants.
    Used for tenant DB routing via subdomain resolution.
    """
    DOMAIN_TYPE = [
        ('PLATFORM', 'Platform (e.g. slug.performanceos.io)'),
        ('CUSTOM', 'Custom (e.g. app.elevatefitness.com)'),
    ]
    DOMAIN_STATUS = [
        ('PENDING', 'Pending'),
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
        ('FAILED', 'Failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.RESTRICT, related_name='domains')
    domain = models.CharField(max_length=255, unique=True, help_text='Hostname (e.g. elevate.performanceos.io)')
    domain_type = models.CharField(max_length=30, choices=DOMAIN_TYPE, default='PLATFORM')
    is_primary = models.BooleanField(default=False)
    is_verified = models.BooleanField(default=False)
    verification_method = models.CharField(max_length=50, blank=True, default='', help_text='DNS / FILE / EMAIL')
    verified_at = models.DateTimeField(null=True, blank=True)
    ssl_status = models.CharField(max_length=30, blank=True, default='')
    status = models.CharField(max_length=30, choices=DOMAIN_STATUS, default='PENDING')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'master'
        db_table = 'tenant_domains'
        ordering = ['tenant', 'is_primary']

    def __str__(self):
        return f"{self.domain} → {self.tenant.slug}"


class PlatformBranding(models.Model):
    """
    Platform-wide default branding (logo, colours, name).
    Applied when no tenant context is active.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    brand_name = models.CharField(max_length=200, default='PerformanceOS')
    platform_name = models.CharField(max_length=200, default='PerformanceOS')
    logo_url = models.TextField(blank=True, default='')
    favicon_url = models.TextField(blank=True, default='')
    primary_color = models.CharField(max_length=20, default='#0f766e')
    secondary_color = models.CharField(max_length=20, null=True, blank=True, default='#f59e0b')
    accent_color = models.CharField(max_length=20, default='#f59e0b')
    login_title = models.CharField(max_length=250, null=True, blank=True)
    support_phone = models.CharField(max_length=50, null=True, blank=True)
    logo_storage_key = models.TextField(null=True, blank=True)
    favicon_storage_key = models.TextField(null=True, blank=True)
    login_logo_key = models.TextField(null=True, blank=True)
    login_background_key = models.TextField(null=True, blank=True)
    login_background_url = models.TextField(blank=True, default='')
    support_email = models.EmailField(blank=True, default='')
    support_url = models.TextField(blank=True, default='')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'master'
        db_table = 'platform_branding'

    def __str__(self):
        return f"Platform Branding — {self.brand_name or self.platform_name}"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._orig_brand_name = self.brand_name
        self._orig_platform_name = self.platform_name
        self._orig_secondary_color = self.secondary_color
        self._orig_accent_color = self.accent_color

    def save(self, *args, **kwargs):
        if hasattr(self, '_orig_brand_name'):
            if self.brand_name != self._orig_brand_name:
                self.platform_name = self.brand_name or ''
            elif self.platform_name != self._orig_platform_name:
                self.brand_name = self.platform_name
        if not self.brand_name and self.platform_name:
            self.brand_name = self.platform_name
        elif not self.platform_name and self.brand_name:
            self.platform_name = self.brand_name

        if hasattr(self, '_orig_secondary_color'):
            if self.secondary_color != self._orig_secondary_color:
                self.accent_color = self.secondary_color or ''
            elif self.accent_color != self._orig_accent_color:
                self.secondary_color = self.accent_color
        if not self.secondary_color and self.accent_color:
            self.secondary_color = self.accent_color
        elif not self.accent_color and self.secondary_color:
            self.accent_color = self.secondary_color

        super().save(*args, **kwargs)
        self._orig_brand_name = self.brand_name
        self._orig_platform_name = self.platform_name
        self._orig_secondary_color = self.secondary_color
        self._orig_accent_color = self.accent_color


class TenantBranding(models.Model):
    """
    Per-tenant branding overrides (white-label support).
    """
    BRANDING_MODE = [
        ('PLATFORM', 'Show Platform Branding'),
        ('CUSTOM', 'Full White-Label Custom Branding'),
        ('CO_BRANDED', 'Co-branded (Tenant + Platform)'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.OneToOneField(Tenant, on_delete=models.RESTRICT, related_name='branding')
    branding_mode = models.CharField(max_length=30, choices=BRANDING_MODE, default='PLATFORM')
    brand_name = models.CharField(max_length=200, null=True, blank=True)
    app_name = models.CharField(max_length=200, blank=True, default='')
    logo_url = models.TextField(blank=True, default='')
    favicon_url = models.TextField(blank=True, default='')
    primary_color = models.CharField(max_length=20, blank=True, default='')
    secondary_color = models.CharField(max_length=20, null=True, blank=True)
    accent_color = models.CharField(max_length=20, blank=True, default='')
    theme_preset_code = models.CharField(max_length=100, null=True, blank=True, default='titanium-teal')
    theme_tokens = models.JSONField(null=True, blank=True)
    support_phone = models.CharField(max_length=50, null=True, blank=True)
    logo_storage_key = models.TextField(null=True, blank=True)
    favicon_storage_key = models.TextField(null=True, blank=True)
    login_logo_key = models.TextField(null=True, blank=True)
    login_background_key = models.TextField(null=True, blank=True)
    email_logo_key = models.TextField(null=True, blank=True)
    login_background_url = models.TextField(blank=True, default='')
    login_tagline = models.CharField(max_length=300, blank=True, default='')
    support_email = models.EmailField(blank=True, default='')
    email_footer = models.TextField(blank=True, default='')
    remove_watermark = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'master'
        db_table = 'tenant_branding'

    def __str__(self):
        return f"Branding — {self.tenant.name} ({self.branding_mode})"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._orig_brand_name = self.brand_name
        self._orig_app_name = self.app_name
        self._orig_secondary_color = self.secondary_color
        self._orig_accent_color = self.accent_color

    def save(self, *args, **kwargs):
        if hasattr(self, '_orig_brand_name'):
            if self.brand_name != self._orig_brand_name:
                self.app_name = self.brand_name or ''
            elif self.app_name != self._orig_app_name:
                self.brand_name = self.app_name
        if not self.brand_name and self.app_name:
            self.brand_name = self.app_name
        elif not self.app_name and self.brand_name:
            self.app_name = self.brand_name

        if hasattr(self, '_orig_secondary_color'):
            if self.secondary_color != self._orig_secondary_color:
                self.accent_color = self.secondary_color or ''
            elif self.accent_color != self._orig_accent_color:
                self.secondary_color = self.accent_color
        if not self.secondary_color and self.accent_color:
            self.secondary_color = self.accent_color
        elif not self.accent_color and self.secondary_color:
            self.secondary_color = self.accent_color

        if not self.theme_preset_code:
            self.theme_preset_code = 'titanium-teal'
        super().save(*args, **kwargs)
        self._orig_brand_name = self.brand_name
        self._orig_app_name = self.app_name
        self._orig_secondary_color = self.secondary_color
        self._orig_accent_color = self.accent_color

