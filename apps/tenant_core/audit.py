"""
apps/tenant_core/audit.py — Tenant Audit Trail Core Service.

Provides:
- emit_audit_event(): Deterministic, transaction-safe audit logging for tenant mutations.
- sanitize_audit_state(): Recursive redactor for sensitive keys (passwords, tokens, secrets).
- snapshot_model_state(): Pre/post mutation dictionary serialization of model instances.
- resolve_actor(): Strict server-side derivation of actor type and identity (no client spoofing).
- resolve_scope(): Invariant extraction of organization/branch/location/entity from persisted models.
"""

import uuid
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional, Tuple, Dict

from django.db import models
from django.core.exceptions import PermissionDenied
from rest_framework.exceptions import AuthenticationFailed

from config.routers import get_tenant_db_alias
from .models_privacy import TenantAuditEvent
from .models_users import TenantUser
from apps.master.models_iam import PlatformUser

logger = logging.getLogger(__name__)

# Sensitive key patterns that must be redacted recursively
SENSITIVE_KEY_PATTERNS = {
    'password', 'password_hash', 'token', 'access', 'refresh',
    'secret', 'api_key', 'authorization', 'cookie', 'cvv',
    'credit_card', 'card_number', 'mfa', 'otp', 'private_key',
    'pass_hash', 'secret_key', 'auth_token'
}


def sanitize_audit_state(data: Any) -> Any:
    """
    Recursively sanitize a dictionary, list, or scalar value.
    Replaces any value whose key matches SENSITIVE_KEY_PATTERNS with '[REDACTED]'.
    Ensures datetimes, UUIDs, decimals are serializable into JSON.
    """
    if data is None:
        return None

    if isinstance(data, dict):
        sanitized = {}
        for key, val in data.items():
            key_str = str(key).lower()
            if any(pattern in key_str for pattern in SENSITIVE_KEY_PATTERNS):
                sanitized[key] = '[REDACTED]'
            else:
                sanitized[key] = sanitize_audit_state(val)
        return sanitized

    if isinstance(data, (list, tuple, set)):
        return [sanitize_audit_state(item) for item in data]

    if isinstance(data, (uuid.UUID, date, datetime, Decimal)):
        return str(data)

    if isinstance(data, (int, float, bool, str)):
        return data

    if hasattr(data, 'pk'):
        return str(data.pk)

    return str(data)


def snapshot_model_state(instance: Optional[models.Model]) -> Optional[Dict[str, Any]]:
    """
    Capture a clean, sanitized dictionary snapshot of a model instance.
    Extracts all concrete database fields and foreign key IDs.
    """
    if instance is None or not isinstance(instance, models.Model):
        return None

    state = {}
    for field in instance._meta.concrete_fields:
        field_name = field.name
        val = getattr(instance, field.attname, None)
        if isinstance(val, (uuid.UUID, date, datetime, Decimal)):
            val = str(val)
        state[field_name] = val

    return sanitize_audit_state(state)


def resolve_actor(
    request=None,
    actor=None,
    actor_type: Optional[str] = None,
    actor_email: Optional[str] = None,
) -> Tuple[Optional[TenantUser], str, str]:
    """
    Deterministically resolves (actor_instance, actor_type, actor_email).
    Enforces that actor details are derived exclusively from verified server-side context.
    Never inspects request.data or client headers for actor identity.
    """
    # 1. Explicit internal system caller (e.g. background job, migration runner, worker)
    if actor_type == 'SYSTEM_JOB' and request is None:
        return None, 'SYSTEM_JOB', actor_email or 'system@internal'

    # 2. If request provided, inspect verified authenticated context
    if request is not None:
        # Check for verified integration context
        if getattr(request, 'is_integration_request', False):
            ident = getattr(request, 'integration_identifier', 'integration@api')
            return None, 'INTEGRATION', ident

        user = getattr(request, 'user', None)
        if not user or not getattr(user, 'is_authenticated', False):
            # Anonymous or invalid authentication NEVER becomes SYSTEM_JOB
            raise AuthenticationFailed("Cannot emit audit event for unauthenticated or anonymous caller.")

        auth_type = getattr(user, '_auth_type', None)

        if isinstance(user, TenantUser) or auth_type == 'tenant':
            return user, 'TENANT_USER', user.email or ''

        if isinstance(user, PlatformUser) or auth_type == 'platform':
            # Platform users have no foreign key in the tenant DB users table
            return None, 'SUPER_ADMIN', user.email or ''

        # Unknown authenticated user type
        user_cls = user.__class__.__name__
        if user_cls == 'TenantUser':
            return user, 'TENANT_USER', getattr(user, 'email', '')
        if user_cls == 'PlatformUser':
            return None, 'SUPER_ADMIN', getattr(user, 'email', '')

        raise PermissionDenied(f"Unrecognized authenticated user type '{user_cls}' for audit logging.")

    # 3. Direct programmatic caller without request
    if actor is not None and isinstance(actor, TenantUser):
        return actor, 'TENANT_USER', actor.email or ''

    if actor_type in ['TENANT_USER', 'SUPER_ADMIN', 'INTEGRATION', 'SYSTEM_JOB']:
        return None, actor_type, actor_email or ''

    raise PermissionDenied("Cannot resolve audit actor without verified request or valid system context.")


def resolve_scope(
    instance: Optional[models.Model] = None,
    request=None,
    db_alias: Optional[str] = None,
) -> Tuple[Optional[uuid.UUID], Optional[uuid.UUID], Optional[uuid.UUID], Optional[uuid.UUID]]:
    """
    Resolves (organization_id, company_entity_id, location_id, branch_id) from the
    persisted model instance. Never trusts request.data.
    """
    org_id = None
    ce_id = None
    loc_id = None
    br_id = None

    if instance is not None and isinstance(instance, models.Model):
        from .models_org import Organization, Branch, Location, CompanyEntity

        if isinstance(instance, Organization):
            org_id = instance.id
        elif isinstance(instance, Branch):
            br_id = instance.id
            loc_id = instance.location_id
            org_id = instance.organization_id
            ce_id = instance.company_entity_id
        elif isinstance(instance, Location):
            loc_id = instance.id
            org_id = instance.organization_id
            ce_id = instance.company_entity_id
        elif isinstance(instance, CompanyEntity):
            ce_id = instance.id
            org_id = instance.organization_id
        else:
            org_id = getattr(instance, 'organization_id', None)
            ce_id = getattr(instance, 'company_entity_id', None)
            loc_id = getattr(instance, 'location_id', None)
            br_id = (
                getattr(instance, 'branch_id', None) or
                getattr(instance, 'home_branch_id', None)
            )

            # If branch is present but org_id is missing, infer org from branch
            if not org_id and br_id and hasattr(instance, 'branch') and instance.branch:
                org_id = getattr(instance.branch, 'organization_id', None)

    # If org_id is still unknown, infer from active tenant organization in DB
    if not org_id and db_alias:
        try:
            from .models_org import Organization
            first_org = Organization.objects.using(db_alias).first()
            if first_org:
                org_id = first_org.id
        except Exception:
            pass

    return org_id, ce_id, loc_id, br_id


def emit_audit_event(
    action: str,
    resource_type: str,
    resource_id: Optional[str] = None,
    request=None,
    instance: Optional[models.Model] = None,
    before_state: Optional[dict] = None,
    after_state: Optional[dict] = None,
    description: str = '',
    actor=None,
    actor_type: Optional[str] = None,
    actor_email: Optional[str] = None,
    organization_id: Optional[uuid.UUID] = None,
    company_entity_id: Optional[uuid.UUID] = None,
    location_id: Optional[uuid.UUID] = None,
    branch_id: Optional[uuid.UUID] = None,
    db_alias: Optional[str] = None,
) -> TenantAuditEvent:
    """
    Emits an immutable TenantAuditEvent into the dedicated tenant database.

    Guarantees:
    - Must participate in the caller's transaction.atomic(using=db_alias).
    - Fails closed if no tenant database context is active.
    - Resolves actor strictly from server-side context (no client trust).
    - Derives scope from the persisted instance.
    - Recursively sanitizes before_state and after_state.
    """
    # 1. Resolve tenant database alias (strictly fail closed)
    target_db = db_alias or get_tenant_db_alias()
    if not target_db or target_db == 'default':
        raise PermissionDenied(
            "Tenant database context is required to record audit events. "
            "Writing tenant audit records to the Master DB ('default') is strictly blocked."
        )

    # 2. Resolve actor
    actor_obj, resolved_actor_type, resolved_actor_email = resolve_actor(
        request=request,
        actor=actor,
        actor_type=actor_type,
        actor_email=actor_email,
    )

    # 3. Resolve scope
    inferred_org, inferred_ce, inferred_loc, inferred_br = resolve_scope(
        instance=instance,
        request=request,
        db_alias=target_db,
    )
    final_org_id = organization_id or inferred_org
    final_ce_id = company_entity_id or inferred_ce
    final_loc_id = location_id or inferred_loc
    final_br_id = branch_id or inferred_br

    # 4. Resolve tracing IDs from request (CorrelationIDMiddleware)
    request_id = ''
    correlation_id = ''
    source_application = 'web_admin'
    ip_address = None
    user_agent = ''

    if request is not None:
        request_id = getattr(request, 'request_id', '') or str(uuid.uuid4())
        correlation_id = getattr(request, 'correlation_id', '') or str(uuid.uuid4())
        source_application = request.META.get('HTTP_X_SOURCE_APPLICATION', 'web_admin')
        ip_address = request.META.get('REMOTE_ADDR')
        user_agent = request.META.get('HTTP_USER_AGENT', '')
    else:
        request_id = str(uuid.uuid4())
        correlation_id = str(uuid.uuid4())
        source_application = 'system_internal'

    # 5. Redact states
    sanitized_before = sanitize_audit_state(before_state)
    sanitized_after = sanitize_audit_state(after_state)

    final_resource_id = str(resource_id or (getattr(instance, 'pk', '') if instance else ''))
    final_desc = description or f"[{action}] {resource_type} ({final_resource_id})"

    # 6. Persist to dedicated tenant DB
    audit_event = TenantAuditEvent.objects.using(target_db).create(
        actor=actor_obj,
        actor_type=resolved_actor_type,
        actor_email=resolved_actor_email,
        organization_id=final_org_id,
        company_entity_id=final_ce_id,
        location_id=final_loc_id,
        branch_id=final_br_id,
        action=action,
        resource_type=resource_type,
        resource_id=final_resource_id,
        description=final_desc,
        before_state=sanitized_before,
        after_state=sanitized_after,
        ip_address=ip_address,
        user_agent=user_agent,
        request_id=request_id,
        correlation_id=correlation_id,
        source_application=source_application,
    )

    return audit_event
