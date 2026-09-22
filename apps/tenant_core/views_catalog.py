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
    ProgramCategory, ProgramType, Program, Package, PackageVersion,
    PackagePrice, PackageBranchAvailability, PackageEntitlementDefinition,
)
from .serializers_catalog import (
    TermsDocumentSerializer, TermsDocumentVersionSerializer, TermsAcceptanceSerializer,
    ProgramCategorySerializer, ProgramTypeSerializer, ProgramSerializer, PackageSerializer, PackageVersionSerializer,
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

class ProgramTypeViewSet(viewsets.ModelViewSet):
    serializer_class = ProgramTypeSerializer
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
        qs = ProgramType.objects.using(alias).filter(organization=org)
        status_param = self.request.query_params.get('status')
        if status_param:
            qs = qs.filter(status=status_param)
        return qs.order_by('display_order', 'name')

    def perform_create(self, serializer):
        org = _get_org(self.request)
        serializer.save(organization=org)


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

    def get_permissions(self):
        # Reading programs catalog is allowed for all active staff (for lead intake, bookings, sales)
        if self.action in ['list', 'retrieve']:
            return [RequireActiveTenantAndOrg()]
        return super().get_permissions()

    def get_queryset(self):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        qs = Program.objects.using(alias).filter(organization=org)
        cat_id = self.request.query_params.get('category_id')
        status_param = self.request.query_params.get('status')
        branch_id = self.request.query_params.get('branch_id')

        if cat_id:
            qs = qs.filter(category_id=cat_id)
        if status_param:
            qs = qs.filter(status=status_param)
        if branch_id:
            # Check programs associated with packages available at this branch
            from .models_catalog import PackageBranchAvailability
            branch_avail = PackageBranchAvailability.objects.using(alias).filter(branch_id=branch_id)
            if branch_avail.exists():
                branch_prog_ids = branch_avail.filter(status='ENABLED').values_list('package__program_id', flat=True)
                qs = qs.filter(id__in=branch_prog_ids)

        return qs.order_by('name')

    def perform_create(self, serializer):
        org = _get_org(self.request)
        serializer.save(organization=org)

    def perform_update(self, serializer):
        alias = _get_db(self.request)
        instance = serializer.save()
        PackageCatalogService.update_program(instance, actor=self.request.user, db_alias=alias)


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
        return qs.prefetch_related('versions__prices', 'versions__entitlement_definitions', 'branch_availabilities__branch').order_by('name')

    def perform_create(self, serializer):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        prog = serializer.validated_data.get('program')
        if not prog:
            prog = Program.objects.using(alias).filter(organization=org).first()
            if not prog:
                prog = PackageCatalogService.create_program(
                    organization=org,
                    code='DEFAULT',
                    name='Default Program',
                    status='ACTIVE',
                    db_alias=alias,
                )
            serializer.save(organization=org, program=prog)
        else:
            serializer.save(organization=org)

    def perform_update(self, serializer):
        alias = _get_db(self.request)
        instance = serializer.save()
        PackageCatalogService.update_package(instance, actor=self.request.user, db_alias=alias)

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
                total_days=int(request.data.get('total_days')) if request.data.get('total_days') else None,
                description_snapshot=request.data.get('description_snapshot'),
                validity_days=request.data.get('validity_days'),
                is_trial_package=request.data.get('is_trial_package', request.data.get('is_trial', False)),
                is_trial=request.data.get('is_trial', request.data.get('is_trial_package', False)),
                only_for_trial=request.data.get('only_for_trial', False),
                show_on_web=request.data.get('show_on_web', True),
                show_on_app=request.data.get('show_on_app', True),
                status=request.data.get('status', 'DRAFT'),
                db_alias=alias,
            )
            # Optional initial pricing
            sale_price = request.data.get('sale_price') or request.data.get('base_price')
            if sale_price is not None:
                PackageCatalogService.create_package_price(
                    package_version=ver,
                    currency=request.data.get('currency', 'INR'),
                    base_price=sale_price,
                    display_price=request.data.get('display_price'),
                    prices_include_tax=request.data.get('prices_include_tax', True),
                    tax_percent=request.data.get('tax_percentage', request.data.get('tax_percent', 0)),
                    branch_id=request.data.get('branch_id'),
                    actor=request.user,
                    db_alias=alias,
                )

            # Optional passport / session entitlements (Step 9 mapping)
            max_sessions = request.data.get('max_sessions') or request.data.get('allocated_units')
            if max_sessions is not None:
                PackageCatalogService.create_entitlement_definition(
                    package_version=ver,
                    entitlement_type='HOME_BRANCH_SESSION',
                    allocated_units=Decimal(str(max_sessions)),
                    actor=request.user,
                    db_alias=alias,
                )
            passport_sessions = request.data.get('passport_sessions')
            passport_cost = request.data.get('passport_cost') or request.data.get('passport_extra_cost')
            if passport_sessions is not None or passport_cost is not None:
                PackageCatalogService.create_entitlement_definition(
                    package_version=ver,
                    entitlement_type='CROSS_BRANCH_SESSION',
                    allocated_units=Decimal(str(passport_sessions or 0)),
                    extra_unit_price=Decimal(str(passport_cost)) if passport_cost is not None else None,
                    actor=request.user,
                    db_alias=alias,
                )

            if request.data.get('publish_immediately', False):
                ver = PackageCatalogService.publish_package_version(
                    package_version_id=ver.id,
                    actor=request.user,
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

    def create(self, request, *args, **kwargs):
        alias = _get_db(request)
        ver_id = request.data.get('package_version')
        if ver_id:
            ver = PackageVersion.objects.using(alias).filter(id=ver_id).first()
            if ver and ver.status in ('ACTIVE', 'RETIRED'):
                return Response(
                    {'error': f"Package version {ver.version_number} is {ver.status} and immutable. Create a new package version to modify pricing."},
                    status=status.HTTP_400_BAD_REQUEST
                )
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.package_version.status in ('ACTIVE', 'RETIRED'):
            return Response(
                {'error': f"Package version {instance.package_version.version_number} is {instance.package_version.status} and immutable. Create a new package version to modify pricing."},
                status=status.HTTP_400_BAD_REQUEST
            )
        return super().update(request, *args, **kwargs)

    def perform_create(self, serializer):
        from .services_reliability import record_business_audit
        alias = _get_db(self.request)
        instance = serializer.save(created_by_user=self.request.user)
        record_business_audit(
            organization=instance.package_version.package.organization,
            module='core',
            action_code='PACKAGE_PRICE_CREATED',
            entity_type='PackagePrice',
            entity_id=instance.id,
            actor_user=self.request.user,
            event_description=f"Created price for {instance.package_version}: {instance.currency} {instance.base_price}",
            after_data={'base_price': str(instance.base_price), 'currency': instance.currency, 'status': instance.status},
            db_alias=alias,
        )

    def perform_update(self, serializer):
        from .services_reliability import record_business_audit
        alias = _get_db(self.request)
        instance = serializer.save()
        record_business_audit(
            organization=instance.package_version.package.organization,
            module='core',
            action_code='PACKAGE_PRICE_UPDATED',
            entity_type='PackagePrice',
            entity_id=instance.id,
            actor_user=self.request.user,
            event_description=f"Updated price for {instance.package_version}: {instance.currency} {instance.base_price}",
            after_data={'base_price': str(instance.base_price), 'currency': instance.currency, 'status': instance.status},
            db_alias=alias,
        )


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

    def create(self, request, *args, **kwargs):
        alias = _get_db(request)
        ver_id = request.data.get('package_version')
        if ver_id:
            ver = PackageVersion.objects.using(alias).filter(id=ver_id).first()
            if ver and ver.status in ('ACTIVE', 'RETIRED'):
                return Response(
                    {'error': f"Package version {ver.version_number} is {ver.status} and immutable. Create a new package version to modify entitlements."},
                    status=status.HTTP_400_BAD_REQUEST
                )
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.package_version.status in ('ACTIVE', 'RETIRED'):
            return Response(
                {'error': f"Package version {instance.package_version.version_number} is {instance.package_version.status} and immutable. Create a new package version to modify entitlements."},
                status=status.HTTP_400_BAD_REQUEST
            )
        return super().update(request, *args, **kwargs)

    def perform_create(self, serializer):
        from .services_reliability import record_business_audit
        alias = _get_db(self.request)
        instance = serializer.save()
        record_business_audit(
            organization=instance.package_version.package.organization,
            module='core',
            action_code='PACKAGE_ENTITLEMENT_CREATED',
            entity_type='PackageEntitlementDefinition',
            entity_id=instance.id,
            actor_user=self.request.user,
            event_description=f"Created entitlement {instance.entitlement_type} for {instance.package_version}",
            after_data={'entitlement_type': instance.entitlement_type, 'allocated_units': str(instance.allocated_units) if instance.allocated_units else 'UNLIMITED'},
            db_alias=alias,
        )

    def perform_update(self, serializer):
        from .services_reliability import record_business_audit
        alias = _get_db(self.request)
        instance = serializer.save()
        record_business_audit(
            organization=instance.package_version.package.organization,
            module='core',
            action_code='PACKAGE_ENTITLEMENT_UPDATED',
            entity_type='PackageEntitlementDefinition',
            entity_id=instance.id,
            actor_user=self.request.user,
            event_description=f"Updated entitlement {instance.entitlement_type} for {instance.package_version}",
            after_data={'entitlement_type': instance.entitlement_type, 'allocated_units': str(instance.allocated_units) if instance.allocated_units else 'UNLIMITED'},
            db_alias=alias,
        )
