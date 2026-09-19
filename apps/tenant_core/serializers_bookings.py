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
    member_name = serializers.SerializerMethodField()
    member_email = serializers.SerializerMethodField()

    class Meta:
        model = MemberAttendanceState
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_member_name(self, obj):
        if not obj.user_profile:
            return 'Unknown'
        name = f"{obj.user_profile.first_name_snapshot or ''} {obj.user_profile.last_name_snapshot or ''}".strip()
        if not name and obj.user_profile.user:
            name = obj.user_profile.user.full_name or obj.user_profile.user.email
        return name or 'Unknown'

    def get_member_email(self, obj):
        return obj.user_profile.user.email if (obj.user_profile and obj.user_profile.user) else None


class AttendancePolicyEventSerializer(serializers.ModelSerializer):
    member_name = serializers.SerializerMethodField()

    class Meta:
        model = AttendancePolicyEvent
        fields = '__all__'
        read_only_fields = ['id', 'created_at']

    def get_member_name(self, obj):
        if not obj.user_profile:
            return 'Unknown'
        name = f"{obj.user_profile.first_name_snapshot or ''} {obj.user_profile.last_name_snapshot or ''}".strip()
        if not name and obj.user_profile.user:
            name = obj.user_profile.user.full_name or obj.user_profile.user.email
        return name or 'Unknown'


class BookingStatusHistorySerializer(serializers.ModelSerializer):
    changed_by_name = serializers.CharField(source='changed_by_user.email', read_only=True)

    class Meta:
        model = BookingStatusHistory
        fields = '__all__'
        read_only_fields = ['id', 'created_at']


class BookingRescheduleSerializer(serializers.ModelSerializer):
    from_class_name = serializers.CharField(source='from_occurrence.class_template.name', read_only=True)
    to_class_name = serializers.CharField(source='to_occurrence.class_template.name', read_only=True)
    from_occurrence_date = serializers.DateField(source='from_occurrence.occurrence_date', read_only=True)
    to_occurrence_date = serializers.DateField(source='to_occurrence.occurrence_date', read_only=True)
    rescheduled_by_name = serializers.CharField(source='rescheduled_by_user.email', read_only=True)

    class Meta:
        model = BookingReschedule
        fields = '__all__'
        read_only_fields = ['id', 'created_at']


class BookingCancellationSerializer(serializers.ModelSerializer):
    cancelled_by_name = serializers.CharField(source='cancelled_by_user.email', read_only=True)

    class Meta:
        model = BookingCancellation
        fields = '__all__'
        read_only_fields = ['id', 'created_at']


class BookingWaitlistEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = BookingWaitlistEvent
        fields = '__all__'
        read_only_fields = ['id', 'created_at']

class BookingListSerializer(serializers.ModelSerializer):
    """Lightweight list serializer — no nested prefetch, fast for table views."""
    user_profile_name = serializers.SerializerMethodField()
    user_profile_email = serializers.SerializerMethodField()
    occurrence_title = serializers.CharField(source='occurrence.class_template.name', read_only=True)
    occurrence_date = serializers.DateField(source='occurrence.occurrence_date', read_only=True)
    occurrence_start_at = serializers.DateTimeField(source='occurrence.start_at', read_only=True)
    occurrence_end_at = serializers.DateTimeField(source='occurrence.end_at', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    package_name = serializers.CharField(source='membership.package.name', read_only=True, allow_null=True)

    class Meta:
        model = Booking
        fields = [
            'id', 'booking_number', 'status', 'booking_type', 'booking_source',
            'waitlist_position', 'booked_at', 'cancelled_at', 'completed_at',
            'user_profile', 'user_profile_name', 'user_profile_email',
            'occurrence', 'occurrence_title', 'occurrence_date', 'occurrence_start_at', 'occurrence_end_at',
            'branch', 'branch_name',
            'membership', 'package_name',
            'entitlement',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'booking_number', 'booked_at', 'cancelled_at', 'completed_at', 'created_at', 'updated_at']

    def get_user_profile_name(self, obj):
        if not obj.user_profile:
            return 'Unknown'
        name = f"{obj.user_profile.first_name_snapshot or ''} {obj.user_profile.last_name_snapshot or ''}".strip()
        if not name and obj.user_profile.user:
            name = obj.user_profile.user.full_name or obj.user_profile.user.email
        return name or 'Unknown'

    def get_user_profile_email(self, obj):
        return obj.user_profile.user.email if (obj.user_profile and obj.user_profile.user) else None


class BookingSerializer(serializers.ModelSerializer):
    member_name = serializers.SerializerMethodField()
    member_email = serializers.SerializerMethodField()
    member_phone = serializers.SerializerMethodField()
    member_number = serializers.SerializerMethodField()
    class_name = serializers.CharField(source='occurrence.class_template.name', read_only=True)
    occurrence_date = serializers.DateField(source='occurrence.occurrence_date', read_only=True)
    start_at = serializers.DateTimeField(source='occurrence.start_at', read_only=True)
    end_at = serializers.DateTimeField(source='occurrence.end_at', read_only=True)
    start_time = serializers.DateTimeField(source='occurrence.start_at', read_only=True)
    end_time = serializers.DateTimeField(source='occurrence.end_at', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    package_name = serializers.CharField(source='membership.package.name', read_only=True, allow_null=True)
    entitlement_name = serializers.CharField(source='entitlement.entitlement_type', read_only=True, allow_null=True)
    reschedules = BookingRescheduleSerializer(many=True, read_only=True)
    status_history = BookingStatusHistorySerializer(many=True, read_only=True)
    cancellations = BookingCancellationSerializer(many=True, read_only=True)
    waitlist_events = BookingWaitlistEventSerializer(many=True, read_only=True)

    class Meta:
        model = Booking
        fields = '__all__'
        read_only_fields = ['id', 'booking_number', 'booked_at', 'cancelled_at', 'completed_at', 'created_at', 'updated_at']

    def get_member_name(self, obj):
        if not obj.user_profile:
            return 'Unknown'
        name = f"{obj.user_profile.first_name_snapshot or ''} {obj.user_profile.last_name_snapshot or ''}".strip()
        if not name and obj.user_profile.user:
            name = obj.user_profile.user.full_name or obj.user_profile.user.email
        return name or 'Unknown'

    def get_member_email(self, obj):
        return obj.user_profile.user.email if (obj.user_profile and obj.user_profile.user) else None

    def get_member_phone(self, obj):
        return obj.user_profile.user.phone if (obj.user_profile and obj.user_profile.user) else None

    def get_member_number(self, obj):
        return obj.user_profile.member_number if obj.user_profile else None


class AttendanceRecordSerializer(serializers.ModelSerializer):
    member_name = serializers.SerializerMethodField()
    member_email = serializers.SerializerMethodField()
    class_name = serializers.CharField(source='occurrence.class_template.name', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    booking_number = serializers.CharField(source='booking.booking_number', read_only=True)

    class Meta:
        model = AttendanceRecord
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_member_name(self, obj):
        if not obj.user_profile:
            return 'Unknown'
        name = f"{obj.user_profile.first_name_snapshot or ''} {obj.user_profile.last_name_snapshot or ''}".strip()
        if not name and obj.user_profile.user:
            name = obj.user_profile.user.full_name or obj.user_profile.user.email
        return name or 'Unknown'

    def get_member_email(self, obj):
        return obj.user_profile.user.email if (obj.user_profile and obj.user_profile.user) else None


class AccessEventSerializer(serializers.ModelSerializer):
    member_name = serializers.SerializerMethodField()
    branch_name = serializers.CharField(source='branch.name', read_only=True)

    class Meta:
        model = AccessEvent
        fields = '__all__'
        read_only_fields = ['id', 'created_at']

    def get_member_name(self, obj):
        if not obj.user_profile:
            return 'Unknown'
        name = f"{obj.user_profile.first_name_snapshot or ''} {obj.user_profile.last_name_snapshot or ''}".strip()
        if not name and obj.user_profile.user:
            name = obj.user_profile.user.full_name or obj.user_profile.user.email
        return name or 'Unknown'

