"""
URL routing for Operations & Scheduling app.
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import TrainerViewSet, FitnessClassViewSet, BookingViewSet, CalendarFeedView

router = DefaultRouter()
router.register('trainers', TrainerViewSet, basename='ops-trainers')
router.register('classes', FitnessClassViewSet, basename='ops-classes')
router.register('bookings', BookingViewSet, basename='ops-bookings')

urlpatterns = [
    # Master Calendar feed: GET /api/v1/ops/calendar/
    path('calendar/', CalendarFeedView.as_view(), name='ops-calendar-feed'),

    # ViewSet CRUD endpoints
    path('', include(router.urls)),
]
