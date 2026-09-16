"""
DRF ViewSets for Layer 2: Module C (Terms / Acceptance) & Module D (Programs / Packages / Catalog)
"""

import logging
from decimal import Decimal
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.core.exceptions import ValidationError

from .context import get_tenant_db_alias
from .models_catalog import (
    TermsDocument, TermsDocumentVersion, TermsAcceptance,
    ProgramCategory, Program, Package, PackageVersion,
    PackagePrice, PackageBranchAvailability, PackageEntitlementDefinition,
)
from .serializers_catalog import (
    TermsDocumentSerializer, TermsDocumentVersionSerializer, TermsAcceptanceSerializer,
    ProgramCategorySerializer, ProgramSerializer, PackageSerializer, PackageVersionSerializer,
    PackagePriceSerializer, PackageBranchAvailabilitySerializer, PackageEntitlementDefinitionSerializer,
)
from .services_catalog import PackageCatalogService, TermsLegalService
from .permissions import RequireActiveTenantAndOrg, TenantRBACPermission

logger = logging.getLogger(__name__)


def _get_db(request):
    return (
        get_tenant_db_alias()
        or getattr(getattr(request, 'user', None), '_db_alias', None)
        or getattr(request, '_tenant_db_alias', None)
        or 'default'
    )


def _get_org(request):
    org = getattr(request, 'organization', None)
    if not org:
        user = getattr(request, 'user', None)
        if user and hasattr(user, 'organization') and user.organization:
            return user.organization
        alias = _get_db(request)
        from .models_org import Organization
        if user and getattr(user, 'organization_id', None):
            return Organization.objects.using(alias).filter(id=user.organization_id).first()
        return Organization.objects.using(alias).filter(status='ACTIVE').first()
    return org


# ============================================================================
# MODULE C: TERMS VIEWSETS
# ============================================================================

class TermsDocumentViewSet(viewsets.ModelViewSet):
    serializer_class = TermsDocumentSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'
    permission_action_map = {
        'create': 'core.settings.edit',
        'update': 'core.settings.edit',
        'partial_update': 'core.settings.edit',
        'destroy': 'core.settings.edit',
        'publish_version': 'core.settings.edit',
    }

    def get_queryset(self):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        qs = TermsDocument.objects.using(alias).filter(organization=org)
        status_param = self.request.query_params.get('status')
        if status_param:
            qs = qs.filter(status=status_param)
        return qs.order_by('name')

    def perform_create(self, serializer):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        serializer.save(organization=org)

    @action(detail=True, methods=['post'], url_path='publish-version')
    def publish_version(self, request, pk=None):
        alias = _get_db(request)
        doc = self.get_object()
        version_id = request.data.get('terms_document_version_id')
        if not version_id:
            return Response({'error': 'terms_document_version_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            published = TermsLegalService.publish_terms_version(
                terms_document_version_id=version_id,
                actor=request.user,
                db_alias=alias,
            )
            return Response(TermsDocumentVersionSerializer(published).data, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


class TermsDocumentVersionViewSet(viewsets.ModelViewSet):
    serializer_class = TermsDocumentVersionSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'
    permission_action_map = {
        'create': 'core.settings.edit',
        'update': 'core.settings.edit',
        'partial_update': 'core.settings.edit',
        'destroy': 'core.settings.edit',
    }

    def get_queryset(self):
        alias = _get_db(self.request)
        qs = TermsDocumentVersion.objects.using(alias).all()
        doc_id = self.request.query_params.get('terms_document_id')
        if doc_id:
            qs = qs.filter(terms_document_id=doc_id)
        return qs.order_by('-version_number')

    def perform_create(self, serializer):
        alias = _get_db(self.request)
        serializer.save(created_by_user=self.request.user)


class TermsAcceptanceViewSet(viewsets.ModelViewSet):
    serializer_class = TermsAcceptanceSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'
    permission_action_map = {
        'create': 'core.settings.edit',
    }

    def get_queryset(self):
        alias = _get_db(self.request)
        qs = TermsAcceptance.objects.using(alias).all()
        user_profile_id = self.request.query_params.get('user_profile_id')
        lead_id = self.request.query_params.get('lead_id')
        if user_profile_id:
            qs = qs.filter(user_profile_id=user_profile_id)
        if lead_id:
            qs = qs.filter(lead_id=lead_id)
        return qs.order_by('-accepted_at')

    def create(self, request, *args, **kwargs):
        alias = _get_db(request)
        try:
            acceptance = TermsLegalService.record_terms_acceptance(
                terms_document_version_id=request.data.get('terms_document_version_id'),
                accepted_via=request.data.get('accepted_via', 'WEB'),
                lead_id=request.data.get('lead_id'),
                user_profile_id=request.data.get('user_profile_id'),
                order_id=request.data.get('order_id'),
                ip_address=request.META.get('REMOTE_ADDR'),
                device_metadata=request.data.get('device_metadata'),
                db_alias=alias,
            )
            return Response(TermsAcceptanceSerializer(acceptance).data, status=status.HTTP_201_CREATED)
        except ValidationError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


# ============================================================================
# MODULE D: CATALOG VIEWSETS
# ============================================================================

class ProgramCategoryViewSet(viewsets.ModelViewSet):
    serializer_class = ProgramCategorySerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'
    permission_action_map = {
        'create': 'core.settings.edit',
        'update': 'core.settings.edit',
        'partial_update': 'core.settings.edit',
        'destroy': 'core.settings.edit',
    }

    def get_queryset(self):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        return ProgramCategory.objects.using(alias).filter(organization=org).order_by('display_order', 'name')

    def perform_create(self, serializer):
        org = _get_org(self.request)
        serializer.save(organization=org)


class ProgramViewSet(viewsets.ModelViewSet):
    serializer_class = ProgramSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'
    permission_action_map = {
        'create': 'core.settings.edit',
        'update': 'core.settings.edit',
        'partial_update': 'core.settings.edit',
        'destroy': 'core.settings.edit',
    }

    def get_queryset(self):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        qs = Program.objects.using(alias).filter(organization=org)
        cat_id = self.request.query_params.get('category_id')
        status_param = self.request.query_params.get('status')
        if cat_id:
            qs = qs.filter(category_id=cat_id)
        if status_param:
            qs = qs.filter(status=status_param)
        return qs.order_by('name')

    def perform_create(self, serializer):
        org = _get_org(self.request)
        serializer.save(organization=org)


class PackageViewSet(viewsets.ModelViewSet):
    serializer_class = PackageSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'
    permission_action_map = {
        'create': 'core.settings.edit',
        'update': 'core.settings.edit',
        'partial_update': 'core.settings.edit',
        'destroy': 'core.settings.edit',
        'create_version': 'core.settings.edit',
        'publish_version': 'core.settings.edit',
        'clone_modify_version': 'core.settings.edit',
    }

    def get_queryset(self):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        qs = Package.objects.using(alias).filter(organization=org)
        program_id = self.request.query_params.get('program_id')
        status_param = self.request.query_params.get('status')
        if program_id:
            qs = qs.filter(program_id=program_id)
        if status_param:
            qs = qs.filter(status=status_param)
        return qs.prefetch_related('versions__prices', 'branch_availabilities__branch').order_by('name')

    def perform_create(self, serializer):
        org = _get_org(self.request)
        serializer.save(organization=org)

    @action(detail=True, methods=['post'], url_path='create-version')
    def create_version(self, request, pk=None):
        alias = _get_db(request)
        package = self.get_object()
        try:
            ver = PackageCatalogService.create_package_version(
                package=package,
                name_snapshot=request.data.get('name_snapshot', package.name),
                duration_value=int(request.data.get('duration_value', 1)),
                duration_unit=request.data.get('duration_unit', 'MONTH'),
                created_by_user=request.user,
                description_snapshot=request.data.get('description_snapshot'),
                validity_days=request.data.get('validity_days'),
                is_trial_package=request.data.get('is_trial_package', False),
                status=request.data.get('status', 'DRAFT'),
                db_alias=alias,
            )
            return Response(PackageVersionSerializer(ver).data, status=status.HTTP_201_CREATED)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='publish-version')
    def publish_version(self, request, pk=None):
        alias = _get_db(request)
        version_id = request.data.get('package_version_id')
        if not version_id:
            return Response({'error': 'package_version_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            published = PackageCatalogService.publish_package_version(
                package_version_id=version_id,
                actor=request.user,
                db_alias=alias,
            )
            return Response(PackageVersionSerializer(published).data, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='clone-modify-version')
    def clone_modify_version(self, request, pk=None):
        alias = _get_db(request)
        version_id = request.data.get('package_version_id')
        if not version_id:
            return Response({'error': 'package_version_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        modifications = request.data.get('modifications', {})
        try:
            new_version = PackageCatalogService.modify_package_version_safely(
                package_version_id=version_id,
                actor=request.user,
                modifications=modifications,
                db_alias=alias,
            )
            return Response(PackageVersionSerializer(new_version).data, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


class PackageVersionViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = PackageVersionSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'

    def get_queryset(self):
        alias = _get_db(self.request)
        qs = PackageVersion.objects.using(alias).all()
        pkg_id = self.request.query_params.get('package_id')
        if pkg_id:
            qs = qs.filter(package_id=pkg_id)
        return qs.prefetch_related('prices', 'entitlement_definitions').order_by('-version_number')


class PackagePriceViewSet(viewsets.ModelViewSet):
    serializer_class = PackagePriceSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'
    permission_action_map = {
        'create': 'core.settings.edit',
        'update': 'core.settings.edit',
        'partial_update': 'core.settings.edit',
        'destroy': 'core.settings.edit',
    }

    def get_queryset(self):
        alias = _get_db(self.request)
        qs = PackagePrice.objects.using(alias).all()
        ver_id = self.request.query_params.get('package_version_id')
        if ver_id:
            qs = qs.filter(package_version_id=ver_id)
        return qs.order_by('-effective_from')

    def perform_create(self, serializer):
        serializer.save(created_by_user=self.request.user)


class PackageBranchAvailabilityViewSet(viewsets.ModelViewSet):
    serializer_class = PackageBranchAvailabilitySerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'
    permission_action_map = {
        'create': 'core.settings.edit',
        'update': 'core.settings.edit',
        'partial_update': 'core.settings.edit',
        'destroy': 'core.settings.edit',
    }

    def get_queryset(self):
        alias = _get_db(self.request)
        qs = PackageBranchAvailability.objects.using(alias).all()
        pkg_id = self.request.query_params.get('package_id')
        branch_id = self.request.query_params.get('branch_id')
        if pkg_id:
            qs = qs.filter(package_id=pkg_id)
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        return qs.select_related('branch')


class PackageEntitlementDefinitionViewSet(viewsets.ModelViewSet):
    serializer_class = PackageEntitlementDefinitionSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'
    permission_action_map = {
        'create': 'core.settings.edit',
        'update': 'core.settings.edit',
        'partial_update': 'core.settings.edit',
        'destroy': 'core.settings.edit',
    }

    def get_queryset(self):
        alias = _get_db(self.request)
        qs = PackageEntitlementDefinition.objects.using(alias).all()
        ver_id = self.request.query_params.get('package_version_id')
        if ver_id:
            qs = qs.filter(package_version_id=ver_id)
        return qs.order_by('entitlement_type')
