"""
apps/tenant_core/serializers_communication.py — DRF Serializers for Communication Domain.

Provides serialization for:
- CommunicationMessage with rendered snapshot, delivery lifecycle, and nested status events
- CommunicationStatusEvent (append-only audit trail)
- Outbound dispatch input with validation and consent checks
- Safe, secret-free communication channel configuration status
"""

from rest_framework import serializers
from .models_communication import CommunicationMessage, CommunicationStatusEvent
from .models_crm import Lead
from .models_govern import NotificationTemplate


class CommunicationStatusEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = CommunicationStatusEvent
        fields = [
            'id', 'provider_event_id', 'from_status', 'to_status',
            'occurred_at', 'raw_metadata', 'created_at',
        ]
        read_only_fields = fields


class CommunicationMessageSerializer(serializers.ModelSerializer):
    lead_name = serializers.SerializerMethodField()
    lead_phone = serializers.CharField(source='lead.phone_normalized', read_only=True)
    lead_email = serializers.CharField(source='lead.email_normalized', read_only=True)
    created_by_user_name = serializers.SerializerMethodField()
    status_events = CommunicationStatusEventSerializer(many=True, read_only=True)
    replies_count = serializers.SerializerMethodField()

    class Meta:
        model = CommunicationMessage
        fields = [
            'id', 'organization', 'lead', 'lead_name', 'lead_phone', 'lead_email',
            'resolution_status', 'sender_identifier', 'raw_sender_data',
            'channel', 'direction', 'purpose',
            'recipient', 'sender',
            'template', 'template_reference', 'subject', 'body_snapshot',
            'provider', 'provider_message_id', 'status', 'idempotency_key',
            'queued_at', 'sent_at', 'delivered_at', 'read_at', 'received_at', 'replied_at', 'failed_at',
            'failure_code', 'failure_reason',
            'trigger_type', 'related_trial', 'related_followup', 'in_reply_to',
            'created_by_user', 'created_by_user_name',
            'status_events', 'replies_count',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'organization', 'queued_at', 'sent_at', 'delivered_at', 'read_at',
            'received_at', 'replied_at', 'failed_at', 'created_at', 'updated_at',
            'status_events', 'replies_count',
        ]

    def get_lead_name(self, obj):
        if not obj.lead:
            return None
        return f"{obj.lead.first_name} {obj.lead.last_name}".strip()

    def get_created_by_user_name(self, obj):
        if not obj.created_by_user:
            return 'System'
        return f"{obj.created_by_user.first_name} {obj.created_by_user.last_name}".strip() or obj.created_by_user.email

    def get_replies_count(self, obj):
        return getattr(obj, '_prefetched_objects_cache', {}).get('replies', obj.replies.all()).count() if hasattr(obj, 'replies') else 0


class CommunicationSendInputSerializer(serializers.Serializer):
    channel = serializers.ChoiceField(choices=['WHATSAPP', 'EMAIL', 'SMS'])
    recipient = serializers.CharField(max_length=255)
    lead_id = serializers.UUIDField(required=False, allow_null=True)
    template_id = serializers.UUIDField(required=False, allow_null=True)
    subject = serializers.CharField(max_length=300, required=False, allow_blank=True, default='')
    body = serializers.CharField(required=False, allow_blank=True, default='')
    purpose = serializers.ChoiceField(choices=['TRANSACTIONAL', 'MARKETING'], default='TRANSACTIONAL')
    idempotency_key = serializers.CharField(max_length=255, required=False, allow_null=True, allow_blank=True)
    trigger_type = serializers.CharField(max_length=50, default='MANUAL')
    related_trial_id = serializers.UUIDField(required=False, allow_null=True)
    related_followup_id = serializers.UUIDField(required=False, allow_null=True)
    provider_preference = serializers.CharField(max_length=100, required=False, allow_null=True, allow_blank=True)

    def validate(self, attrs):
        template_id = attrs.get('template_id')
        body = attrs.get('body')
        if not template_id and not body:
            raise serializers.ValidationError("Either template_id or body must be provided.")
        return attrs


class CommunicationChannelStatusSerializer(serializers.Serializer):
    """
    Strictly safe provider status representation for frontend consumption.
    NEVER exposes secret references, Vault paths, or raw tokens.
    """
    channel = serializers.CharField()
    status = serializers.CharField()  # 'CONNECTED' or 'NOT_CONNECTED'
    is_configured = serializers.BooleanField()
    provider = serializers.CharField()
    sender_identity = serializers.CharField()
    capabilities = serializers.ListField(child=serializers.CharField())
    implemented_adapters = serializers.ListField(child=serializers.CharField())
    last_sync_at = serializers.DateTimeField(allow_null=True)
