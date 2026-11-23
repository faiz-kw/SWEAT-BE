"""
Centralized, Dynamic RBAC Sensitive-Field & Phone Number Privacy Utilities.

Enforces server-side visibility of sensitive fields (starting with phone numbers)
using the Tenant RBAC Permission Matrix (RolePermissionSetItem) rather than
hardcoded role names.

Rules:
  - Platform Super Admin (is_superuser or platform SUPER_ADMIN): Allowed.
  - Self-access (user viewing their own profile when target_user_id matches): Allowed.
  - All tenant roles (including ORG_ADMIN, BRANCH_MANAGER, SALES_REP, FRONT_DESK,
    TRAINER, custom roles): Evaluated dynamically against live Tenant DB
    RolePermissionSetItem(granted=True) for 'core.privacy.view_phone_number'.
  - Fail-closed: Missing request, unauthenticated user, or lookup errors mask data.
"""

import logging
import re
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)

PERM_VIEW_PHONE_NUMBER = 'core.privacy.view_phone_number'

# Extensible sensitive-field registry: map logical sensitive field keys to
# canonical Permission Matrix codes. New sensitive fields (e.g. email, address)
# can be added here without rebuilding authorization logic.
SENSITIVE_FIELD_PERMISSIONS: Dict[str, str] = {
    'phone_number': PERM_VIEW_PHONE_NUMBER,
}

SENSITIVE_PERMISSION_CODES: frozenset = frozenset(SENSITIVE_FIELD_PERMISSIONS.values())

MASKED_PHONE_PLACEHOLDER = 'Phone number hidden'
_PHONE_FIELD_KEYWORDS = ('phone', 'mobile', 'whatsapp', 'contact_num', 'tel')


def get_user_role_codes(request, user=None) -> Set[str]:
    """Return active role codes for a tenant user (retained for existing callers)."""
    if not user:
        if not request or not hasattr(request, 'user') or not getattr(request.user, 'is_authenticated', False):
            return set()
        user = request.user
    role_codes = getattr(user, '_role_codes', None)
    if role_codes is not None:
        return set(role_codes)
    try:
        from .models_rbac import RoleAssignment

        alias = _resolve_db_alias(request, user)
        role_codes = set(
            RoleAssignment.objects.using(alias)
            .filter(user=user, is_active=True)
            .values_list('role__code', flat=True)
        )
    except Exception as exc:
        logger.warning('get_user_role_codes failed for user=%s: %s', getattr(user, 'id', None), exc)
        role_codes = set()
    return role_codes


def _resolve_db_alias(request=None, user=None, db_alias: Optional[str] = None) -> str:
    if db_alias:
        return db_alias
    if user is not None:
        user_alias = getattr(user, '_db_alias', None)
        if user_alias:
            return user_alias
    if request is not None:
        req_alias = getattr(request, '_tenant_db_alias', None)
        if req_alias:
            return req_alias
    try:
        from config.routers import get_tenant_db_alias

        router_alias = get_tenant_db_alias()
        if router_alias:
            return router_alias
    except Exception as exc:
        logger.debug('_resolve_db_alias router fallback failed: %s', exc)
    if user is not None and getattr(user, '_state', None) and getattr(user._state, 'db', None):
        return user._state.db
    return 'default'


def is_platform_super_admin(user) -> bool:
    """
    True ONLY for platform-level Super Admins / Django superusers.
    Tenant roles (including ORG_ADMIN) are NOT bypassed here so their permissions
    remain dynamically configurable in the Permission Matrix.
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if getattr(user, 'is_superuser', False):
        return True
    auth_type = getattr(user, '_auth_type', None)
    if auth_type == 'platform':
        role_codes = {str(r).upper() for r in (getattr(user, '_role_codes', None) or set())}
        if role_codes & {'SUPER_ADMIN', 'PLATFORM_SUPERADMIN'}:
            return True
    return False


def can_view_sensitive_field(
    request,
    field_key: str,
    target_user_id: Optional[Any] = None,
    user=None,
    db_alias: Optional[str] = None,
) -> bool:
    """
    Dynamically evaluate whether the requesting user is authorized to view a
    sensitive field (e.g. 'phone_number') based on live Tenant RBAC grants.
    Fails closed on any missing context or error.
    """
    resolved_user = user or (getattr(request, 'user', None) if request is not None else None)
    if not resolved_user or not getattr(resolved_user, 'is_authenticated', False):
        return False

    if is_platform_super_admin(resolved_user):
        return True

    # Users viewing their own personal profile may see their own phone number.
    if target_user_id is not None:
        req_uid = getattr(resolved_user, 'id', None)
        if req_uid is not None and str(target_user_id) == str(req_uid):
            return True

    perm_code = SENSITIVE_FIELD_PERMISSIONS.get(field_key)
    if not perm_code:
        return False

    user_id = getattr(resolved_user, 'id', None)
    if not user_id:
        return False

    alias = _resolve_db_alias(request, resolved_user, db_alias=db_alias)
    cache_key = (str(user_id), alias, perm_code)

    if request is not None:
        cache = getattr(request, '_sensitive_field_perm_cache', None)
        if cache is None:
            cache = {}
            setattr(request, '_sensitive_field_perm_cache', cache)
        if cache_key in cache:
            return cache[cache_key]

    allowed = _evaluate_sensitive_permission_in_db(user_id=user_id, perm_code=perm_code, db_alias=alias)

    if request is not None and hasattr(request, '_sensitive_field_perm_cache'):
        request._sensitive_field_perm_cache[cache_key] = allowed

    return allowed


def _evaluate_sensitive_permission_in_db(user_id: Any, perm_code: str, db_alias: str) -> bool:
    """Query live Tenant DB RoleAssignments and RolePermissionSetItems."""
    try:
        from django.db.models import Q
        from .models_rbac import Permission, RoleAssignment, RolePermissionSetItem

        active_assignments = list(
            RoleAssignment.objects.using(db_alias)
            .filter(user_id=user_id, is_active=True, role__is_active=True)
            .select_related('role')
        )
        if not active_assignments:
            return False

        role_ids = [ra.role_id for ra in active_assignments]

        # Authoritative check against live Permission Matrix entries.
        perm_exists = Permission.objects.using(db_alias).filter(
            Q(permission_code__iexact=perm_code) | Q(code__iexact=perm_code),
            is_active=True,
        ).exists()

        if perm_exists:
            return RolePermissionSetItem.objects.using(db_alias).filter(
                permission_set__role_id__in=role_ids,
                permission_set__is_active=True,
                granted=True,
            ).filter(
                Q(permission__permission_code__iexact=perm_code)
                | Q(permission__code__iexact=perm_code)
            ).exists()

        # Transition safety ONLY before the new permission catalog row has been
        # synced into an existing tenant DB: allow ORG_ADMIN until catalog sync runs.
        role_codes = {(ra.role.code or '').upper() for ra in active_assignments if ra.role}
        return bool(role_codes & {'ORG_ADMIN', 'TENANT_ADMIN', 'TENANT_OWNER'})
    except Exception as exc:
        logger.warning(
            'Sensitive permission check failed (fail-closed) user=%s perm=%s db=%s: %s',
            user_id,
            perm_code,
            db_alias,
            exc,
        )
        return False


def can_view_phone_number(
    request,
    target_user_id: Optional[Any] = None,
    user=None,
    db_alias: Optional[str] = None,
) -> bool:
    """Return True if the requester holds permission to view full phone numbers."""
    return can_view_sensitive_field(
        request=request,
        field_key='phone_number',
        target_user_id=target_user_id,
        user=user,
        db_alias=db_alias,
    )


def is_masked_phone(value: Optional[str]) -> bool:
    """Detect whether a phone string is already masked so updates never overwrite real data."""
    if not value:
        return False
    val = str(value).strip()
    if not val:
        return False
    return (
        '***' in val
        or 'XXXXX' in val.upper()
        or val.lower() == MASKED_PHONE_PLACEHOLDER.lower()
    )


def mask_phone_number(raw_phone: Optional[str]) -> str:
    """
    Format a phone number using the standard privacy mask: ******7890.
    Falls back to 'Phone number hidden' if fewer than 4 digits are present.
    Idempotent if called on an already-masked value.
    """
    if not raw_phone:
        return ''
    raw_str = str(raw_phone).strip()
    if not raw_str:
        return ''
    if is_masked_phone(raw_str):
        return raw_str
    clean_digits = re.sub(r'[^\d]', '', raw_str)
    if len(clean_digits) >= 4:
        return f'******{clean_digits[-4:]}'
    return MASKED_PHONE_PLACEHOLDER


def mask_phone_if_restricted(
    request,
    raw_phone: Optional[str],
    target_user_id: Optional[Any] = None,
    user=None,
    db_alias: Optional[str] = None,
) -> str:
    """
    Return the full phone number if the requesting user holds dynamic RBAC
    permission 'core.privacy.view_phone_number'; otherwise return ******7890.
    """
    if not raw_phone:
        return ''
    raw_str = str(raw_phone).strip()
    if not raw_str:
        return ''
    if can_view_phone_number(
        request=request,
        target_user_id=target_user_id,
        user=user,
        db_alias=db_alias,
    ):
        return raw_str
    return mask_phone_number(raw_str)


def mask_communication_identifier(request, channel: Optional[str], value: Optional[str]) -> str:
    """Mask phone-based communication identifiers (WhatsApp/SMS/Voice) while leaving emails intact."""
    if not value:
        return ''
    val_str = str(value).strip()
    if not val_str:
        return ''
    ch = (channel or '').upper()
    if ch == 'EMAIL' or ('@' in val_str and not re.fullmatch(r'[\d\s+\-()]+', val_str)):
        return val_str
    return mask_phone_if_restricted(request, val_str)


def _is_phone_field_name(key: Any) -> bool:
    key_lower = str(key or '').lower()
    return any(tok in key_lower for tok in _PHONE_FIELD_KEYWORDS)


def sanitize_meta_field_data(request, field_data: Any) -> Any:
    """
    Scrub phone values inside Meta Lead Ads field_data JSON when caller lacks
    phone visibility permission. Supports both list[{name, values}] and dict forms.
    """
    if not field_data or can_view_phone_number(request):
        return field_data

    if isinstance(field_data, list):
        sanitized = []
        for entry in field_data:
            if isinstance(entry, dict) and _is_phone_field_name(entry.get('name')):
                copied = dict(entry)
                vals = copied.get('values')
                if isinstance(vals, list):
                    copied['values'] = [mask_phone_number(v) for v in vals]
                elif vals is not None:
                    copied['values'] = mask_phone_number(vals)
                if 'value' in copied and copied['value'] is not None:
                    copied['value'] = mask_phone_number(copied['value'])
                sanitized.append(copied)
            else:
                sanitized.append(entry)
        return sanitized

    if isinstance(field_data, dict):
        sanitized_dict = {}
        for k, v in field_data.items():
            if _is_phone_field_name(k):
                if isinstance(v, list):
                    sanitized_dict[k] = [mask_phone_number(item) for item in v]
                else:
                    sanitized_dict[k] = mask_phone_number(v)
            else:
                sanitized_dict[k] = v
        return sanitized_dict

    return field_data
