"""
Serializers for Trainers, Classes, Bookings, and Master Calendar feed.
"""

from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field, OpenApiTypes
from .models import Trainer, FitnessClass, Booking, BookingType, BookingStatus
from apps.tenants.models import Location
from apps.members.models import Member

class TrainerSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source='user.full_name', read_only=True)
    email = serializers.EmailField(source='user.email', read_only=True)
    phone = serializers.CharField(source='user.phone', read_only=True)
    tenant_id = serializers.CharField(source='tenant.id', read_only=True)

    class Meta:
        model = Trainer
        fields = [
            'id',
            'tenant_id',
            'user',
            'name',
            'email',
            'phone',
            'specialization',
            'certification',
            'rating',
            'pt_hourly_rate',
            'is_available',
        ]
        read_only_fields = ['tenant_id']


class FitnessClassSerializer(serializers.ModelSerializer):
    location_name = serializers.CharField(source='location.name', read_only=True)
    location_city = serializers.CharField(source='location.city', read_only=True)
    trainer_name = serializers.CharField(source='trainer.user.full_name', read_only=True, default='')
    booked_count = serializers.IntegerField(read_only=True)
    spots_remaining = serializers.IntegerField(read_only=True)
    tenant_id = serializers.CharField(source='tenant.id', read_only=True)

    class Meta:
        model = FitnessClass
        fields = [
            'id',
            'tenant_id',
            'location',
            'location_name',
            'location_city',
            'name',
            'category',
            'trainer',
            'trainer_name',
            'start_time',
            'end_time',
            'max_capacity',
            'booked_count',
            'spots_remaining',
            'is_cancelled',
        ]
        read_only_fields = ['tenant_id', 'booked_count', 'spots_remaining']


class BookingSerializer(serializers.ModelSerializer):
    member_name = serializers.CharField(source='member.name', read_only=True)
    member_phone = serializers.CharField(source='member.phone', read_only=True)
    class_name = serializers.CharField(source='fitness_class.name', read_only=True, default='')
    trainer_name = serializers.CharField(source='trainer.user.full_name', read_only=True, default='')
    location_name = serializers.CharField(source='location.name', read_only=True)
    tenant_id = serializers.CharField(source='tenant.id', read_only=True)
    qr_access_token = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = [
            'id',
            'tenant_id',
            'member',
            'member_name',
            'member_phone',
            'booking_type',
            'fitness_class',
            'class_name',
            'trainer',
            'trainer_name',
            'location',
            'location_name',
            'scheduled_at',
            'duration_minutes',
            'status',
            'qr_access_token',
            'notes',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['tenant_id', 'qr_access_token', 'created_at', 'updated_at']

    @extend_schema_field(OpenApiTypes.STR)
    def get_qr_access_token(self, obj):
        try:
            return obj.get_qr_access_token()
        except Exception:
            return ''



class CalendarEventSerializer(serializers.Serializer):
    id = serializers.CharField()
    title = serializers.CharField()
    type = serializers.CharField()
    start = serializers.DateTimeField()
    end = serializers.DateTimeField()
    instructor = serializers.CharField()
    location = serializers.CharField()
    status = serializers.CharField()
    spots = serializers.IntegerField(required=False)
