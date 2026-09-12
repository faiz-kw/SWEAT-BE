from django.apps import AppConfig


class TenantCoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.tenant_core'
    label = 'tenant_core'
    verbose_name = 'Tenant Core (Dedicated DB)'
