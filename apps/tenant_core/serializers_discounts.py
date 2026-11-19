"""
apps/tenant_core/serializers_discounts.py — Serializers for Layer 2 Module H: Discounts & Dynamic Offers
"""

import uuid
from django.utils import timezone
from rest_framework import serializers
from apps.tenant_core.context import get_tenant_db_alias
from .models_catalog import Package, Program
from .models_org import Branch
from .models_discounts import (
    DiscountCampaign,
    DiscountCode,
    DiscountEligibilityRule,
    DiscountRuleCondition,
    DiscountRuleAction,
    DiscountRedemption,
)


class DiscountCodeSerializer(serializers.ModelSerializer):
    campaign_name = serializers.ReadOnlyField(source='campaign.name')
    branch_name = serializers.ReadOnlyField(source='branch.name')
    package_name = serializers.ReadOnlyField(source='package.name')
    computed_status = serializers.SerializerMethodField()
    redemption_count = serializers.SerializerMethodField()
    usage_remaining = serializers.SerializerMethodField()

    class Meta:
        model = DiscountCode
        fields = [
            'id',
            'campaign',
            'campaign_name',
            'code',
            'branch',
            'branch_name',
            'package',
            'package_name',
            'status',
            'computed_status',
            'redemption_count',
            'usage_remaining',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'computed_status', 'redemption_count', 'usage_remaining']

    def get_computed_status(self, obj) -> str:
        now = timezone.now()
        if obj.status == 'INACTIVE':
            return 'INACTIVE'
        camp = obj.campaign
        if not camp:
            return obj.status
        if camp.status == 'PAUSED':
            return 'INACTIVE'
        if camp.status == 'DRAFT':
            return 'SCHEDULED'
        if camp.valid_from and now < camp.valid_from:
            return 'SCHEDULED'
        if (camp.valid_until and now > camp.valid_until) or camp.status == 'EXPIRED':
            return 'EXPIRED'
        if camp.usage_limit is not None and camp.redemptions.count() >= camp.usage_limit:
            return 'USAGE_EXHAUSTED'
        return 'ACTIVE'

    def get_redemption_count(self, obj) -> int:
        return obj.redemptions.count()

    def get_usage_remaining(self, obj):
        camp = obj.campaign
        if not camp or camp.usage_limit is None:
            return None
        return max(0, camp.usage_limit - camp.redemptions.count())


class DiscountCampaignSerializer(serializers.ModelSerializer):
    codes = DiscountCodeSerializer(many=True, read_only=True)
    redemption_count = serializers.SerializerMethodField()
    computed_status = serializers.SerializerMethodField()
    usage_remaining = serializers.SerializerMethodField()

    class Meta:
        model = DiscountCampaign
        fields = [
            'id',
            'organization',
            'name',
            'description',
            'discount_type',
            'discount_value',
            'max_discount',
            'minimum_order_amount',
            'usage_limit',
            'per_user_limit',
            'valid_from',
            'valid_until',
            'status',
            'computed_status',
            'usage_remaining',
            'configuration',
            'codes',
            'redemption_count',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at', 'computed_status', 'usage_remaining']

    def get_redemption_count(self, obj) -> int:
        return obj.redemptions.count()

    def get_computed_status(self, obj) -> str:
        now = timezone.now()
        if obj.status == 'PAUSED':
            return 'INACTIVE'
        if obj.status == 'DRAFT':
            return 'SCHEDULED'
        if obj.valid_from and now < obj.valid_from:
            return 'SCHEDULED'
        if (obj.valid_until and now > obj.valid_until) or obj.status == 'EXPIRED':
            return 'EXPIRED'
        if obj.usage_limit is not None and obj.redemptions.count() >= obj.usage_limit:
            return 'USAGE_EXHAUSTED'
        return 'ACTIVE'

    def get_usage_remaining(self, obj):
        if obj.usage_limit is None:
            return None
        return max(0, obj.usage_limit - obj.redemptions.count())

    def validate_configuration(self, value):
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise serializers.ValidationError("Configuration must be a valid JSON object.")

        # Determine tenant DB alias and organization
        db = get_tenant_db_alias()
        request = self.context.get('request')
        org = None
        if request and hasattr(request, 'user'):
            org = getattr(request.user, 'organization', None)
        if not org and self.instance:
            org = getattr(self.instance, 'organization', None)

        branch_qs = Branch.objects.using(db) if db else Branch.objects.all()
        program_qs = Program.objects.using(db) if db else Program.objects.all()
        package_qs = Package.objects.using(db) if db else Package.objects.all()

        if org:
            branch_qs = branch_qs.filter(organization=org)
            program_qs = program_qs.filter(organization=org)
            package_qs = package_qs.filter(organization=org)

        # 1. Validate branch_ids / branch_id
        branch_ids = value.get('branch_ids')
        if branch_ids is not None:
            if not isinstance(branch_ids, list):
                raise serializers.ValidationError("branch_ids must be a list of branch UUIDs.")
            cleaned_branch_ids = [str(b).strip() for b in branch_ids if str(b).strip()]
            if cleaned_branch_ids:
                for bid in cleaned_branch_ids:
                    try:
                        uuid.UUID(str(bid))
                    except (ValueError, TypeError):
                        raise serializers.ValidationError(f"Invalid branch ID format: '{bid}'.")

                existing_branch_ids = set(
                    str(x) for x in branch_qs.filter(id__in=cleaned_branch_ids, status='ACTIVE').values_list('id', flat=True)
                )
                invalid_branches = [bid for bid in cleaned_branch_ids if bid not in existing_branch_ids]
                if invalid_branches:
                    raise serializers.ValidationError(f"Branch(es) not found or inactive: {', '.join(invalid_branches)}.")

                value['branch_ids'] = cleaned_branch_ids
                if not value.get('branch_id'):
                    value['branch_id'] = cleaned_branch_ids[0]
            else:
                value['branch_ids'] = []
        elif value.get('branch_id'):
            bid = str(value['branch_id']).strip()
            try:
                uuid.UUID(bid)
            except (ValueError, TypeError):
                raise serializers.ValidationError(f"Invalid branch ID format: '{bid}'.")
            if not branch_qs.filter(id=bid, status='ACTIVE').exists():
                raise serializers.ValidationError(f"Branch '{bid}' not found or inactive.")
            value['branch_ids'] = [bid]

        # 2. Validate program_ids
        program_ids = value.get('program_ids')
        cleaned_prog_ids = []
        if program_ids is not None:
            if not isinstance(program_ids, list):
                raise serializers.ValidationError("program_ids must be a list of program UUIDs.")
            cleaned_prog_ids = [str(p).strip() for p in program_ids if str(p).strip()]
            if cleaned_prog_ids:
                for pid in cleaned_prog_ids:
                    try:
                        uuid.UUID(str(pid))
                    except (ValueError, TypeError):
                        raise serializers.ValidationError(f"Invalid program ID format: '{pid}'.")

                existing_prog_ids = set(
                    str(x) for x in program_qs.filter(id__in=cleaned_prog_ids, status='ACTIVE').values_list('id', flat=True)
                )
                invalid_progs = [pid for pid in cleaned_prog_ids if pid not in existing_prog_ids]
                if invalid_progs:
                    raise serializers.ValidationError(f"Program(s) not found or inactive: {', '.join(invalid_progs)}.")

                value['program_ids'] = cleaned_prog_ids
            else:
                value['program_ids'] = []

        # 3. Validate package_ids / package_id
        package_ids = value.get('package_ids')
        cleaned_pkg_ids = []
        if package_ids is not None:
            if not isinstance(package_ids, list):
                raise serializers.ValidationError("package_ids must be a list of package UUIDs.")
            cleaned_pkg_ids = [str(p).strip() for p in package_ids if str(p).strip()]
            if cleaned_pkg_ids:
                for pkg_id in cleaned_pkg_ids:
                    try:
                        uuid.UUID(str(pkg_id))
                    except (ValueError, TypeError):
                        raise serializers.ValidationError(f"Invalid package ID format: '{pkg_id}'.")

                active_pkgs = list(package_qs.filter(id__in=cleaned_pkg_ids, status='ACTIVE').values('id', 'name', 'program_id'))
                existing_pkg_map = {str(p['id']): p for p in active_pkgs}

                invalid_pkgs = [pkg_id for pkg_id in cleaned_pkg_ids if pkg_id not in existing_pkg_map]
                if invalid_pkgs:
                    raise serializers.ValidationError(f"Package(s) not found or inactive: {', '.join(invalid_pkgs)}.")

                if cleaned_prog_ids:
                    selected_prog_set = set(cleaned_prog_ids)
                    mismatched = [
                        existing_pkg_map[pkg_id]['name']
                        for pkg_id in cleaned_pkg_ids
                        if str(existing_pkg_map[pkg_id]['program_id']) not in selected_prog_set
                    ]
                    if mismatched:
                        raise serializers.ValidationError(
                            f"The following package(s) do not belong to the selected programs: {', '.join(mismatched)}."
                        )

                value['package_ids'] = cleaned_pkg_ids
                if not value.get('package_id'):
                    value['package_id'] = cleaned_pkg_ids[0]
            else:
                value['package_ids'] = []
        elif value.get('package_id'):
            pkg_id = str(value['package_id']).strip()
            try:
                uuid.UUID(pkg_id)
            except (ValueError, TypeError):
                raise serializers.ValidationError(f"Invalid package ID format: '{pkg_id}'.")
            pkg = package_qs.filter(id=pkg_id, status='ACTIVE').first()
            if not pkg:
                raise serializers.ValidationError(f"Package '{pkg_id}' not found or inactive.")
            if cleaned_prog_ids and str(pkg.program_id) not in set(cleaned_prog_ids):
                raise serializers.ValidationError(f"Package '{pkg.name}' does not belong to the selected programs.")
            value['package_ids'] = [pkg_id]

        return value


class DiscountRuleConditionSerializer(serializers.ModelSerializer):
    class Meta:
        model = DiscountRuleCondition
        fields = [
            'id',
            'discount_eligibility_rule',
            'condition_type',
            'operator',
            'numeric_value',
            'text_value',
            'boolean_value',
            'reference_type',
            'reference_id',
            'sequence',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'discount_eligibility_rule', 'created_at', 'updated_at']


class DiscountRuleActionSerializer(serializers.ModelSerializer):
    class Meta:
        model = DiscountRuleAction
        fields = [
            'id',
            'discount_eligibility_rule',
            'action_type',
            'discount_campaign',
            'discount_code',
            'discount_percentage',
            'discount_amount',
            'maximum_discount',
            'message',
            'auto_apply',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'discount_eligibility_rule', 'created_at', 'updated_at']


class DiscountEligibilityRuleSerializer(serializers.ModelSerializer):
    conditions = DiscountRuleConditionSerializer(many=True, read_only=True)
    actions = DiscountRuleActionSerializer(many=True, read_only=True)
    branch_name = serializers.ReadOnlyField(source='branch.name')
    source_package_name = serializers.ReadOnlyField(source='source_package.name')
    target_package_name = serializers.ReadOnlyField(source='target_package.name')

    class Meta:
        model = DiscountEligibilityRule
        fields = [
            'id',
            'organization',
            'name',
            'description',
            'rule_type',
            'source_package',
            'source_package_name',
            'source_package_version',
            'target_package',
            'target_package_name',
            'target_package_version',
            'branch',
            'branch_name',
            'priority',
            'evaluation_mode',
            'rule_behavior',
            'valid_from',
            'valid_until',
            'status',
            'created_by_user',
            'conditions',
            'actions',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'organization', 'created_by_user', 'created_at', 'updated_at']

    def validate(self, attrs):
        branch = attrs.get('branch')
        if branch and getattr(branch, 'status', None) and branch.status != 'ACTIVE':
            raise serializers.ValidationError({"branch": f"Branch '{branch.name}' is inactive."})

        source_package = attrs.get('source_package')
        if source_package and getattr(source_package, 'status', None) and source_package.status != 'ACTIVE':
            raise serializers.ValidationError({"source_package": f"Source package '{source_package.name}' is inactive."})

        target_package = attrs.get('target_package')
        if target_package and getattr(target_package, 'status', None) and target_package.status != 'ACTIVE':
            raise serializers.ValidationError({"target_package": f"Target package '{target_package.name}' is inactive."})

        return attrs


class DiscountRedemptionSerializer(serializers.ModelSerializer):
    code_str = serializers.ReadOnlyField(source='discount_code.code')
    campaign_name = serializers.ReadOnlyField(source='campaign.name')
    member_name = serializers.SerializerMethodField()

    class Meta:
        model = DiscountRedemption
        fields = [
            'id',
            'discount_code',
            'code_str',
            'campaign',
            'campaign_name',
            'eligibility_rule',
            'user_profile',
            'member_name',
            'order',
            'discount_amount',
            'eligibility_snapshot',
            'redeemed_at',
            'created_at',
        ]
        read_only_fields = ['id', 'created_at']

    def get_member_name(self, obj) -> str:
        if obj.user_profile:
            first = obj.user_profile.first_name_snapshot or ''
            last = obj.user_profile.last_name_snapshot or ''
            return f"{first} {last}".strip() or str(obj.user_profile.id)
        return ''


class CouponValidationRequestSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=100)
    user_profile_id = serializers.UUIDField()
    order_subtotal = serializers.DecimalField(max_digits=14, decimal_places=2)
    branch_id = serializers.UUIDField(required=False, allow_null=True)
    package_id = serializers.UUIDField(required=False, allow_null=True)


class MemberOffersRequestSerializer(serializers.Serializer):
    user_profile_id = serializers.UUIDField()
    branch_id = serializers.UUIDField(required=False, allow_null=True)
    target_package_id = serializers.UUIDField(required=False, allow_null=True)
    current_package_id = serializers.UUIDField(required=False, allow_null=True)
    sessions_consumed = serializers.IntegerField(required=False, default=0)
    sessions_remaining = serializers.IntegerField(required=False, default=0)
    usage_percentage = serializers.DecimalField(max_digits=5, decimal_places=2, required=False, default=0)
    package_age_days = serializers.IntegerField(required=False, default=0)
