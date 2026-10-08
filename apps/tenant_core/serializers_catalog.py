"""
DRF Serializers for Layer 2: Module C (Terms) & Module D (Programs / Packages / Catalog)
"""

import re
from decimal import Decimal
from django.db import models
from rest_framework import serializers
from .models_catalog import (
    TermsDocument, TermsDocumentVersion, TermsAcceptance,
    ProgramCategory, ProgramType, Program, ProgramBranchAvailability,
    Package, PackageVersion, PackagePrice, PackageBranchAvailability,
    PackageEntitlementDefinition,
)


def _generate_unique_code(model_class, organization, name, db_alias=None, field_name='code'):
    """
    Auto-generates a stable internal code from a human-readable name.
    E.g. "Pilates Group" -> "PILATES_GROUP".
    Handles collisions by deterministically appending _2, _3, etc.
    """
    if not name:
        base_code = 'ITEM'
    else:
        cleaned = re.sub(r'[^A-Za-z0-9]+', '_', str(name).strip().upper()).strip('_')
        base_code = cleaned[:80] if cleaned else 'ITEM'

    from config.routers import get_tenant_db_alias
    alias = db_alias or (getattr(organization, '_state', None) and getattr(organization._state, 'db', None)) or get_tenant_db_alias() or 'default'
    qs = model_class.objects.using(alias)
    if organization is not None:
        qs = qs.filter(organization=organization)

    candidate = base_code
    counter = 1
    while qs.filter(**{field_name: candidate}).exists():
        counter += 1
        suffix = f"_{counter}"
        candidate = f"{base_code[:100 - len(suffix)]}{suffix}"

    return candidate


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
    code = serializers.CharField(required=False, allow_blank=True, max_length=100)
    active_version = serializers.SerializerMethodField()
    versions_count = serializers.SerializerMethodField()

    class Meta:
        model = TermsDocument
        fields = [
            'id', 'organization', 'code', 'name', 'document_type',
            'status', 'active_version', 'versions_count', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at']
        extra_kwargs = {
            'code': {'required': False, 'allow_blank': True},
        }

    def create(self, validated_data):
        if not validated_data.get('code'):
            req = self.context.get('request')
            alias = getattr(getattr(req, 'user', None), '_db_alias', None) or self.context.get('db_alias')
            org = validated_data.get('organization') or getattr(req, 'organization', None) or self.context.get('organization')
            validated_data['code'] = _generate_unique_code(TermsDocument, org, validated_data.get('name', 'TERMS'), db_alias=alias)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        if 'code' in validated_data and not validated_data['code']:
            validated_data.pop('code')
        return super().update(instance, validated_data)

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
    code = serializers.CharField(required=False, allow_blank=True, max_length=100)
    name = serializers.CharField(required=True, max_length=150)
    programs_count = serializers.SerializerMethodField()

    class Meta:
        model = ProgramType
        fields = [
            'id', 'organization', 'code', 'name', 'description',
            'display_order', 'status', 'programs_count', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at']
        extra_kwargs = {
            'code': {'required': False, 'allow_blank': True},
        }

    def validate_code(self, value):
        cleaned = str(value or '').strip().upper()
        if not cleaned:
            return cleaned
        if not re.match(r'^[A-Z0-9_-]+$', cleaned):
            raise serializers.ValidationError("Program Type code must contain only uppercase letters, numbers, underscores, and hyphens.")
        return cleaned

    def validate_name(self, value):
        cleaned = str(value or '').strip()
        if not cleaned:
            raise serializers.ValidationError("Program Type name is required.")
        return cleaned

    def validate(self, attrs):
        req = self.context.get('request')
        from .views_catalog import _get_org, _get_db
        alias = _get_db(req) if req else (self.context.get('db_alias') or 'default')
        org = getattr(req, 'organization', None) or (self.instance.organization if self.instance else None) or self.context.get('organization')
        if not org and req:
            org = _get_org(req)
        code = attrs.get('code')
        if code and org:
            qs = ProgramType.objects.using(alias).filter(organization=org, code=code)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError({'code': f"A Program Type with code '{code}' already exists."})
        return attrs

    def create(self, validated_data):
        if not validated_data.get('code'):
            req = self.context.get('request')
            alias = getattr(getattr(req, 'user', None), '_db_alias', None) or self.context.get('db_alias')
            org = validated_data.get('organization') or getattr(req, 'organization', None) or self.context.get('organization')
            if not org and req:
                from .views_catalog import _get_org
                org = _get_org(req)
            validated_data['code'] = _generate_unique_code(ProgramType, org, validated_data.get('name', 'PROGRAM_TYPE'), db_alias=alias)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        if 'code' in validated_data and not validated_data['code']:
            validated_data.pop('code')
        return super().update(instance, validated_data)

    def get_programs_count(self, obj):
        return obj.programs.count()


class ProgramCategorySerializer(serializers.ModelSerializer):
    code = serializers.CharField(required=False, allow_blank=True, max_length=100)
    name = serializers.CharField(required=True, max_length=150)
    programs_count = serializers.SerializerMethodField()

    class Meta:
        model = ProgramCategory
        fields = [
            'id', 'organization', 'code', 'name', 'description',
            'display_order', 'status', 'programs_count', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at']
        extra_kwargs = {
            'code': {'required': False, 'allow_blank': True},
        }

    def validate_code(self, value):
        cleaned = str(value or '').strip().upper()
        if not cleaned:
            return cleaned
        if not re.match(r'^[A-Z0-9_-]+$', cleaned):
            raise serializers.ValidationError("Program Category code must contain only uppercase letters, numbers, underscores, and hyphens.")
        return cleaned

    def validate_name(self, value):
        cleaned = str(value or '').strip()
        if not cleaned:
            raise serializers.ValidationError("Program Category name is required.")
        return cleaned

    def validate(self, attrs):
        req = self.context.get('request')
        from .views_catalog import _get_org, _get_db
        alias = _get_db(req) if req else (self.context.get('db_alias') or 'default')
        org = getattr(req, 'organization', None) or (self.instance.organization if self.instance else None) or self.context.get('organization')
        if not org and req:
            org = _get_org(req)
        code = attrs.get('code')
        if code and org:
            qs = ProgramCategory.objects.using(alias).filter(organization=org, code=code)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError({'code': f"A Program Category with code '{code}' already exists."})
        return attrs

    def create(self, validated_data):
        if not validated_data.get('code'):
            req = self.context.get('request')
            alias = getattr(getattr(req, 'user', None), '_db_alias', None) or self.context.get('db_alias')
            org = validated_data.get('organization') or getattr(req, 'organization', None) or self.context.get('organization')
            if not org and req:
                from .views_catalog import _get_org
                org = _get_org(req)
            validated_data['code'] = _generate_unique_code(ProgramCategory, org, validated_data.get('name', 'CATEGORY'), db_alias=alias)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        if 'code' in validated_data and not validated_data['code']:
            validated_data.pop('code')
        return super().update(instance, validated_data)

    def get_programs_count(self, obj):
        return obj.programs.count()


class ProgramBranchAvailabilitySerializer(serializers.ModelSerializer):
    program_name = serializers.CharField(source='program.name', read_only=True)
    program_code = serializers.CharField(source='program.code', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    branch_code = serializers.CharField(source='branch.code', read_only=True)

    class Meta:
        model = ProgramBranchAvailability
        fields = [
            'id', 'program', 'program_name', 'program_code',
            'branch', 'branch_name', 'branch_code',
            'is_active', 'effective_from', 'effective_to',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate(self, attrs):
        program = attrs.get('program') or (self.instance.program if self.instance else None)
        branch = attrs.get('branch') or (self.instance.branch if self.instance else None)
        if program and branch:
            if program.organization_id != branch.organization_id:
                raise serializers.ValidationError("Program and branch must belong to the same organization.")
            if branch.status in ('INACTIVE', 'CLOSED'):
                raise serializers.ValidationError({'branch': f"Branch '{branch.name}' is inactive or closed and cannot be assigned."})
        return attrs


class ProgramSerializer(serializers.ModelSerializer):
    code = serializers.CharField(required=False, allow_blank=True, max_length=100)
    name = serializers.CharField(required=True, max_length=200)
    category = serializers.PrimaryKeyRelatedField(
        queryset=ProgramCategory.objects.all(), required=False, allow_null=True
    )
    category_name = serializers.CharField(source='category.name', read_only=True)
    category_code = serializers.CharField(source='category.code', read_only=True)
    program_type = serializers.PrimaryKeyRelatedField(
        queryset=ProgramType.objects.all(), required=False, allow_null=True
    )
    program_type_name = serializers.CharField(source='program_type.name', read_only=True)
    program_type_code = serializers.CharField(source='program_type.code', read_only=True)
    available_branch_ids = serializers.ListField(
        child=serializers.UUIDField(), required=False, write_only=False
    )
    available_branches = serializers.SerializerMethodField()
    packages_count = serializers.SerializerMethodField()

    class Meta:
        model = Program
        fields = [
            'id', 'organization', 'category', 'category_name', 'category_code',
            'code', 'name', 'description', 'delivery_mode', 'display_order',
            'program_type', 'program_type_name', 'program_type_code',
            'trial_allowed', 'status', 'packages_count',
            'available_branch_ids', 'available_branches',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at']
        extra_kwargs = {
            'code': {'required': False, 'allow_blank': True},
        }

    def validate_code(self, value):
        cleaned = str(value or '').strip().upper()
        if not cleaned:
            return cleaned
        if not re.match(r'^[A-Z0-9_-]+$', cleaned):
            raise serializers.ValidationError("Program code must contain only uppercase letters, numbers, underscores, and hyphens.")
        return cleaned

    def validate_name(self, value):
        cleaned = str(value or '').strip()
        if not cleaned:
            raise serializers.ValidationError("Program name is required.")
        return cleaned

    def validate(self, attrs):
        req = self.context.get('request')
        from .views_catalog import _get_org, _get_db
        alias = _get_db(req) if req else (self.context.get('db_alias') or 'default')
        org = getattr(req, 'organization', None) or (self.instance.organization if self.instance else None) or self.context.get('organization')
        if not org and req:
            org = _get_org(req)

        # 1. Resolve canonical Category (primary) and ProgramType (compatibility)
        cat = attrs.get('category') or (self.instance.category if self.instance else None)
        pt = attrs.get('program_type') or (self.instance.program_type if self.instance else None)

        if cat:
            if cat.organization_id != org.id:
                raise serializers.ValidationError({'category': "Program Category does not belong to this organization."})
            if not self.instance and cat.status != 'ACTIVE':
                raise serializers.ValidationError({'category': f"Program Category '{cat.name}' is inactive and cannot be selected for a new program."})
        elif pt:
            # Compatibility bridge for legacy callers/tests
            if pt.organization_id != org.id:
                raise serializers.ValidationError({'program_type': "Program Type does not belong to this organization."})
            if not self.instance and pt.status != 'ACTIVE':
                raise serializers.ValidationError({'program_type': f"Program Type '{pt.name}' is inactive and cannot be selected for a new program."})
            from .models_catalog import ProgramCategory
            matching_cat = ProgramCategory.objects.using(alias).filter(organization=org, name__iexact=pt.name).first()
            if not matching_cat:
                matching_cat = ProgramCategory.objects.using(alias).filter(organization=org, code__iexact=pt.code).first()
            if not matching_cat:
                cat_code = _generate_unique_code(ProgramCategory, org, pt.name, db_alias=alias)
                matching_cat = ProgramCategory.objects.using(alias).create(
                    organization=org,
                    name=pt.name,
                    code=cat_code,
                    description=pt.description,
                    display_order=pt.display_order,
                    status=pt.status,
                )
            attrs['category'] = matching_cat
        else:
            raise serializers.ValidationError({'category': "Program Category is required."})

        # 2. Normalize delivery_mode (Service Structure engine semantics)
        dm = attrs.get('delivery_mode')
        if dm:
            dm_norm = {
                'GROUP': 'GROUP_CLASS',
                'GROUP_CLASS': 'GROUP_CLASS',
                'PERSONAL_TRAINING': 'INDIVIDUAL_SERVICE',
                'INDIVIDUAL_SERVICE': 'INDIVIDUAL_SERVICE',
                'OPEN_GYM': 'OPEN_ACCESS',
                'OPEN_ACCESS': 'OPEN_ACCESS',
                'HYBRID': 'GROUP_CLASS',
            }.get(str(dm).upper(), dm)
            attrs['delivery_mode'] = dm_norm

        code = attrs.get('code')
        if code and org:
            qs = Program.objects.using(alias).filter(organization=org, code=code)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError({'code': f"A Program with code '{code}' already exists in this organization."})

        branch_ids = attrs.get('available_branch_ids')
        if branch_ids and org:
            from .models_org import Branch
            for bid in branch_ids:
                br = Branch.objects.using(alias).filter(pk=bid).first()
                if not br or br.organization_id != org.id:
                    raise serializers.ValidationError({'available_branch_ids': f"Branch with ID '{bid}' does not belong to this organization."})
                if br.status in ('INACTIVE', 'CLOSED'):
                    raise serializers.ValidationError({'available_branch_ids': f"Branch '{br.name}' is inactive and cannot be assigned."})

        return attrs

    def create(self, validated_data):
        branch_ids = validated_data.pop('available_branch_ids', None)
        req = self.context.get('request')
        alias = getattr(getattr(req, 'user', None), '_db_alias', None) or self.context.get('db_alias') or 'default'
        org = validated_data.get('organization') or getattr(req, 'organization', None) or self.context.get('organization')
        if not org and req:
            from .views_catalog import _get_org
            org = _get_org(req)
        if not validated_data.get('code'):
            validated_data['code'] = _generate_unique_code(Program, org, validated_data.get('name', 'PROGRAM'), db_alias=alias)

        program = super().create(validated_data)
        self._sync_branch_availability(program, branch_ids, alias, org)
        return program

    def update(self, instance, validated_data):
        branch_ids = validated_data.pop('available_branch_ids', None)
        if 'code' in validated_data and not validated_data['code']:
            validated_data.pop('code')
        req = self.context.get('request')
        alias = getattr(getattr(req, 'user', None), '_db_alias', None) or self.context.get('db_alias') or instance._state.db or 'default'
        org = instance.organization

        program = super().update(instance, validated_data)
        if branch_ids is not None:
            self._sync_branch_availability(program, branch_ids, alias, org)
        return program

    def _sync_branch_availability(self, program, branch_ids, alias, org):
        if branch_ids is None:
            return
        from .models_catalog import ProgramBranchAvailability
        # Activate selected branches
        for bid in branch_ids:
            pba, created = ProgramBranchAvailability.objects.using(alias).get_or_create(
                program=program, branch_id=bid,
                defaults={'is_active': True}
            )
            if not created and not pba.is_active:
                pba.is_active = True
                pba.save(using=alias)

        # Deactivate unselected branches
        ProgramBranchAvailability.objects.using(alias).filter(
            program=program
        ).exclude(branch_id__in=branch_ids).update(is_active=False)

    def get_available_branches(self, obj):
        alias = obj._state.db or 'default'
        from .models_catalog import ProgramBranchAvailability
        pbas = ProgramBranchAvailability.objects.using(alias).filter(
            program=obj, is_active=True
        ).select_related('branch')
        return [
            {'id': str(pba.branch_id), 'code': pba.branch.code, 'name': pba.branch.name}
            for pba in pbas if pba.branch and pba.branch.status == 'ACTIVE'
        ]

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        branches = self.get_available_branches(instance)
        ret['available_branches'] = branches
        ret['available_branch_ids'] = [b['id'] for b in branches]
        return ret

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
                    pt = ProgramType.objects.using(alias).filter(
                        organization=org,
                        code__iexact=raw_pt.strip()
                    ).first()
                    if not pt:
                        pt = ProgramType.objects.using(alias).create(
                            organization=org,
                            code=raw_pt.upper().strip(),
                            name=raw_pt.replace('_', ' ').title(),
                            status='ACTIVE'
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

    def validate(self, attrs):
        req = self.context.get('request')
        alias = getattr(getattr(req, 'user', None), '_db_alias', None) or self.context.get('db_alias') or 'default'

        pkg_ver = attrs.get('package_version') or (self.instance.package_version if self.instance else None)
        if pkg_ver:
            if not self.instance and pkg_ver.status in ('ACTIVE', 'RETIRED'):
                raise serializers.ValidationError({
                    'package_version': f"Package version {pkg_ver.version_number} is {pkg_ver.status} and immutable. Create a new package version to configure pricing."
                })
            elif self.instance and self.instance.package_version.status in ('ACTIVE', 'RETIRED'):
                raise serializers.ValidationError({
                    'package_version': f"Package version {self.instance.package_version.version_number} is {self.instance.package_version.status} and immutable. Create a new package version to modify pricing."
                })

        base_price = attrs.get('base_price')
        if base_price is not None and base_price < Decimal('0.00'):
            raise serializers.ValidationError({'base_price': "Price cannot be negative."})

        tax_percent = attrs.get('tax_percent')
        if tax_percent is not None and tax_percent < Decimal('0.00'):
            raise serializers.ValidationError({'tax_percent': "Tax percentage cannot be negative."})

        eff_from = attrs.get('effective_from') or (self.instance.effective_from if self.instance else timezone.now())
        eff_until = attrs.get('effective_until') if 'effective_until' in attrs else (self.instance.effective_until if self.instance else None)
        if eff_from and eff_until and eff_until <= eff_from:
            raise serializers.ValidationError({'effective_until': "effective_until must be after effective_from."})

        branch = attrs.get('branch') if 'branch' in attrs else (self.instance.branch if self.instance else None)
        if branch and pkg_ver:
            pkg_org_id = pkg_ver.package.organization_id
            if branch.organization_id != pkg_org_id:
                raise serializers.ValidationError({'branch': "Branch does not belong to the same organization as the package."})
            if branch.status in ('INACTIVE', 'CLOSED'):
                raise serializers.ValidationError({'branch': f"Branch '{branch.name}' is inactive or closed and cannot be assigned."})

        # Check conflicting active prices with overlapping effective dates
        if pkg_ver:
            currency = attrs.get('currency') or (self.instance.currency if self.instance else 'INR')
            price_status = attrs.get('status') or (self.instance.status if self.instance else 'ACTIVE')
            if price_status == 'ACTIVE':
                overlapping = PackagePrice.objects.using(alias).filter(
                    package_version=pkg_ver,
                    branch=branch,
                    currency=currency,
                    status='ACTIVE',
                )
                if self.instance:
                    overlapping = overlapping.exclude(pk=self.instance.pk)
                
                # Check date overlap
                if eff_until:
                    overlapping = overlapping.filter(
                        models.Q(effective_until__isnull=True, effective_from__lte=eff_until) |
                        models.Q(effective_until__gte=eff_from, effective_from__lte=eff_until)
                    )
                else:
                    overlapping = overlapping.filter(
                        models.Q(effective_until__isnull=True) |
                        models.Q(effective_until__gte=eff_from)
                    )
                if overlapping.exists():
                    raise serializers.ValidationError({
                        'effective_from': "An active price for this package version, branch, and currency already covers this overlapping effective date window."
                    })

        return attrs


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

    def validate(self, attrs):
        package = attrs.get('package') or (self.instance.package if self.instance else None)
        branch = attrs.get('branch') or (self.instance.branch if self.instance else None)

        if not package:
            raise serializers.ValidationError({'package': "Package is required."})
        if not branch:
            raise serializers.ValidationError({'branch': "Branch is required."})

        if package.organization_id != branch.organization_id:
            raise serializers.ValidationError("Package and branch must belong to the same organization.")

        if branch.status in ('INACTIVE', 'CLOSED'):
            raise serializers.ValidationError({'branch': f"Branch '{branch.name}' is inactive or closed and cannot be assigned."})

        # Validate prerequisite: Package's Program must be available at that branch
        req = self.context.get('request')
        alias = getattr(getattr(req, 'user', None), '_db_alias', None) or self.context.get('db_alias') or 'default'
        from .models_catalog import ProgramBranchAvailability
        has_prog_avail = ProgramBranchAvailability.objects.using(alias).filter(
            program=package.program,
            branch=branch,
            is_active=True,
        ).exists()
        if not has_prog_avail:
            raise serializers.ValidationError({
                'branch': f"Program '{package.program.name}' is not available at branch '{branch.name}'."
            })

        return attrs


class PackageEntitlementDefinitionSerializer(serializers.ModelSerializer):
    class Meta:
        model = PackageEntitlementDefinition
        fields = [
            'id', 'package_version', 'entitlement_type', 'allocated_units',
            'is_unlimited', 'extra_unit_price', 'validity_days', 'reference_type', 'reference_id',
            'configuration', 'status', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate(self, attrs):
        pkg_ver = attrs.get('package_version') or (self.instance.package_version if self.instance else None)
        if pkg_ver:
            if not self.instance and pkg_ver.status in ('ACTIVE', 'RETIRED'):
                raise serializers.ValidationError({
                    'package_version': f"Package version {pkg_ver.version_number} is {pkg_ver.status} and immutable. Create a new package version to configure entitlements."
                })
            elif self.instance and self.instance.package_version.status in ('ACTIVE', 'RETIRED'):
                raise serializers.ValidationError({
                    'package_version': f"Package version {self.instance.package_version.version_number} is {self.instance.package_version.status} and immutable. Create a new package version to modify entitlements."
                })

        is_unlimited = attrs.get('is_unlimited', self.instance.is_unlimited if self.instance else False)
        allocated_units = attrs.get('allocated_units', self.instance.allocated_units if self.instance else None)

        if not is_unlimited and (allocated_units is None or allocated_units <= 0):
            raise serializers.ValidationError({
                'allocated_units': "Must specify allocated units greater than zero or mark as unlimited."
            })

        if allocated_units is not None and allocated_units < 0:
            raise serializers.ValidationError({'allocated_units': "Allocated units cannot be negative."})

        extra_unit_price = attrs.get('extra_unit_price')
        if extra_unit_price is not None and extra_unit_price < Decimal('0.00'):
            raise serializers.ValidationError({'extra_unit_price': "Extra unit price cannot be negative."})

        ent_type = attrs.get('entitlement_type') or (self.instance.entitlement_type if self.instance else None)
        valid_types = [choice[0] for choice in PackageEntitlementDefinition.ENTITLEMENT_TYPE_CHOICES]
        if ent_type and ent_type not in valid_types:
            raise serializers.ValidationError({
                'entitlement_type': f"Invalid entitlement type '{ent_type}'. Allowed types: {', '.join(valid_types)}"
            })

        return attrs


class PackageVersionSerializer(serializers.ModelSerializer):
    prices = PackagePriceSerializer(many=True, read_only=True)
    entitlement_definitions = PackageEntitlementDefinitionSerializer(many=True, read_only=True)
    package_code = serializers.CharField(source='package.code', read_only=True)
    package_name = serializers.CharField(source='package.name', read_only=True)
    effective_price = serializers.SerializerMethodField()
    currency = serializers.SerializerMethodField()

    def get_effective_price(self, obj):
        req = self.context.get('request')
        branch_id = req.query_params.get('branch_id') if req else None
        active_prices = [p for p in obj.prices.all() if p.status == 'ACTIVE']
        if branch_id:
            bp = next((p for p in active_prices if str(p.branch_id) == str(branch_id)), None)
            if bp:
                return str(bp.base_price)
        gp = next((p for p in active_prices if p.branch_id is None), None)
        if gp:
            return str(gp.base_price)
        return str(active_prices[0].base_price) if active_prices else None

    def get_currency(self, obj):
        active_prices = [p for p in obj.prices.all() if p.status == 'ACTIVE']
        return active_prices[0].currency if active_prices else 'INR'

    class Meta:
        model = PackageVersion
        fields = [
            'id', 'package', 'package_code', 'package_name', 'version_number',
            'name_snapshot', 'description_snapshot', 'duration_value', 'duration_unit',
            'total_days', 'validity_days', 'is_trial_package', 'is_trial',
            'only_for_trial', 'show_on_web', 'show_on_app',
            'effective_from', 'effective_until', 'published_at',
            'status', 'prices', 'effective_price', 'currency', 'entitlement_definitions',
            'created_by_user', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'version_number', 'created_by_user', 'created_at', 'updated_at']

    def validate(self, attrs):
        if self.instance and self.instance.status in ('ACTIVE', 'RETIRED'):
            immutable_fields = [
                'name_snapshot', 'duration_value', 'duration_unit', 'total_days',
                'validity_days', 'is_trial_package', 'is_trial', 'only_for_trial'
            ]
            for f in immutable_fields:
                if f in attrs and getattr(self.instance, f) != attrs[f]:
                    raise serializers.ValidationError({
                        f: f"PackageVersion {self.instance.version_number} is {self.instance.status} and immutable. Field '{f}' cannot be changed. Create a new version."
                    })

        total_days = attrs.get('total_days', self.instance.total_days if self.instance else None)
        if total_days is not None and total_days <= 0:
            raise serializers.ValidationError({'total_days': "Total days must be greater than 0."})

        duration_value = attrs.get('duration_value', self.instance.duration_value if self.instance else None)
        if duration_value is not None and duration_value <= 0:
            raise serializers.ValidationError({'duration_value': "Duration value must be greater than 0."})

        eff_from = attrs.get('effective_from', self.instance.effective_from if self.instance else None)
        eff_until = attrs.get('effective_until', self.instance.effective_until if self.instance else None)
        if eff_from and eff_until and eff_until <= eff_from:
            raise serializers.ValidationError({'effective_until': "effective_until must be after effective_from."})

        return attrs


class PackageSerializer(serializers.ModelSerializer):
    code = serializers.CharField(required=False, allow_blank=True, max_length=100)
    program = serializers.PrimaryKeyRelatedField(queryset=Program.objects.all(), required=True)
    program_name = serializers.CharField(source='program.name', read_only=True)
    available_branch_ids = serializers.ListField(
        child=serializers.UUIDField(), required=False, write_only=False
    )
    available_branches = serializers.SerializerMethodField()
    latest_version = serializers.SerializerMethodField()
    active_version = serializers.SerializerMethodField()
    versions = PackageVersionSerializer(many=True, read_only=True)
    versions_count = serializers.SerializerMethodField()
    branch_availabilities = PackageBranchAvailabilitySerializer(many=True, read_only=True)

    class Meta:
        model = Package
        fields = [
            'id', 'organization', 'program', 'program_name',
            'code', 'name', 'status', 'available_branch_ids', 'available_branches',
            'latest_version', 'active_version',
            'versions', 'versions_count',
            'branch_availabilities', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at']
        extra_kwargs = {
            'code': {'required': False, 'allow_blank': True},
        }

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, 'copy') else dict(data)
        if self.instance and self.instance.code:
            data['code'] = self.instance.code
        elif not data.get('code'):
            req = self.context.get('request')
            from .views_catalog import _get_org, _get_db
            alias = _get_db(req) if req else (self.context.get('db_alias') or 'default')
            org = getattr(req, 'organization', None) or (self.instance.organization if self.instance else None) or self.context.get('organization')
            if not org and req:
                org = _get_org(req)
            data['code'] = _generate_unique_code(Package, org, data.get('name') or 'PKG', db_alias=alias)
        return super().to_internal_value(data)

    def validate_code(self, value):
        cleaned = str(value or '').strip().upper()
        if not cleaned:
            return cleaned
        if not re.match(r'^[A-Z0-9_-]+$', cleaned):
            raise serializers.ValidationError("Package code must contain only uppercase letters, numbers, underscores, and hyphens.")
        return cleaned

    def validate_name(self, value):
        cleaned = str(value or '').strip()
        if not cleaned:
            raise serializers.ValidationError("Package name is required.")
        return cleaned

    def validate(self, attrs):
        req = self.context.get('request')
        from .views_catalog import _get_org, _get_db
        alias = _get_db(req) if req else (self.context.get('db_alias') or 'default')
        org = getattr(req, 'organization', None) or (self.instance.organization if self.instance else None) or self.context.get('organization')
        if not org and req:
            org = _get_org(req)

        # 1. Program validation
        program = attrs.get('program') or (self.instance.program if self.instance else None)
        if not program:
            raise serializers.ValidationError({'program': "Program is required."})

        if program.organization_id != org.id:
            raise serializers.ValidationError({'program': "Program does not belong to this organization."})

        if not self.instance and program.status != 'ACTIVE':
            raise serializers.ValidationError({
                'program': f"Program '{program.name}' is {program.status} and cannot be used for new packages."
            })

        # 2. Code uniqueness in organization
        code = attrs.get('code')
        if code and org:
            qs = Package.objects.using(alias).filter(organization=org, code=code)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError({'code': f"A Package with code '{code}' already exists in this organization."})

        # 3. Available branches validation (Program prerequisite check)
        branch_ids = attrs.get('available_branch_ids')
        if branch_ids is not None and org:
            from .models_org import Branch
            from .models_catalog import ProgramBranchAvailability
            for bid in branch_ids:
                br = Branch.objects.using(alias).filter(pk=bid).first()
                if not br or br.organization_id != org.id:
                    raise serializers.ValidationError({
                        'available_branch_ids': f"Branch with ID '{bid}' does not belong to this organization."
                    })
                if br.status in ('INACTIVE', 'CLOSED'):
                    raise serializers.ValidationError({
                        'available_branch_ids': f"Branch '{br.name}' is inactive or closed and cannot be assigned."
                    })
                # Verify program is available at this branch
                has_prog_avail = ProgramBranchAvailability.objects.using(alias).filter(
                    program=program, branch=br, is_active=True
                ).exists()
                if not has_prog_avail:
                    raise serializers.ValidationError({
                        'available_branch_ids': f"Program '{program.name}' is not available at branch '{br.name}'. Enable the program at this branch first."
                    })

        return attrs

    def create(self, validated_data):
        branch_ids = validated_data.pop('available_branch_ids', None)
        req = self.context.get('request')
        alias = getattr(getattr(req, 'user', None), '_db_alias', None) or self.context.get('db_alias') or 'default'
        org = validated_data.get('organization') or getattr(req, 'organization', None) or self.context.get('organization')
        if not org and req:
            from .views_catalog import _get_org
            org = _get_org(req)

        if not validated_data.get('code'):
            validated_data['code'] = _generate_unique_code(Package, org, validated_data.get('name', 'PACKAGE'), db_alias=alias)

        package = super().create(validated_data)
        self._sync_branch_availability(package, branch_ids, alias, org)
        return package

    def update(self, instance, validated_data):
        branch_ids = validated_data.pop('available_branch_ids', None)
        validated_data.pop('code', None)

        req = self.context.get('request')
        alias = getattr(getattr(req, 'user', None), '_db_alias', None) or self.context.get('db_alias') or instance._state.db or 'default'
        org = instance.organization

        package = super().update(instance, validated_data)
        if branch_ids is not None:
            self._sync_branch_availability(package, branch_ids, alias, org)
        return package

    def _sync_branch_availability(self, package, branch_ids, alias, org):
        if branch_ids is None:
            return
        from .models_catalog import PackageBranchAvailability
        # Enable selected branches
        for bid in branch_ids:
            pba, created = PackageBranchAvailability.objects.using(alias).get_or_create(
                package=package, branch_id=bid,
                defaults={'status': 'ENABLED'}
            )
            if not created and pba.status != 'ENABLED':
                pba.status = 'ENABLED'
                pba.save(using=alias)

        # Disable unselected branches
        PackageBranchAvailability.objects.using(alias).filter(
            package=package
        ).exclude(branch_id__in=branch_ids).update(status='DISABLED')

    def get_available_branches(self, obj):
        alias = obj._state.db or 'default'
        from .models_catalog import PackageBranchAvailability
        pbas = PackageBranchAvailability.objects.using(alias).filter(
            package=obj, status='ENABLED'
        ).select_related('branch')
        return [
            {'id': str(pba.branch_id), 'code': pba.branch.code, 'name': pba.branch.name}
            for pba in pbas if pba.branch and pba.branch.status == 'ACTIVE'
        ]

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        branches = self.get_available_branches(instance)
        ret['available_branches'] = branches
        ret['available_branch_ids'] = [b['id'] for b in branches]
        return ret

    def get_versions_count(self, obj):
        return obj.versions.count()

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
                    'id': str(p.id),
                    'branch_id': str(p.branch_id) if p.branch_id else None,
                    'branch_name': p.branch.name if p.branch else None,
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
                    'id': str(e.id),
                    'entitlement_type': e.entitlement_type,
                    'allocated_units': str(e.allocated_units) if e.allocated_units is not None else None,
                    'is_unlimited': e.is_unlimited,
                    'extra_unit_price': str(e.extra_unit_price) if e.extra_unit_price is not None else None,
                    'validity_days': e.validity_days,
                    'configuration': e.configuration or {},
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

