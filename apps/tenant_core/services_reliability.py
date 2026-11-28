"""
apps/tenant_core/services_reliability.py — Layer 2 Reliability, Idempotency & Outbox Engine
"""

import hashlib
import json
import logging
import uuid
from decimal import Decimal
from typing import Any, Callable, Dict, Optional
from datetime import timedelta
from django.utils import timezone
from django.db import transaction

from .models_audit_outbox import BusinessAuditEvent, IdempotencyRecord, DomainOutboxEvent, ReasonCode
from .models_org import Organization, Branch
from .models_users import TenantUser

logger = logging.getLogger(__name__)

SENSITIVE_KEYS = {
    'password', 'token', 'secret', 'key', 'cvv', 'card_number', 'pan',
    'medical', 'health_notes', 'intake_medical_answers', 'auth'
}


def sanitize_payload(data: Any) -> Any:
    """
    Recursively strip or mask sensitive keys from audit payloads and ensure JSON-safe primitives.
    """
    if isinstance(data, dict):
        sanitized = {}
        for k, v in data.items():
            if any(sensitive in k.lower() for sensitive in SENSITIVE_KEYS):
                sanitized[k] = '[REDACTED]'
            else:
                sanitized[k] = sanitize_payload(v)
        return sanitized
    elif isinstance(data, list):
        return [sanitize_payload(item) for item in data]
    elif isinstance(data, Decimal):
        return float(data)
    elif isinstance(data, uuid.UUID):
        return str(data)
    elif hasattr(data, 'isoformat'):
        return data.isoformat()
    return data



def compute_request_hash(data: Any) -> str:
    """
    Computes deterministic SHA-256 hash of a request payload.
    """
    if data is None:
        return ""
    try:
        serialized = json.dumps(data, sort_keys=True, default=str)
    except Exception:
        serialized = str(data)
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()


class IdempotencyConflictError(Exception):
    """Raised when an idempotent request is in progress or payload hash mismatches."""
    pass


def execute_idempotent_operation(
    organization: Organization,
    operation_type: str,
    idempotency_key: str,
    operation_func: Callable[[], Dict[str, Any]],
    request_data: Optional[Any] = None,
    actor_user: Optional[TenantUser] = None,
    ttl_seconds: int = 86400,
    db_alias: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Executes an operation idempotently within the tenant database.
    If already completed with identical payload hash, returns the cached response snapshot.
    If request payload hash differs from original record, raises IdempotencyConflictError.
    If processing, raises IdempotencyConflictError.
    If new, marks PROCESSING, executes, saves response snapshot, and marks COMPLETED.
    """
    from apps.tenant_core.context import get_tenant_db_alias
    alias = db_alias or get_tenant_db_alias() or 'default'
    req_hash = compute_request_hash(request_data)
    now = timezone.now()
    expires_at = now + timedelta(seconds=ttl_seconds)

    with transaction.atomic(using=alias):
        record, created = IdempotencyRecord.objects.using(alias).select_for_update().get_or_create(
            organization=organization,
            operation_type=operation_type,
            idempotency_key=idempotency_key,
            defaults={
                'actor_user': actor_user,
                'request_hash': req_hash,
                'status': 'PROCESSING',
                'expires_at': expires_at,
            }
        )

        if not created:
            # ── Payload mismatch check ──────────────────────────────────────
            if record.request_hash and req_hash and record.request_hash != req_hash:
                raise IdempotencyConflictError(
                    f"Idempotent operation '{operation_type}' with key '{idempotency_key}' "
                    "was previously submitted with a different payload."
                )

            if record.status == 'COMPLETED':
                logger.info(
                    "Idempotency hit: replaying completed response for key=%s op=%s",
                    idempotency_key, operation_type
                )
                return record.response_snapshot or {}
            elif record.status == 'PROCESSING':
                # Check if expired or stuck in processing beyond 5 minutes (e.g. server crash during execution)
                is_stuck = bool(record.updated_at and (now - record.updated_at).total_seconds() > 300)
                if (record.expires_at and record.expires_at < now) or is_stuck:
                    logger.warning("Idempotency lock expired or stuck for key=%s op=%s. Overriding.", idempotency_key, operation_type)
                    record.status = 'PROCESSING'
                    record.request_hash = req_hash
                    record.expires_at = expires_at
                    record.save(using=alias)
                else:
                    raise IdempotencyConflictError(
                        f"Idempotent operation '{operation_type}' with key '{idempotency_key}' is currently processing."
                    )
            elif record.status == 'FAILED':
                # Allow retry on failed previous attempts
                record.status = 'PROCESSING'
                record.request_hash = req_hash
                record.save(using=alias)

    # Execute operation outside initial lock but commit results
    try:
        result = operation_func()
        with transaction.atomic(using=alias):
            record = IdempotencyRecord.objects.using(alias).select_for_update().get(id=record.id)
            record.status = 'COMPLETED'
            record.response_snapshot = sanitize_payload(result)
            record.save(using=alias, update_fields=['status', 'response_snapshot', 'updated_at'])
        return result
    except Exception as exc:
        with transaction.atomic(using=alias):
            record = IdempotencyRecord.objects.using(alias).select_for_update().get(id=record.id)
            record.status = 'FAILED'
            record.response_snapshot = {'error': str(exc)}
            record.save(using=alias, update_fields=['status', 'response_snapshot', 'updated_at'])
        raise


def record_business_audit(
    organization: Organization,
    module: str,
    action_code: str,
    entity_type: str,
    entity_id: Optional[uuid.UUID] = None,
    branch: Optional[Branch] = None,
    actor_type: str = 'USER',
    actor_user: Optional[TenantUser] = None,
    actor_employee_profile_id: Optional[uuid.UUID] = None,
    source_channel: str = 'WEB_ADMIN',
    event_description: Optional[str] = None,
    before_data: Optional[Dict[str, Any]] = None,
    after_data: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    request_id: Optional[uuid.UUID] = None,
    session_id: Optional[str] = None,
    db_alias: Optional[str] = None,
) -> BusinessAuditEvent:
    """
    Creates an immutable business audit event with sanitized before/after state diffs.
    """
    # If actor_user is not a TenantUser instance (e.g. PlatformUser/SuperAdmin), resolve or fallback safely
    if actor_user is not None and getattr(actor_user, '__class__', None).__name__ != 'TenantUser':
        resolved_tenant_user = None
        try:
            from .models_users import TenantUser
            from django.db.models import Q
            alias = db_alias or 'default'
            actor_email = getattr(actor_user, 'email', None)
            actor_id = getattr(actor_user, 'id', None)
            resolved_tenant_user = TenantUser.objects.using(alias).filter(
                Q(id=actor_id) | (Q(email__iexact=actor_email) if actor_email else Q(pk__isnull=True))
            ).first()
        except Exception:
            resolved_tenant_user = None
        if not metadata:
            metadata = {}
        metadata['raw_actor'] = str(actor_user)
        actor_user = resolved_tenant_user

    manager = BusinessAuditEvent.objects.using(db_alias) if db_alias else BusinessAuditEvent.objects
    event = manager.create(
        organization=organization,
        branch=branch,
        actor_type=actor_type,
        actor_user=actor_user,
        actor_employee_profile_id=actor_employee_profile_id,
        source_channel=source_channel,
        module=module,
        action_code=action_code,
        entity_type=entity_type,
        entity_id=entity_id,
        event_description=event_description,
        before_data=sanitize_payload(before_data) if before_data else None,
        after_data=sanitize_payload(after_data) if after_data else None,
        metadata=sanitize_payload(metadata) if metadata else None,
        ip_address=ip_address,
        user_agent=user_agent,
        request_id=request_id,
        session_id=session_id,
        occurred_at=timezone.now(),
    )

    # Bridge to Canonical ActivityAuditLog for complete unified CRM visibility
    try:
        from .audit_engine import UnifiedAuditEngine
        UnifiedAuditEngine.record_activity(
            action_code=action_code,
            module=module,
            entity_type=entity_type,
            entity_id=entity_id,
            actor_user=actor_user,
            actor_type='STAFF' if actor_type == 'USER' else actor_type,
            organization=organization,
            branch=branch,
            before_state=before_data,
            after_state=after_data,
            event_description=event_description or '',
            metadata=metadata,
            ip_address=ip_address,
            user_agent=user_agent or '',
            request_id=str(request_id) if request_id else '',
            source_channel=source_channel,
            db_alias=db_alias,
        )
    except Exception as bridge_err:
        logger.warning(f"Failed to mirror BusinessAuditEvent to ActivityAuditLog: {bridge_err}")

    return event


def enqueue_outbox_event(
    organization: Organization,
    event_type: str,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    payload: Dict[str, Any],
    db_alias: Optional[str] = None,
) -> DomainOutboxEvent:
    """
    Saves an outbox event in the current transaction to be reliably dispatched asynchronously.
    """
    manager = DomainOutboxEvent.objects.using(db_alias) if db_alias else DomainOutboxEvent.objects
    return manager.create(
        organization=organization,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=sanitize_payload(payload),
        status='PENDING',
    )
