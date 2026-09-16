"""
apps/tenant_core/serializers_discounts.py — Serializers for Layer 2 Module H: Discounts & Dynamic Offers
"""

from rest_framework import serializers
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
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class DiscountCampaignSerializer(serializers.ModelSerializer):
    codes = DiscountCodeSerializer(many=True, read_only=True)
    redemption_count = serializers.SerializerMethodField()

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
            'configuration',
            'codes',
            'redemption_count',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at']

    def get_redemption_count(self, obj) -> int:
        return obj.redemptions.count()


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
        read_only_fields = ['id', 'created_at', 'updated_at']


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
        read_only_fields = ['id', 'created_at', 'updated_at']


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
