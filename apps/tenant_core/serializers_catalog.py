"""
DRF Serializers for Layer 2: Module C (Terms) & Module D (Programs / Packages / Catalog)
"""

from rest_framework import serializers
from .models_catalog import (
    TermsDocument, TermsDocumentVersion, TermsAcceptance,
    ProgramCategory, Program, Package, PackageVersion,
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
    packages_count = serializers.SerializerMethodField()

    class Meta:
        model = Program
        fields = [
            'id', 'organization', 'category', 'category_name',
            'code', 'name', 'description', 'program_type',
            'trial_allowed', 'status', 'packages_count', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at']

    def get_packages_count(self, obj):
        return obj.packages.count()


class PackagePriceSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    total_price = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = PackagePrice
        fields = [
            'id', 'package_version', 'branch', 'branch_name',
            'currency', 'base_price', 'tax_percent', 'total_price',
            'effective_from', 'effective_until', 'status',
            'created_by_user', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_by_user', 'total_price', 'created_at', 'updated_at']


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
            'is_unlimited', 'validity_days', 'reference_type', 'reference_id',
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
            'validity_days', 'is_trial_package', 'effective_from', 'effective_until',
            'status', 'prices', 'entitlement_definitions',
            'created_by_user', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'version_number', 'created_by_user', 'created_at', 'updated_at']


class PackageSerializer(serializers.ModelSerializer):
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
        if active:
            prices = [
                {
                    'branch_id': str(p.branch_id) if p.branch_id else None,
                    'currency': p.currency,
                    'base_price': str(p.base_price),
                    'total_price': str(p.total_price),
                }
                for p in active.prices.filter(status='ACTIVE')
            ]
            return {
                'id': str(active.id),
                'version_number': active.version_number,
                'name_snapshot': active.name_snapshot,
                'duration_value': active.duration_value,
                'duration_unit': active.duration_unit,
                'prices': prices,
            }
        return None
