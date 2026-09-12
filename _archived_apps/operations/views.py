"""
Views for Trainers, Classes, Bookings, and Master Calendar Feed.
Enforces strict Tenant isolation across all operations.
"""

from rest_framework import viewsets, permissions, status, filters
from rest_framework.views import APIView
from rest_framework.decorators import action
from rest_framework.response import Response
from django.utils import timezone
from datetime import timedelta
from drf_spectacular.utils import extend_schema, OpenApiParameter

from .models import Trainer, FitnessClass, Booking, BookingStatus, BookingType
from .serializers import (
    TrainerSerializer,
    FitnessClassSerializer,
    BookingSerializer,
    CalendarEventSerializer,
)
from apps.members.models import MemberSubscription

class TrainerViewSet(viewsets.ModelViewSet):
    """
    CRUD ViewSet for Gym Trainers and Coaches.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = TrainerSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['user__first_name', 'user__last_name', 'specialization', 'certification']
    ordering_fields = ['rating', 'pt_hourly_rate']
    ordering = ['-rating']

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser and not user.tenant:
            return Trainer.objects.all().select_related('user', 'tenant')
        return Trainer.objects.filter(tenant=user.tenant).select_related('user', 'tenant')

    def perform_create(self, serializer):
        tenant = self.request.user.tenant
        trainer_id = serializer.validated_data.get('id')
        if not trainer_id:
            import uuid
            trainer_id = f"TRN-{uuid.uuid4().hex[:6].upper()}"
        serializer.save(id=trainer_id, tenant=tenant)


class FitnessClassViewSet(viewsets.ModelViewSet):
    """
    CRUD ViewSet for Scheduled Fitness Classes and Timetables.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = FitnessClassSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'category']
    ordering_fields = ['start_time', 'max_capacity']
    ordering = ['start_time']

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser and not user.tenant:
            qs = FitnessClass.objects.all()
        else:
            qs = FitnessClass.objects.filter(tenant=user.tenant)

        # Filters
        location_id = self.request.query_params.get('location')
        if location_id and location_id != 'all':
            qs = qs.filter(location_id=location_id)

        category = self.request.query_params.get('category')
        if category:
            qs = qs.filter(category=category)

        trainer_id = self.request.query_params.get('trainer')
        if trainer_id:
            qs = qs.filter(trainer_id=trainer_id)

        return qs.select_related('location', 'trainer__user', 'tenant').prefetch_related('bookings')

    def perform_create(self, serializer):
        tenant = self.request.user.tenant
        class_id = serializer.validated_data.get('id')
        if not class_id:
            import uuid
            class_id = f"CLS-{uuid.uuid4().hex[:6].upper()}"
        serializer.save(id=class_id, tenant=tenant)


class BookingViewSet(viewsets.ModelViewSet):
    """
    CRUD ViewSet for Class & Personal Training Bookings.
    Automatically decrements session balances for PT bookings.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = BookingSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['member__name', 'member__phone', 'notes']
    ordering_fields = ['scheduled_at', 'status']
    ordering = ['-scheduled_at']

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser and not user.tenant:
            qs = Booking.objects.all()
        else:
            qs = Booking.objects.filter(tenant=user.tenant)

        # Filters
        member_id = self.request.query_params.get('member')
        if member_id:
            qs = qs.filter(member_id=member_id)

        trainer_id = self.request.query_params.get('trainer')
        if trainer_id:
            qs = qs.filter(trainer_id=trainer_id)

        status_param = self.request.query_params.get('status')
        if status_param:
            qs = qs.filter(status=status_param)

        booking_type = self.request.query_params.get('type')
        if booking_type:
            qs = qs.filter(booking_type=booking_type)

        return qs.select_related('member', 'trainer__user', 'fitness_class', 'location', 'tenant')

    def perform_create(self, serializer):
        tenant = self.request.user.tenant
        booking_id = serializer.validated_data.get('id')
        if not booking_id:
            import uuid
            booking_id = f"BKG-{uuid.uuid4().hex[:6].upper()}"

        booking = serializer.save(id=booking_id, tenant=tenant)

        # If booking is Personal Training, decrement remaining sessions on member's active plan
        if booking.booking_type in [BookingType.PT, BookingType.PILATES]:
            active_sub = MemberSubscription.objects.filter(
                member=booking.member,
                tenant=tenant,
                status='Active'
            ).first()

            if active_sub and active_sub.sessions_remaining is not None and active_sub.sessions_remaining > 0:
                active_sub.sessions_remaining -= 1
                active_sub.save(update_fields=['sessions_remaining'])


class CalendarFeedView(APIView):
    """
    Unified master calendar feed endpoint for the Calendar View.
    Combines scheduled classes and PT bookings into a clean timeline.
    """
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="Master Operations Calendar Feed",
        description="Returns combined timeline of scheduled classes and 1-on-1 bookings.",
        responses={200: CalendarEventSerializer(many=True)}
    )
    def get(self, request):
        user = request.user
        tenant = user.tenant

        events = []

        # 1. Group Fitness Classes
        classes = FitnessClass.objects.filter(tenant=tenant, is_cancelled=False).select_related('trainer__user', 'location')
        for c in classes:
            events.append({
                'id': c.id,
                'title': c.name,
                'type': f"Class ({c.category})",
                'start': c.start_time,
                'end': c.end_time,
                'instructor': c.trainer.user.full_name if c.trainer else 'Unassigned',
                'location': c.location.name,
                'status': 'Scheduled',
                'spots': c.spots_remaining,
            })

        # 2. 1-on-1 PT Bookings
        bookings = Booking.objects.filter(tenant=tenant, status__in=['Confirmed', 'Attended']).select_related('member', 'trainer__user', 'location')
        for b in bookings:
            events.append({
                'id': b.id,
                'title': f"{b.booking_type} - {b.member.name}",
                'type': b.booking_type,
                'start': b.scheduled_at,
                'end': b.scheduled_at + timedelta(minutes=b.duration_minutes),
                'instructor': b.trainer.user.full_name if b.trainer else 'Staff',
                'location': b.location.name,
                'status': b.status,
                'spots': 0,
            })

        # Sort chronologically
        events.sort(key=lambda x: x['start'])
        return Response(events, status=status.HTTP_200_OK)
