"""
Tenant Database Middleware — Phase 1 Layer 1.

Per request:
1. Extract tenant identifier from JWT claim 'tid' using VERIFIED signature decode
2. Look up tenant's data source record in Master DB
3. Register dynamic DB connection if not already registered (thread-safe)
4. Set thread-local tenant DB alias for DB router
5. Verify tenant status is ACTIVE (gate check)

SECURITY NOTE:
  Previously, this middleware decoded the JWT without signature verification
  (verify_signature: False). This allowed an attacker to forge any 'tid' claim
  and redirect their request to another tenant's database.

  Fixed: JWT is now decoded with full signature verification using SECRET_KEY.
  An invalid or tampered token results in no tenant DB being set — the request
  continues but all tenant_core model queries will fail with routing errors,
  which is the correct secure behavior.
"""

import logging
import threading
from django.conf import settings
from config.routers import set_tenant_db_alias

logger = logging.getLogger(__name__)

# Paths that bypass tenant DB resolution (platform-only and auth endpoints)
TENANT_EXEMPT_PREFIXES = (
    '/api/v1/platform/',
    '/api/v1/auth/',
    '/admin/',
    '/api/schema/',
    '/api/docs/',
    '/static/',
)

# Thread lock to protect mutation of settings.DATABASES at runtime
# This prevents race conditions in multi-process/multi-threaded gunicorn deployments
_db_registration_lock = threading.Lock()


def _register_tenant_connection(alias: str, db_name: str) -> None:
    """
    Dynamically add a tenant DB connection to Django's connection handler.

    Thread-safe: uses a module-level lock to prevent concurrent mutation of
    settings.DATABASES, which would cause connection pool collisions in gunicorn.

    The connection is registered once and reused on subsequent requests via
    the alias check before acquiring the lock.
    """
    # Fast path — already registered, no lock needed
    if alias in settings.DATABASES:
        return

    with _db_registration_lock:
        # Double-check after acquiring lock (another thread may have registered)
        if alias in settings.DATABASES:
            return

        # Copy the full master DB config (inherits ENGINE, USER, PASSWORD, HOST, PORT,
        # OPTIONS, CONN_MAX_AGE, TIME_ZONE, ATOMIC_REQUESTS, etc.) then override only NAME.
        # This ensures all required Django DB settings keys are always present.
        tenant_config = dict(settings.DATABASES['default'])
        tenant_config['NAME'] = db_name

        settings.DATABASES[alias] = tenant_config

        # Also update the Django connections handler (required for runtime registration)
        from django.db import connections
        connections.databases[alias] = tenant_config

        logger.debug('Registered tenant DB connection: alias=%s db=%s', alias, db_name)


def _extract_tenant_id_from_jwt(request) -> str | None:
    """
    Extract 'tid' (tenant_id) claim from Authorization Bearer JWT.

    SECURITY: The JWT is now decoded with full signature verification using
    Django's SECRET_KEY. This prevents an attacker from crafting a JWT with
    a forged 'tid' to redirect requests to a different tenant's database.

    Returns:
        str — tenant UUID if JWT is valid and contains 'tid'
        None — if no bearer token, token is expired, or signature is invalid
    """
    import jwt as pyjwt
    from jwt.exceptions import InvalidTokenError, ExpiredSignatureError

    auth_header = request.META.get('HTTP_AUTHORIZATION', '')
    if not auth_header.startswith('Bearer '):
        return None

    token = auth_header.split(' ', 1)[1]
    if not token:
        return None

    try:
        payload = pyjwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=['HS256'],
            options={
                'verify_exp': True,       # Reject expired tokens
                'verify_signature': True,  # Verify against SECRET_KEY
                'require': ['tid'],        # Must have tid claim
            },
        )
        return payload.get('tid') or None

    except ExpiredSignatureError:
        # Expired tokens: no warning needed, this is normal flow
        return None
    except InvalidTokenError as e:
        # Tampered, malformed, or wrongly-signed token — log as warning
        logger.warning(
            'TenantDatabaseMiddleware: JWT signature invalid or malformed — '
            'refusing to set tenant DB alias. Error: %s | correlation_id=%s',
            type(e).__name__,
            getattr(request, 'correlation_id', '-'),
        )
        return None
    except Exception as e:
        logger.error(
            'TenantDatabaseMiddleware: Unexpected error decoding JWT: %s',
            e,
        )
        return None


class TenantDatabaseMiddleware:
    """
    Sets the active tenant DB alias per request so TenantRouter can route correctly.
    Skips platform-only and auth endpoints (TENANT_EXEMPT_PREFIXES).

    Security properties:
    - JWT is verified with SECRET_KEY before trusting any claims
    - Forged or expired tokens result in no tenant DB alias being set
    - Thread-local state is always reset before and after each request
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Always reset tenant alias at start of each request
        # This is critical for thread safety in worker reuse scenarios
        set_tenant_db_alias(None)

        path = request.path_info
        is_exempt = any(path.startswith(prefix) for prefix in TENANT_EXEMPT_PREFIXES)

        if not is_exempt:
            # Only trust JWT-extracted tid — X-Tenant-ID header removed
            # (header was not verified; JWT provides the only trusted tenant source)
            tenant_id = _extract_tenant_id_from_jwt(request)

            if tenant_id:
                try:
                    from apps.master.models_infra import TenantDataSource
                    data_source = TenantDataSource.objects.using('default').filter(
                        tenant_id=tenant_id,
                        status='ACTIVE'
                    ).select_related('tenant').first()

                    if data_source and data_source.db_name:
                        alias = f"tenant_{data_source.db_name}"
                        _register_tenant_connection(alias, data_source.db_name)
                        set_tenant_db_alias(alias)
                        logger.debug(
                            'TenantDatabaseMiddleware: tenant=%s alias=%s path=%s',
                            tenant_id, alias, path,
                        )
                    else:
                        logger.warning(
                            'TenantDatabaseMiddleware: No active data source for tenant_id=%s '
                            'correlation_id=%s',
                            tenant_id,
                            getattr(request, 'correlation_id', '-'),
                        )
                except Exception as e:
                    logger.error(
                        'TenantDatabaseMiddleware: Error resolving tenant DB for tenant_id=%s: %s',
                        tenant_id, e,
                    )

        response = self.get_response(request)

        # Always clean up thread-local after response (prevents state leaking between requests)
        set_tenant_db_alias(None)
        return response
