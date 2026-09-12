"""
Serializers for Members, Membership Plans, Subscriptions, Attendance, and Client 360 view.
"""

from rest_framework import serializers
from django.utils import timezone
from datetime import timedelta
from drf_spectacular.utils import extend_schema_field, OpenApiTypes

from .models import (
    MembershipPlan,
    Member,
    MemberSubscription,
    Attendance,
    ReferralLedger,
    PlanCategory,
    MemberStatus,
    RiskLevel,
)
from apps.tenants.models import Location
from apps.users.models import User


class MembershipPlanSerializer(serializers.ModelSerializer):
    tenant_id = serializers.CharField(source='tenant.id', read_only=True)

    class Meta:
        model = MembershipPlan
        fields = [
            'id',
            'tenant_id',
            'name',
            'category',
            'duration_months',
            'price',
            'total_sessions',
            'is_active',
        ]
        read_only_fields = ['tenant_id']


class MemberSubscriptionSerializer(serializers.ModelSerializer):
    tenant_id = serializers.CharField(source='tenant.id', read_only=True)
    plan_name = serializers.CharField(source='plan.name', read_only=True)
    member_name = serializers.CharField(source='member.name', read_only=True)

    class Meta:
        model = MemberSubscription
        fields = [
            'id',
            'tenant_id',
            'member',
            'member_name',
            'plan',
            'plan_name',
            'start_date',
            'end_date',
            'sessions_remaining',
            'status',
            'amount_paid',
        ]
        read_only_fields = ['tenant_id', 'plan_name', 'member_name']


class AttendanceSerializer(serializers.ModelSerializer):
    tenant_id = serializers.CharField(source='tenant.id', read_only=True)
    member_name = serializers.CharField(source='member.name', read_only=True)
    location_name = serializers.CharField(source='location.name', read_only=True)

    class Meta:
        model = Attendance
        fields = [
            'id',
            'tenant_id',
            'member',
            'member_name',
            'location',
            'location_name',
            'check_in_time',
            'method',
        ]
        read_only_fields = ['tenant_id', 'member_name', 'location_name', 'check_in_time']


class ReferralLedgerSerializer(serializers.ModelSerializer):
    tenant_id = serializers.CharField(source='tenant.id', read_only=True)
    member_name = serializers.CharField(source='member.name', read_only=True)
    referred_member_name = serializers.CharField(source='referred_member.name', read_only=True, default='')

    class Meta:
        model = ReferralLedger
        fields = [
            'id',
            'tenant_id',
            'member',
            'member_name',
            'referred_member',
            'referred_member_name',
            'event_type',
            'points',
            'description',
            'created_at',
        ]
        read_only_fields = ['tenant_id', 'member_name', 'referred_member_name', 'created_at']


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


class MemberSerializer(serializers.ModelSerializer):
    id = serializers.CharField(required=False, allow_blank=True)
    location = SafeLocationField(queryset=Location.objects.all(), required=False, allow_null=True)
    tenant_id = serializers.CharField(source='tenant.id', read_only=True)
    location_name = serializers.CharField(source='location.name', read_only=True)
    primary_coach_name = serializers.CharField(source='primary_coach.full_name', read_only=True, default='')
    active_plan = serializers.SerializerMethodField()
    attendance_count_30d = serializers.SerializerMethodField()

    class Meta:
        model = Member
        fields = [
            'id',
            'tenant_id',
            'location',
            'location_name',
            'name',
            'phone',
            'email',
            'gender',
            'age',
            'status',
            'risk_level',
            'health_score',
            'performance_score',
            'fitness_goal',
            'emergency_contact',
            'from_lead_id',
            'primary_coach',
            'primary_coach_name',
            'joined_at',
            'active_plan',
            'attendance_count_30d',
        ]
        read_only_fields = [
            'tenant_id',
            'location_name',
            'primary_coach_name',
            'active_plan',
            'attendance_count_30d',
            'joined_at',
        ]

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_active_plan(self, obj):
        active_sub = obj.subscriptions.filter(status='Active').select_related('plan').first()
        if active_sub:
            return {
                'id': active_sub.id,
                'plan_name': active_sub.plan.name,
                'category': active_sub.plan.category,
                'end_date': active_sub.end_date,
                'sessions_remaining': active_sub.sessions_remaining,
                'status': active_sub.status,
            }
        return None

    @extend_schema_field(OpenApiTypes.INT)
    def get_attendance_count_30d(self, obj):
        thirty_days_ago = timezone.now() - timedelta(days=30)
        return obj.attendance_records.filter(check_in_time__gte=thirty_days_ago).count()


class MemberDetailSerializer(MemberSerializer):
    subscriptions = MemberSubscriptionSerializer(many=True, read_only=True)
    recent_attendance = AttendanceSerializer(source='attendance_records', many=True, read_only=True)
    referrals = ReferralLedgerSerializer(source='referral_entries', many=True, read_only=True)
    total_referral_points = serializers.SerializerMethodField()

    class Meta(MemberSerializer.Meta):
        fields = MemberSerializer.Meta.fields + [
            'subscriptions',
            'recent_attendance',
            'referrals',
            'total_referral_points',
        ]

    @extend_schema_field(OpenApiTypes.INT)
    def get_total_referral_points(self, obj):
        entries = obj.referral_entries.all()
        return sum(e.points for e in entries)
