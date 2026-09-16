from rest_framework import serializers
from .models_bookings import (
    BookingPolicySet,
    BookingCancellationRule,
    RewardRedemptionRule,
    AttendancePolicySet,
    AttendancePenaltyRule,
    MemberAttendanceState,
    AttendancePolicyEvent,
    Booking,
    BookingStatusHistory,
    BookingReschedule,
    BookingCancellation,
    BookingWaitlistEvent,
)
from .models_attendance import AttendanceRecord, AccessEvent


class BookingPolicySetSerializer(serializers.ModelSerializer):
    class Meta:
        model = BookingPolicySet
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class BookingCancellationRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = BookingCancellationRule
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class RewardRedemptionRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = RewardRedemptionRule
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class AttendancePolicySetSerializer(serializers.ModelSerializer):
    class Meta:
        model = AttendancePolicySet
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class AttendancePenaltyRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = AttendancePenaltyRule
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class MemberAttendanceStateSerializer(serializers.ModelSerializer):
    member_name = serializers.CharField(source='user_profile.full_name', read_only=True)
    member_email = serializers.CharField(source='user_profile.email', read_only=True)

    class Meta:
        model = MemberAttendanceState
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class AttendancePolicyEventSerializer(serializers.ModelSerializer):
    member_name = serializers.CharField(source='user_profile.full_name', read_only=True)

    class Meta:
        model = AttendancePolicyEvent
        fields = '__all__'
        read_only_fields = ['id', 'created_at']


class BookingStatusHistorySerializer(serializers.ModelSerializer):
    changed_by_name = serializers.CharField(source='changed_by_user.email', read_only=True)

    class Meta:
        model = BookingStatusHistory
        fields = '__all__'
        read_only_fields = ['id', 'created_at']


class BookingRescheduleSerializer(serializers.ModelSerializer):
    class Meta:
        model = BookingReschedule
        fields = '__all__'
        read_only_fields = ['id', 'created_at']


class BookingCancellationSerializer(serializers.ModelSerializer):
    class Meta:
        model = BookingCancellation
        fields = '__all__'
        read_only_fields = ['id', 'created_at']


class BookingWaitlistEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = BookingWaitlistEvent
        fields = '__all__'
        read_only_fields = ['id', 'created_at']


class BookingSerializer(serializers.ModelSerializer):
    member_name = serializers.CharField(source='user_profile.full_name', read_only=True)
    member_email = serializers.CharField(source='user_profile.email', read_only=True)
    class_name = serializers.CharField(source='occurrence.class_template.name', read_only=True)
    start_time = serializers.DateTimeField(source='occurrence.start_time', read_only=True)
    end_time = serializers.DateTimeField(source='occurrence.end_time', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    status_history = BookingStatusHistorySerializer(many=True, read_only=True)
    cancellations = BookingCancellationSerializer(many=True, read_only=True)
    waitlist_events = BookingWaitlistEventSerializer(many=True, read_only=True)

    class Meta:
        model = Booking
        fields = '__all__'
        read_only_fields = ['id', 'booking_number', 'booked_at', 'cancelled_at', 'completed_at', 'created_at', 'updated_at']


class AttendanceRecordSerializer(serializers.ModelSerializer):
    member_name = serializers.CharField(source='user_profile.full_name', read_only=True)
    member_email = serializers.CharField(source='user_profile.email', read_only=True)
    class_name = serializers.CharField(source='occurrence.class_template.name', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    booking_number = serializers.CharField(source='booking.booking_number', read_only=True)

    class Meta:
        model = AttendanceRecord
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class AccessEventSerializer(serializers.ModelSerializer):
    member_name = serializers.CharField(source='user_profile.full_name', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)

    class Meta:
        model = AccessEvent
        fields = '__all__'
        read_only_fields = ['id', 'created_at']
