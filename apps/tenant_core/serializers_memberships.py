"""
apps/tenant_core/serializers_memberships.py — Serializers for Layer 2 Module I: Memberships & Entitlements
"""

from rest_framework import serializers
from .models_memberships import (
    Membership,
    MembershipContractSnapshot,
    MembershipEntitlement,
    MembershipEntitlementLedger,
    MembershipBranchHistory,
    MembershipStatusHistory,
    MembershipFreeze,
    MembershipRenewalPolicy,
    MembershipChangePolicy,
    MembershipChangePolicyRule,
    MembershipChangeRequest,
    MembershipPackageHistory,
)


class MembershipContractSnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = MembershipContractSnapshot
        fields = [
            'id',
            'membership',
            'package',
            'package_version',
            'package_price',
            'package_name_snapshot',
            'purchase_price',
            'discount_amount',
            'tax_amount',
            'final_amount',
            'currency',
            'duration_value',
            'duration_unit',
            'start_date',
            'end_date',
            'entitlements_snapshot',
            'class_access_snapshot',
            'purchase_branch',
            'terms_document_version_ids',
            'applicable_policy_versions',
            'source_order',
            'source_order_item',
            'created_at',
        ]
        read_only_fields = ['id', 'created_at']


class MembershipEntitlementLedgerSerializer(serializers.ModelSerializer):
    class Meta:
        model = MembershipEntitlementLedger
        fields = [
            'id',
            'membership_entitlement',
            'transaction_type',
            'units',
            'booking_id',
            'reference_type',
            'reference_id',
            'reason_code',
            'reason_text',
            'balance_after',
            'created_by_user',
            'created_at',
        ]
        read_only_fields = ['id', 'created_at']


class MembershipEntitlementSerializer(serializers.ModelSerializer):
    remaining_units = serializers.ReadOnlyField()

    class Meta:
        model = MembershipEntitlement
        fields = [
            'id',
            'membership',
            'source_definition',
            'entitlement_type',
            'reference_type',
            'reference_id',
            'allocated_units',
            'consumed_units',
            'remaining_units',
            'is_unlimited',
            'valid_from',
            'valid_until',
            'status',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class MembershipBranchHistorySerializer(serializers.ModelSerializer):
    from_branch_name = serializers.ReadOnlyField(source='from_branch.name')
    to_branch_name = serializers.ReadOnlyField(source='to_branch.name')

    class Meta:
        model = MembershipBranchHistory
        fields = [
            'id',
            'membership',
            'from_branch',
            'from_branch_name',
            'to_branch',
            'to_branch_name',
            'change_type',
            'reason',
            'effective_at',
            'changed_by_user',
            'created_at',
        ]
        read_only_fields = ['id', 'created_at']


class MembershipStatusHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = MembershipStatusHistory
        fields = [
            'id',
            'membership',
            'from_status',
            'to_status',
            'reason_code',
            'reason_text',
            'changed_by_user',
            'changed_at',
            'created_at',
        ]
        read_only_fields = ['id', 'created_at']


class MembershipFreezeSerializer(serializers.ModelSerializer):
    class Meta:
        model = MembershipFreeze
        fields = [
            'id',
            'membership',
            'freeze_from',
            'freeze_until',
            'reason_code',
            'reason_text',
            'extend_membership_days',
            'status',
            'approved_by_user',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class MembershipRenewalPolicySerializer(serializers.ModelSerializer):
    package_name = serializers.ReadOnlyField(source='package.name')

    class Meta:
        model = MembershipRenewalPolicy
        fields = [
            'id',
            'package',
            'package_name',
            'package_version',
            'version_number',
            'renewal_pricing_mode',
            'renewal_entitlement_mode',
            'grace_days',
            'configuration',
            'effective_from',
            'effective_until',
            'status',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class MembershipChangePolicyRuleSerializer(serializers.ModelSerializer):
    target_package_name = serializers.ReadOnlyField(source='target_package.name')

    class Meta:
        model = MembershipChangePolicyRule
        fields = [
            'id',
            'membership_change_policy',
            'change_type',
            'rule_name',
            'min_membership_age_days',
            'max_membership_age_days',
            'min_remaining_days',
            'max_remaining_days',
            'min_remaining_sessions',
            'notice_period_days',
            'effective_mode',
            'pricing_mode',
            'unused_session_handling',
            'validity_handling',
            'refund_mode',
            'cancellation_fee_type',
            'cancellation_fee_value',
            'requires_payment',
            'requires_manager_approval',
            'requires_member_confirmation',
            'requires_terms_acceptance',
            'target_package',
            'target_package_name',
            'target_package_version',
            'priority',
            'configuration',
            'status',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class MembershipChangePolicySerializer(serializers.ModelSerializer):
    rules = MembershipChangePolicyRuleSerializer(many=True, read_only=True)

    class Meta:
        model = MembershipChangePolicy
        fields = [
            'id',
            'organization',
            'branch',
            'program',
            'package',
            'package_version',
            'policy_name',
            'version_number',
            'rule_behavior',
            'effective_from',
            'effective_until',
            'status',
            'created_by_user',
            'rules',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'organization', 'created_by_user', 'created_at', 'updated_at']


class MembershipChangeRequestSerializer(serializers.ModelSerializer):
    current_package_name = serializers.ReadOnlyField(source='current_package.name')
    target_package_name = serializers.ReadOnlyField(source='target_package.name')

    class Meta:
        model = MembershipChangeRequest
        fields = [
            'id',
            'membership',
            'membership_change_policy',
            'membership_change_policy_rule',
            'policy_version_number',
            'change_type',
            'current_package',
            'current_package_name',
            'current_package_version',
            'target_package',
            'target_package_name',
            'target_package_version',
            'effective_mode_applied',
            'pricing_mode_applied',
            'remaining_sessions_snapshot',
            'remaining_days_snapshot',
            'original_remaining_value',
            'credit_amount',
            'refund_amount',
            'penalty_amount',
            'additional_amount',
            'final_amount_payable',
            'source_order',
            'status',
            'requested_by_user',
            'approved_by_user',
            'requested_at',
            'approved_at',
            'applied_at',
            'reason',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class MembershipPackageHistorySerializer(serializers.ModelSerializer):
    from_package_name = serializers.ReadOnlyField(source='from_package.name')
    to_package_name = serializers.ReadOnlyField(source='to_package.name')

    class Meta:
        model = MembershipPackageHistory
        fields = [
            'id',
            'membership',
            'from_package',
            'from_package_name',
            'from_package_version',
            'to_package',
            'to_package_name',
            'to_package_version',
            'change_type',
            'effective_at',
            'order',
            'membership_change_request',
            'changed_by_user',
            'reason',
            'created_at',
        ]
        read_only_fields = ['id', 'created_at']


class MembershipSerializer(serializers.ModelSerializer):
    member_name = serializers.SerializerMethodField()
    package_name = serializers.ReadOnlyField(source='package.name')
    home_branch_name = serializers.ReadOnlyField(source='home_branch.name')
    purchase_branch_name = serializers.ReadOnlyField(source='purchase_branch.name')
    contract_snapshot = MembershipContractSnapshotSerializer(read_only=True)
    entitlements = MembershipEntitlementSerializer(many=True, read_only=True)

    class Meta:
        model = Membership
        fields = [
            'id',
            'user_profile',
            'member_name',
            'program',
            'package',
            'package_name',
            'package_version',
            'package_price',
            'source_order',
            'source_order_item',
            'purchase_branch',
            'purchase_branch_name',
            'home_branch',
            'home_branch_name',
            'membership_number',
            'start_date',
            'end_date',
            'status',
            'activated_at',
            'cancelled_at',
            'legacy_reference',
            'contract_snapshot',
            'entitlements',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'membership_number', 'activated_at', 'cancelled_at', 'created_at', 'updated_at']

    def get_member_name(self, obj) -> str:
        if obj.user_profile:
            first = obj.user_profile.first_name_snapshot or ''
            last = obj.user_profile.last_name_snapshot or ''
            return f"{first} {last}".strip() or str(obj.user_profile.id)
        return ''


class ActivateMembershipRequestSerializer(serializers.Serializer):
    order_id = serializers.UUIDField()
    order_item_id = serializers.UUIDField()
    start_date = serializers.DateField(required=False, allow_null=True)


class ConsumeEntitlementRequestSerializer(serializers.Serializer):
    entitlement_type = serializers.CharField(max_length=50)
    units = serializers.DecimalField(max_digits=12, decimal_places=2, default=1.00)
    booking_id = serializers.UUIDField(required=False, allow_null=True)
    reason_text = serializers.CharField(required=False, allow_blank=True, default='')


class FreezeRequestSerializer(serializers.Serializer):
    freeze_from = serializers.DateField()
    freeze_until = serializers.DateField()
    reason_text = serializers.CharField(required=False, allow_blank=True, default='')
