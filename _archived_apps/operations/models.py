"""
Operations & Scheduling Models for PerformanceOS (Phase 3).
Provides Trainer, FitnessClass, and Booking models with tenant isolation.
"""

from django.db import models
from apps.tenants.models import TenantAwareModel, Location
from apps.users.models import User
from apps.members.models import Member

class Trainer(TenantAwareModel):
    """
    Coach or fitness instructor profile linked to a User account.
    """
    id = models.CharField(
        max_length=64,
        primary_key=True,
        help_text="Trainer ID (e.g. TRN-001)"
    )
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='trainer_profile'
    )
    specialization = models.CharField(
        max_length=128,
        help_text="e.g. Strength & Conditioning, Reformer Pilates, Hypertrophy"
    )
    certification = models.CharField(max_length=255, blank=True, default='')
    rating = models.DecimalField(max_digits=3, decimal_places=2, default=4.90)
    pt_hourly_rate = models.DecimalField(max_digits=10, decimal_places=2, default=1500.00)
    is_available = models.BooleanField(default=True)

    class Meta:
        db_table = 'trainers'
        ordering = ['-rating']

    def __str__(self):
        return f"{self.user.full_name} ({self.specialization})"

    @property
    def name(self):
        return self.user.full_name


class ClassCategory(models.TextChoices):
    STRENGTH = 'Strength', 'Strength'
    PILATES = 'Pilates', 'Pilates'
    HIIT = 'HIIT', 'HIIT'
    YOGA = 'Yoga', 'Yoga'
    CONDITIONING = 'Conditioning', 'Conditioning'


class FitnessClass(TenantAwareModel):
    """
    Scheduled group fitness class session or master timetable entry.
    """
    id = models.CharField(
        max_length=64,
        primary_key=True,
        help_text="Class ID (e.g. CLS-001)"
    )
    location = models.ForeignKey(
        Location,
        on_delete=models.CASCADE,
        related_name='classes'
    )
    name = models.CharField(max_length=255, help_text="e.g. Morning Strength Circuit")
    category = models.CharField(
        max_length=64,
        choices=ClassCategory.choices,
        default=ClassCategory.STRENGTH
    )
    trainer = models.ForeignKey(
        Trainer,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='assigned_classes'
    )
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    max_capacity = models.IntegerField(default=15)
    is_cancelled = models.BooleanField(default=False)

    class Meta:
        db_table = 'fitness_classes'
        ordering = ['start_time']

    def __str__(self):
        return f"{self.name} at {self.start_time.strftime('%Y-%m-%d %H:%M')}"

    @property
    def booked_count(self):
        return self.bookings.filter(status__in=['Confirmed', 'Attended']).count()

    @property
    def spots_remaining(self):
        return max(0, self.max_capacity - self.booked_count)


class BookingType(models.TextChoices):
    CLASS = 'Class', 'Class'
    PT = 'Personal Training', 'Personal Training'
    PILATES = 'Pilates', 'Pilates'
    ASSESSMENT = 'Assessment', 'Assessment'


class BookingStatus(models.TextChoices):
    CONFIRMED = 'Confirmed', 'Confirmed'
    ATTENDED = 'Attended', 'Attended'
    CANCELLED = 'Cancelled', 'Cancelled'
    NO_SHOW = 'No-Show', 'No-Show'


class Booking(TenantAwareModel):
    """
    Session reservation for a group class or 1-on-1 personal training.
    """
    id = models.CharField(
        max_length=64,
        primary_key=True,
        help_text="Booking ID (e.g. BKG-001)"
    )
    member = models.ForeignKey(
        Member,
        on_delete=models.CASCADE,
        related_name='bookings'
    )
    booking_type = models.CharField(
        max_length=32,
        choices=BookingType.choices,
        default=BookingType.PT
    )
    fitness_class = models.ForeignKey(
        FitnessClass,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='bookings'
    )
    trainer = models.ForeignKey(
        Trainer,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='pt_bookings'
    )
    location = models.ForeignKey(
        Location,
        on_delete=models.CASCADE,
        related_name='bookings'
    )
    scheduled_at = models.DateTimeField()
    duration_minutes = models.IntegerField(default=60)
    status = models.CharField(
        max_length=32,
        choices=BookingStatus.choices,
        default=BookingStatus.CONFIRMED
    )
    notes = models.TextField(blank=True, default='')

    class Meta:
        db_table = 'ops_bookings'
        ordering = ['-scheduled_at']

    def __str__(self):
        return f"{self.booking_type} - {self.member.name} ({self.status})"

    def get_qr_access_token(self) -> str:
        """
        Generates a time-boxed QR credential valid from 15 min before to 30 min after session.
        """
        from datetime import timedelta
        from apps.integrations.adapters.access_control_adapter import AccessControlAdapter

        valid_from = (self.scheduled_at - timedelta(minutes=15)).isoformat()
        valid_until = (self.scheduled_at + timedelta(minutes=self.duration_minutes + 30)).isoformat()

        adapter = AccessControlAdapter()
        return adapter.generate_qr_credential(
            member_id=self.member_id,
            booking_id=self.id,
            valid_from=valid_from,
            valid_until=valid_until
        )

