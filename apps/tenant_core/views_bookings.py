from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404

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
from .models_classes import ClassOccurrence
from .models_crm import UserProfile
from .models_memberships import Membership
from .serializers_bookings import (
    BookingPolicySetSerializer,
    BookingCancellationRuleSerializer,
    RewardRedemptionRuleSerializer,
    AttendancePolicySetSerializer,
    AttendancePenaltyRuleSerializer,
    MemberAttendanceStateSerializer,
    AttendancePolicyEventSerializer,
    BookingSerializer,
    BookingStatusHistorySerializer,
    BookingRescheduleSerializer,
    BookingCancellationSerializer,
    BookingWaitlistEventSerializer,
    AttendanceRecordSerializer,
    AccessEventSerializer,
)
from .services_bookings import BookingWaitlistAttendanceService


class BookingViewSet(viewsets.ModelViewSet):
    serializer_class = BookingSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = Booking.objects.select_related('user_profile', 'occurrence', 'branch', 'membership').all()
        status_filter = self.request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        branch_id = self.request.query_params.get('branch_id')
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        occurrence_id = self.request.query_params.get('occurrence_id')
        if occurrence_id:
            qs = qs.filter(occurrence_id=occurrence_id)
        user_id = self.request.query_params.get('user_id')
        if user_id:
            qs = qs.filter(user_profile_id=user_id)
        return qs

    def create(self, request, *args, **kwargs):
        user_profile_id = request.data.get('user_profile')
        occurrence_id = request.data.get('occurrence')
        booking_type = request.data.get('booking_type', 'MEMBER')
        booking_source = request.data.get('booking_source', 'WEB')
        membership_id = request.data.get('membership')

        if not user_profile_id or not occurrence_id:
            return Response({'detail': 'user_profile and occurrence are required.'}, status=status.HTTP_400_BAD_REQUEST)

        user_profile = get_object_or_404(UserProfile, id=user_profile_id)
        occurrence = get_object_or_404(ClassOccurrence, id=occurrence_id)
        membership = None
        if membership_id:
            membership = get_object_or_404(Membership, id=membership_id)

        try:
            booking = BookingWaitlistAttendanceService.create_booking(
                user_profile=user_profile,
                occurrence=occurrence,
                booking_type=booking_type,
                booking_source=booking_source,
                membership=membership,
                created_by_user=request.user if hasattr(request.user, 'tenantuser') else None
            )
            serializer = self.get_serializer(booking)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        except ValidationError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='cancel')
    def cancel(self, request, pk=None):
        booking = self.get_object()
        reason_code = request.data.get('reason_code', 'MEMBER_REQUEST')
        reason_text = request.data.get('reason_text', '')

        try:
            cancellation = BookingWaitlistAttendanceService.cancel_booking(
                booking=booking,
                cancelled_by_user=request.user if hasattr(request.user, 'tenantuser') else None,
                reason_code=reason_code,
                reason_text=reason_text
            )
            serializer = BookingCancellationSerializer(cancellation)
            return Response(serializer.data, status=status.HTTP_200_OK)
        except ValidationError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='record-attendance')
    def record_attendance(self, request, pk=None):
        booking = self.get_object()
        att_status = request.data.get('status', 'PRESENT')
        check_in_method = request.data.get('check_in_method', 'MANUAL')

        record = BookingWaitlistAttendanceService.record_attendance(
            booking=booking,
            status=att_status,
            check_in_method=check_in_method,
            marked_by_user=request.user if hasattr(request.user, 'tenantuser') else None
        )
        serializer = AttendanceRecordSerializer(record)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='promote-waitlist')
    def promote_waitlist(self, request):
        occurrence_id = request.data.get('occurrence_id')
        if not occurrence_id:
            return Response({'detail': 'occurrence_id is required.'}, status=status.HTTP_400_BAD_REQUEST)
        occurrence = get_object_or_404(ClassOccurrence, id=occurrence_id)
        promoted = BookingWaitlistAttendanceService.auto_promote_from_waitlist(occurrence)
        if promoted:
            return Response(self.get_serializer(promoted).data, status=status.HTTP_200_OK)
        return Response({'detail': 'No waitlisted members available for promotion or capacity full.'}, status=status.HTTP_200_OK)


class BookingPolicySetViewSet(viewsets.ModelViewSet):
    serializer_class = BookingPolicySetSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return BookingPolicySet.objects.all()

    def perform_create(self, serializer):
        serializer.save()


class BookingCancellationRuleViewSet(viewsets.ModelViewSet):
    serializer_class = BookingCancellationRuleSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return BookingCancellationRule.objects.all()


class RewardRedemptionRuleViewSet(viewsets.ModelViewSet):
    serializer_class = RewardRedemptionRuleSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return RewardRedemptionRule.objects.all()


class AttendancePolicySetViewSet(viewsets.ModelViewSet):
    serializer_class = AttendancePolicySetSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return AttendancePolicySet.objects.all()


class AttendancePenaltyRuleViewSet(viewsets.ModelViewSet):
    serializer_class = AttendancePenaltyRuleSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return AttendancePenaltyRule.objects.all()


class MemberAttendanceStateViewSet(viewsets.ModelViewSet):
    serializer_class = MemberAttendanceStateSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return MemberAttendanceState.objects.select_related('user_profile', 'attendance_policy_set').all()

    @action(detail=True, methods=['post'], url_path='reset-restriction')
    def reset_restriction(self, request, pk=None):
        state = self.get_object()
        policy = state.attendance_policy_set
        state.booking_mode = 'NORMAL'
        state.current_max_advance_bookings = policy.normal_max_advance_bookings
        state.consecutive_no_show_count = 0
        state.save()

        AttendancePolicyEvent.objects.create(
            user_profile=state.user_profile,
            attendance_policy_set=policy,
            event_type='POLICY_RESET',
            new_booking_mode='NORMAL',
            reason=request.data.get('reason', 'Administrative reset'),
            triggered_by_type='ADMIN',
            triggered_by_user=request.user if hasattr(request.user, 'tenantuser') else None
        )

        return Response(self.get_serializer(state).data, status=status.HTTP_200_OK)


class AttendanceRecordViewSet(viewsets.ModelViewSet):
    serializer_class = AttendanceRecordSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = AttendanceRecord.objects.select_related('user_profile', 'occurrence', 'branch', 'booking').all()
        branch_id = self.request.query_params.get('branch_id')
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        att_status = self.request.query_params.get('status')
        if att_status:
            qs = qs.filter(status=att_status)
        return qs


class AccessEventViewSet(viewsets.ModelViewSet):
    serializer_class = AccessEventSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return AccessEvent.objects.select_related('user_profile', 'branch', 'booking').all()

    def create(self, request, *args, **kwargs):
        user_profile_id = request.data.get('user_profile')
        branch_id = request.data.get('branch')
        event_type = request.data.get('event_type', 'ENTRY')
        device_reference = request.data.get('device_reference')
        provider_reference = request.data.get('provider_reference')

        user_profile = get_object_or_404(UserProfile, id=user_profile_id)
        from .models_org import Branch
        branch = get_object_or_404(Branch, id=branch_id)

        event = BookingWaitlistAttendanceService.log_access_event(
            user_profile=user_profile,
            branch=branch,
            event_type=event_type,
            device_reference=device_reference,
            provider_reference=provider_reference
        )
        serializer = self.get_serializer(event)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
