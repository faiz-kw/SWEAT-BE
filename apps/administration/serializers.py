"""
Serializers for Administration models: Service, TenantSettings, CustomForm, ApiKey, AuditLog, and SecurityPolicy.
"""

from rest_framework import serializers
from .models import Service, TenantSettings, CustomForm, ApiKey, AuditLog, SecurityPolicy


class ServiceSerializer(serializers.ModelSerializer):
    location_name = serializers.CharField(source='location.name', read_only=True)

    class Meta:
        model = Service
        fields = [
            'id', 'tenant', 'location', 'location_name', 'name', 
            'category', 'description', 'duration_minutes', 'price', 
            'capacity', 'is_active', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'tenant', 'created_at', 'updated_at']


class TenantSettingsSerializer(serializers.ModelSerializer):
    timezone = serializers.CharField(source='tenant.timezone', required=False, allow_blank=True)

    class Meta:
        model = TenantSettings
        fields = [
            'id', 'tenant', 'currency', 'timezone', 'tax_rate_gst', 'tax_id_number',
            'booking_cancellation_window_hours', 'late_cancellation_fee',
            'allow_guest_passes', 'guest_passes_per_month',
            'membership_grace_period_days', 'allow_member_freeze',
            'max_freeze_days_per_year', 'business_open_time', 'business_close_time',
            'updated_at'
        ]
        read_only_fields = ['id', 'tenant', 'updated_at']

    def update(self, instance, validated_data):
        tenant_data = validated_data.pop('tenant', {})
        tz = tenant_data.get('timezone')
        if tz and instance.tenant:
            instance.tenant.timezone = tz
            instance.tenant.save(update_fields=['timezone'])
        return super().update(instance, validated_data)


class CustomFormSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomForm
        fields = [
            'id', 'tenant', 'title', 'code', 'description', 
            'is_mandatory', 'fields_schema', 'is_active', 
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'tenant', 'created_at', 'updated_at']


class ApiKeySerializer(serializers.ModelSerializer):
    raw_key = serializers.CharField(read_only=True)

    class Meta:
        model = ApiKey
        fields = [
            'id', 'tenant', 'name', 'key_prefix', 'raw_key',
            'permissions_scope', 'webhook_url', 'webhook_secret',
            'is_active', 'last_used_at', 'created_at'
        ]
        read_only_fields = ['id', 'tenant', 'key_prefix', 'created_at']


class AuditLogSerializer(serializers.ModelSerializer):
    user_name = serializers.CharField(source='user.full_name', read_only=True)

    class Meta:
        model = AuditLog
        fields = [
            'id', 'tenant', 'user', 'user_name', 'user_email', 
            'action', 'module', 'entity_type', 'entity_id', 
            'description', 'diff_payload', 'ip_address', 
            'user_agent', 'created_at'
        ]
        read_only_fields = ['id', 'tenant', 'created_at']


class SecurityPolicySerializer(serializers.ModelSerializer):
    class Meta:
        model = SecurityPolicy
        fields = [
            'id', 'tenant', 'enforce_mfa', 'session_timeout_minutes',
            'password_min_length', 'require_special_character',
            'max_failed_attempts_lockout', 'ip_whitelist', 'updated_at'
        ]
        read_only_fields = ['id', 'tenant', 'updated_at']
