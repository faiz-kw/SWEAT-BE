"""
Dedicated URL configuration for the ASGI Telephony Service.
Exposes only the SSE event streaming endpoint.
"""

from django.urls import re_path
from apps.tenant_core.views_telephony import TelephonyEventsAsgiSseView

urlpatterns = [
    re_path(r'^api/v1/tenant/crm/telephony/events/?$', TelephonyEventsAsgiSseView.as_view(), name='asgi-crm-telephony-events'),
    re_path(r'^telephony/events/?$', TelephonyEventsAsgiSseView.as_view(), name='asgi-telephony-events-short'),
]
