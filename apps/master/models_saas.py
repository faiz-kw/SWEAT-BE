"""
Master DB — SaaS Billing Models (13 Tables)
Tables: saas_plans, saas_plan_prices, saas_plan_modules, resource_metrics,
        saas_plan_resource_limits, tenant_resource_limits, tenant_resource_usage,
        tenant_usage_alerts, tenant_subscriptions, tenant_billing_methods,
        subscription_invoices, subscription_payments, subscription_dunning_events
"""

import uuid
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from .models_tenant import Tenant


class SaasPlan(models.Model):
    """
    SaaS subscription plans offered by the platform (e.g. Starter, Growth, Enterprise).
    """
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('ACTIVE', 'Active'),
        ('ARCHIVED', 'Archived'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=100, unique=True, help_text='e.g. PLAN-STARTER')
    description = models.TextField(blank=True, default='')
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='DRAFT')
    is_custom = models.BooleanField(default=False)
    is_featured = models.BooleanField(default=False)
    tier = models.CharField(max_length=50, default='standard', help_text='e.g. standard, premium, enterprise')
    is_public = models.BooleanField(default=True, help_text='Visible in pricing page')
    is_popular = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    trial_days = models.IntegerField(default=0)
    sort_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'master'
        db_table = 'saas_plans'
        ordering = ['sort_order', 'name']

    def __str__(self):
        return f"{self.name} ({self.code})"

    def save(self, *args, **kwargs):
        if self.status == 'ACTIVE':
            self.is_active = True
        elif self.status in ('ARCHIVED', 'DRAFT') and not self.is_active:
            pass
        elif not self.is_active and self.status == 'ACTIVE':
            self.status = 'ARCHIVED'
        super().save(*args, **kwargs)


class SaasPlanPrice(models.Model):
    """
    Billing intervals and pricing for each plan (monthly, annual).
    """
    BILLING_CYCLE = [
        ('MONTHLY', 'Monthly'),
        ('ANNUAL', 'Annual'),
        ('QUARTERLY', 'Quarterly'),
    ]
    STATUS_CHOICES = [
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
        ('ARCHIVED', 'Archived'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.ForeignKey(SaasPlan, on_delete=models.RESTRICT, related_name='prices')
    billing_cycle = models.CharField(max_length=30, choices=BILLING_CYCLE)
    currency = models.CharField(max_length=10, default='INR')
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    effective_from = models.DateTimeField(default=timezone.now, blank=True, null=True)
    effective_until = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='ACTIVE')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = 'master'
        db_table = 'saas_plan_prices'
        unique_together = ('plan', 'billing_cycle', 'currency')

    def __str__(self):
        return f"{self.plan.code} / {self.billing_cycle} / {self.currency} {self.amount}"

    def save(self, *args, **kwargs):
        if self.status == 'ACTIVE':
            self.is_active = True
        elif self.status == 'INACTIVE':
            self.is_active = False
        super().save(*args, **kwargs)


class ProductModule(models.Model):
    """
    Product modules available in the platform (e.g. CRM, Bookings, Nutrition, AI Voice).
    Tenant modules are enabled/disabled per plan or explicit entitlement.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=100, unique=True, help_text='e.g. CRM, BOOKINGS, NUTRITION, AI_VOICE')
    description = models.TextField(blank=True, default='')
    icon = models.CharField(max_length=100, blank=True, default='')
    supports_branch_scope = models.BooleanField(default=False)
    display_order = models.IntegerField(default=0)
    status = models.CharField(max_length=30, default='ACTIVE')
    is_core = models.BooleanField(default=False, help_text='Core modules always included in all plans')
    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'master'
        db_table = 'product_modules'
        ordering = ['display_order', 'sort_order', 'name']

    def __str__(self):
        return f"{self.name} ({self.code})"

    def save(self, *args, **kwargs):
        if not self.is_active and self.status == 'ACTIVE':
            self.status = 'INACTIVE'
        elif self.is_active and self.status != 'ACTIVE':
            self.status = 'ACTIVE'
        elif self.status == 'ACTIVE':
            self.is_active = True
        else:
            self.is_active = False
        if self.display_order and not self.sort_order:
            self.sort_order = self.display_order
        elif self.sort_order and not self.display_order:
            self.display_order = self.sort_order
        super().save(*args, **kwargs)


class ProductSubmodule(models.Model):
    """
    Sub-sections within product modules (e.g. CRM → Leads, CRM → Pipeline).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    module = models.ForeignKey(ProductModule, on_delete=models.RESTRICT, related_name='submodules')
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=100)
    description = models.TextField(blank=True, default='')
    display_order = models.IntegerField(default=0)
    status = models.CharField(max_length=30, default='ACTIVE')
    sort_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'master'
        db_table = 'product_submodules'
        unique_together = ('module', 'code')
        ordering = ['module', 'display_order', 'sort_order']

    def __str__(self):
        return f"{self.module.code} → {self.code}"

    def save(self, *args, **kwargs):
        if not self.is_active and self.status == 'ACTIVE':
            self.status = 'INACTIVE'
        elif self.is_active and self.status != 'ACTIVE':
            self.status = 'ACTIVE'
        elif self.status == 'ACTIVE':
            self.is_active = True
        else:
            self.is_active = False
        if self.display_order and not self.sort_order:
            self.sort_order = self.display_order
        elif self.sort_order and not self.display_order:
            self.display_order = self.sort_order
        super().save(*args, **kwargs)


class SaasPlanModule(models.Model):
    """
    Which product modules are included in each SaaS plan.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.ForeignKey(SaasPlan, on_delete=models.RESTRICT, related_name='plan_modules')
    module = models.ForeignKey(ProductModule, on_delete=models.RESTRICT, related_name='plan_inclusions')
    is_included = models.BooleanField(default=True)
    usage_limit = models.IntegerField(null=True, blank=True, help_text='NULL = unlimited')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = 'master'
        db_table = 'saas_plan_modules'
        unique_together = ('plan', 'module')

    def __str__(self):
        return f"{self.plan.code} → {self.module.code}: {'✓' if self.is_included else '✗'}"


class TenantPermissionCatalog(models.Model):
    """
    Platform-managed permission definitions synchronized into each tenant's DB.
    These are the seed permissions that all tenants share for the RBAC catalogue.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    module = models.ForeignKey(ProductModule, on_delete=models.RESTRICT, related_name='tenant_permissions')
    submodule = models.ForeignKey(ProductSubmodule, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=50, help_text='e.g. view, create, edit, delete, export')
    code = models.CharField(max_length=150, unique=True, help_text='e.g. crm.leads.view')
    label = models.CharField(max_length=200)
    description = models.TextField(blank=True, default='')
    version = models.IntegerField(default=1)
    status = models.CharField(max_length=30, default='ACTIVE')
    is_active = models.BooleanField(default=True)
    catalog_version = models.CharField(max_length=20, default='1.0', help_text='Catalog version for sync')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'master'
        db_table = 'tenant_permission_catalog'
        ordering = ['module', 'action']

    def __str__(self):
        return f"{self.code} — {self.label}"

    def save(self, *args, **kwargs):
        if not self.is_active and self.status == 'ACTIVE':
            self.status = 'INACTIVE'
        elif self.is_active and self.status != 'ACTIVE':
            self.status = 'ACTIVE'
        elif self.status == 'ACTIVE':
            self.is_active = True
        else:
            self.is_active = False
        super().save(*args, **kwargs)


class TenantModule(models.Model):
    """
    Per-tenant module enablement record.
    Created when a tenant is assigned a plan or explicitly entitles a module.
    """
    AVAILABILITY_MODE = [
        ('ALL_BRANCHES', 'Available to all branches automatically'),
        ('SELECTED_BRANCHES', 'Available only to explicitly selected branches'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.RESTRICT, related_name='enabled_modules_set')
    module = models.ForeignKey(ProductModule, on_delete=models.RESTRICT)
    availability_mode = models.CharField(max_length=30, choices=AVAILABILITY_MODE, default='ALL_BRANCHES')
    status = models.CharField(max_length=30, default='ENABLED')
    configuration = models.JSONField(null=True, blank=True)
    is_enabled = models.BooleanField(default=True)
    enabled_at = models.DateTimeField(default=timezone.now)
    disabled_at = models.DateTimeField(null=True, blank=True)
    enabled_by_plan = models.BooleanField(default=True, help_text='True if included via plan, False if manual override')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'master'
        db_table = 'tenant_modules'
        unique_together = ('tenant', 'module')

    def __str__(self):
        return f"{self.tenant.slug} → {self.module.code} ({self.availability_mode})"

    def save(self, *args, **kwargs):
        if not self.is_enabled and self.status == 'ENABLED':
            self.status = 'DISABLED'
        elif self.is_enabled and self.status in ('DISABLED', 'SUSPENDED'):
            self.status = 'ENABLED'
        elif self.status == 'ENABLED':
            self.is_enabled = True
        else:
            self.is_enabled = False
        super().save(*args, **kwargs)


class ResourceMetric(models.Model):
    """
    Catalogue of measurable resource metrics (e.g. active_members, storage_gb, ai_minutes).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=100, unique=True, help_text='e.g. ACTIVE_MEMBERS, STORAGE_GB, AI_MINUTES')
    name = models.CharField(max_length=150)
    unit = models.CharField(max_length=50, help_text='e.g. count, GB, minutes, requests')
    description = models.TextField(blank=True, default='')
    aggregation_period = models.CharField(max_length=30, default='REALTIME')
    status = models.CharField(max_length=30, default='ACTIVE')
    is_billable = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = 'master'
        db_table = 'resource_metrics'
        ordering = ['code']

    def __str__(self):
        return f"{self.name} ({self.unit})"

    def save(self, *args, **kwargs):
        if self.status == 'ACTIVE':
            self.is_active = True
        elif self.status == 'INACTIVE':
            self.is_active = False
        super().save(*args, **kwargs)


class SaasPlanResourceLimit(models.Model):
    """
    Resource limits defined in each SaaS plan (e.g. Starter: max 500 active_members).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.ForeignKey(SaasPlan, on_delete=models.RESTRICT, related_name='resource_limits')
    metric = models.ForeignKey(ResourceMetric, on_delete=models.RESTRICT)
    limit_value = models.DecimalField(max_digits=20, decimal_places=4, default=-1, help_text='-1 = unlimited')
    is_unlimited = models.BooleanField(default=False)
    warning_percent = models.DecimalField(max_digits=5, decimal_places=2, default=80.00)
    soft_limit_pct = models.IntegerField(default=80, help_text='Alert threshold percentage')
    enforcement_mode = models.CharField(max_length=30, default='MONITOR_ONLY')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = 'master'
        db_table = 'saas_plan_resource_limits'
        unique_together = ('plan', 'metric')

    def __str__(self):
        return f"{self.plan.code} → {self.metric.code}: {self.limit_value}"

    def save(self, *args, **kwargs):
        if self.limit_value == -1:
            self.is_unlimited = True
        if not self.warning_percent and self.soft_limit_pct:
            self.warning_percent = self.soft_limit_pct
        super().save(*args, **kwargs)


class TenantResourceLimit(models.Model):
    """
    Per-tenant resource limits (override plan defaults or set custom limits).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.RESTRICT, related_name='resource_limits')
    metric = models.ForeignKey(ResourceMetric, on_delete=models.RESTRICT)
    limit_value = models.DecimalField(max_digits=20, decimal_places=4, default=-1, help_text='-1 = unlimited')
    is_unlimited = models.BooleanField(default=False)
    warning_threshold_percent = models.DecimalField(max_digits=5, decimal_places=2, default=80.00)
    is_plan_default = models.BooleanField(default=True, help_text='False if manually overridden')
    effective_from = models.DateTimeField(default=timezone.now)
    effective_until = models.DateTimeField(blank=True, null=True)
    override_reason = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'master'
        db_table = 'tenant_resource_limits'
        unique_together = ('tenant', 'metric')

    def __str__(self):
        return f"{self.tenant.slug} → {self.metric.code}: {self.limit_value}"

    def save(self, *args, **kwargs):
        if self.limit_value == -1:
            self.is_unlimited = True
        super().save(*args, **kwargs)


class TenantResourceUsage(models.Model):
    """
    Current resource usage per tenant per metric.
    Updated asynchronously via Celery workers.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.RESTRICT, related_name='resource_usage')
    metric = models.ForeignKey(ResourceMetric, on_delete=models.RESTRICT)
    current_value = models.BigIntegerField(default=0)
    usage_value = models.DecimalField(max_digits=20, decimal_places=4, default=0.0000)
    period_start = models.DateTimeField(blank=True, null=True)
    period_end = models.DateTimeField(blank=True, null=True)
    measured_at = models.DateTimeField(default=timezone.now)
    billing_period_start = models.DateField(null=True, blank=True)
    billing_period_end = models.DateField(null=True, blank=True)
    last_calculated_at = models.DateTimeField(default=timezone.now)
    is_platform_billable = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = 'master'
        db_table = 'tenant_resource_usage'
        unique_together = ('tenant', 'metric', 'billing_period_start')

    def __str__(self):
        return f"{self.tenant.slug} → {self.metric.code}: {self.usage_value}"

    def save(self, *args, **kwargs):
        if not self.usage_value and self.current_value:
            self.usage_value = self.current_value
        elif not self.current_value and self.usage_value:
            self.current_value = int(self.usage_value)
        if not self.measured_at and self.last_calculated_at:
            self.measured_at = self.last_calculated_at
        super().save(*args, **kwargs)


class TenantUsageAlert(models.Model):
    """
    Alerts triggered when tenant resource usage crosses thresholds.
    """
    ALERT_STATUS = [('OPEN', 'Open'), ('ACKNOWLEDGED', 'Acknowledged'), ('RESOLVED', 'Resolved')]
    ALERT_SEVERITY = [('INFO', 'Info'), ('WARNING', 'Warning'), ('CRITICAL', 'Critical')]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.RESTRICT, related_name='usage_alerts')
    metric = models.ForeignKey(ResourceMetric, on_delete=models.RESTRICT)
    usage_value = models.DecimalField(max_digits=20, decimal_places=4, default=0.0000)
    limit_value = models.DecimalField(max_digits=20, decimal_places=4, default=0.0000)
    threshold_percent = models.DecimalField(max_digits=5, decimal_places=2, default=80.00)
    threshold_pct = models.IntegerField(default=80)
    current_pct = models.IntegerField(default=0)
    severity = models.CharField(max_length=20, choices=ALERT_SEVERITY, default='WARNING')
    status = models.CharField(max_length=30, choices=ALERT_STATUS, default='OPEN')
    triggered_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)
    message = models.TextField(blank=True, default='')

    class Meta:
        app_label = 'master'
        db_table = 'tenant_usage_alerts'
        ordering = ['-triggered_at']

    def __str__(self):
        return f"[{self.severity}] {self.tenant.slug} → {self.metric.code} at {self.current_pct}%"


class TenantSubscription(models.Model):
    """
    Active subscription linking a tenant to a SaaS plan with billing cycle.
    """
    STATUS = [
        ('TRIALING', 'Trialing'),
        ('ACTIVE', 'Active'),
        ('PAST_DUE', 'Past Due'),
        ('PAUSED', 'Paused'),
        ('SUSPENDED', 'Suspended'),
        ('CANCELED', 'Canceled'),
        ('EXPIRED', 'Expired'),
    ]
    BILLING_CYCLE = [('MONTHLY', 'Monthly'), ('ANNUAL', 'Annual'), ('QUARTERLY', 'Quarterly')]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.RESTRICT, related_name='subscriptions')
    plan = models.ForeignKey(SaasPlan, on_delete=models.RESTRICT, related_name='subscriptions')
    plan_price = models.ForeignKey(SaasPlanPrice, on_delete=models.RESTRICT, null=True, blank=True)
    billing_cycle = models.CharField(max_length=30, choices=BILLING_CYCLE, default='MONTHLY')
    status = models.CharField(max_length=30, choices=STATUS, default='TRIALING')
    currency = models.CharField(max_length=10, default='INR')
    billing_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    started_at = models.DateTimeField(default=timezone.now, blank=True, null=True)
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    next_renewal_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.TextField(blank=True, default='')
    autopay_enabled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def canceled_at(self):
        return self.cancelled_at

    @canceled_at.setter
    def canceled_at(self, value):
        self.cancelled_at = value

    class Meta:
        app_label = 'master'
        db_table = 'tenant_subscriptions'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.tenant.slug} → {self.plan.code} ({self.status})"

    def save(self, *args, **kwargs):
        from datetime import timedelta
        now = timezone.now()
        if not self.started_at:
            self.started_at = now
        if not self.current_period_start:
            self.current_period_start = self.started_at or now
        if not self.current_period_end:
            if self.status == 'TRIALING' and self.trial_ends_at:
                self.current_period_end = self.trial_ends_at
            elif self.billing_cycle == 'ANNUAL':
                self.current_period_end = self.current_period_start + timedelta(days=365)
            elif self.billing_cycle == 'QUARTERLY':
                self.current_period_end = self.current_period_start + timedelta(days=90)
            else:
                self.current_period_end = self.current_period_start + timedelta(days=30)
        if not self.next_renewal_at:
            self.next_renewal_at = self.current_period_end
        if not self.billing_amount and self.plan_price:
            self.billing_amount = self.plan_price.amount
            if not self.currency:
                self.currency = self.plan_price.currency
        super().save(*args, **kwargs)


class TenantBillingMethod(models.Model):
    """
    Payment methods on file for a tenant (provider-neutral mandate, card, etc.)
    """
    METHOD_TYPE = [
        ('RAZORPAY_MANDATE', 'Razorpay Auto-Pay Mandate'),
        ('MANDATE', 'Auto-Pay Mandate'),
        ('BANK_TRANSFER', 'Bank Transfer / NEFT'),
        ('CARD', 'Credit/Debit Card'),
        ('CASH', 'Cash'),
        ('CUSTOM', 'Custom'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.RESTRICT, related_name='billing_methods')
    method_type = models.CharField(max_length=50, choices=METHOD_TYPE)
    provider = models.CharField(max_length=100, blank=True, default='')
    provider_customer_ref = models.CharField(max_length=255, blank=True, default='', null=True)
    provider_method_ref = models.CharField(max_length=255, blank=True, default='', null=True)
    mandate_reference = models.CharField(max_length=255, blank=True, default='', null=True)
    status = models.CharField(max_length=30, default='ACTIVE')
    provider_customer_id = models.CharField(max_length=255, blank=True, default='')
    provider_method_id = models.CharField(max_length=255, blank=True, default='')
    display_name = models.CharField(max_length=200, blank=True, default='')
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'master'
        db_table = 'tenant_billing_methods'
        ordering = ['-is_default', '-created_at']

    def __str__(self):
        return f"{self.tenant.slug} → {self.method_type} ({self.display_name})"

    def save(self, *args, **kwargs):
        if not self.provider_customer_ref and self.provider_customer_id:
            self.provider_customer_ref = self.provider_customer_id
        elif not self.provider_customer_id and self.provider_customer_ref:
            self.provider_customer_id = self.provider_customer_ref
        if not self.provider_method_ref and self.provider_method_id:
            self.provider_method_ref = self.provider_method_id
        elif not self.provider_method_id and self.provider_method_ref:
            self.provider_method_id = self.provider_method_ref
        super().save(*args, **kwargs)


class SubscriptionInvoice(models.Model):
    """
    Invoices generated for tenant subscriptions.
    """
    STATUS = [
        ('DRAFT', 'Draft'),
        ('ISSUED', 'Issued'),
        ('OPEN', 'Open'),
        ('PAID', 'Paid'),
        ('VOID', 'Void'),
        ('UNCOLLECTIBLE', 'Uncollectible'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscription = models.ForeignKey(TenantSubscription, on_delete=models.RESTRICT, related_name='invoices')
    tenant = models.ForeignKey(Tenant, on_delete=models.RESTRICT, related_name='subscription_invoices')
    invoice_number = models.CharField(max_length=100, unique=True)
    status = models.CharField(max_length=30, choices=STATUS, default='DRAFT')
    currency = models.CharField(max_length=10, default='INR')
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    tax_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    discount_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    total = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    billing_period_start = models.DateField(null=True, blank=True)
    billing_period_end = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    issued_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    invoice_file_key = models.TextField(blank=True, null=True)
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def pdf_url(self):
        return self.invoice_file_key

    @pdf_url.setter
    def pdf_url(self, value):
        self.invoice_file_key = value

    class Meta:
        app_label = 'master'
        db_table = 'subscription_invoices'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.invoice_number} — {self.tenant.slug} [{self.status}]"

    def clean(self):
        if self.pk:
            orig = SubscriptionInvoice.objects.filter(pk=self.pk).values(
                'status', 'subtotal', 'tax_amount', 'total_amount', 'currency', 'invoice_number'
            ).first()
            if orig and orig['status'] in ('ISSUED', 'PAID'):
                if (orig['total_amount'] != self.total_amount or
                    orig['subtotal'] != self.subtotal or
                    orig['tax_amount'] != self.tax_amount or
                    orig['currency'] != self.currency or
                    orig['invoice_number'] != self.invoice_number):
                    raise ValidationError("Issued or paid invoices are financially immutable.")
        super().clean()

    def save(self, *args, **kwargs):
        if self.total_amount is None or self.total_amount == Decimal('0.00'):
            if self.total:
                self.total_amount = self.total
        if self.total is None or self.total == Decimal('0.00'):
            if self.total_amount:
                self.total = self.total_amount
        if not self.billing_period_start:
            if self.subscription and self.subscription.current_period_start:
                self.billing_period_start = self.subscription.current_period_start.date()
            else:
                self.billing_period_start = timezone.now().date()
        if not self.billing_period_end:
            if self.subscription and self.subscription.current_period_end:
                self.billing_period_end = self.subscription.current_period_end.date()
            else:
                from datetime import timedelta
                self.billing_period_end = self.billing_period_start + timedelta(days=30)
        if not self.due_at and self.due_date:
            from datetime import datetime, time
            self.due_at = timezone.make_aware(datetime.combine(self.due_date, time.min))
        elif not self.due_date and self.due_at:
            self.due_date = self.due_at.date()
        self.clean()
        super().save(*args, **kwargs)


class SubscriptionPayment(models.Model):
    """
    Payment records linked to subscription invoices.
    """
    STATUS = [
        ('PENDING', 'Pending'),
        ('PROCESSING', 'Processing'),
        ('SUCCEEDED', 'Succeeded'),
        ('FAILED', 'Failed'),
        ('REFUNDED', 'Refunded'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey(SubscriptionInvoice, on_delete=models.RESTRICT, related_name='payments')
    tenant = models.ForeignKey(Tenant, on_delete=models.RESTRICT)
    billing_method = models.ForeignKey(TenantBillingMethod, on_delete=models.SET_NULL, null=True, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=10, default='INR')
    status = models.CharField(max_length=30, choices=STATUS, default='PENDING')
    provider = models.CharField(max_length=100, blank=True, default='', help_text='e.g. Razorpay, Stripe')
    provider_payment_id = models.CharField(max_length=255, blank=True, default='')
    provider_order_id = models.CharField(max_length=255, blank=True, default='')
    failure_code = models.CharField(max_length=100, blank=True, default='')
    failure_message = models.TextField(blank=True, default='')
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'master'
        db_table = 'subscription_payments'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.tenant.slug} → {self.amount} {self.currency} ({self.status})"


class SubscriptionDunningEvent(models.Model):
    """
    Dunning events — retry/recovery actions for failed payments.
    """
    EVENT_TYPE = [
        ('PAYMENT_FAILED', 'Payment Failed'),
        ('RETRY_SCHEDULED', 'Retry Scheduled'),
        ('RETRY_ATTEMPTED', 'Retry Attempted'),
        ('TENANT_NOTIFIED', 'Tenant Notified'),
        ('SUBSCRIPTION_SUSPENDED', 'Subscription Suspended'),
        ('SUBSCRIPTION_RECOVERED', 'Subscription Recovered'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.RESTRICT, null=True, related_name='dunning_events')
    subscription = models.ForeignKey(TenantSubscription, on_delete=models.RESTRICT, related_name='dunning_events')
    invoice = models.ForeignKey(SubscriptionInvoice, on_delete=models.RESTRICT, null=True, blank=True, related_name='dunning_events')
    payment = models.ForeignKey(SubscriptionPayment, on_delete=models.PROTECT, null=True, blank=True)
    event_type = models.CharField(max_length=50, choices=EVENT_TYPE)
    attempt_number = models.IntegerField(default=1)
    scheduled_at = models.DateTimeField(null=True, blank=True)
    executed_at = models.DateTimeField(null=True, blank=True)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    result = models.CharField(max_length=30, null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'master'
        db_table = 'subscription_dunning_events'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.event_type} — {self.subscription.tenant.slug} (attempt #{self.attempt_number})"

    def save(self, *args, **kwargs):
        if not self.tenant and self.subscription:
            self.tenant = self.subscription.tenant
        super().save(*args, **kwargs)


# ============================================================================
# NEW SUPPORTING BILLING ARCHITECTURE (Excluded from Canonical 751 tally)
# ============================================================================

class SubscriptionInvoiceItem(models.Model):
    """
    Supporting table: Individual line items for subscription invoices.
    NOT part of the canonical 751-field specification.
    """
    ITEM_TYPE = [
        ('BASE_PLAN', 'Base Plan Subscription'),
        ('ADDON', 'Addon / Extra Module'),
        ('USAGE_CHARGE', 'Metered Usage Charge'),
        ('DISCOUNT', 'Discount / Credit'),
        ('ADJUSTMENT', 'Manual Adjustment'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey(SubscriptionInvoice, on_delete=models.CASCADE, related_name='items')
    item_type = models.CharField(max_length=30, choices=ITEM_TYPE, default='BASE_PLAN')
    description = models.CharField(max_length=255)
    quantity = models.IntegerField(default=1)
    unit_price = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'))
    tax_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = 'master'
        db_table = 'subscription_invoice_items'
        ordering = ['created_at']

    def __str__(self):
        return f"Item: {self.description} ({self.total_amount})"


class BillingWebhookEvent(models.Model):
    """
    Supporting table: Webhook ingestion and idempotency log.
    NOT part of the canonical 751-field specification.
    """
    STATUS = [
        ('RECEIVED', 'Received'),
        ('PROCESSING', 'Processing'),
        ('PROCESSED', 'Processed'),
        ('FAILED', 'Failed'),
        ('IGNORED', 'Ignored'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=50)
    provider_event_id = models.CharField(max_length=255)
    event_type = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=STATUS, default='RECEIVED')
    signature_verified = models.BooleanField(default=False)
    retry_count = models.IntegerField(default=0)
    payload = models.JSONField(default=dict)
    received_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True, default='')

    class Meta:
        app_label = 'master'
        db_table = 'billing_webhook_events'
        unique_together = ('provider', 'provider_event_id')
        indexes = [
            models.Index(fields=['provider', 'status'], name='billing_web_provide_1f6213_idx'),
            models.Index(fields=['received_at'], name='billing_web_receive_8cf68d_idx'),
        ]

    def __str__(self):
        return f"Webhook [{self.provider}:{self.event_type}] ({self.status})"


class TenantInvoiceSequence(models.Model):
    """
    Supporting table: Sequence tracking for deterministic, concurrency-safe invoice numbering.
    NOT part of the canonical 751-field specification.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.RESTRICT, related_name='invoice_sequences')
    year = models.IntegerField()
    last_sequence = models.IntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'master'
        db_table = 'tenant_invoice_sequences'
        unique_together = ('tenant', 'year')

    def __str__(self):
        return f"{self.tenant.slug} — {self.year}: {self.last_sequence}"

