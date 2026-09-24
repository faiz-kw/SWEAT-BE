from rest_framework import serializers
from .models_approvals import ApprovalRequest, ApprovalAction


class ApprovalActionSerializer(serializers.ModelSerializer):
    approver_email = serializers.CharField(source='approver_user.email', read_only=True)

    class Meta:
        model = ApprovalAction
        fields = '__all__'
        read_only_fields = ['id', 'acted_at', 'created_at']


class ApprovalRequestSerializer(serializers.ModelSerializer):
    requested_by_email = serializers.CharField(source='requested_by_user.email', read_only=True)
    actions = ApprovalActionSerializer(many=True, read_only=True)

    class Meta:
        model = ApprovalRequest
        fields = '__all__'
        read_only_fields = ['id', 'status', 'resolved_at', 'created_at', 'updated_at']
        extra_kwargs = {
            'organization': {'required': False},
            'requested_by_user': {'required': False},
        }
