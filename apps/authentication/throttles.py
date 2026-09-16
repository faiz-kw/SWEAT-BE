"""
Authentication endpoint throttles — Phase 1 Layer 1 security hardening.

DRF SimpleRateThrottle subclasses applied to login, MFA, and token-refresh
endpoints as a production-compatible, Redis-independent rate-limiting layer.

Design rationale
----------------
The primary lockout mechanism (security.py check_login_lockout / record_login_failure)
uses Redis and is credential-aware (per-account, per-IP, per-tenant).  When Redis is
temporarily unavailable it fails-open (allows the request through) to maintain
availability.

These DRF throttles are the SECONDARY, Redis-independent layer.  They use Django's
DEFAULT_CACHE backend — typically in-memory LocMemCache in development and a dedicated
Redis cache DB in production.  Even if the primary Redis lockout logic is down, the
DRF throttle continues to enforce coarse-grained request rate limits per IP.

Scopes and rates are controlled via settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']:
    'auth_login'   — applied to /auth/login/, /auth/platform/login/, /auth/tenant/login/
    'auth_mfa'     — applied to /auth/mfa/verify/
    'auth_refresh' — applied to /auth/token/refresh/

Production-recommended defaults (can be overridden in settings.py or .env):
    auth_login:   10/minute  (per IP)
    auth_mfa:     5/minute   (per IP)
    auth_refresh: 20/minute  (per IP)
"""

from rest_framework.throttling import SimpleRateThrottle


class LoginRateThrottle(SimpleRateThrottle):
    """
    Coarse-grained per-IP rate limit for all login endpoints.
    Keyed on client IP — operates independently of Redis lockout logic.
    """
    scope = 'auth_login'

    def get_cache_key(self, request, view):
        ident = self.get_ident(request)
        return self.cache_format % {
            'scope': self.scope,
            'ident': ident,
        }


class MFARateThrottle(SimpleRateThrottle):
    """
    Per-IP rate limit for MFA verification endpoint.
    Tighter than login to prevent brute-forcing TOTP codes.
    """
    scope = 'auth_mfa'

    def get_cache_key(self, request, view):
        ident = self.get_ident(request)
        return self.cache_format % {
            'scope': self.scope,
            'ident': ident,
        }


class TokenRefreshRateThrottle(SimpleRateThrottle):
    """
    Per-IP rate limit for token refresh endpoint.
    Prevents token-farming via automated refresh loops.
    """
    scope = 'auth_refresh'

    def get_cache_key(self, request, view):
        ident = self.get_ident(request)
        return self.cache_format % {
            'scope': self.scope,
            'ident': ident,
        }
