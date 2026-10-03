from django.urls import include, path
from rest_framework.routers import DefaultRouter
from apps.tenant_core.views_meta_leads import MetaLeadMappingViewSet, MetaLeadImportViewSet
from apps.tenant_core.views_meta_webhook import MetaLeadWebhookView

router = DefaultRouter()
router.register('meta-lead-mappings', MetaLeadMappingViewSet, basename='meta-lead-mapping')
router.register('meta-lead-imports', MetaLeadImportViewSet, basename='meta-lead-import')

urlpatterns = [
    path('api/v1/webhooks/meta/leads/', MetaLeadWebhookView.as_view(), name='meta-lead-webhook-global'),
    path('api/v1/webhooks/meta/leads/<str:tenant_public_id>/', MetaLeadWebhookView.as_view(), name='meta-lead-webhook-tenant'),
    path('', include(router.urls)),
]
