from rest_framework import serializers
from .models_referrals import (
    ReferralProgram,
    ReferralIdentifier,
    Referral,
    ReferralQualificationRule,
    ReferralBenefitRule,
)
from .models_rewards import (
    RewardAccount,
    RewardLedger,
    OrderRewardRedemption,
)


class ReferralProgramSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReferralProgram
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class ReferralIdentifierSerializer(serializers.ModelSerializer):
    owner_email = serializers.CharField(source='owner_user.email', read_only=True)

    class Meta:
        model = ReferralIdentifier
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class ReferralSerializer(serializers.ModelSerializer):
    referrer_email = serializers.CharField(source='referrer_user.email', read_only=True)
    referred_user_email = serializers.CharField(source='referred_user.email', read_only=True, allow_null=True)
    program_name = serializers.CharField(source='referral_program.name', read_only=True)

    class Meta:
        model = Referral
        fields = '__all__'
        read_only_fields = ['id', 'registered_at', 'qualified_at', 'rewarded_at', 'created_at', 'updated_at']


class ReferralQualificationRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReferralQualificationRule
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class ReferralBenefitRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReferralBenefitRule
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class RewardAccountSerializer(serializers.ModelSerializer):
    user_name = serializers.CharField(source='user_profile.full_name', read_only=True)
    member_number = serializers.CharField(source='user_profile.member_number', read_only=True)

    class Meta:
        model = RewardAccount
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class RewardLedgerSerializer(serializers.ModelSerializer):
    user_name = serializers.CharField(source='user_profile.full_name', read_only=True)

    class Meta:
        model = RewardLedger
        fields = '__all__'
        read_only_fields = ['id', 'created_at']


class OrderRewardRedemptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderRewardRedemption
        fields = '__all__'
        read_only_fields = ['id', 'created_at']
