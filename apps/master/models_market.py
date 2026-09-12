"""
Master DB — Marketplace & Integration Models (3 Tables)
Tables: marketplace_integrations, saas_plan_integrations, tenant_integration_entitlements
"""

import uuid
from django.db import models
from django.utils import timezone
from .models_tenant import Tenant
from .models_saas import SaasPlan


class MarketplaceIntegration(models.Model):
    """
    Verified third-party integrations available in the marketplace.
    Examples: Razorpay, TeleCMI, Gupshup, Meta, Timewatch, SES, ICICI.
    """
    INTEGRATION_TYPE = [
        ('PAYMENT', 'Payment Gateway'),
        ('CALLING', 'Voice Calling / IVR'),
        ('WHATSAPP', 'WhatsApp Messaging'),
        ('EMAIL', 'Email / SMTP'),
        ('LEADS', 'Lead Generation / Meta Ads'),
        ('ACCESS_CONTROL', 'Access Control / Biometrics'),
        ('ACCOUNTING', 'Accounting / ERP'),
        ('ANALYTICS', 'Analytics'),
        ('OTHER', 'Other'),
    ]
    STATUS = [('ACTIVE', 'Active'), ('INACTIVE', 'Inactive'), ('DEPRECATED', 'Deprecated')]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=100, unique=True, help_text='e.g. RAZORPAY, TELECMI, GUPSHUP')
    category = models.CharField(max_length=50, default='OTHER')
    integration_type = models.CharField(max_length=30, choices=INTEGRATION_TYPE)
    provider = models.CharField(max_length=100)
    description = models.TextField(blank=True, default='')
    configuration_schema = models.JSONField(null=True, blank=True)
    icon_text = models.CharField(max_length=10, blank=True, default='')
    developer = models.CharField(max_length=200, blank=True, default='')
    docs_url = models.TextField(blank=True, default='')
    price_monthly = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    is_free = models.BooleanField(default=True)
    is_popular = models.BooleanField(default=False)
    status = models.CharField(max_length=30, choices=STATUS, default='DRAFT')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'master'
        db_table = 'marketplace_integrations'
        ordering = ['integration_type', 'name']

    def __str__(self):
        return f"{self.name} ({self.integration_type})"


class SaasPlanIntegration(models.Model):
    """
    Which marketplace integrations are bundled with each SaaS plan.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.ForeignKey(SaasPlan, on_delete=models.RESTRICT, related_name='bundled_integrations')
    integration = models.ForeignKey(MarketplaceIntegration, on_delete=models.RESTRICT)
    is_included = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = 'master'
        db_table = 'saas_plan_integrations'
        unique_together = ('plan', 'integration')

    def __str__(self):
        return f"{self.plan.code} → {self.integration.code}"


class TenantIntegrationEntitlement(models.Model):
    """
    Per-tenant integration access records — either from plan bundle or explicit grant.
    """
    STATUS = [('AVAILABLE', 'Available'), ('ENABLED', 'Enabled'), ('ACTIVE', 'Active'), ('DISABLED', 'Disabled'), ('INACTIVE', 'Inactive'), ('SUSPENDED', 'Suspended')]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.RESTRICT, related_name='integration_entitlements')
    integration = models.ForeignKey(MarketplaceIntegration, on_delete=models.RESTRICT)
    status = models.CharField(max_length=30, choices=STATUS, default='AVAILABLE')
    is_from_plan = models.BooleanField(default=True, help_text='True if included via SaaS plan')
    enabled_at = models.DateTimeField(null=True, blank=True)
    disabled_at = models.DateTimeField(null=True, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'master'
        db_table = 'tenant_integration_entitlements'
        unique_together = ('tenant', 'integration')

    def __str__(self):
        return f"{self.tenant.slug} → {self.integration.code} [{self.status}]"

    def save(self, *args, **kwargs):
        if not self.enabled_at and self.activated_at:
            self.enabled_at = self.activated_at
        elif not self.activated_at and self.enabled_at:
            self.activated_at = self.enabled_at
        if not self.disabled_at and self.deactivated_at:
            self.disabled_at = self.deactivated_at
        elif not self.deactivated_at and self.disabled_at:
            self.deactivated_at = self.disabled_at
        super().save(*args, **kwargs)
