"""
JWT Authentication Backends — Phase 1 Layer 1.

Two distinct authentication systems:
1. PlatformJWTAuthentication — platform team (PlatformUser in Master DB)
2. TenantJWTAuthentication   — tenant end-users (TenantUser in Tenant-dedicated DB)

JWT payload claims:
  sub        — user UUID
  user_type  — 'platform' | 'tenant'
  roles      — list of role codes (always a list, never a single string)
  tid        — tenant UUID (tenant users only)
  db_alias   — tenant DB alias (set during login, used by TenantRouter)

Security notes:
  - Tokens are validated by simplejwt's AccessToken — signature verified against SECRET_KEY
  - User status is re-checked from DB on every request (not trusted from token)
  - Tenant status is re-checked from DB on every request (ACTIVE or TRIALING required)
  - TenantJWTAuthentication refuses to fall back to 'default' DB if db_alias is missing
"""

import logging
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.tokens import AccessToken
from rest_framework_simplejwt.exceptions import TokenError, InvalidToken

logger = logging.getLogger(__name__)


class PlatformJWTAuthentication(BaseAuthentication):
    """
    Authenticates platform team members (PlatformUser from Master DB).
    JWT must have user_type='platform' claim.

    Attaches to user object:
      user._auth_type   — 'platform'
      user._roles       — list of role codes from token
      user._role_codes  — set of role codes for O(1) membership checks
    """

    def authenticate(self, request):
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        if not auth_header.startswith('Bearer '):
            return None

        token_str = auth_header.split(' ', 1)[1]

        try:
            token = AccessToken(token_str)
        except (TokenError, InvalidToken):
            return None

        # Only handle platform tokens — pass non-platform tokens to next backend
        if token.get('user_type') != 'platform':
            return None

        user_id = token.get('sub')
        if not user_id:
            raise AuthenticationFailed('Invalid platform token: missing sub claim.')

        try:
            from apps.master.models_iam import PlatformUser
            user = PlatformUser.objects.using('default').get(id=user_id)
        except PlatformUser.DoesNotExist:
            raise AuthenticationFailed('Platform user not found or deactivated.')

        if user.status != 'ACTIVE':
            raise AuthenticationFailed(
                f'Platform user account is {user.status}. Contact platform support.'
            )

        # Attach authentication context — consumed by permission classes
        user._auth_type = 'platform'
        roles = token.get('roles', [])
        user._roles = roles if isinstance(roles, list) else []
        user._role_codes = set(user._roles)

        logger.debug(
            'PlatformJWTAuthentication: Authenticated user=%s roles=%s | correlation_id=%s',
            user.email, user._roles,
            getattr(request, 'correlation_id', '-'),
        )
        return (user, token)

    def authenticate_header(self, request):
        return 'Bearer realm="PerformanceOS Platform"'


class TenantJWTAuthentication(BaseAuthentication):
    """
    Authenticates tenant end-users (TenantUser from Tenant-dedicated DB).
    JWT must have user_type='tenant' and tid (tenant_id) claims.

    Security checks performed on EVERY request:
      1. Token signature valid (simplejwt AccessToken)
      2. user_type == 'tenant'
      3. sub + tid claims present
      4. Tenant still exists in Master DB
      5. Tenant status is ACTIVE or TRIALING
      6. db_alias claim is present (refuses to fall back to 'default')
      7. TenantUser exists in Tenant DB
      8. TenantUser.status == 'ACTIVE'

    Attaches to user object:
      user._auth_type   — 'tenant'
      user._tenant_id   — tenant UUID string
      user._db_alias    — tenant DB alias string
      user._roles       — list of role codes from token
      user._role_codes  — set of role codes for O(1) membership checks
    """

    # Subscription statuses that allow API access
    ALLOWED_SUBSCRIPTION_STATUSES = {'ACTIVE', 'TRIALING'}

    def authenticate(self, request):
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        if not auth_header.startswith('Bearer '):
            return None

        token_str = auth_header.split(' ', 1)[1]

        try:
            token = AccessToken(token_str)
        except (TokenError, InvalidToken):
            return None

        # Only handle tenant tokens
        if token.get('user_type') != 'tenant':
            return None

        user_id = token.get('sub')
        tenant_id = token.get('tid')
        db_alias = token.get('db_alias')

        if not user_id:
            raise AuthenticationFailed('Invalid tenant token: missing sub claim.')
        if not tenant_id:
            raise AuthenticationFailed('Invalid tenant token: missing tid claim.')
        if not db_alias:
            # Security: never fall back to 'default' DB — that is the Master DB
            # A missing db_alias means the token was issued before Sprint 1 or is malformed
            raise AuthenticationFailed(
                'Invalid tenant token: missing db_alias claim. Please log in again.'
            )

        # Re-verify tenant is still ACTIVE or TRIALING (status gate on every request)
        try:
            from apps.master.models_tenant import Tenant
            tenant = Tenant.objects.using('default').only(
                'id', 'status', 'slug'
            ).get(id=tenant_id)
        except Tenant.DoesNotExist:
            raise AuthenticationFailed('Tenant not found. Please log in again.')

        if not tenant.is_accessible:
            raise AuthenticationFailed(
                f'Organization access denied: tenant is {tenant.status}. '
                'Contact your administrator.'
            )

        # Check subscription status against live Master DB (Fail-closed)
        try:
            from apps.master.models_saas import TenantSubscription
            sub = TenantSubscription.objects.using('default').filter(
                tenant_id=tenant_id
            ).order_by('-created_at').first()

            if not sub:
                raise AuthenticationFailed(
                    'No subscription found for your organization. Please contact your administrator.'
                )

            if sub.status == 'ACTIVE':
                pass  # Allowed
            elif sub.status == 'TRIALING':
                if sub.trial_ends_at and sub.trial_ends_at < timezone.now():
                    raise AuthenticationFailed(
                        'Your organization trial period has expired. Please upgrade your subscription.'
                    )
                # Valid trial with future trial_ends_at or trial_ends_at is None
            elif sub.status == 'PAST_DUE':
                raise AuthenticationFailed(
                    'Your organization subscription payment is past due. Access is suspended.'
                )
            elif sub.status in ('CANCELED', 'PAUSED', 'SUSPENDED', 'EXPIRED'):
                raise AuthenticationFailed(
                    f'Your organization subscription is {sub.get_status_display().lower()}. Access denied.'
                )
            else:
                # Any other unexpected or unapproved status
                raise AuthenticationFailed(
                    f'Your organization subscription status ({sub.status}) does not permit access.'
                )
        except AuthenticationFailed:
            raise
        except Exception as e:
            # Strictly fail closed on Master DB or subscription lookup errors
            logger.error(
                'TenantJWTAuthentication: Subscription verification error for tenant_id=%s: %s',
                tenant_id, e,
            )
            raise AuthenticationFailed(
                'Unable to verify organization subscription status. Access denied.'
            )

        # Register and set the tenant DB alias for this request
        try:
            from config.tenant_middleware import _register_tenant_connection
            from config.routers import set_tenant_db_alias
            from apps.master.models_infra import TenantDataSource
            data_source = TenantDataSource.objects.using('default').filter(
                tenant_id=tenant_id,
                status='ACTIVE',
            ).only('db_name').first()
            if data_source and data_source.db_name:
                _register_tenant_connection(db_alias, data_source.db_name)
            set_tenant_db_alias(db_alias)
        except Exception as e:
            logger.error(
                'TenantJWTAuthentication: Failed to register DB alias=%s: %s',
                db_alias, e,
            )
            raise AuthenticationFailed('Failed to connect to organization database.')

        # Load user from Tenant DB — never from Master DB
        try:
            from apps.tenant_core.models_users import TenantUser
            user = TenantUser.objects.using(db_alias).select_related(
                'home_branch', 'organization'
            ).get(id=user_id)
        except TenantUser.DoesNotExist:
            raise AuthenticationFailed(
                'Tenant user not found. Account may have been removed.'
            )
        except Exception as e:
            logger.error(
                'TenantJWTAuthentication: Error loading user id=%s from alias=%s: %s',
                user_id, db_alias, e,
            )
            raise AuthenticationFailed('Failed to load user from organization database.')

        if user.status != 'ACTIVE':
            raise AuthenticationFailed(
                f'User account is {user.status}. Contact your administrator.'
            )

        # Attach authentication context — consumed by RBAC permission classes
        user._auth_type = 'tenant'
        user._tenant_id = tenant_id
        user._db_alias = db_alias
        roles = token.get('roles', [])
        user._roles = roles if isinstance(roles, list) else []
        user._role_codes = set(user._roles)

        logger.debug(
            'TenantJWTAuthentication: Authenticated user=%s tenant=%s roles=%s | correlation_id=%s',
            user.email, tenant.slug, user._roles,
            getattr(request, 'correlation_id', '-'),
        )
        return (user, token)

    def authenticate_header(self, request):
        return 'Bearer realm="PerformanceOS Tenant"'
