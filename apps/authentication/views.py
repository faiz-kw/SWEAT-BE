"""
Authentication Views — Phase 1 Layer 1.

Provides JWTs for Platform Users (Master DB) and Tenant Users (Tenant DB).
All tokens include real role claims read from the database — no hardcoded values.

Endpoints:
  POST /api/v1/auth/login/             — UniversalLoginView (requires tenant_slug for tenant users)
  POST /api/v1/auth/platform/login/   — PlatformLoginView
  POST /api/v1/auth/tenant/login/     — TenantLoginView
  POST /api/v1/auth/token/refresh/    — TokenRefreshView
  POST /api/v1/auth/logout/           — LogoutView (blacklists refresh token)
  GET  /api/v1/auth/me/               — MeView
"""

import logging
from django.utils import timezone
from rest_framework import status, serializers
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError
from apps.authentication.security import (
    set_refresh_cookie,
    delete_refresh_cookie,
    revoke_all_user_sessions,
    is_token_revoked,
    check_login_lockout,
    record_login_failure,
    reset_login_lockout,
    generate_totp_secret,
    get_totp_uri,
    verify_totp_code,
    create_mfa_challenge,
    consume_mfa_challenge,
    get_mfa_challenge,
)
from apps.authentication.throttles import (
    LoginRateThrottle,
    MFARateThrottle,
    TokenRefreshRateThrottle,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token Builders — Real claims from DB, no hardcoded values
# ---------------------------------------------------------------------------

def _build_platform_token(user):
    """
    Issue a JWT for a platform user.

    Role claims are read from PlatformUserRole (Master DB).
    If the user has no active role assignments, roles list is empty — they
    will be blocked by platform permission checks (not silently promoted).

    Claims:
      sub        — PlatformUser UUID
      user_type  — 'platform'
      roles      — list of PlatformRole.code (from active PlatformUserRole records)
      is_staff   — bool (from PlatformUser.is_staff)
      tid        — empty string (platform users have no tenant)
      email      — user's email
      full_name  — user's display name
    """
    from apps.master.models_iam import PlatformUserRole

    # Read actual role assignments from Master DB
    active_roles = list(
        PlatformUserRole.objects.using('default')
        .filter(platform_user=user, is_active=True)
        .select_related('role')
        .values_list('role__code', flat=True)
    )

    refresh = RefreshToken()
    refresh['sub'] = str(user.id)
    refresh['user_type'] = 'platform'
    refresh['roles'] = active_roles         # Real roles — list, not hardcoded string
    refresh['is_staff'] = user.is_staff
    refresh['is_superuser'] = user.is_superuser
    refresh['tid'] = ''                     # No tenant context for platform users
    refresh['email'] = user.email
    refresh['full_name'] = user.full_name

    logger.debug(
        'Built platform token for user=%s roles=%s',
        user.email, active_roles,
    )
    return refresh


def _build_tenant_token(user, tenant, db_alias):
    """
    Issue a JWT for a tenant user.

    Role claims are read from RoleAssignment (Tenant DB).
    If the user has no active role assignments, roles list is empty — they
    will receive 403 on any RBAC-protected endpoint.

    Claims:
      sub         — TenantUser UUID
      user_type   — 'tenant'
      roles       — list of Role.code (from active RoleAssignment records)
      tid         — Tenant UUID (verified + DB-backed)
      db_alias    — tenant DB alias (e.g. 'tenant_tenant_cult_fit')
      email       — user email
      full_name   — user display name
      home_branch — UUID of user's home branch (or None)
    """
    from apps.tenant_core.models_rbac import RoleAssignment

    # Read actual active role assignments from Tenant DB
    active_roles = list(
        RoleAssignment.objects.using(db_alias)
        .filter(user=user, is_active=True)
        .select_related('role')
        .values_list('role__code', flat=True)
    )

    home_branch_id = (
        str(user.home_branch.id)
        if getattr(user, 'home_branch', None) and user.home_branch
        else None
    )

    refresh = RefreshToken()
    refresh['sub'] = str(user.id)
    refresh['user_type'] = 'tenant'
    refresh['roles'] = active_roles         # Real roles — list, not hardcoded string
    refresh['tid'] = str(tenant.id)
    refresh['tenant_slug'] = tenant.slug
    refresh['db_alias'] = db_alias
    refresh['email'] = user.email
    refresh['full_name'] = user.full_name
    refresh['home_branch_id'] = home_branch_id

    logger.debug(
        'Built tenant token for user=%s tenant=%s roles=%s',
        user.email, tenant.slug, active_roles,
    )
    return refresh


# ---------------------------------------------------------------------------
# Shared Login Helper
# ---------------------------------------------------------------------------

def _register_and_resolve_tenant(tenant):
    """
    Given an ACTIVE Tenant object, register its DB connection and return the alias.
    Raises ValueError if the data source is unavailable.
    """
    from apps.master.models_infra import TenantDataSource
    from config.tenant_middleware import _register_tenant_connection
    from config.routers import set_tenant_db_alias

    try:
        data_source = TenantDataSource.objects.using('default').get(
            tenant=tenant,
            status='ACTIVE',
        )
    except TenantDataSource.DoesNotExist:
        raise ValueError('Tenant database not provisioned or not active.')

    effective_db_name = data_source.database_name or data_source.db_name
    if not effective_db_name:
        raise ValueError('Tenant database name is not configured.')

    import sys
    from django.conf import settings
    from config.routers import build_tenant_db_alias

    if 'test' in sys.argv and 'tenant_test' in settings.DATABASES and effective_db_name in ('fitness_tenant', 'test_fitness_tenant', 'test'):
        db_alias = 'tenant_test'
    else:
        db_alias = build_tenant_db_alias(tenant.id)

    _register_tenant_connection(db_alias, effective_db_name, data_source=data_source, tenant_id=tenant.id)
    set_tenant_db_alias(db_alias)
    return db_alias


def _build_login_response(refresh, user_type, user_data, extra=None, request=None):
    """Build the standard login response dict with access token and HttpOnly refresh cookie."""
    payload = {
        'access': str(refresh.access_token),
        'user_type': user_type,
        'user': user_data,
    }
    if extra:
        payload.update(extra)
    resp = Response(payload)
    set_refresh_cookie(resp, str(refresh), request=request)
    return resp


# ---------------------------------------------------------------------------
# View: UniversalLoginView
# ---------------------------------------------------------------------------

class UniversalLoginView(APIView):
    """
    POST /api/v1/auth/login/

    Unified login for the web frontend.
    Detects Platform User vs Tenant User by presence of tenant_slug.

    - Without tenant_slug: attempts Platform User login (Master DB)
    - With tenant_slug: attempts Tenant User login (specified Tenant DB only)

    NOTE: The previous implementation scanned ALL active tenants when tenant_slug
    was omitted for a tenant user. This was an O(N) unbounded query that also
    leaked information about which tenants had a given email. That behavior has
    been removed. Tenant login now REQUIRES tenant_slug.
    """
    permission_classes = [AllowAny]
    throttle_classes = [LoginRateThrottle]

    def post(self, request):
        email = request.data.get('email', '').lower().strip()
        password = request.data.get('password', '')
        tenant_slug = request.data.get('tenant_slug', '').lower().strip()

        if not email or not password:
            return Response(
                {'error': 'Email and password are required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Route by presence of tenant_slug
        if not tenant_slug:
            return self._platform_login(request, email, password)
        else:
            return self._tenant_login(request, email, password, tenant_slug)

    def _platform_login(self, request, email, password):
        """Authenticate against Master DB platform_users table."""
        # Check brute force lockout
        is_locked, remaining = check_login_lockout(request, email, tenant_id=None)
        if is_locked:
            return Response(
                {'error': f'Too many failed login attempts. Account temporarily locked. Try again in {remaining} seconds.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        from apps.master.models_iam import PlatformUser

        try:
            user = PlatformUser.objects.using('default').get(email=email)
        except PlatformUser.DoesNotExist:
            record_login_failure(request, email, tenant_id=None)
            return Response(
                {'error': 'Invalid email or password.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not user.check_password(password):
            is_locked, rem = record_login_failure(request, email, tenant_id=None)
            if is_locked:
                return Response(
                    {'error': f'Too many failed login attempts. Account temporarily locked. Try again in {rem} seconds.'},
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )
            return Response(
                {'error': 'Invalid email or password.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if user.status != 'ACTIVE':
            reset_login_lockout(request, email, tenant_id=None)
            return Response(
                {'error': f'Account is {user.status}. Contact platform support.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Successful password verification — reset lockout
        reset_login_lockout(request, email, tenant_id=None)

        # MFA Challenge Gate
        if getattr(user, 'is_mfa_enabled', False):
            challenge_token = create_mfa_challenge(
                user_id=str(user.id),
                user_type='platform',
                email=user.email,
            )
            return Response({
                'mfa_required': True,
                'challenge_token': challenge_token,
                'method': 'totp',
                'message': 'MFA verification required. Please enter your 6-digit TOTP code.',
            }, status=status.HTTP_200_OK)

        refresh = _build_platform_token(user)
        _update_last_login(user, request, db='default')

        return _build_login_response(
            refresh,
            user_type='platform',
            user_data={
                'id': str(user.id),
                'email': user.email,
                'full_name': user.full_name,
                'status': user.status,
            },
            request=request,
        )

    def _tenant_login(self, request, email, password, tenant_slug):
        """Authenticate against the specified tenant's dedicated DB."""
        from apps.master.models_tenant import Tenant
        from apps.tenant_core.models_users import TenantUser

        try:
            tenant = Tenant.objects.using('default').get(
                slug=tenant_slug,
                status='ACTIVE',
            )
        except Tenant.DoesNotExist:
            # Do not reveal whether the slug exists — return same error
            return Response(
                {'error': 'Invalid email or password.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        tenant_id_str = str(tenant.id)

        # Check brute force lockout
        is_locked, remaining = check_login_lockout(request, email, tenant_id=tenant_id_str)
        if is_locked:
            return Response(
                {'error': f'Too many failed login attempts. Account temporarily locked. Try again in {remaining} seconds.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        try:
            db_alias = _register_and_resolve_tenant(tenant)
        except ValueError as e:
            logger.error(
                'UniversalLoginView: tenant DB unavailable for slug=%s: %s',
                tenant_slug, e,
            )
            return Response(
                {'error': 'Organization database is currently unavailable. Please try again.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            user = TenantUser.objects.using(db_alias).get(email=email)
        except TenantUser.DoesNotExist:
            record_login_failure(request, email, tenant_id=tenant_id_str)
            return Response(
                {'error': 'Invalid email or password.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not user.check_password(password):
            is_locked, rem = record_login_failure(request, email, tenant_id=tenant_id_str)
            if is_locked:
                return Response(
                    {'error': f'Too many failed login attempts. Account temporarily locked. Try again in {rem} seconds.'},
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )
            return Response(
                {'error': 'Invalid email or password.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if user.status != 'ACTIVE':
            reset_login_lockout(request, email, tenant_id=tenant_id_str)
            return Response(
                {'error': f'Account is {user.status}. Contact your administrator.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        if not user.is_login_allowed:
            reset_login_lockout(request, email, tenant_id=tenant_id_str)
            return Response(
                {'error': 'Account login is disabled. Contact your administrator.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Successful password verification — reset lockout
        reset_login_lockout(request, email, tenant_id=tenant_id_str)

        # MFA Challenge Gate: check user flag or tenant security policy
        from apps.tenant_core.views_security_policy import get_tenant_security_policy
        sec_policy = get_tenant_security_policy(db_alias)
        is_mfa_req = getattr(user, 'is_mfa_enabled', False) or (sec_policy and sec_policy.get('mfa_required'))

        if is_mfa_req:
            challenge_token = create_mfa_challenge(
                user_id=str(user.id),
                user_type='tenant',
                email=user.email,
                db_alias=db_alias,
                tenant_id=str(tenant.id),
                tenant_slug=tenant.slug,
            )
            return Response({
                'mfa_required': True,
                'challenge_token': challenge_token,
                'method': 'totp',
                'message': 'MFA verification required. Please enter your 6-digit TOTP code.',
            }, status=status.HTTP_200_OK)

        refresh = _build_tenant_token(user, tenant, db_alias)
        _update_last_login(user, request, db=db_alias)

        return _build_login_response(
            refresh,
            user_type='tenant',
            user_data={
                'id': str(user.id),
                'email': user.email,
                'full_name': user.full_name,
                'status': user.status,
            },
            extra={
                'tenant': {
                    'id': str(tenant.id),
                    'slug': tenant.slug,
                    'name': tenant.name,
                },
            },
            request=request,
        )


# ---------------------------------------------------------------------------
# View: PlatformLoginView
# ---------------------------------------------------------------------------

class PlatformLoginView(APIView):
    """POST /api/v1/auth/platform/login/ — Explicit platform user login."""
    permission_classes = [AllowAny]
    throttle_classes = [LoginRateThrottle]

    def post(self, request):
        email = request.data.get('email', '').lower().strip()
        password = request.data.get('password', '')

        if not email or not password:
            return Response(
                {'error': 'Email and password are required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check brute force lockout
        is_locked, remaining = check_login_lockout(request, email, tenant_id=None)
        if is_locked:
            return Response(
                {'error': f'Too many failed login attempts. Account temporarily locked. Try again in {remaining} seconds.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        from apps.master.models_iam import PlatformUser

        try:
            user = PlatformUser.objects.using('default').get(email=email)
        except PlatformUser.DoesNotExist:
            record_login_failure(request, email, tenant_id=None)
            return Response(
                {'error': 'Invalid credentials.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not user.check_password(password):
            is_locked, rem = record_login_failure(request, email, tenant_id=None)
            if is_locked:
                return Response(
                    {'error': f'Too many failed login attempts. Account temporarily locked. Try again in {rem} seconds.'},
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )
            return Response(
                {'error': 'Invalid credentials.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if user.status != 'ACTIVE':
            reset_login_lockout(request, email, tenant_id=None)
            return Response(
                {'error': f'Account is {user.status}. Contact platform support.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Successful password verification — reset lockout
        reset_login_lockout(request, email, tenant_id=None)

        # MFA Challenge Gate
        if getattr(user, 'is_mfa_enabled', False):
            challenge_token = create_mfa_challenge(
                user_id=str(user.id),
                user_type='platform',
                email=user.email,
            )
            return Response({
                'mfa_required': True,
                'challenge_token': challenge_token,
                'method': 'totp',
                'message': 'MFA verification required. Please enter your 6-digit TOTP code.',
            }, status=status.HTTP_200_OK)

        refresh = _build_platform_token(user)
        _update_last_login(user, request, db='default')

        return _build_login_response(
            refresh,
            user_type='platform',
            user_data={
                'id': str(user.id),
                'email': user.email,
                'full_name': user.full_name,
                'status': user.status,
            },
            request=request,
        )


# ---------------------------------------------------------------------------
# View: TenantLoginView
# ---------------------------------------------------------------------------

class TenantLoginView(APIView):
    """POST /api/v1/auth/tenant/login/ — Explicit tenant user login."""
    permission_classes = [AllowAny]
    throttle_classes = [LoginRateThrottle]

    def post(self, request):
        email = request.data.get('email', '').lower().strip()
        password = request.data.get('password', '')
        tenant_slug = request.data.get('tenant_slug', '').lower().strip()

        if not all([email, password, tenant_slug]):
            return Response(
                {'error': 'email, password and tenant_slug are required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from apps.master.models_tenant import Tenant
        from apps.tenant_core.models_users import TenantUser

        try:
            tenant = Tenant.objects.using('default').get(slug=tenant_slug)
        except Tenant.DoesNotExist:
            return Response(
                {'error': 'Tenant not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not tenant.is_accessible:
            return Response(
                {'error': f'Organization access is {tenant.status}. Contact your administrator.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        tenant_id_str = str(tenant.id)

        # Check brute force lockout
        is_locked, remaining = check_login_lockout(request, email, tenant_id=tenant_id_str)
        if is_locked:
            return Response(
                {'error': f'Too many failed login attempts. Account temporarily locked. Try again in {remaining} seconds.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        try:
            db_alias = _register_and_resolve_tenant(tenant)
        except ValueError as e:
            logger.error(
                'TenantLoginView: tenant DB unavailable for slug=%s: %s',
                tenant_slug, e,
            )
            return Response(
                {'error': 'Organization database is currently unavailable. Please try again.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            user = TenantUser.objects.using(db_alias).get(email=email)
        except TenantUser.DoesNotExist:
            record_login_failure(request, email, tenant_id=tenant_id_str)
            return Response(
                {'error': 'Invalid credentials.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not user.check_password(password):
            is_locked, rem = record_login_failure(request, email, tenant_id=tenant_id_str)
            if is_locked:
                return Response(
                    {'error': f'Too many failed login attempts. Account temporarily locked. Try again in {rem} seconds.'},
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )
            return Response(
                {'error': 'Invalid credentials.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if user.status != 'ACTIVE':
            reset_login_lockout(request, email, tenant_id=tenant_id_str)
            return Response(
                {'error': f'Account is {user.status}.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        if not user.is_login_allowed:
            reset_login_lockout(request, email, tenant_id=tenant_id_str)
            return Response(
                {'error': 'Account login is disabled.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Successful password verification — reset lockout
        reset_login_lockout(request, email, tenant_id=tenant_id_str)

        # MFA Challenge Gate
        from apps.tenant_core.views_security_policy import get_tenant_security_policy
        sec_policy = get_tenant_security_policy(db_alias)
        is_mfa_req = getattr(user, 'is_mfa_enabled', False) or (sec_policy and sec_policy.get('mfa_required'))

        if is_mfa_req:
            challenge_token = create_mfa_challenge(
                user_id=str(user.id),
                user_type='tenant',
                email=user.email,
                db_alias=db_alias,
                tenant_id=str(tenant.id),
                tenant_slug=tenant.slug,
            )
            return Response({
                'mfa_required': True,
                'challenge_token': challenge_token,
                'method': 'totp',
                'message': 'MFA verification required. Please enter your 6-digit TOTP code.',
            }, status=status.HTTP_200_OK)

        refresh = _build_tenant_token(user, tenant, db_alias)
        _update_last_login(user, request, db=db_alias)

        return _build_login_response(
            refresh,
            user_type='tenant',
            user_data={
                'id': str(user.id),
                'email': user.email,
                'full_name': user.full_name,
                'status': user.status,
            },
            extra={
                'tenant': {
                    'id': str(tenant.id),
                    'slug': tenant.slug,
                    'name': tenant.name,
                },
            },
            request=request,
        )


# ---------------------------------------------------------------------------
# View: MeView
# ---------------------------------------------------------------------------

class MeView(APIView):
    """
    GET /api/v1/auth/me/

    Returns authenticated user's profile, roles, enabled modules, and branding.
    All data is read from the database — no hardcoded values.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        auth_type = getattr(user, '_auth_type', None)

        if auth_type == 'platform':
            return self._platform_me(request, user)
        elif auth_type == 'tenant':
            return self._tenant_me(request, user)
        else:
            return Response(
                {'error': 'Unable to determine user type from token.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

    def _platform_me(self, request, user):
        """Build /me response for a platform team member."""
        from apps.master.models_iam import PlatformUserRole

        active_roles = list(
            PlatformUserRole.objects.using('default')
            .filter(platform_user=user, is_active=True)
            .select_related('role')
            .values('role__code', 'role__name')
        )

        return Response({
            'id': str(user.id),
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'full_name': user.full_name,
            'user_type': 'platform',
            'roles': [{'code': r['role__code'], 'name': r['role__name']} for r in active_roles],
            'is_staff': user.is_staff,
            'is_superuser': user.is_superuser,
            'tenant_id': None,
            'tenant_name': None,
            'active_branch_id': None,
            'enabled_modules': None,  # Platform users see all tenant modules via admin
            'allowed_branches': [],
            'branding': None,
        })

    def _tenant_me(self, request, user):
        """Build /me response for a tenant end-user."""
        from apps.master.models_tenant import Tenant
        from apps.tenant_core.models_rbac import RoleAssignment

        tenant_id = getattr(user, '_tenant_id', None)
        db_alias = getattr(user, '_db_alias', None)

        # Ensure tenant router is set for this response (may have been reset)
        if db_alias:
            from config.routers import set_tenant_db_alias
            set_tenant_db_alias(db_alias)

        # Load tenant details from Master DB
        tenant_data = {}
        enabled_modules = []
        branding = None

        if tenant_id:
            try:
                tenant = Tenant.objects.using('default').select_related(
                    'branding'
                ).filter(id=tenant_id).first()

                if tenant:
                    tenant_data = {
                        'id': str(tenant.id),
                        'slug': tenant.slug,
                        'name': tenant.name,
                    }
                    # Enabled modules come from master TenantModule — real data
                    enabled_modules = list(
                        tenant.enabled_modules_set
                        .filter(is_enabled=True)
                        .select_related('module')
                        .values_list('module__code', flat=True)
                    )
                    # Branding from master DB
                    if hasattr(tenant, 'branding') and tenant.branding:
                        b = tenant.branding
                        branding = {
                            'primary_color': b.primary_color,
                            'accent_color': b.accent_color,
                            'logo_url': b.logo_url,
                            'app_name': b.app_name,
                        }
            except Exception as e:
                logger.error('MeView: Error loading tenant context for tid=%s: %s', tenant_id, e)

        # Load active role assignments from Tenant DB
        active_roles = []
        allowed_branches = []

        if db_alias:
            try:
                assignments = (
                    RoleAssignment.objects.using(db_alias)
                    .filter(user=user, is_active=True)
                    .select_related('role', 'branch')
                )
                for ra in assignments:
                    active_roles.append({
                        'code': ra.role.code,
                        'name': ra.role.name,
                        'scope': ra.role.scope,
                        'branch_id': str(ra.branch.id) if ra.branch else None,
                        'branch_name': ra.branch.name if ra.branch else None,
                    })
                    if ra.branch:
                        allowed_branches.append({
                            'id': str(ra.branch.id),
                            'name': ra.branch.name,
                            'code': ra.branch.code,
                        })
            except Exception as e:
                logger.error('MeView: Error loading role assignments from db_alias=%s: %s', db_alias, e)

        # Home branch details from Tenant DB
        home_branch_data = None
        try:
            hb = getattr(user, 'home_branch', None)
            if hb:
                home_branch_data = {
                    'id': str(hb.id),
                    'name': hb.name,
                    'code': hb.code,
                    'address': getattr(hb, 'address', ''),
                    'phone': getattr(hb, 'phone', ''),
                    'status': getattr(hb, 'status', ''),
                }
        except Exception:
            pass

        return Response({
            'id': str(user.id),
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'full_name': user.full_name,
            'user_type': 'tenant',
            'roles': active_roles,                     # Real role assignments from DB
            'is_staff': False,
            'is_superuser': False,
            'tenant_id': str(tenant_id) if tenant_id else None,
            'tenant_name': tenant_data.get('name') if tenant_data else None,
            'tenant': tenant_data,
            'home_branch': home_branch_data,
            'enabled_modules': enabled_modules,        # Real module enablement from Master DB
            'allowed_branches': allowed_branches,
            'branding': branding,
        })


# ---------------------------------------------------------------------------
# Serializer: PerformanceOSTokenRefreshSerializer
# ---------------------------------------------------------------------------

class PerformanceOSTokenRefreshSerializer(serializers.Serializer):
    """
    Tenant-aware Token Refresh Serializer.
    
    SimpleJWT's default TokenRefreshSerializer hardcodes get_user_model().objects.get(id=user_id)
    against the Master DB. In our multi-tenant architecture:
      - 'platform' tokens map to PlatformUser in the Master DB ('default')
      - 'tenant' tokens map to TenantUser in the tenant's dedicated DB (db_alias)
    
    This serializer validates the user in the correct database based on the 'user_type'
    claim, and then performs full Refresh Token Rotation (RTR) and blacklisting.
    """
    refresh = serializers.CharField()
    access = serializers.CharField(read_only=True)
    token_class = RefreshToken

    def validate(self, attrs: dict) -> dict:
        from rest_framework_simplejwt.settings import api_settings
        from rest_framework.exceptions import AuthenticationFailed

        refresh = self.token_class(attrs["refresh"])
        user_type = refresh.payload.get('user_type')
        user_id = refresh.payload.get(api_settings.USER_ID_CLAIM)

        if user_type == 'tenant':
            from apps.tenant_core.models_users import TenantUser
            from config.tenant_middleware import _register_tenant_connection
            from config.routers import set_tenant_db_alias
            from apps.master.models_infra import TenantDataSource

            db_alias = refresh.payload.get('db_alias')
            tid = refresh.payload.get('tid')

            if not db_alias and tid:
                ds = TenantDataSource.objects.using('default').filter(tenant_id=tid, status='ACTIVE').first()
                if ds and (ds.db_name or ds.database_name):
                    effective_db_name = ds.database_name or ds.db_name
                    from config.routers import build_tenant_db_alias
                    import sys
                    from django.conf import settings
                    if 'test' in sys.argv and 'tenant_test' in settings.DATABASES and effective_db_name in ('fitness_tenant', 'test_fitness_tenant', 'test'):
                        db_alias = 'tenant_test'
                    else:
                        db_alias = build_tenant_db_alias(tid)

            if db_alias:
                # Ensure tenant connection is registered in thread
                ds = TenantDataSource.objects.using('default').filter(tenant_id=tid, status='ACTIVE').first() if tid else None
                effective_db_name = (ds.database_name or ds.db_name) if ds else (db_alias.replace('tenant_', '', 1) if db_alias.startswith('tenant_') else db_alias)
                _register_tenant_connection(db_alias, effective_db_name, data_source=ds, tenant_id=tid)
                try:
                    user = TenantUser.objects.using(db_alias).get(id=user_id)
                    if user.status != 'ACTIVE':
                        raise AuthenticationFailed('User account is not active.')
                except TenantUser.DoesNotExist:
                    raise AuthenticationFailed('Tenant user not found.')
        else:
            from apps.master.models_iam import PlatformUser
            try:
                user = PlatformUser.objects.using('default').get(id=user_id)
                if user.status != 'ACTIVE':
                    raise AuthenticationFailed('Platform user account is not active.')
            except PlatformUser.DoesNotExist:
                raise AuthenticationFailed('Platform user not found.')

        # Check if user's sessions have been revoked
        token_iat = refresh.payload.get('iat')
        if is_token_revoked(str(user_id), token_iat):
            raise AuthenticationFailed('Token has been revoked. Please log in again.')

        data = {"access": str(refresh.access_token)}

        if api_settings.ROTATE_REFRESH_TOKENS:
            if api_settings.BLACKLIST_AFTER_ROTATION:
                try:
                    refresh.blacklist()
                except AttributeError:
                    pass

            refresh.set_jti()
            refresh.set_exp()
            refresh.set_iat()
            refresh.outstand()

            data["refresh"] = str(refresh)

        return data


# ---------------------------------------------------------------------------
# View: TokenRefreshView
# ---------------------------------------------------------------------------

class TokenRefreshView(APIView):
    """
    POST /api/v1/auth/token/refresh/ — RTR: rotate refresh token and issue new access token.

    Uses PerformanceOSTokenRefreshSerializer to enforce:
      - Validation against expiration, blacklist, and invalid signatures
      - Automatic blacklisting of old refresh token when ROTATE_REFRESH_TOKENS and BLACKLIST_AFTER_ROTATION are True
      - Multi-tenant user validation against correct database (PlatformUser vs TenantUser)
      - Generation of new rotated refresh token with new JTI and fresh expiration
      - Inclusion in OutstandingToken table via refresh.outstand()
      - Rejection of reused old refresh tokens with 401 Unauthorized
    """
    permission_classes = [AllowAny]
    throttle_classes = [TokenRefreshRateThrottle]

    def post(self, request):
        from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
        from rest_framework.exceptions import AuthenticationFailed

        refresh_token_str = request.data.get('refresh') or request.COOKIES.get('refresh_token')

        if not refresh_token_str:
            return Response(
                {'error': 'Refresh token is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = PerformanceOSTokenRefreshSerializer(data={'refresh': refresh_token_str})
        try:
            serializer.is_valid(raise_exception=True)
        except (TokenError, InvalidToken) as e:
            logger.debug(
                'TokenRefreshView: Token validation/blacklist failed — %s | correlation_id=%s',
                type(e).__name__,
                getattr(request, 'correlation_id', '-'),
            )
            return Response(
                {'error': 'Invalid or expired refresh token.', 'detail': str(e)},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        except AuthenticationFailed as e:
            logger.debug(
                'TokenRefreshView: AuthenticationFailed — %s | correlation_id=%s',
                e,
                getattr(request, 'correlation_id', '-'),
            )
            return Response(
                {'error': 'Authentication failed.', 'detail': str(e)},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        except Exception as e:
            logger.debug(
                'TokenRefreshView: Refresh validation failed — %s | correlation_id=%s',
                e,
                getattr(request, 'correlation_id', '-'),
            )
            return Response(
                {'error': 'Invalid or expired refresh token.', 'detail': str(e)},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        validated_data = serializer.validated_data
        new_access_str = validated_data['access']
        new_refresh_str = validated_data.get('refresh') or refresh_token_str

        resp = Response({
            'access': new_access_str,
        }, status=status.HTTP_200_OK)

        set_refresh_cookie(resp, new_refresh_str, request=request)
        logger.debug(
            'TokenRefreshView: RTR complete — new access issued | correlation_id=%s',
            getattr(request, 'correlation_id', '-'),
        )
        return resp


# ---------------------------------------------------------------------------
# View: LogoutView
# ---------------------------------------------------------------------------

class LogoutView(APIView):
    """
    POST /api/v1/auth/logout/

    Blacklists the refresh token in the database (via simplejwt token_blacklist app)
    then clears the HttpOnly cookie.

    This ensures that even if a refresh token is stolen after logout, it
    cannot be used to obtain a new access token.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        refresh_token_str = request.data.get('refresh') or request.COOKIES.get('refresh_token')

        if refresh_token_str:
            try:
                refresh = RefreshToken(refresh_token_str)
                refresh.blacklist()
                logger.debug(
                    'LogoutView: Blacklisted refresh token | correlation_id=%s',
                    getattr(request, 'correlation_id', '-'),
                )
            except TokenError:
                # Already expired or blacklisted — not an error, still clear the cookie
                logger.debug('LogoutView: Refresh token was already expired/blacklisted at logout.')
            except Exception as e:
                # Unexpected error — log but don't fail logout
                logger.error('LogoutView: Unexpected error blacklisting token: %s', e)

        resp = Response({'message': 'Logged out successfully.'})
        delete_refresh_cookie(resp)
        return resp


# ---------------------------------------------------------------------------
# View: SessionRevocationView
# ---------------------------------------------------------------------------

class SessionRevocationView(APIView):
    """
    POST /api/v1/auth/sessions/revoke-all/
    POST /api/v1/auth/revoke-sessions/

    Revokes all active sessions for the authenticated user (or target user if authorized admin).
    Uses SimpleJWT blacklist and Redis revocation watermark for instant token invalidation.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        target_user_id = request.data.get('target_user_id') or str(user.id)
        user_type = getattr(user, '_auth_type', 'tenant')
        db_alias = getattr(user, '_db_alias', None)

        if target_user_id != str(user.id):
            if user_type == 'platform':
                if not (user.is_staff or user.is_superuser):
                    return Response({'error': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
            else:
                roles = getattr(user, '_roles', [])
                if 'TENANT_ADMIN' not in roles:
                    return Response({'error': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
                from apps.tenant_core.models_users import TenantUser
                if not TenantUser.objects.using(db_alias).filter(id=target_user_id).exists():
                    return Response({'error': 'Target user not found in this organization.'}, status=status.HTTP_404_NOT_FOUND)

        count = revoke_all_user_sessions(
            user_id=target_user_id,
            revoked_by_id=str(user.id),
            ip_address=request.META.get('REMOTE_ADDR'),
            user_type=user_type,
            db_alias=db_alias,
        )

        resp = Response({
            'message': 'All active sessions have been revoked successfully.',
            'revoked_count': count,
        }, status=status.HTTP_200_OK)

        if target_user_id == str(user.id):
            delete_refresh_cookie(resp)

        return resp


# ---------------------------------------------------------------------------
# View: MFAVerifyView
# ---------------------------------------------------------------------------

class MFAVerifyView(APIView):
    """
    POST /api/v1/auth/mfa/verify/

    Verifies a TOTP code against an active MFA challenge token.
    Issues real JWT access and refresh tokens upon successful verification.
    """
    permission_classes = [AllowAny]
    throttle_classes = [MFARateThrottle]

    def post(self, request):
        challenge_token = request.data.get('challenge_token')
        code = request.data.get('code')

        if not challenge_token or not code:
            return Response(
                {'error': 'challenge_token and code are required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        challenge_data = get_mfa_challenge(challenge_token)
        if not challenge_data:
            return Response(
                {'error': 'Invalid or expired MFA challenge token.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user_id = challenge_data.get('user_id')
        user_type = challenge_data.get('user_type')
        db_alias = challenge_data.get('db_alias')
        tenant_id = challenge_data.get('tenant_id')
        tenant_slug = challenge_data.get('tenant_slug')

        if user_type == 'platform':
            from apps.master.models_iam import PlatformUser
            try:
                user = PlatformUser.objects.using('default').get(id=user_id)
            except PlatformUser.DoesNotExist:
                return Response({'error': 'User not found.'}, status=status.HTTP_401_UNAUTHORIZED)

            if user.status != 'ACTIVE':
                return Response({'error': f'Account is {user.status}.'}, status=status.HTTP_403_FORBIDDEN)

            if not verify_totp_code(user.mfa_secret, code):
                return Response({'error': 'Invalid MFA verification code.'}, status=status.HTTP_400_BAD_REQUEST)

            # Consume challenge token on verified success
            consume_mfa_challenge(challenge_token)

            refresh = _build_platform_token(user)
            _update_last_login(user, request, db='default')
            return _build_login_response(
                refresh,
                user_type='platform',
                user_data={
                    'id': str(user.id),
                    'email': user.email,
                    'full_name': user.full_name,
                    'status': user.status,
                },
                request=request,
            )

        elif user_type == 'tenant':
            from apps.master.models_tenant import Tenant
            from apps.tenant_core.models_users import TenantUser
            from apps.tenant_core.audit import emit_audit_event

            try:
                tenant = Tenant.objects.using('default').get(id=tenant_id)
                user = TenantUser.objects.using(db_alias).get(id=user_id)
            except Exception:
                return Response({'error': 'User or organization not found.'}, status=status.HTTP_401_UNAUTHORIZED)

            if user.status != 'ACTIVE' or not user.is_login_allowed:
                return Response({'error': 'Account is not active or login is disabled.'}, status=status.HTTP_403_FORBIDDEN)

            if not verify_totp_code(user.mfa_secret, code):
                try:
                    emit_audit_event(
                        actor_id=str(user.id),
                        actor_email=user.email,
                        actor_roles=[],
                        action='SECURITY.MFA_FAILED',
                        target_type='TenantUser',
                        target_id=str(user.id),
                        db_alias=db_alias,
                        request=request,
                        metadata={'reason': 'Invalid TOTP code'},
                    )
                except Exception:
                    pass
                return Response({'error': 'Invalid MFA verification code.'}, status=status.HTTP_400_BAD_REQUEST)

            try:
                emit_audit_event(
                    actor_id=str(user.id),
                    actor_email=user.email,
                    actor_roles=[],
                    action='SECURITY.MFA_VERIFIED',
                    target_type='TenantUser',
                    target_id=str(user.id),
                    db_alias=db_alias,
                    request=request,
                )
            except Exception:
                pass

            # Consume challenge token on verified success
            consume_mfa_challenge(challenge_token)

            refresh = _build_tenant_token(user, tenant, db_alias)
            _update_last_login(user, request, db=db_alias)
            return _build_login_response(
                refresh,
                user_type='tenant',
                user_data={
                    'id': str(user.id),
                    'email': user.email,
                    'full_name': user.full_name,
                    'status': user.status,
                },
                extra={
                    'tenant': {
                        'id': str(tenant.id),
                        'slug': tenant.slug,
                        'name': tenant.name,
                    },
                },
                request=request,
            )


# ---------------------------------------------------------------------------
# View: MFAEnrollView
# ---------------------------------------------------------------------------

class MFAEnrollView(APIView):
    """
    POST /api/v1/auth/mfa/enroll/
    Generates a new TOTP secret and otpauth URI for enrolling in 2FA.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        import redis
        from django.conf import settings

        user = request.user
        secret = generate_totp_secret()
        uri = get_totp_uri(secret, user.email, issuer="PerformanceOS")

        try:
            r = redis.from_url(getattr(settings, 'REDIS_URL', 'redis://127.0.0.1:6379/0'))
            r.set(f"auth:mfa_pending:{user.id}", secret, ex=600)
        except Exception:
            pass

        return Response({
            'secret': secret,
            'otpauth_url': uri,
        }, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# View: MFAEnableView
# ---------------------------------------------------------------------------

class MFAEnableView(APIView):
    """
    POST /api/v1/auth/mfa/enable/
    Verifies code against pending or submitted secret and enables MFA on the account.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        import redis
        from django.conf import settings

        user = request.user
        code = request.data.get('code')
        secret = request.data.get('secret')

        if not code:
            return Response({'error': 'Verification code is required.'}, status=status.HTTP_400_BAD_REQUEST)

        if not secret:
            try:
                r = redis.from_url(getattr(settings, 'REDIS_URL', 'redis://127.0.0.1:6379/0'))
                cached_secret = r.get(f"auth:mfa_pending:{user.id}")
                if cached_secret:
                    secret = cached_secret.decode('utf-8')
            except Exception:
                pass

        if not secret:
            return Response({'error': 'MFA secret not found or expired. Please re-enroll.'}, status=status.HTTP_400_BAD_REQUEST)

        if not verify_totp_code(secret, code):
            return Response({'error': 'Invalid verification code.'}, status=status.HTTP_400_BAD_REQUEST)

        user.mfa_secret = secret
        user.is_mfa_enabled = True
        db = getattr(user, '_db_alias', 'default')
        user.save(using=db, update_fields=['mfa_secret', 'is_mfa_enabled'])

        if getattr(user, '_auth_type', None) == 'tenant':
            from apps.tenant_core.audit import emit_audit_event
            try:
                emit_audit_event(
                    actor_id=str(user.id),
                    actor_email=user.email,
                    actor_roles=getattr(user, '_roles', []),
                    action='SECURITY.MFA_ENABLED',
                    target_type='TenantUser',
                    target_id=str(user.id),
                    db_alias=db,
                    request=request,
                )
            except Exception:
                pass

        return Response({'message': 'MFA has been successfully enabled.'}, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# View: MFADisableView
# ---------------------------------------------------------------------------

class MFADisableView(APIView):
    """
    POST /api/v1/auth/mfa/disable/
    Disables MFA for the user after validating password.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        password = request.data.get('password')
        code = request.data.get('code')

        if not password:
            return Response({'error': 'Password is required to disable MFA.'}, status=status.HTTP_400_BAD_REQUEST)

        if not user.check_password(password):
            return Response({'error': 'Invalid password.'}, status=status.HTTP_400_BAD_REQUEST)

        if user.is_mfa_enabled and code:
            if not verify_totp_code(user.mfa_secret, code):
                return Response({'error': 'Invalid verification code.'}, status=status.HTTP_400_BAD_REQUEST)

        user.is_mfa_enabled = False
        user.mfa_secret = ''
        db = getattr(user, '_db_alias', 'default')
        user.save(using=db, update_fields=['mfa_secret', 'is_mfa_enabled'])

        if getattr(user, '_auth_type', None) == 'tenant':
            from apps.tenant_core.audit import emit_audit_event
            try:
                emit_audit_event(
                    actor_id=str(user.id),
                    actor_email=user.email,
                    actor_roles=getattr(user, '_roles', []),
                    action='SECURITY.MFA_DISABLED',
                    target_type='TenantUser',
                    target_id=str(user.id),
                    db_alias=db,
                    request=request,
                )
            except Exception:
                pass

        return Response({'message': 'MFA has been disabled.'}, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Shared Helper
# ---------------------------------------------------------------------------

def _update_last_login(user, request, db: str) -> None:
    """Update last_login_at and last_login_ip for any user type."""
    try:
        user.last_login_at = timezone.now()
        user.last_login_ip = request.META.get('REMOTE_ADDR')
        user.save(using=db, update_fields=['last_login_at', 'last_login_ip'])
    except Exception as e:
        # Non-critical — don't fail login because of a timestamp update error
        logger.warning('_update_last_login: Failed to update login timestamp: %s', e)
