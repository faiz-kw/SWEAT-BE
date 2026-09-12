"""
URL routing for Members app.
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import MemberViewSet, MembershipPlanViewSet, ReferralLedgerViewSet

router = DefaultRouter()
router.register('plans', MembershipPlanViewSet, basename='membership-plans')
router.register('referrals', ReferralLedgerViewSet, basename='member-referrals')
router.register('', MemberViewSet, basename='members')


urlpatterns = [
    path('', include(router.urls)),
]
