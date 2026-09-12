"""
Master app models — re-exports from all model modules.
Django requires models to be importable from <app>.models for AUTH_USER_MODEL and migrations.
"""

from .models_iam import (
    PlatformUser, PlatformDepartment, PlatformUserDepartment,
    PlatformRole, PlatformModule, PlatformSubmodule, PlatformPermission,
    PlatformRoleModuleAccess, PlatformRoleSubmoduleAccess,
    PlatformRolePermission, PlatformUserRole,
)

from .models_tenant import (
    Tenant, TenantDomain, PlatformBranding, TenantBranding,
)

from .models_saas import (
    SaasPlan, SaasPlanPrice, ProductModule, ProductSubmodule,
    SaasPlanModule, TenantPermissionCatalog, TenantModule,
    ResourceMetric, SaasPlanResourceLimit, TenantResourceLimit,
    TenantResourceUsage, TenantUsageAlert, TenantSubscription,
    TenantBillingMethod, SubscriptionInvoice, SubscriptionPayment,
    SubscriptionDunningEvent,
    SubscriptionInvoiceItem, BillingWebhookEvent, TenantInvoiceSequence,
)

from .models_market import (
    MarketplaceIntegration, SaasPlanIntegration, TenantIntegrationEntitlement,
)

from .models_infra import (
    TenantDataSource, TenantDataHostingPolicy, TenantDataSourceHealth,
    TenantProvisioning, PlatformSetting, PlatformAuditEvent,
)

__all__ = [
    # IAM
    'PlatformUser', 'PlatformDepartment', 'PlatformUserDepartment',
    'PlatformRole', 'PlatformModule', 'PlatformSubmodule', 'PlatformPermission',
    'PlatformRoleModuleAccess', 'PlatformRoleSubmoduleAccess',
    'PlatformRolePermission', 'PlatformUserRole',
    # Tenant Registry
    'Tenant', 'TenantDomain', 'PlatformBranding', 'TenantBranding',
    # SaaS
    'SaasPlan', 'SaasPlanPrice', 'ProductModule', 'ProductSubmodule',
    'SaasPlanModule', 'TenantPermissionCatalog', 'TenantModule',
    'ResourceMetric', 'SaasPlanResourceLimit', 'TenantResourceLimit',
    'TenantResourceUsage', 'TenantUsageAlert', 'TenantSubscription',
    'TenantBillingMethod', 'SubscriptionInvoice', 'SubscriptionPayment',
    'SubscriptionDunningEvent',
    'SubscriptionInvoiceItem', 'BillingWebhookEvent', 'TenantInvoiceSequence',
    # Marketplace
    'MarketplaceIntegration', 'SaasPlanIntegration', 'TenantIntegrationEntitlement',
    # Infrastructure
    'TenantDataSource', 'TenantDataHostingPolicy', 'TenantDataSourceHealth',
    'TenantProvisioning', 'PlatformSetting', 'PlatformAuditEvent',
]
