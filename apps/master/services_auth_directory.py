"""
Universal Authentication Routing Directory Services (Master DB only).

Provides O(1) identifier resolution, privacy-preserving lookup hashes,
organization-scoped uniqueness validation, and cross-database
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


def resolve_identity(identifier: str, account_type=None, tenant_id=None):
    """
    Resolve an incoming login identifier (email or username) to its AuthenticationIdentity.
    Returns None if not found or ambiguous without an explicit scope.
    Queries exclusively from Master DB ('default').
    """
    if not identifier:
        return None
    from apps.master.models_iam import AuthenticationIdentity
    lookup_hash = compute_lookup_hash(identifier)
    qs = AuthenticationIdentity.objects.using('default').filter(lookup_hash=lookup_hash)
    if account_type:
        qs = qs.filter(account_type=account_type, tenant_id=tenant_id)
    matches = list(qs[:2])
    return matches[0] if len(matches) == 1 else None


def check_identifier_available(identifier: str, exclude_subject_id: Optional[uuid.UUID] = None, account_type='PLATFORM', db=None) -> bool:
    """
    Check identifier availability within the selected login scope.
    """
    if not identifier:
        return True
    if account_type == 'TENANT':
        from apps.tenant_core.models_users import TenantUser
        from config.routers import get_tenant_db_alias
        from django.db.models import Q
        alias = db or get_tenant_db_alias()
        if not alias or alias == 'default':
            raise ValidationError('An organization database is required.')
        qs = TenantUser.objects.using(alias).filter(
            Q(email__iexact=normalize_identifier(identifier)) |
            Q(username__iexact=normalize_identifier(identifier))
        )
        if exclude_subject_id:
            qs = qs.exclude(pk=exclude_subject_id)
        return not qs.exists()
    try:
        from apps.master.models_iam import AuthenticationIdentity
        lookup_hash = compute_lookup_hash(identifier)
        qs = AuthenticationIdentity.objects.using('default').filter(lookup_hash=lookup_hash, account_type='PLATFORM', tenant_id=None)
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
    Enforces uniqueness within the platform or selected organization.
    """
    from apps.master.models_iam import AuthenticationIdentity

    clean_val = normalize_identifier(identifier)
    if not clean_val:
        return None

    lookup_hash = compute_lookup_hash(clean_val)
    id_type = identifier_type or infer_identifier_type(clean_val)

    if account_type == 'TENANT' and not tenant_id:
        raise ValidationError('An organization is required for tenant identities.')
    if account_type == 'PLATFORM' and tenant_id is not None:
        raise ValidationError('Platform identities cannot belong to an organization.')
    # Check for collisions within this login scope.
    existing = AuthenticationIdentity.objects.using('default').filter(lookup_hash=lookup_hash, account_type=account_type, tenant_id=tenant_id).first()
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
            raise ValidationError('Cannot determine organization for login identity.')

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
                account_type='TENANT', tenant_id=tenant_id
            ).exclude(lookup_hash__in=current_identities).delete()

    except Exception as e:
        logger.error("Failed to sync tenant user identity for %s: %s", getattr(user, 'email', user.id), e)
        if "Database queries to 'default' are not allowed" in str(e):
            return
        raise
