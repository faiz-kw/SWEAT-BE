from django.urls import path
from .views import (
    UniversalLoginView, PlatformLoginView, TenantLoginView,
    TokenRefreshView, MeView, LogoutView
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
]
