"""
URLs for User Management & RBAC Permissions.
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    UserViewSet, RoleDefinitionViewSet, PermissionDefinitionViewSet, UserInviteView
)

router = DefaultRouter()
router.register(r'users', UserViewSet, basename='user')
router.register(r'roles', RoleDefinitionViewSet, basename='role')
router.register(r'permissions', PermissionDefinitionViewSet, basename='permission')

urlpatterns = [
    path('invite/', UserInviteView.as_view(), name='user-invite'),
    path('', include(router.urls)),
]
