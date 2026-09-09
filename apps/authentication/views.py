"""
Authentication Views handling Login, Refresh (RTR), Logout, Current User profile,
and Change Password.
HttpOnly cookies are used for refresh tokens to protect against XSS attacks.
"""

from django.conf import settings
from rest_framework import status, permissions, serializers
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from drf_spectacular.utils import extend_schema, OpenApiResponse

from .serializers import (
    CustomTokenObtainPairSerializer,
    LoginRequestSerializer,
    TokenRefreshResponseSerializer,
)
from apps.users.serializers import UserSerializer

def set_refresh_cookie(response: Response, refresh_token: str):
    """
    Sets the refresh token inside a secure, HttpOnly cookie.
    """
    cookie_max_age = int(settings.SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'].total_seconds())
    is_secure = not settings.DEBUG  # False in dev, True in production (HTTPS)

    response.set_cookie(
        key='refresh',
        value=refresh_token,
        max_age=cookie_max_age,
        httponly=True,       # Prevents client-side JS from accessing the token
        secure=is_secure,    # Only send over HTTPS in production
        samesite='Lax',      # CSRF protection while allowing top-level navigation
        path='/',            # Accessible across all API endpoints
    )


class CookieTokenObtainPairView(TokenObtainPairView):
    """
    Login endpoint: Validates email + password, returns 15-minute access token,
    and attaches the refresh token as an HttpOnly cookie.
    """
    serializer_class = CustomTokenObtainPairSerializer
    permission_classes = [permissions.AllowAny]

    @extend_schema(
        summary="User Login",
        description="Authenticates user and returns 15-minute JWT. Sets HttpOnly refresh token cookie.",
        request=LoginRequestSerializer,
        responses={
            200: OpenApiResponse(description="Login successful, returns access token"),
            401: OpenApiResponse(description="Invalid credentials"),
        }
    )
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except TokenError as e:
            raise InvalidToken(e.args[0])

        tokens = serializer.validated_data
        refresh_token = tokens.pop('refresh', None)

        response = Response(tokens, status=status.HTTP_200_OK)

        if refresh_token:
            set_refresh_cookie(response, refresh_token)

        return response


class CookieTokenRefreshView(TokenRefreshView):
    """
    Token refresh endpoint (RTR): Reads the HttpOnly refresh cookie,
    issues a fresh 15-minute access token, and rotates the refresh cookie.
    """
    permission_classes = [permissions.AllowAny]

    @extend_schema(
        summary="Token Refresh (RTR)",
        description="Reads HttpOnly refresh cookie, validates it, rotates it, and returns new 15-minute access token.",
        responses={
            200: TokenRefreshResponseSerializer,
            401: OpenApiResponse(description="Refresh token expired or invalid"),
        }
    )
    def post(self, request, *args, **kwargs):
        # 1. Read refresh token from HttpOnly cookie, fallback to request body if sent
        refresh_token = request.COOKIES.get('refresh') or request.data.get('refresh')

        if not refresh_token:
            return Response(
                {"detail": "Refresh token cookie not found. Please log in again."},
                status=status.HTTP_401_UNAUTHORIZED
            )

        data = {'refresh': refresh_token}
        serializer = self.get_serializer(data=data)

        try:
            serializer.is_valid(raise_exception=True)
        except TokenError as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_401_UNAUTHORIZED
            )

        tokens = serializer.validated_data
        new_refresh = tokens.pop('refresh', None)

        response = Response(tokens, status=status.HTTP_200_OK)

        # If refresh token rotation (RTR) is enabled, set the new rotated cookie
        if new_refresh:
            set_refresh_cookie(response, new_refresh)

        return response


class LogoutResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()


class CookieTokenLogoutView(APIView):
    """
    Logout endpoint: Blacklists the refresh token and clears the HttpOnly cookie.
    """
    permission_classes = [permissions.AllowAny]

    @extend_schema(
        summary="User Logout",
        description="Blacklists the current refresh token and clears the HttpOnly cookie.",
        responses={
            200: LogoutResponseSerializer,
        }
    )
    def post(self, request, *args, **kwargs):

        refresh_token = request.COOKIES.get('refresh') or request.data.get('refresh')

        if refresh_token:
            try:
                token = RefreshToken(refresh_token)
                token.blacklist()
            except (TokenError, Exception):
                # Token might already be expired or blacklisted, continue to clear cookie
                pass

        response = Response({"detail": "Successfully logged out."}, status=status.HTTP_200_OK)
        response.delete_cookie(key='refresh', path='/api/v1/auth/')
        return response


class MeView(APIView):
    """
    Returns the authenticated user's profile, tenant permissions, and enabled modules.
    The enabled_modules list drives the sidebar navigation filtering on the frontend.
    """
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="Current User Profile",
        description="Returns details of the currently authenticated user, including tenant enabled_modules.",
        responses={200: UserSerializer}
    )
    def get(self, request):
        user = request.user
        serializer = UserSerializer(user)
        data = serializer.data

        # Attach tenant's enabled_modules so the frontend can enforce
        # module/submodule access in the sidebar and route guards.
        # Super admins (no tenant) get None = unrestricted.
        if user.tenant_id:
            from apps.tenants.models import Tenant, Location, TenantBranding
            from apps.tenants.serializers import TenantBrandingSerializer
            from apps.users.serializers import LocationBriefSerializer
            tenant = Tenant.objects.filter(id=user.tenant_id).first()
            if tenant:
                data['enabled_modules'] = tenant.enabled_modules if tenant.enabled_modules is not None else []
                data['tenant_name'] = tenant.name

                # Attach tenant's white-label branding
                branding = TenantBranding.objects.filter(tenant=tenant).first()
                if branding:
                    data['branding'] = TenantBrandingSerializer(branding).data
                else:
                    data['branding'] = {
                        'app_name': tenant.name,
                        'primary_color': '#0f766e',
                        'accent_color': '#f59e0b',
                        'logo_url': '',
                        'favicon_url': '',
                        'custom_domain': '',
                        'email_footer': ''
                    }

                # Ensure tenant admins and staff always get the latest studio branches
                if user.role in ['Admin', 'Super Admin'] or not user.allowed_locations.exists():
                    tenant_locs = Location.objects.filter(tenant=tenant, is_active=True)
                    data['allowed_locations'] = LocationBriefSerializer(tenant_locs, many=True).data
                    data['allowed_locations_list'] = data['allowed_locations']
            else:
                data['enabled_modules'] = []
                data['branding'] = None
        else:
            # Super admin — null means unrestricted access to everything
            data['enabled_modules'] = None
            data['branding'] = None

        return Response(data, status=status.HTTP_200_OK)


class ChangePasswordView(APIView):
    """
    Change the authenticated user's password.
    Requires current password verification before setting a new one.

    POST /api/v1/auth/change-password/
    Body: { current_password, new_password, confirm_password }
    """
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="Change Password",
        description="Validates current password then sets a new password for the authenticated user.",
        responses={
            200: OpenApiResponse(description="Password changed successfully"),
            400: OpenApiResponse(description="Validation error (wrong current password or mismatch)"),
        }
    )
    def post(self, request, *args, **kwargs):
        user = request.user
        current_password = request.data.get('current_password', '')
        new_password = request.data.get('new_password', '')
        confirm_password = request.data.get('confirm_password', '')

        # Validate current password
        if not user.check_password(current_password):
            return Response(
                {'detail': 'Current password is incorrect.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Validate new password fields
        if not new_password:
            return Response(
                {'detail': 'New password cannot be empty.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if new_password != confirm_password:
            return Response(
                {'detail': 'New passwords do not match.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if len(new_password) < 3:
            return Response(
                {'detail': 'Password must be at least 3 characters.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Set the new password
        user.set_password(new_password)
        user.save(update_fields=['password'])

        return Response(
            {'detail': 'Password changed successfully.'},
            status=status.HTTP_200_OK
        )
