"""
URL routing for Authentication endpoints.
"""

from django.urls import path
from .views import (
    CookieTokenObtainPairView,
    CookieTokenRefreshView,
    CookieTokenLogoutView,
    MeView,
    ChangePasswordView,
)

urlpatterns = [
    # POST /api/v1/auth/login/
    path('login/', CookieTokenObtainPairView.as_view(), name='auth-login'),

    # POST /api/v1/auth/token/refresh/
    path('token/refresh/', CookieTokenRefreshView.as_view(), name='auth-refresh'),

    # POST /api/v1/auth/logout/
    path('logout/', CookieTokenLogoutView.as_view(), name='auth-logout'),

    # GET /api/v1/auth/me/
    path('me/', MeView.as_view(), name='auth-me'),

    # POST /api/v1/auth/change-password/
    path('change-password/', ChangePasswordView.as_view(), name='auth-change-password'),
]
