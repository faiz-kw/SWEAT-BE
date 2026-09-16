"""
Database Router for Phase 1 Layer 1 Multi-Tenant Architecture.

Routing rules:
  - apps.master models    → 'default' (Master/Control DB)
  - apps.tenant_core      → dynamic tenant DB alias (set per-request by TenantDatabaseMiddleware)
  - Django internals      → 'default'
"""

import threading

import uuid
from typing import Union

# Thread-local storage for active tenant DB alias per-request
_thread_local = threading.local()


def build_tenant_db_alias(tenant_id: Union[str, uuid.UUID]) -> str:
    """
    Build deterministic, tenant-unique database alias bound to tenant identity.
    Guarantees logical database isolation across tenants even if external database
    names collide on customer-managed databases.
    Format: tenant_<normalized_uuid_hex>
    """
    if not tenant_id:
        raise ValueError("tenant_id must not be empty.")
    if isinstance(tenant_id, uuid.UUID):
        normalized = tenant_id.hex
    else:
        normalized = uuid.UUID(str(tenant_id).strip()).hex
    return f"tenant_{normalized}"


def get_tenant_db_alias() -> str | None:
    """Return the active tenant DB alias for the current request thread."""
    return getattr(_thread_local, 'tenant_db_alias', None)


from django.core.exceptions import PermissionDenied


class TenantRoutingError(PermissionDenied):
    """
    Raised when a tenant-scoped model is accessed without an active tenant DB context.
    Fail-closed: tenant operations must NEVER fall back to Master DB ('default').
    """
    pass


def set_tenant_db_alias(alias: str | None) -> None:
    """Set the active tenant DB alias for the current request thread."""
    _thread_local.tenant_db_alias = alias


class MasterRouter:
    """
    Routes all models in apps.master to the Master/Control DB ('default').
    Platform IAM, tenant registry, billing, marketplace, provisioning all live here.
    """
    MASTER_APP_LABELS = {'master', 'token_blacklist', 'admin', 'auth', 'contenttypes', 'sessions'}

    def db_for_read(self, model, **hints):
        if model._meta.app_label in self.MASTER_APP_LABELS:
            return 'default'
        return None

    def db_for_write(self, model, **hints):
        if model._meta.app_label in self.MASTER_APP_LABELS:
            return 'default'
        return None

    def allow_relation(self, obj1, obj2, **hints):
        # Disallow relations between master models and tenant models
        if (obj1._meta.app_label in self.MASTER_APP_LABELS and obj2._meta.app_label == 'tenant_core') or \
           (obj2._meta.app_label in self.MASTER_APP_LABELS and obj1._meta.app_label == 'tenant_core'):
            return False
        # Allow relations within master or between master and auth internals
        if obj1._meta.app_label in self.MASTER_APP_LABELS and obj2._meta.app_label in self.MASTER_APP_LABELS:
            return True
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if app_label in self.MASTER_APP_LABELS:
            return db == 'default'
        return None


class TenantRouter:
    """
    Routes all models in apps.tenant_core to the active tenant's dedicated DB.
    The active tenant DB alias is set per-request by TenantDatabaseMiddleware.

    FAIL-CLOSED ARCHITECTURE:
    Tenant operations MUST NEVER fall back to 'default' (Master DB).
    If no tenant context is active when a tenant_core model is accessed,
    TenantRoutingError is raised explicitly.
    """
    TENANT_APP_LABEL = 'tenant_core'

    def db_for_read(self, model, **hints):
        if model._meta.app_label == self.TENANT_APP_LABEL:
            alias = get_tenant_db_alias()
            if not alias:
                raise TenantRoutingError(
                    f"Tenant database routing failed for read on '{model.__name__}': "
                    f"no tenant DB context is active. Requests targeting tenant-scoped models "
                    f"must provide a valid tenant context and must never route to the master database."
                )
            return alias
        return None

    def db_for_write(self, model, **hints):
        if model._meta.app_label == self.TENANT_APP_LABEL:
            alias = get_tenant_db_alias()
            if not alias:
                raise TenantRoutingError(
                    f"Tenant database routing failed for write on '{model.__name__}': "
                    f"no tenant DB context is active. Requests targeting tenant-scoped models "
                    f"must provide a valid tenant context and must never route to the master database."
                )
            return alias
        return None

    def allow_relation(self, obj1, obj2, **hints):
        if obj1._meta.app_label == self.TENANT_APP_LABEL and obj2._meta.app_label == self.TENANT_APP_LABEL:
            return True
        # Explicitly disallow relations between tenant models and non-tenant models
        if obj1._meta.app_label == self.TENANT_APP_LABEL or obj2._meta.app_label == self.TENANT_APP_LABEL:
            return False
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if app_label == self.TENANT_APP_LABEL:
            # Explicitly disallow running tenant_core migrations on the default/master database
            if db == 'default':
                return False
            # Allow migration only on explicitly named tenant databases
            return db.startswith('tenant_')
        return None
