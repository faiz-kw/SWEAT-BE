"""
Universal Authentication Routing Directory Services (Master DB only).

Provides O(1) identifier resolution, privacy-preserving lookup hashes,
global uniqueness validation across platform & all tenants, and cross-database
lifecycle synchronization.
"""

import hmac
import hashlib
import uuid
import logging
from typing import Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

logger = logging.getLogger(__name__)


def normalize_identifier(identifier: str) -> str:
    """Normalize identifier: strip whitespace and lowercase."""
    if not identifier:
        return ''
    return str(identifier).strip().lower()


def compute_lookup_hash(identifier: str) -> str:
    """
    Compute deterministic HMAC-SHA256 hex digest for an identifier.
    Uses AUTH_DIRECTORY_HMAC_KEY or settings.SECRET_KEY.
    """
    normalized = normalize_identifier(identifier)
    key = getattr(settings, 'AUTH_DIRECTORY_HMAC_KEY', settings.SECRET_KEY)
    if isinstance(key, str):
        key_bytes = key.encode('utf-8')
    else:
        key_bytes = bytes(key)
    return hmac.new(key_bytes, normalized.encode('utf-8'), hashlib.sha256).hexdigest()


def infer_identifier_type(identifier: str) -> str:
    """Infer whether an identifier is an EMAIL or USERNAME."""
    return 'EMAIL' if '@' in identifier else 'USERNAME'


def resolve_identity(identifier: str):
    """
    Resolve an incoming login identifier (email or username) to its AuthenticationIdentity.
    Returns None if not found.
    Queries exclusively from Master DB ('default').
    """
    if not identifier:
        return None
    from apps.master.models_iam import AuthenticationIdentity
    lookup_hash = compute_lookup_hash(identifier)
    return AuthenticationIdentity.objects.using('default').filter(lookup_hash=lookup_hash).first()


def check_identifier_available(identifier: str, exclude_subject_id: Optional[uuid.UUID] = None) -> bool:
    """
    Check if a normalized identifier is available globally across platform and all tenants.
    """
    if not identifier:
        return True
    try:
        from apps.master.models_iam import AuthenticationIdentity
        lookup_hash = compute_lookup_hash(identifier)
        qs = AuthenticationIdentity.objects.using('default').filter(lookup_hash=lookup_hash)
        if exclude_subject_id:
            qs = qs.exclude(subject_id=exclude_subject_id)
        return not qs.exists()
    except Exception as e:
        logger.warning("check_identifier_available skipped due to: %s", e)
        return True


def register_identity(
    identifier: str,
    account_type: str,
    subject_id: uuid.UUID,
    tenant_id: Optional[uuid.UUID] = None,
    identifier_type: Optional[str] = None,
    status: str = 'ACTIVE',
    db: str = 'default'
):
    """
    Register or update an identity in the Universal Authentication Directory.
    Enforces global uniqueness across the entire system.
    """
    from apps.master.models_iam import AuthenticationIdentity

    clean_val = normalize_identifier(identifier)
    if not clean_val:
        return None

    lookup_hash = compute_lookup_hash(clean_val)
    id_type = identifier_type or infer_identifier_type(clean_val)

    # Check for collisions with different users
    existing = AuthenticationIdentity.objects.using('default').filter(lookup_hash=lookup_hash).first()
    if existing:
        if str(existing.subject_id) != str(subject_id) or existing.account_type != account_type:
            raise ValidationError('This username or email is already registered.')
        # Existing points to same user -> update attributes
        existing.identifier = clean_val
        existing.identifier_type = id_type
        existing.tenant_id = tenant_id
        existing.status = status
        existing.save(using='default')
        return existing

    identity = AuthenticationIdentity.objects.using('default').create(
        lookup_hash=lookup_hash,
        identifier=clean_val,
        identifier_type=id_type,
        account_type=account_type,
        subject_id=subject_id,
        tenant_id=tenant_id,
        status=status,
    )
    return identity


def set_identity_status(subject_id: uuid.UUID, status: str, account_type: Optional[str] = None):
    """Update status for all identities belonging to a subject."""
    from apps.master.models_iam import AuthenticationIdentity
    qs = AuthenticationIdentity.objects.using('default').filter(subject_id=subject_id)
    if account_type:
        qs = qs.filter(account_type=account_type)
    qs.update(status=status, updated_at=timezone.now())


def delete_identity(subject_id: uuid.UUID, identifier: Optional[str] = None):
    """Delete directory entry for a user or a specific identifier."""
    from apps.master.models_iam import AuthenticationIdentity
    qs = AuthenticationIdentity.objects.using('default').filter(subject_id=subject_id)
    if identifier:
        lookup_hash = compute_lookup_hash(identifier)
        qs = qs.filter(lookup_hash=lookup_hash)
    qs.delete()


def sync_platform_user_identity(user):
    """
    Synchronize a PlatformUser record into AuthenticationIdentity in Master DB.
    """
    try:
        from apps.master.models_iam import AuthenticationIdentity
        user_status = getattr(user, 'status', 'ACTIVE')
        is_active = getattr(user, 'is_active', True)

        if user_status == 'ACTIVE' and is_active:
            status = 'ACTIVE'
        elif user_status == 'INVITED':
            status = 'INVITED'
        else:
            status = 'INACTIVE'

        current_identities = []
        if getattr(user, 'email', None):
            email_val = normalize_identifier(user.email)
            register_identity(
                identifier=email_val,
                account_type='PLATFORM',
                subject_id=user.id,
                tenant_id=None,
                identifier_type='EMAIL',
                status=status,
            )
            current_identities.append(compute_lookup_hash(email_val))

        username_val = getattr(user, 'username', None)
        if username_val:
            username_norm = normalize_identifier(username_val)
            register_identity(
                identifier=username_norm,
                account_type='PLATFORM',
                subject_id=user.id,
                tenant_id=None,
                identifier_type='USERNAME',
                status=status,
            )
            current_identities.append(compute_lookup_hash(username_norm))

        # Clean up any stale identities for this subject
        if current_identities:
            AuthenticationIdentity.objects.using('default').filter(
                subject_id=user.id,
                account_type='PLATFORM'
            ).exclude(lookup_hash__in=current_identities).delete()

    except Exception as e:
        logger.error("Failed to sync platform user identity for %s: %s", getattr(user, 'email', user.id), e)
        raise


def sync_tenant_user_identity(user, tenant_id=None, db: Optional[str] = None):
    """
    Synchronize a TenantUser record into AuthenticationIdentity in Master DB.
    Resolves tenant_id from parameter, database alias, or active tenant context.
    """
    try:
        from apps.master.models_iam import AuthenticationIdentity
        from apps.master.models_tenant import Tenant
        from apps.master.models_infra import TenantDataSource

        # 1. Resolve tenant_id
        resolved_tenant_id = None
        if tenant_id is not None:
            if isinstance(tenant_id, uuid.UUID):
                resolved_tenant_id = tenant_id
            elif isinstance(tenant_id, str):
                try:
                    resolved_tenant_id = uuid.UUID(tenant_id)
                except Exception:
                    db = tenant_id

        db_alias = db or getattr(getattr(user, '_state', None), 'db', None)
        if not resolved_tenant_id and isinstance(db_alias, str) and db_alias.startswith('tenant_'):
            hex_part = db_alias[7:]
            if len(hex_part) == 32:
                try:
                    resolved_tenant_id = uuid.UUID(hex_part)
                except Exception:
                    pass

        if not resolved_tenant_id and isinstance(db_alias, str):
            ds = TenantDataSource.objects.using('default').filter(
                db_name=db_alias
            ).first()
            if ds:
                resolved_tenant_id = ds.tenant_id

        if not resolved_tenant_id:
            t = Tenant.objects.using('default').filter(status='ACTIVE').first()
            if t:
                resolved_tenant_id = t.id

        tenant_id = resolved_tenant_id

        user_status = getattr(user, 'status', 'ACTIVE')
        is_login_allowed = getattr(user, 'is_login_allowed', True)

        if user_status == 'ACTIVE' and is_login_allowed:
            status = 'ACTIVE'
        elif user_status == 'INVITED':
            status = 'INVITED'
        else:
            status = 'INACTIVE'

        current_identities = []
        if getattr(user, 'email', None):
            email_val = normalize_identifier(user.email)
            register_identity(
                identifier=email_val,
                account_type='TENANT',
                subject_id=user.id,
                tenant_id=tenant_id,
                identifier_type='EMAIL',
                status=status,
            )
            current_identities.append(compute_lookup_hash(email_val))

        username_val = getattr(user, 'username', None)
        if username_val:
            username_norm = normalize_identifier(username_val)
            register_identity(
                identifier=username_norm,
                account_type='TENANT',
                subject_id=user.id,
                tenant_id=tenant_id,
                identifier_type='USERNAME',
                status=status,
            )
            current_identities.append(compute_lookup_hash(username_norm))

        # Clean up any stale identities for this subject
        if current_identities:
            AuthenticationIdentity.objects.using('default').filter(
                subject_id=user.id,
                account_type='TENANT'
            ).exclude(lookup_hash__in=current_identities).delete()

    except Exception as e:
        logger.error("Failed to sync tenant user identity for %s: %s", getattr(user, 'email', user.id), e)
        if "Database queries to 'default' are not allowed" in str(e):
            return
        raise
