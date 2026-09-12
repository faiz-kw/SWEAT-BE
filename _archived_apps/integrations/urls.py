"""
URL routing for Integration Layer adapters & webhooks.
"""

from django.urls import path
from .views import (
    FacebookLeadWebhookView,
    AccessControlScanWebhookView,
    IntegrationStatusView,
)

urlpatterns = [
    # Status endpoint: GET /api/v1/integrations/status/
    path('status/', IntegrationStatusView.as_view(), name='integrations-status'),

    # Inbound Webhooks
    path('webhooks/facebook-leads/', FacebookLeadWebhookView.as_view(), name='webhook-facebook-leads'),
    path('webhooks/access-control/', AccessControlScanWebhookView.as_view(), name='webhook-access-control'),
]
