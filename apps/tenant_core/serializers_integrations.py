"""
apps/tenant_core/serializers_integrations.py — Serializer for Tenant Integrations.
"""

from rest_framework import serializers
from .models_infra import Integration
from config.secrets import SecretResolver


class TenantIntegrationSerializer(serializers.ModelSerializer):
    """
    Serializer for tenant-level third-party integration configurations.
    Enforces strict credential security: raw credentials are never returned or stored in plaintext.
    """
    masked_secret_reference = serializers.SerializerMethodField()
    has_credentials = serializers.SerializerMethodField()

    class Meta:
        model = Integration
        fields = [
            'id', 'integration_type', 'provider', 'secret_reference',
            'masked_secret_reference', 'has_credentials', 'configuration',
            'status', 'last_sync_at', 'last_error', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'last_sync_at', 'last_error', 'created_at', 'updated_at']
        extra_kwargs = {
            'secret_reference': {'write_only': True, 'required': False},
        }

    def get_masked_secret_reference(self, obj) -> str:
        if not obj.secret_reference:
            return ''
        return SecretResolver.redact_reference(obj.secret_reference)

    def get_has_credentials(self, obj) -> bool:
        return bool(obj.secret_reference and obj.secret_reference.strip())

    def validate_secret_reference(self, value):
        if not value:
            return value
        v = value.strip()
        # Ensure it is a valid reference URI (not a raw plaintext password)
        valid_schemes = ('vault://', 'env://', 'aws-secretsmanager://', 'arn:aws:secretsmanager:')
        if not any(v.startswith(scheme) for scheme in valid_schemes):
            raise serializers.ValidationError(
                "Plaintext credentials are forbidden. Must provide a valid secret reference "
                "(e.g., 'vault://...', 'env://...', or 'aws-secretsmanager://...')."
            )
        return v
