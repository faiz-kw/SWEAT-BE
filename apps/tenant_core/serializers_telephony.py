"""Serializers for Vobiz telephony webhooks."""

from rest_framework import serializers


class VobizDialEventSerializer(serializers.Serializer):
    """Fields Vobiz posts to a Dial callbackUrl. Unknown fields are ignored."""

    session_id = serializers.UUIDField(required=False)
    CallUUID = serializers.CharField(required=False, allow_blank=True, max_length=64)
    DialAction = serializers.CharField(required=False, allow_blank=True, max_length=32)
    DialBLegUUID = serializers.CharField(required=False, allow_blank=True, max_length=64)
    DialBLegStatus = serializers.CharField(required=False, allow_blank=True, max_length=32)
    DialBLegDuration = serializers.CharField(required=False, allow_blank=True, max_length=16)
    HangupCause = serializers.CharField(required=False, allow_blank=True, max_length=64)
    DialBLegHangupCause = serializers.CharField(required=False, allow_blank=True, max_length=64)
