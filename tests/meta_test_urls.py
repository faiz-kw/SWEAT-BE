from django.urls import include, path
from rest_framework.routers import DefaultRouter
from apps.tenant_core.views_meta_leads import MetaLeadMappingViewSet, MetaLeadImportViewSet

router = DefaultRouter()
router.register('meta-lead-mappings', MetaLeadMappingViewSet, basename='meta-lead-mapping')
router.register('meta-lead-imports', MetaLeadImportViewSet, basename='meta-lead-import')
urlpatterns = [path('', include(router.urls))]
