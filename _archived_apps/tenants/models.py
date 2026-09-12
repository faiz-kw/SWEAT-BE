"""
Multi-Tenancy Models and Managers for PerformanceOS.
Provides Tenant, Location, PlatformPlan, TenantBranding, TenantUsage, and TenantAwareModel.
"""

from django.db import models
from django.utils import timezone


class TenantStatus(models.TextChoices):
    ACTIVE = 'Active', 'Active'
    SUSPENDED = 'Suspended', 'Suspended'
    TRIAL = 'Trial', 'Trial'
    EXPIRED = 'Expired', 'Expired'


class TenantTier(models.TextChoices):
    STARTER = 'Starter', 'Starter'
    GROWTH = 'Growth', 'Growth'
    ENTERPRISE = 'Enterprise', 'Enterprise'


class PlatformPlan(models.Model):
    """
    SaaS subscription plans offered at platform level.
    """
    id = models.CharField(max_length=64, primary_key=True, help_text="e.g. PLAN-STARTER")
    name = models.CharField(max_length=128)
    code = models.CharField(max_length=64, unique=True)
    description = models.TextField(blank=True, default='')
    price_monthly = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    price_annual = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    currency = models.CharField(max_length=8, default='INR')
    max_locations = models.IntegerField(default=1)
    max_members = models.IntegerField(default=500)
    max_trainers = models.IntegerField(default=10)
    ai_voice_minutes = models.IntegerField(default=100)
    features = models.JSONField(default=list, blank=True)
    is_popular = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'platform_plans'
        ordering = ['price_monthly']

    def __str__(self):
        return f"{self.name} ({self.code})"


class Tenant(models.Model):
    """
    Represents a fitness brand/organization (e.g. Elevate Fitness).
    All data in the platform is strictly scoped to a Tenant.
    """
    id = models.CharField(
        max_length=64,
        primary_key=True,
        help_text="Tenant identifier (e.g. TEN-001)"
    )
    name = models.CharField(max_length=255, help_text="Brand or gym name")
    slug = models.SlugField(max_length=255, unique=True, help_text="URL-friendly slug / subdomain")
    status = models.CharField(max_length=32, choices=TenantStatus.choices, default=TenantStatus.ACTIVE)
    tier = models.CharField(max_length=32, choices=TenantTier.choices, default=TenantTier.GROWTH)
    plan = models.ForeignKey(PlatformPlan, on_delete=models.SET_NULL, null=True, blank=True, related_name='tenants')
    
    contact_email = models.EmailField(blank=True, default='')
    phone = models.CharField(max_length=32, blank=True, default='')
    website = models.URLField(blank=True, default='')
    currency = models.CharField(max_length=8, default='INR')
    timezone = models.CharField(max_length=64, default='Asia/Kolkata')
    
    max_locations = models.IntegerField(default=3)
    max_members = models.IntegerField(default=2000)
    enabled_modules = models.JSONField(default=list, blank=True)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tenants'
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.id})"


class Location(models.Model):
    """
    Represents a physical branch/studio under a Tenant (e.g. Indiranagar, Koramangala).
    """
    id = models.CharField(
        max_length=64,
        primary_key=True,
        help_text="Location identifier (e.g. LOC-001)"
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name='locations'
    )
    name = models.CharField(max_length=255, help_text="Studio or branch name")
    city = models.CharField(max_length=128)
    address = models.TextField(blank=True, default='')
    phone = models.CharField(max_length=32, blank=True, default='')
    capacity = models.IntegerField(default=150)
    operating_hours = models.CharField(max_length=128, default='06:00 - 22:00')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tenant_locations'
        ordering = ['city', 'name']

    def __str__(self):
        return f"{self.city} - {self.name} ({self.id})"


class TenantBranding(models.Model):
    """
    White-label branding and custom theme configuration per tenant.
    """
    tenant = models.OneToOneField(Tenant, on_delete=models.CASCADE, primary_key=True, related_name='branding')
    app_name = models.CharField(max_length=128, default='PerformanceOS')
    logo_url = models.TextField(blank=True, default='')
    favicon_url = models.TextField(blank=True, default='')
    primary_color = models.CharField(max_length=32, default='#0f766e', help_text="Primary Brand Hex Color")
    accent_color = models.CharField(max_length=32, default='#f59e0b', help_text="Accent Hex Color")
    custom_domain = models.CharField(max_length=255, blank=True, default='')
    cname_verified = models.BooleanField(default=False)
    email_footer = models.TextField(blank=True, default='')
    support_email = models.CharField(max_length=255, blank=True, default='')
    remove_watermark = models.BooleanField(default=False)
    login_tagline = models.CharField(max_length=255, blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tenant_branding'

    def __str__(self):
        return f"Branding - {self.tenant.name}"


class TenantUsage(models.Model):
    """
    Tracks resource utilization and quotas for billing/limits.
    """
    tenant = models.OneToOneField(Tenant, on_delete=models.CASCADE, primary_key=True, related_name='usage')
    active_members_count = models.IntegerField(default=0)
    locations_count = models.IntegerField(default=1)
    trainers_count = models.IntegerField(default=0)
    storage_used_mb = models.FloatField(default=0.0)
    ai_minutes_used = models.IntegerField(default=0)
    api_requests_count = models.BigIntegerField(default=0)
    last_calculated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'tenant_usage'

    def __str__(self):
        return f"Usage - {self.tenant.name}"


class MarketplaceCategory(models.TextChoices):
    AI_VOICE = 'AI & Voice', 'AI & Voice'
    PAYMENTS = 'Payments', 'Payments'
    MESSAGING = 'Messaging', 'Messaging'
    ACCESS_CONTROL = 'Access Control', 'Access Control'
    ACCOUNTING = 'Accounting', 'Accounting'


class MarketplaceApp(models.Model):
    """
    Integration modules and 3rd party partner extensions available across the platform.
    """
    id = models.CharField(max_length=64, primary_key=True, help_text="e.g. app-sarvam")
    name = models.CharField(max_length=128)
    category = models.CharField(max_length=64, choices=MarketplaceCategory.choices, default=MarketplaceCategory.AI_VOICE)
    icon_text = models.CharField(max_length=8, default='AP')
    developer = models.CharField(max_length=128, default='PerformanceOS Verified')
    description = models.TextField(blank=True, default='')
    price_monthly = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    required_tier = models.CharField(max_length=32, choices=TenantTier.choices, default=TenantTier.STARTER)
    is_popular = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'marketplace_apps'
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.category})"


class TenantAppInstallation(models.Model):
    """
    Tracks which marketplace apps and integrations are activated for a specific tenant.
    """
    id = models.CharField(max_length=64, primary_key=True, help_text="e.g. INST-001")
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='app_installations')
    app = models.ForeignKey(MarketplaceApp, on_delete=models.CASCADE, related_name='installations')
    is_active = models.BooleanField(default=True)
    config_data = models.JSONField(default=dict, blank=True)
    installed_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tenant_app_installations'
        unique_together = ('tenant', 'app')
        ordering = ['-installed_at']

    def __str__(self):
        return f"{self.tenant.name} -> {self.app.name} ({'Active' if self.is_active else 'Disabled'})"


class HistoricalUsageSnapshot(models.Model):
    """
    Daily / monthly historical usage metrics snapshot for tracking resource trends.
    """
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='usage_snapshots')
    snapshot_date = models.DateField(default=timezone.now)
    members_count = models.IntegerField(default=0)
    storage_used_mb = models.FloatField(default=0.0)
    ai_minutes_used = models.IntegerField(default=0)
    api_requests_count = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tenant_usage_snapshots'
        ordering = ['-snapshot_date']
        unique_together = ('tenant', 'snapshot_date')

    def __str__(self):
        return f"{self.tenant.name} - {self.snapshot_date}"


class TenantQuerySet(models.QuerySet):
    """
    QuerySet with tenant isolation helper.
    """
    def for_tenant(self, tenant_id):
        if not tenant_id:
            return self.none()
        return self.filter(tenant_id=tenant_id)


class TenantManager(models.Manager):
    """
    Manager to enforce tenant scoping across all tenant-aware models.
    """
    def get_queryset(self):
        return TenantQuerySet(self.model, using=self._db)

    def for_tenant(self, tenant_id):
        return self.get_queryset().for_tenant(tenant_id)


class TenantAwareModel(models.Model):
    """
    Abstract base class for every model that belongs to a specific tenant.
    Guarantees tenant ForeignKey and TenantManager.
    """
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_set"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantManager()

    class Meta:
        abstract = True

