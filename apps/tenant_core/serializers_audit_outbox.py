"""
apps/tenant_core/serializers_audit_outbox.py — Serializers for Layer 2 Module N
"""

from rest_framework import serializers
from .models_audit_outbox import BusinessAuditEvent, IdempotencyRecord, DomainOutboxEvent, ReasonCode


class BusinessAuditEventSerializer(serializers.ModelSerializer):
    actor_email = serializers.EmailField(source='actor_user.email', read_only=True)

    class Meta:
        model = BusinessAuditEvent
        fields = [
            'id', 'organization', 'branch', 'actor_type', 'actor_user', 'actor_email',
            'actor_employee_profile_id', 'source_channel', 'module', 'action_code',
            'entity_type', 'entity_id', 'parent_entity_type', 'parent_entity_id',
            'event_description', 'before_data', 'after_data', 'metadata',
            'ip_address', 'user_agent', 'request_id', 'session_id',
            'occurred_at', 'created_at',
        ]
        read_only_fields = fields


class IdempotencyRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = IdempotencyRecord
        fields = [
            'id', 'organization', 'idempotency_key', 'operation_type',
            'actor_user', 'request_hash', 'resource_type', 'resource_id',
            'status', 'response_snapshot', 'expires_at', 'created_at', 'updated_at',
        ]
        read_only_fields = fields


class DomainOutboxEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = DomainOutboxEvent
        fields = [
            'id', 'organization', 'event_type', 'aggregate_type', 'aggregate_id',
            'payload', 'status', 'attempt_count', 'next_attempt_at', 'published_at',
            'last_error', 'created_at', 'updated_at',
        ]
        read_only_fields = fields


class ReasonCodeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReasonCode
        fields = [
            'id', 'organization', 'module', 'action_type', 'code', 'label',
            'description', 'requires_comment', 'status', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
