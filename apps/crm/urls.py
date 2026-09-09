"""
URL routing for CRM app.
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import LeadViewSet

router = DefaultRouter()
router.register('leads', LeadViewSet, basename='crm-leads')

urlpatterns = [
    path('', include(router.urls)),
]
