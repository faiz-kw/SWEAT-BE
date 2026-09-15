"""
apps/tenant_core/serializers_privacy.py — Serializers for Privacy, Consent & DSR.
"""

from rest_framework import serializers
from .models_privacy import ProcessingPurpose, ConsentRecord, PrivacyRequest
from .models_users import TenantUser


class ProcessingPurposeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProcessingPurpose
        fields = [
            'id', 'code', 'name', 'description', 'notice_version',
            'is_mandatory', 'status', 'legal_basis', 'retention_days',
            'is_active', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class ConsentRecordSerializer(serializers.ModelSerializer):
    purpose_code = serializers.CharField(source='purpose.code', read_only=True)
    purpose_name = serializers.CharField(source='purpose.name', read_only=True)
    user_email = serializers.CharField(source='user.email', read_only=True)

    class Meta:
        model = ConsentRecord
        fields = [
            'id', 'user', 'user_email', 'purpose', 'purpose_code', 'purpose_name',
            'status', 'notice_version', 'granted_at', 'withdrawn_at',
            'capture_source', 'ip_address', 'proof_metadata', 'created_at',
        ]
        read_only_fields = ['id', 'created_at', 'withdrawn_at']

    def validate_user(self, value):
        if not value or value.status != 'ACTIVE':
            raise serializers.ValidationError("Cannot record consent for an inactive or nonexistent user.")
        return value


class ConsentWithdrawSerializer(serializers.Serializer):
    purpose_id = serializers.UUIDField(required=True)
    user_id = serializers.UUIDField(required=False)
    notes = serializers.CharField(required=False, allow_blank=True, default='')


class PrivacyRequestSerializer(serializers.ModelSerializer):
    user_email = serializers.CharField(source='user.email', read_only=True)
    assigned_to_email = serializers.CharField(source='assigned_to.email', read_only=True, allow_null=True)

    class Meta:
        model = PrivacyRequest
        fields = [
            'id', 'user', 'user_email', 'request_type', 'status',
            'identity_verified_at', 'assigned_to', 'assigned_to_email',
            'received_at', 'due_at', 'completed_at', 'resolution',
            'evidence', 'details', 'rejection_reason', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'status', 'received_at', 'due_at', 'completed_at',
            'resolution', 'evidence', 'created_at', 'updated_at',
        ]


class PrivacyRequestAdminActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=['execute', 'reject', 'assign', 'extend'])
    assigned_to_id = serializers.UUIDField(required=False)
    reason = serializers.CharField(required=False, allow_blank=True, default='')
    resolution = serializers.CharField(required=False, allow_blank=True, default='')
