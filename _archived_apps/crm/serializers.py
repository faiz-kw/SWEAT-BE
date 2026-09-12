"""
Serializers for CRM Leads and Activity Timeline.
"""

from rest_framework import serializers
from apps.tenants.models import Location
from .models import Lead, LeadActivity, LeadStage, LeadStatus, LeadSource, ActivityType


class LeadActivitySerializer(serializers.ModelSerializer):
    tenant_id = serializers.CharField(source='tenant.id', read_only=True)
    performed_by_name = serializers.CharField(source='performed_by.full_name', read_only=True, default='')

    class Meta:
        model = LeadActivity
        fields = [
            'id',
            'tenant_id',
            'lead',
            'activity_type',
            'summary',
            'performed_by',
            'performed_by_name',
            'created_at',
        ]
        read_only_fields = ['id', 'tenant_id', 'performed_by_name', 'created_at']


class SafeLocationField(serializers.PrimaryKeyRelatedField):
    """Gracefully falls back to the first available location if an invalid ID (e.g. LOC-001) is provided."""
    def to_internal_value(self, data):
        try:
            return super().to_internal_value(data)
        except serializers.ValidationError:
            first_loc = self.get_queryset().first()
            if first_loc:
                return first_loc
            raise


class LeadSerializer(serializers.ModelSerializer):
    id = serializers.CharField(required=False, allow_blank=True)
    location = SafeLocationField(queryset=Location.objects.all(), required=False, allow_null=True)
    tenant_id = serializers.CharField(source='tenant.id', read_only=True)
    location_name = serializers.CharField(source='location.name', read_only=True)
    assigned_to_name = serializers.CharField(source='assigned_to.full_name', read_only=True, default='')
    activities = LeadActivitySerializer(many=True, read_only=True)

    class Meta:
        model = Lead
        fields = [
            'id',
            'tenant_id',
            'location',
            'location_name',
            'name',
            'phone',
            'email',
            'source',
            'interested_service',
            'goal',
            'assigned_to',
            'assigned_to_name',
            'stage',
            'status',
            'score',
            'budget',
            'notes',
            'last_contact_at',
            'next_follow_up_at',
            'trial_date',
            'activities',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['tenant_id', 'location_name', 'assigned_to_name', 'activities', 'created_at', 'updated_at']
