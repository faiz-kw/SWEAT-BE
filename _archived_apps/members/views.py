"""
Views for Members, Membership Plans, Attendance, and Referral tracking.
Enforces multi-tenant isolation and handles Lead-to-Member conversions.
"""

from rest_framework import viewsets, permissions, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from drf_spectacular.utils import extend_schema
import uuid

from .models import MembershipPlan, Member, MemberSubscription, Attendance, ReferralLedger, ReferralEventType
from .serializers import (
    MembershipPlanSerializer,
    MemberSerializer,
    MemberDetailSerializer,
    MemberSubscriptionSerializer,
    AttendanceSerializer,
    ReferralLedgerSerializer,
)
from apps.tenants.models import Location
from apps.crm.models import Lead, LeadStage, LeadStatus


class MembershipPlanViewSet(viewsets.ModelViewSet):
    """
    CRUD ViewSet for Membership and Service Plans.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = MembershipPlanSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'category']
    ordering_fields = ['price', 'duration_months', 'name']
    ordering = ['price']

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser and not user.tenant:
            return MembershipPlan.objects.all()
        return MembershipPlan.objects.filter(tenant=user.tenant)

    def perform_create(self, serializer):
        tenant = self.request.user.tenant
        plan_id = serializer.validated_data.get('id') or f"PLN-{uuid.uuid4().hex[:6].upper()}"
        serializer.save(id=plan_id, tenant=tenant)


class ReferralLedgerViewSet(viewsets.ModelViewSet):
    """
    ViewSet for tracking member referrals and rewards.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ReferralLedgerSerializer
    filter_backends = [filters.OrderingFilter]
    ordering = ['-created_at']

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser and not user.tenant:
            return ReferralLedger.objects.all().select_related('member', 'referred_member')
        return ReferralLedger.objects.filter(tenant=user.tenant).select_related('member', 'referred_member')

    def perform_create(self, serializer):
        tenant = self.request.user.tenant
        ref_id = serializer.validated_data.get('id') or f"REF-{uuid.uuid4().hex[:6].upper()}"
        serializer.save(id=ref_id, tenant=tenant)


class MemberViewSet(viewsets.ModelViewSet):
    """
    CRUD ViewSet for Members with Client 360 view, attendance logging, and lead conversion.
    """
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'phone', 'email', 'fitness_goal']
    ordering_fields = ['name', 'joined_at', 'health_score', 'performance_score']
    ordering = ['-joined_at']

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return MemberDetailSerializer
        return MemberSerializer

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser and not user.tenant:
            qs = Member.objects.all()
        else:
            qs = Member.objects.filter(tenant=user.tenant)

        status_param = self.request.query_params.get('status')
        if status_param:
            qs = qs.filter(status=status_param)

        risk_param = self.request.query_params.get('risk_level')
        if risk_param:
            qs = qs.filter(risk_level=risk_param)

        location_param = self.request.query_params.get('location')
        if location_param and location_param != 'all':
            qs = qs.filter(location_id=location_param)

        return qs.select_related('location', 'primary_coach', 'tenant').prefetch_related(
            'subscriptions__plan',
            'attendance_records',
            'referral_entries'
        )

    def perform_create(self, serializer):
        from apps.tenants.models import Tenant, Location
        user = self.request.user
        tenant = user.tenant or Tenant.objects.first()
        location = serializer.validated_data.get('location')
        if not location:
            location = Location.objects.filter(tenant=tenant).first()
        mem_id = serializer.validated_data.get('id') or f"MEM-{uuid.uuid4().hex[:6].upper()}"
        member = serializer.save(id=mem_id, tenant=tenant, location=location)

        # Auto-convert linked Lead if from_lead_id is present
        from_lead_id = serializer.validated_data.get('from_lead_id')
        if from_lead_id:
            Lead.objects.filter(id=from_lead_id, tenant=tenant).update(
                stage=LeadStage.CONVERTED,
                status=LeadStatus.WON
            )

    @action(detail=True, methods=['post'], url_path='check-in')
    def check_in(self, request, pk=None):
        """
        Logs member attendance check-in.
        """
        member = self.get_object()
        method = request.data.get('method', 'Front Desk')
        location_id = request.data.get('location')
        if location_id:
            location = Location.objects.filter(id=location_id, tenant=member.tenant).first() or member.location
        else:
            location = member.location

        attendance = Attendance.objects.create(
            tenant=member.tenant,
            member=member,
            location=location,
            method=method,
        )
        return Response(AttendanceSerializer(attendance).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='referral-reward')
    def award_referral_reward(self, request, pk=None):
        """
        Awards referral / promotional reward points to this member.
        """
        member = self.get_object()
        points = int(request.data.get('points', 0))
        event_type = request.data.get('event_type', ReferralEventType.REFERRAL_SIGNUP)
        description = request.data.get('description', '')
        ref_id = f"REF-{uuid.uuid4().hex[:6].upper()}"
        entry = ReferralLedger.objects.create(
            id=ref_id,
            tenant=member.tenant,
            member=member,
            event_type=event_type,
            points=points,
            description=description,
        )
        return Response(ReferralLedgerSerializer(entry).data, status=status.HTTP_201_CREATED)
