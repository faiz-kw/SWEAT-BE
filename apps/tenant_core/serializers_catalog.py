"""
DRF Serializers for Layer 2: Module C (Terms) & Module D (Programs / Packages / Catalog)
"""

from rest_framework import serializers
from .models_catalog import (
    TermsDocument, TermsDocumentVersion, TermsAcceptance,
    ProgramCategory, ProgramType, Program, Package, PackageVersion,
    PackagePrice, PackageBranchAvailability, PackageEntitlementDefinition,
)


# ============================================================================
# MODULE C: TERMS SERIALIZERS
# ============================================================================

class TermsDocumentVersionSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source='created_by_user.full_name', read_only=True)
    terms_document_code = serializers.CharField(source='terms_document.code', read_only=True)
    terms_document_name = serializers.CharField(source='terms_document.name', read_only=True)

    class Meta:
        model = TermsDocumentVersion
        fields = [
            'id', 'terms_document', 'terms_document_code', 'terms_document_name',
            'version_number', 'content_text', 'file', 'effective_from',
            'effective_until', 'status', 'created_by_user', 'created_by_name',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'version_number', 'created_by_user', 'created_at', 'updated_at']


class TermsDocumentSerializer(serializers.ModelSerializer):
    active_version = serializers.SerializerMethodField()
    versions_count = serializers.SerializerMethodField()

    class Meta:
        model = TermsDocument
        fields = [
            'id', 'organization', 'code', 'name', 'document_type',
            'status', 'active_version', 'versions_count', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at']

    def get_active_version(self, obj):
        active_ver = obj.versions.filter(status='ACTIVE').order_by('-version_number').first()
        if active_ver:
            return {
                'id': str(active_ver.id),
                'version_number': active_ver.version_number,
                'effective_from': active_ver.effective_from.isoformat() if active_ver.effective_from else None,
            }
        return None

    def get_versions_count(self, obj):
        return obj.versions.count()


class TermsAcceptanceSerializer(serializers.ModelSerializer):
    document_code = serializers.CharField(source='terms_document_version.terms_document.code', read_only=True)
    version_number = serializers.IntegerField(source='terms_document_version.version_number', read_only=True)

    class Meta:
        model = TermsAcceptance
        fields = [
            'id', 'terms_document_version', 'document_code', 'version_number',
            'lead', 'user_profile', 'order_id', 'accepted_at',
            'accepted_via', 'ip_address', 'device_metadata', 'created_at',
        ]
        read_only_fields = ['id', 'accepted_at', 'created_at']


# ============================================================================
# MODULE D: CATALOG SERIALIZERS
# ============================================================================

class ProgramTypeSerializer(serializers.ModelSerializer):
    programs_count = serializers.SerializerMethodField()

    class Meta:
        model = ProgramType
        fields = [
            'id', 'organization', 'code', 'name', 'description',
            'display_order', 'status', 'programs_count', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at']

    def get_programs_count(self, obj):
        return obj.programs.count()


class ProgramCategorySerializer(serializers.ModelSerializer):
    programs_count = serializers.SerializerMethodField()

    class Meta:
        model = ProgramCategory
        fields = [
            'id', 'organization', 'code', 'name', 'description',
            'display_order', 'status', 'programs_count', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at']

    def get_programs_count(self, obj):
        return obj.programs.count()


class ProgramSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.name', read_only=True)
    program_type_name = serializers.CharField(source='program_type.name', read_only=True)
    program_type_code = serializers.CharField(source='program_type.code', read_only=True)
    packages_count = serializers.SerializerMethodField()

    class Meta:
        model = Program
        fields = [
            'id', 'organization', 'category', 'category_name',
            'code', 'name', 'description', 'program_type',
            'program_type_name', 'program_type_code',
            'trial_allowed', 'status', 'packages_count', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at']

    def get_packages_count(self, obj):
        return obj.packages.count()

    def to_internal_value(self, data):
        ret = super().to_internal_value(data)
        raw_pt = data.get('program_type')
        if raw_pt and isinstance(raw_pt, str):
            import uuid
            try:
                uuid.UUID(raw_pt)
            except ValueError:
                # String code e.g. 'MEMBERSHIP' or 'PILATES'
                req = self.context.get('request')
                org = getattr(req, 'organization', None) or (self.instance.organization if self.instance else None)
                if not org and req and hasattr(req, 'user'):
                    org = getattr(req.user, 'organization', None)
                if org:
                    from .models_catalog import ProgramType
                    alias = getattr(getattr(req, 'user', None), '_db_alias', None) or 'default'
                    pt, _ = ProgramType.objects.using(alias).get_or_create(
                        organization=org,
                        code=raw_pt.upper().strip(),
                        defaults={'name': raw_pt.replace('_', ' ').title(), 'status': 'ACTIVE'}
                    )
                    ret['program_type'] = pt
        return ret


class PackagePriceSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    sale_price = serializers.DecimalField(source='base_price', max_digits=14, decimal_places=2, read_only=True)
    tax_percentage = serializers.DecimalField(source='tax_percent', max_digits=6, decimal_places=3, read_only=True)
    total_price = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = PackagePrice
        fields = [
            'id', 'package_version', 'branch', 'branch_name',
            'currency', 'base_price', 'sale_price', 'display_price',
            'prices_include_tax', 'tax_percent', 'tax_percentage', 'total_price',
            'effective_from', 'effective_until', 'status',
            'created_by_user', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_by_user', 'sale_price', 'tax_percentage', 'total_price', 'created_at', 'updated_at']


class PackageBranchAvailabilitySerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    branch_code = serializers.CharField(source='branch.code', read_only=True)

    class Meta:
        model = PackageBranchAvailability
        fields = [
            'id', 'package', 'branch', 'branch_name', 'branch_code',
            'status', 'available_from', 'available_until', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class PackageEntitlementDefinitionSerializer(serializers.ModelSerializer):
    class Meta:
        model = PackageEntitlementDefinition
        fields = [
            'id', 'package_version', 'entitlement_type', 'allocated_units',
            'is_unlimited', 'extra_unit_price', 'validity_days', 'reference_type', 'reference_id',
            'configuration', 'status', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class PackageVersionSerializer(serializers.ModelSerializer):
    prices = PackagePriceSerializer(many=True, read_only=True)
    entitlement_definitions = PackageEntitlementDefinitionSerializer(many=True, read_only=True)
    package_code = serializers.CharField(source='package.code', read_only=True)
    package_name = serializers.CharField(source='package.name', read_only=True)

    class Meta:
        model = PackageVersion
        fields = [
            'id', 'package', 'package_code', 'package_name', 'version_number',
            'name_snapshot', 'description_snapshot', 'duration_value', 'duration_unit',
            'total_days', 'validity_days', 'is_trial_package', 'is_trial',
            'only_for_trial', 'show_on_web', 'show_on_app',
            'effective_from', 'effective_until', 'published_at',
            'status', 'prices', 'entitlement_definitions',
            'created_by_user', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'version_number', 'created_by_user', 'created_at', 'updated_at']


class PackageSerializer(serializers.ModelSerializer):
    program = serializers.PrimaryKeyRelatedField(queryset=Program.objects.all(), required=False, allow_null=True)
    program_name = serializers.CharField(source='program.name', read_only=True)
    latest_version = serializers.SerializerMethodField()
    active_version = serializers.SerializerMethodField()
    branch_availabilities = PackageBranchAvailabilitySerializer(many=True, read_only=True)

    class Meta:
        model = Package
        fields = [
            'id', 'organization', 'program', 'program_name',
            'code', 'name', 'status', 'latest_version', 'active_version',
            'branch_availabilities', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at']

    def to_internal_value(self, data):
        data = data.copy()
        if 'program' not in data or not data['program']:
            req = self.context.get('request')
            alias = getattr(getattr(req, 'user', None), '_db_alias', None) or 'default'
            org = getattr(req, 'organization', None) or (getattr(req.user, 'organization', None) if req and hasattr(req, 'user') else None)
            if org:
                from .models_catalog import Program
                prog = Program.objects.using(alias).filter(organization=org).first()
                if not prog:
                    from .services_catalog import PackageCatalogService
                    prog = PackageCatalogService.create_program(
                        organization=org,
                        code='DEFAULT',
                        name='Default Program',
                        status='ACTIVE',
                        db_alias=alias,
                    )
                data['program'] = str(prog.id)
        return super().to_internal_value(data)

    def get_latest_version(self, obj):
        latest = obj.versions.order_by('-version_number').first()
        if latest:
            return {
                'id': str(latest.id),
                'version_number': latest.version_number,
                'name_snapshot': latest.name_snapshot,
                'duration_value': latest.duration_value,
                'duration_unit': latest.duration_unit,
                'status': latest.status,
            }
        return None

    def get_active_version(self, obj):
        active = obj.versions.filter(status='ACTIVE').order_by('-version_number').first()
        if not active:
            active = obj.versions.order_by('-version_number').first()
        if active:
            prices = [
                {
                    'branch_id': str(p.branch_id) if p.branch_id else None,
                    'currency': p.currency,
                    'base_price': str(p.base_price),
                    'sale_price': str(p.base_price),
                    'display_price': str(p.display_price) if p.display_price else None,
                    'prices_include_tax': p.prices_include_tax,
                    'tax_percent': str(p.tax_percent),
                    'tax_percentage': str(p.tax_percent),
                    'total_price': str(p.total_price),
                }
                for p in active.prices.filter(status='ACTIVE')
            ]
            entitlements = [
                {
                    'entitlement_type': e.entitlement_type,
                    'allocated_units': str(e.allocated_units) if e.allocated_units is not None else None,
                    'extra_unit_price': str(e.extra_unit_price) if e.extra_unit_price is not None else None,
                }
                for e in active.entitlement_definitions.filter(status='ACTIVE')
            ]
            return {
                'id': str(active.id),
                'version_number': active.version_number,
                'name_snapshot': active.name_snapshot,
                'duration_value': active.duration_value,
                'duration_unit': active.duration_unit,
                'total_days': active.total_days,
                'validity_days': active.validity_days,
                'show_on_web': active.show_on_web,
                'show_on_app': active.show_on_app,
                'status': active.status,
                'prices': prices,
                'entitlements': entitlements,
            }
        return None
