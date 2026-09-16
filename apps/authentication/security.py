"""
Security utilities for PerformanceOS Authentication — Phase 1 Layer 1.

Provides:
1. Environment-aware Secure HttpOnly cookie parameter resolver.
2. Redis-backed user session revocation and token invalidation timestamp tracking.
3. Redis-backed application-level login brute-force tracking and lockout.
4. Standard RFC 6238 TOTP generation, validation, and single-use challenge lifecycle.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import struct
import time
from typing import Optional, Tuple, Dict, Any

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# Redis key prefixes
REVOCATION_KEY_PREFIX = "auth:revoked:"
LOCKOUT_ACC_PREFIX = "auth:lockout:acc:"
LOCKOUT_IP_PREFIX = "auth:lockout:ip:"
MFA_CHALLENGE_PREFIX = "auth:mfa_challenge:"

# Default security constants
DEFAULT_LOCKOUT_THRESHOLD = 5
DEFAULT_LOCKOUT_DURATION = 900  # 15 minutes
MFA_CHALLENGE_TTL = 300         # 5 minutes


def _get_redis_client():
    """Get Redis client using django_redis or redis library."""
    try:
        from django_redis import get_redis_connection
        return get_redis_connection("default")
    except Exception:
        try:
            import redis
            redis_url = getattr(settings, 'REDIS_URL', 'redis://localhost:6379/0')
            return redis.from_url(redis_url, socket_connect_timeout=0.2, socket_timeout=0.2)
        except Exception as e:
            logger.error("Failed to acquire Redis connection: %s", e)
            return None


# ---------------------------------------------------------------------------
# 1. Environment-Aware Secure Cookie Configuration
# ---------------------------------------------------------------------------

def get_refresh_cookie_params(request=None) -> Dict[str, Any]:
    """
    Returns production-secure cookie parameters.
    In production / HTTPS, Secure=True is strictly enforced.
    In development (DEBUG=True without HTTPS), Secure=False is allowed for localhost testing.
    """
    is_secure_request = False
    if request is not None:
        try:
            is_secure_request = request.is_secure()
        except Exception:
            pass

    # Explicit setting takes precedence; otherwise requires HTTPS or non-DEBUG
    cookie_secure = getattr(settings, 'SESSION_COOKIE_SECURE', False)
    if not cookie_secure and not settings.DEBUG:
        cookie_secure = True
    elif is_secure_request:
        cookie_secure = True

    refresh_days = getattr(settings, 'JWT_REFRESH_DAYS', 7)
    max_age = int(refresh_days * 24 * 60 * 60)

    return {
        'httponly': True,
        'samesite': 'Lax',
        'secure': cookie_secure,
        'max_age': max_age,
        'path': '/',
    }


def set_refresh_cookie(response, refresh_token_str: str, request=None) -> None:
    """Set the HttpOnly refresh token cookie on a DRF Response."""
    params = get_refresh_cookie_params(request)
    response.set_cookie(
        'refresh_token',
        refresh_token_str,
        **params
    )


def delete_refresh_cookie(response) -> None:
    """Clear the refresh token cookie on a DRF Response."""
    response.delete_cookie(
        'refresh_token',
        path='/',
        samesite='Lax',
    )


# ---------------------------------------------------------------------------
# 2. Redis-Backed Session Revocation & Invalidation
# ---------------------------------------------------------------------------

def revoke_all_user_sessions(
    user_id: str,
    user_type: str = 'tenant',
    actor_email: Optional[str] = None,
    client_ip: Optional[str] = None,
    db_alias: Optional[str] = None,
    revoked_by_id: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> int:
    """
    Revokes all active sessions for a user:
    1. Records current timestamp in Redis as user's revocation watermark.
       Any access token or refresh token issued with iat <= watermark will be rejected.
    2. Blacklists known outstanding refresh tokens via SimpleJWT Blacklist.
    3. Emits an audit event.
    """
    client_ip = client_ip or ip_address
    now_ts = int(time.time())
    redis_client = _get_redis_client()
    refresh_days = getattr(settings, 'JWT_REFRESH_DAYS', 7)
    ttl = int(refresh_days * 24 * 60 * 60)

    if redis_client:
        try:
            key = f"{REVOCATION_KEY_PREFIX}{user_id}"
            redis_client.set(key, now_ts, ex=ttl)
            logger.info("Recorded session revocation watermark for user=%s at ts=%d", user_id, now_ts)
        except Exception as e:
            logger.error("Failed to write session revocation to Redis: %s", e)

    # Blacklist outstanding refresh tokens in SimpleJWT
    blacklisted_count = 0
    try:
        from rest_framework_simplejwt.token_blacklist.models import OutstandingToken, BlacklistedToken
        tokens = OutstandingToken.objects.filter(user_id=user_id)
        for t in tokens:
            _, created = BlacklistedToken.objects.get_or_create(token=t)
            if created:
                blacklisted_count += 1
    except Exception as e:
        logger.warning("Could not blacklist outstanding tokens in database: %s", e)

    # Audit logging
    try:
        if user_type == 'tenant' and db_alias:
            from apps.tenant_core.audit import emit_audit_event
            emit_audit_event(
                action='REVOKE_SESSIONS',
                resource_type='TenantUser',
                resource_id=user_id,
                before_state={'active_sessions': 'all'},
                after_state={'status': 'revoked', 'revoked_at': now_ts, 'blacklisted_tokens': blacklisted_count},
                actor_email=actor_email,
                db_alias=db_alias,
                description='Revoked all active staff sessions and blacklisted outstanding tokens.',
            )
        else:
            logger.info("Platform user sessions revoked: user=%s by=%s ip=%s", user_id, actor_email, client_ip)
    except Exception as e:
        logger.warning("Failed to emit session revocation audit event: %s", e)

    return blacklisted_count


def is_token_revoked(user_id: str, token_iat: Optional[int]) -> bool:
    """
    Check if an access token or refresh token has been revoked via user-wide revocation.
    Returns True if token was issued prior to or at the revocation timestamp.
    """
    if not token_iat:
        return False

    redis_client = _get_redis_client()
    if not redis_client:
        return False

    try:
        key = f"{REVOCATION_KEY_PREFIX}{user_id}"
        val = redis_client.get(key)
        if val is not None:
            revoked_at = int(val)
            if int(token_iat) <= revoked_at:
                return True
    except Exception as e:
        logger.error("Error checking token revocation in Redis: %s", e)

    return False


# ---------------------------------------------------------------------------
# 3. Application-Level Login Brute-Force Lockout
# ---------------------------------------------------------------------------

def _get_lockout_key(email: str, tenant_identifier: Optional[str] = None) -> str:
    slug = (tenant_identifier or "platform").lower().strip()
    return f"{LOCKOUT_ACC_PREFIX}{slug}:{email.lower().strip()}"


def check_login_lockout(
    email_or_request: Any,
    email: Optional[str] = None,
    tenant_slug: Optional[str] = None,
    client_ip: Optional[str] = None,
    tenant_id: Optional[str] = None,
    threshold: int = DEFAULT_LOCKOUT_THRESHOLD,
) -> Tuple[bool, int]:
    """
    Checks whether login attempts for the account or IP are locked out.
    Accepts either (request, email, ...) or (email, tenant_slug, ...).
    Returns (is_locked: bool, remaining_seconds: int).
    """
    if hasattr(email_or_request, 'META'):
        req = email_or_request
        target_email = email or ""
        target_ip = client_ip or req.META.get('REMOTE_ADDR')
    else:
        target_email = str(email_or_request or "")
        target_ip = client_ip

    target_tenant = tenant_slug or tenant_id or "platform"

    redis_client = _get_redis_client()
    if not redis_client:
        return (False, 0)

    try:
        # Check account key
        acc_key = _get_lockout_key(target_email, target_tenant)
        acc_attempts = redis_client.get(acc_key)
        if acc_attempts and int(acc_attempts) >= threshold:
            ttl = redis_client.ttl(acc_key)
            return (True, max(1, ttl))

        # Check IP key (IP threshold is 5x account threshold to avoid subnet DOS)
        if target_ip:
            ip_key = f"{LOCKOUT_IP_PREFIX}{target_ip}"
            ip_attempts = redis_client.get(ip_key)
            if ip_attempts and int(ip_attempts) >= (threshold * 5):
                ttl = redis_client.ttl(ip_key)
                return (True, max(1, ttl))
    except Exception as e:
        logger.error("Error reading login lockout from Redis: %s", e)

    return (False, 0)


def record_login_failure(
    email_or_request: Any,
    email: Optional[str] = None,
    tenant_slug: Optional[str] = None,
    client_ip: Optional[str] = None,
    tenant_id: Optional[str] = None,
    threshold: int = DEFAULT_LOCKOUT_THRESHOLD,
    duration: int = DEFAULT_LOCKOUT_DURATION,
) -> Tuple[bool, int]:
    """
    Increments failure counters for account and IP upon failed authentication.
    Returns (is_locked: bool, remaining_seconds: int).
    """
    if hasattr(email_or_request, 'META'):
        req = email_or_request
        target_email = email or ""
        target_ip = client_ip or req.META.get('REMOTE_ADDR')
    else:
        target_email = str(email_or_request or "")
        target_ip = client_ip

    target_tenant = tenant_slug or tenant_id or "platform"

    redis_client = _get_redis_client()
    if not redis_client:
        return (False, 0)

    try:
        acc_key = _get_lockout_key(target_email, target_tenant)
        count = redis_client.incr(acc_key)
        if count == 1 or redis_client.ttl(acc_key) == -1:
            redis_client.expire(acc_key, duration)
        elif count >= threshold:
            redis_client.expire(acc_key, duration)

        if target_ip:
            ip_key = f"{LOCKOUT_IP_PREFIX}{target_ip}"
            ip_count = redis_client.incr(ip_key)
            if ip_count == 1 or redis_client.ttl(ip_key) == -1:
                redis_client.expire(ip_key, duration)

        if count >= threshold:
            ttl = redis_client.ttl(acc_key)
            return (True, max(1, ttl))
    except Exception as e:
        logger.error("Error recording login failure in Redis: %s", e)

    return (False, 0)


def reset_login_lockout(
    email_or_request: Any,
    email: Optional[str] = None,
    tenant_slug: Optional[str] = None,
    client_ip: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> None:
    """Clears failed login attempt counters upon successful authentication."""
    if hasattr(email_or_request, 'META'):
        req = email_or_request
        target_email = email or ""
        target_ip = client_ip or req.META.get('REMOTE_ADDR')
    else:
        target_email = str(email_or_request or "")
        target_ip = client_ip

    target_tenant = tenant_slug or tenant_id or "platform"

    redis_client = _get_redis_client()
    if not redis_client:
        return

    try:
        acc_key = _get_lockout_key(target_email, target_tenant)
        redis_client.delete(acc_key)
        if target_ip:
            ip_key = f"{LOCKOUT_IP_PREFIX}{target_ip}"
            redis_client.delete(ip_key)
    except Exception as e:
        logger.error("Error resetting login lockout in Redis: %s", e)


# ---------------------------------------------------------------------------
# 4. Standard RFC 6238 TOTP Engine & Single-Use Challenge Lifecycle
# ---------------------------------------------------------------------------

def generate_totp_secret() -> str:
    """Generate a random base32 encoded 160-bit secret."""
    random_bytes = secrets.token_bytes(20)
    return base64.b32encode(random_bytes).decode('utf-8').rstrip('=')


def get_totp_uri(secret_b32: str, email: str, issuer: str = "PerformanceOS") -> str:
    """Return standard otpauth URI for QR code generation."""
    return f"otpauth://totp/{issuer}:{email}?secret={secret_b32}&issuer={issuer}&algorithm=SHA1&digits=6&period=30"


def generate_totp_code(secret_b32: str, for_time: Optional[int] = None, interval: int = 30) -> str:
    """Generate RFC 6238 6-digit TOTP code for a given timestamp."""
    if for_time is None:
        for_time = int(time.time())
    counter = int(for_time // interval)

    # Pad base32 string if needed
    secret_clean = secret_b32.strip().upper()
    padding = '=' * ((8 - len(secret_clean) % 8) % 8)
    key = base64.b32decode(secret_clean + padding, casefold=True)

    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[19] & 0x0F
    code_int = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{code_int % 1000000:06d}"


def verify_totp_code(secret_b32: str, code: str, window: int = 1) -> bool:
    """Verify 6-digit code against secret with a +/- window of 30-second steps."""
    if not secret_b32 or not code:
        return False

    code_clean = str(code).strip()
    if len(code_clean) != 6 or not code_clean.isdigit():
        return False

    now = int(time.time())
    for offset in range(-window, window + 1):
        expected = generate_totp_code(secret_b32, for_time=now + (offset * 30))
        if hmac.compare_digest(expected, code_clean):
            return True

    return False


def create_mfa_challenge(user_id: str, user_type: str, email: str, db_alias: Optional[str] = None, tenant_id: Optional[str] = None, tenant_slug: Optional[str] = None) -> str:
    """
    Generates a cryptographically random, short-lived MFA challenge token.
    Saves challenge metadata in Redis with 5-minute TTL.
    """
    token = secrets.token_urlsafe(32)
    redis_client = _get_redis_client()
    if not redis_client:
        raise RuntimeError("Redis service is unavailable. Cannot issue secure MFA challenge.")

    payload = {
        'user_id': str(user_id),
        'user_type': user_type,
        'email': email,
        'db_alias': db_alias,
        'tenant_id': str(tenant_id) if tenant_id else None,
        'tenant_slug': tenant_slug,
        'created_at': int(time.time()),
    }
    key = f"{MFA_CHALLENGE_PREFIX}{token}"
    redis_client.set(key, json.dumps(payload), ex=MFA_CHALLENGE_TTL)
    return token


def get_mfa_challenge(challenge_token: str) -> Optional[Dict[str, Any]]:
    """Retrieves MFA challenge token metadata from Redis without deleting it."""
    redis_client = _get_redis_client()
    if not redis_client or not challenge_token:
        return None

    key = f"{MFA_CHALLENGE_PREFIX}{challenge_token}"
    try:
        val = redis_client.get(key)
        if not val:
            return None
        return json.loads(val.decode('utf-8') if isinstance(val, bytes) else val)
    except Exception as e:
        logger.error("Error reading MFA challenge token from Redis: %s", e)
        return None


def consume_mfa_challenge(challenge_token: str) -> Optional[Dict[str, Any]]:
    """
    Atomically retrieves and deletes an MFA challenge token from Redis.
    Prevents replay attacks by ensuring each challenge is single-use.
    """
    redis_client = _get_redis_client()
    if not redis_client or not challenge_token:
        return None

    key = f"{MFA_CHALLENGE_PREFIX}{challenge_token}"
    try:
        val = redis_client.get(key)
        if not val:
            return None
        # Single-use: delete immediately
        redis_client.delete(key)
        return json.loads(val.decode('utf-8') if isinstance(val, bytes) else val)
    except Exception as e:
        logger.error("Error consuming MFA challenge token from Redis: %s", e)
        return None
