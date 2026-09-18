from django.urls import path
from .views import (
    UniversalLoginView, PlatformLoginView, TenantLoginView,
    TokenRefreshView, MeView, LogoutView,
    SessionRevocationView, MFAVerifyView, MFAEnrollView,
    MFAEnableView, MFADisableView,
    PasswordResetRequestView, PasswordResetConfirmView,
    PublicBrandingView,
)

app_name = 'authentication'

urlpatterns = [
    # Universal login (frontend default)
    path('login/', UniversalLoginView.as_view(), name='universal-login'),
    # Specific logins
    path('platform/login/', PlatformLoginView.as_view(), name='platform-login'),
    path('tenant/login/', TenantLoginView.as_view(), name='tenant-login'),
    # Token refresh
    path('token/refresh/', TokenRefreshView.as_view(), name='token-refresh'),
    path('refresh/', TokenRefreshView.as_view(), name='refresh'),
    # Session / Me profile
    path('me/', MeView.as_view(), name='auth-me'),
    path('logout/', LogoutView.as_view(), name='logout'),
    # Session Revocation
    path('sessions/revoke-all/', SessionRevocationView.as_view(), name='sessions-revoke-all'),
    path('revoke-sessions/', SessionRevocationView.as_view(), name='revoke-sessions'),
    # MFA Authentication & Enrollment
    path('mfa/verify/', MFAVerifyView.as_view(), name='mfa-verify'),
    path('mfa/enroll/', MFAEnrollView.as_view(), name='mfa-enroll'),
    path('mfa/enable/', MFAEnableView.as_view(), name='mfa-enable'),
    path('mfa/disable/', MFADisableView.as_view(), name='mfa-disable'),
    # Password Reset
    path('password/reset-request/', PasswordResetRequestView.as_view(), name='password-reset-request'),
    path('password/reset-confirm/', PasswordResetConfirmView.as_view(), name='password-reset-confirm'),
    # Public branding — no auth required, used by login page
    path('branding/', PublicBrandingView.as_view(), name='public-branding'),
]
