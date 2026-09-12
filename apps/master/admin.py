"""
Master DB Admin Configuration — Phase 1 Layer 1
Provides full inline editing and dynamic configuration for:
- SaaS Plans (pricing, included modules, resource limits)
- Product Modules (submodules and permission catalogs)
- Tenants (domains, branding, modules, subscriptions, data sources)
- Resource Metrics and Usage
- Marketplace Integrations
- Platform IAM (Users, Roles, Departments, Permissions)
"""

from django.contrib import admin
from django.utils.html import format_html

# IAM Models
from .models_iam import (
    PlatformUser, PlatformRole, PlatformDepartment,
    PlatformUserRole, PlatformModule, PlatformSubmodule,
    PlatformPermission, PlatformUserDepartment
)
# Tenant Registry Models
from .models_tenant import (
    Tenant, TenantDomain, TenantBranding, PlatformBranding
)
# SaaS Billing Models
from .models_saas import (
    SaasPlan, SaasPlanPrice, SaasPlanModule, SaasPlanResourceLimit,
    ProductModule, ProductSubmodule, TenantPermissionCatalog,
    TenantModule, TenantSubscription, ResourceMetric,
    TenantResourceLimit, TenantResourceUsage, TenantUsageAlert,
    SubscriptionInvoice, SubscriptionPayment, SubscriptionDunningEvent,
    TenantBillingMethod
)
# Marketplace Models
from .models_market import (
    MarketplaceIntegration, SaasPlanIntegration, TenantIntegrationEntitlement
)
# Infrastructure & Control Plane Models
from .models_infra import (
    TenantDataSource, TenantDataHostingPolicy, TenantDataSourceHealth,
    TenantProvisioning, PlatformSetting, PlatformAuditEvent
)


# ===========================================================================
# Inlines for SaaS Plans
# ===========================================================================

class SaasPlanPriceInline(admin.TabularInline):
    model = SaasPlanPrice
    extra = 1
    fields = ['billing_cycle', 'currency', 'amount', 'is_active']


class SaasPlanModuleInline(admin.TabularInline):
    model = SaasPlanModule
    extra = 1
    autocomplete_fields = ['module']
    fields = ['module', 'is_included', 'usage_limit']


class SaasPlanResourceLimitInline(admin.TabularInline):
    model = SaasPlanResourceLimit
    extra = 1
    autocomplete_fields = ['metric']
    fields = ['metric', 'limit_value', 'soft_limit_pct']


@admin.register(SaasPlan)
class SaasPlanAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'tier', 'trial_days', 'is_public', 'is_popular', 'is_active', 'sort_order']
    list_filter = ['is_active', 'is_public', 'tier']
    search_fields = ['name', 'code', 'description']
    inlines = [SaasPlanPriceInline, SaasPlanModuleInline, SaasPlanResourceLimitInline]
    ordering = ['sort_order', 'name']


# ===========================================================================
# Inlines for Product Modules
# ===========================================================================

class ProductSubmoduleInline(admin.TabularInline):
    model = ProductSubmodule
    extra = 1
    fields = ['code', 'name', 'description', 'sort_order', 'is_active']


@admin.register(ProductModule)
class ProductModuleAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'icon', 'is_core', 'is_active', 'sort_order']
    list_filter = ['is_core', 'is_active']
    search_fields = ['name', 'code']
    inlines = [ProductSubmoduleInline]
    ordering = ['sort_order', 'name']


@admin.register(ProductSubmodule)
class ProductSubmoduleAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'module', 'sort_order', 'is_active']
    list_filter = ['module', 'is_active']
    search_fields = ['name', 'code']


@admin.register(TenantPermissionCatalog)
class TenantPermissionCatalogAdmin(admin.ModelAdmin):
    list_display = ['code', 'label', 'module', 'submodule', 'action', 'is_active']
    list_filter = ['module', 'action', 'is_active']
    search_fields = ['code', 'label']


@admin.register(ResourceMetric)
class ResourceMetricAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'unit', 'is_billable', 'is_active']
    list_filter = ['is_billable', 'is_active']
    search_fields = ['name', 'code']


# ===========================================================================
# Inlines for Tenants
# ===========================================================================

class TenantDomainInline(admin.TabularInline):
    model = TenantDomain
    extra = 0
    fields = ['domain', 'domain_type', 'is_primary', 'is_verified', 'ssl_status']


class TenantBrandingInline(admin.StackedInline):
    model = TenantBranding
    extra = 0
    fields = ['branding_mode', 'app_name', 'primary_color', 'accent_color', 'logo_url']


class TenantModuleInline(admin.TabularInline):
    model = TenantModule
    extra = 0
    fields = ['module', 'availability_mode', 'is_enabled', 'enabled_by_plan']
    autocomplete_fields = ['module']


class TenantSubscriptionInline(admin.TabularInline):
    model = TenantSubscription
    extra = 0
    fields = ['plan', 'billing_cycle', 'status', 'current_period_end', 'trial_ends_at']
    readonly_fields = ['created_at']


class TenantDataSourceInline(admin.TabularInline):
    model = TenantDataSource
    extra = 0
    fields = ['db_name', 'source_type', 'db_host', 'status', 'provisioned_at']
    readonly_fields = ['provisioned_at']


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug', 'code', 'status', 'country', 'created_at']
    list_filter = ['status', 'country']
    search_fields = ['name', 'slug', 'code']
    readonly_fields = ['id', 'activated_at', 'suspended_at', 'deactivated_at', 'terminated_at', 'created_at', 'updated_at']
    inlines = [TenantDomainInline, TenantBrandingInline, TenantModuleInline, TenantSubscriptionInline, TenantDataSourceInline]
    ordering = ['-created_at']


# ===========================================================================
# Billing & Subscriptions Admin
# ===========================================================================

@admin.register(TenantSubscription)
class TenantSubscriptionAdmin(admin.ModelAdmin):
    list_display = ['tenant', 'plan', 'billing_cycle', 'status', 'current_period_end']
    list_filter = ['status', 'billing_cycle']
    search_fields = ['tenant__name', 'plan__name']


@admin.register(SubscriptionInvoice)
class SubscriptionInvoiceAdmin(admin.ModelAdmin):
    list_display = ['invoice_number', 'tenant', 'total', 'currency', 'status', 'due_date', 'paid_at']
    list_filter = ['status', 'currency']
    search_fields = ['invoice_number', 'tenant__name']


@admin.register(SubscriptionPayment)
class SubscriptionPaymentAdmin(admin.ModelAdmin):
    list_display = ['id', 'tenant', 'amount', 'provider', 'status', 'created_at']
    list_filter = ['status', 'provider']
    search_fields = ['provider_payment_id', 'provider_order_id']


# ===========================================================================
# Platform IAM Admin
# ===========================================================================

@admin.register(PlatformUser)
class PlatformUserAdmin(admin.ModelAdmin):
    list_display = ['email', 'first_name', 'last_name', 'status', 'is_staff', 'is_mfa_enabled', 'created_at']
    list_filter = ['status', 'is_staff', 'is_mfa_enabled']
    search_fields = ['email', 'first_name', 'last_name']
    readonly_fields = ['id', 'created_at', 'updated_at']


@admin.register(PlatformRole)
class PlatformRoleAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'is_system_role', 'is_active']
    list_filter = ['is_system_role', 'is_active']
    search_fields = ['name', 'code']


@admin.register(PlatformDepartment)
class PlatformDepartmentAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'is_active']
    list_filter = ['is_active']
    search_fields = ['name', 'code']


@admin.register(PlatformModule)
class PlatformModuleAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'display_order', 'is_active']
    list_filter = ['is_active']
    ordering = ['display_order']


# ===========================================================================
# Infrastructure & Control Plane Admin
# ===========================================================================

@admin.register(TenantDataSource)
class TenantDataSourceAdmin(admin.ModelAdmin):
    list_display = ['tenant', 'db_name', 'source_type', 'db_host', 'status', 'provisioned_at']
    list_filter = ['status', 'source_type']
    readonly_fields = ['id', 'provisioned_at', 'created_at']


@admin.register(TenantProvisioning)
class TenantProvisioningAdmin(admin.ModelAdmin):
    list_display = ['tenant', 'status', 'completed_steps', 'total_steps', 'started_at', 'completed_at']
    list_filter = ['status']
    readonly_fields = ['id', 'step_log', 'started_at', 'completed_at', 'created_at']


@admin.register(PlatformSetting)
class PlatformSettingAdmin(admin.ModelAdmin):
    list_display = ['key', 'data_type', 'is_public', 'updated_at']
    list_filter = ['data_type', 'is_public']
    search_fields = ['key', 'description']


@admin.register(PlatformAuditEvent)
class PlatformAuditEventAdmin(admin.ModelAdmin):
    list_display = ['action', 'resource_type', 'resource_id', 'actor_email', 'created_at']
    list_filter = ['action', 'resource_type']
    readonly_fields = ['id', 'created_at']
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False


# ===========================================================================
# Marketplace Admin
# ===========================================================================

@admin.register(MarketplaceIntegration)
class MarketplaceIntegrationAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'integration_type', 'provider', 'is_free', 'status']
    list_filter = ['integration_type', 'status', 'is_free']
    search_fields = ['name', 'code', 'provider']


# Standalone registrations for auxiliary models
admin.site.register(TenantDomain)
admin.site.register(TenantBranding)
admin.site.register(PlatformBranding)
admin.site.register(SaasPlanPrice)
admin.site.register(SaasPlanModule)
admin.site.register(SaasPlanResourceLimit)
admin.site.register(TenantModule)
admin.site.register(TenantResourceLimit)
admin.site.register(TenantResourceUsage)
admin.site.register(TenantUsageAlert)
admin.site.register(TenantBillingMethod)
admin.site.register(SubscriptionDunningEvent)
admin.site.register(PlatformUserRole)
admin.site.register(PlatformUserDepartment)
admin.site.register(PlatformPermission)
admin.site.register(SaasPlanIntegration)
admin.site.register(TenantIntegrationEntitlement)
admin.site.register(TenantDataHostingPolicy)
admin.site.register(TenantDataSourceHealth)
