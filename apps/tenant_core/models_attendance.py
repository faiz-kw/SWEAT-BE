import uuid
from django.db import models
from django.utils import timezone
from .models_users import TenantUser
from .models_org import Branch
from .models_crm import UserProfile
from .models_classes import ClassOccurrence
from .models_bookings import Booking


class AttendanceRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking = models.ForeignKey(Booking, on_delete=models.PROTECT, null=True, blank=True, related_name='attendance_records')
    user_profile = models.ForeignKey(UserProfile, on_delete=models.PROTECT, related_name='attendance_records')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='attendance_records')
    occurrence = models.ForeignKey(ClassOccurrence, on_delete=models.PROTECT, null=True, blank=True, related_name='attendance_records')
    status = models.CharField(
        max_length=20,
        default='PRESENT',
        choices=[
            ('PRESENT', 'PRESENT'),
            ('ABSENT', 'ABSENT'),
            ('NO_SHOW', 'NO_SHOW'),
            ('LATE', 'LATE'),
            ('MANUAL', 'MANUAL'),
        ]
    )
    check_in_status = models.CharField(
        max_length=20,
        default='SUCCESSFUL',
        choices=[
            ('SUCCESSFUL', 'SUCCESSFUL'),
            ('FAILED', 'FAILED'),
            ('NOT_ATTEMPTED', 'NOT_ATTEMPTED'),
            ('MANUAL', 'MANUAL'),
        ]
    )
    check_in_method = models.CharField(
        max_length=30,
        default='QR',
        choices=[
            ('BARCODE', 'BARCODE'),
            ('QR', 'QR'),
            ('ACCESS_DEVICE', 'ACCESS_DEVICE'),
            ('MOBILE', 'MOBILE'),
            ('FRONT_DESK', 'FRONT_DESK'),
            ('ADMIN', 'ADMIN'),
            ('FACE_LIVENESS', 'FACE_LIVENESS'),
        ]
    )
    check_in_at = models.DateTimeField(null=True, blank=True)
    check_out_at = models.DateTimeField(null=True, blank=True)
    attendance_completed = models.BooleanField(default=False)
    completion_verified_at = models.DateTimeField(null=True, blank=True)
    no_show_evaluated_at = models.DateTimeField(null=True, blank=True)
    marked_by_user = models.ForeignKey(TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='marked_attendances')

    # Geofencing & Biometric Anti-Spoofing Verification
    trainer_latitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    trainer_longitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    trainer_accuracy_meters = models.FloatField(null=True, blank=True)
    is_within_geofence = models.BooleanField(default=True)
    geofence_distance_meters = models.FloatField(null=True, blank=True)
    face_verified = models.BooleanField(default=False)
    liveness_score = models.FloatField(null=True, blank=True)
    liveness_method = models.CharField(max_length=50, blank=True, default='')
    trainer_selfie_url = models.TextField(blank=True, default='')
    liveness_challenges_passed = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'attendance_records'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['booking']),
            models.Index(fields=['user_profile', 'created_at']),
            models.Index(fields=['occurrence', 'status']),
        ]

    def __str__(self):
        return f"Attendance {self.user_profile} ({self.status})"


class AccessEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_profile = models.ForeignKey(UserProfile, on_delete=models.PROTECT, related_name='access_events')
    booking = models.ForeignKey(Booking, on_delete=models.PROTECT, null=True, blank=True, related_name='access_events')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='access_events')
    event_type = models.CharField(
        max_length=20,
        default='ENTRY',
        choices=[
            ('ENTRY', 'ENTRY'),
            ('EXIT', 'EXIT'),
            ('DENIED', 'DENIED'),
        ]
    )
    direction = models.CharField(
        max_length=10,
        default='ENTRY',
        choices=[
            ('ENTRY', 'ENTRY'),
            ('EXIT', 'EXIT'),
        ]
    )
    reconciliation_status = models.CharField(
        max_length=40,
        default='NORMAL',
        choices=[
            ('NORMAL', 'NORMAL'),
            ('OFFLINE_OVERRIDE', 'OFFLINE_OVERRIDE'),
            ('REPLAY_ATTEMPT', 'REPLAY_ATTEMPT'),
            ('UNAUTHORIZED', 'UNAUTHORIZED'),
            ('EXPIRED', 'EXPIRED'),
        ]
    )
    token_hash = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    device_reference = models.CharField(max_length=255, null=True, blank=True)
    event_at = models.DateTimeField(default=timezone.now)
    provider_reference = models.CharField(max_length=255, null=True, blank=True)
    raw_reference = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'access_events'
        ordering = ['-event_at']
        indexes = [
            models.Index(fields=['user_profile', 'event_at']),
            models.Index(fields=['branch', 'event_at']),
            models.Index(fields=['provider_reference']),
            models.Index(fields=['token_hash']),
            models.Index(fields=['reconciliation_status']),
        ]

    def __str__(self):
        return f"Access {self.event_type} ({self.direction}) - {self.user_profile} at {self.event_at}"


class AccessPass(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_profile = models.ForeignKey(UserProfile, on_delete=models.PROTECT, related_name='access_passes')
    booking = models.OneToOneField(Booking, on_delete=models.CASCADE, null=True, blank=True, related_name='access_pass')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='access_passes')
    token_hash = models.CharField(max_length=64, db_index=True)
    pass_type = models.CharField(
        max_length=30,
        default='CLASS_BOOKING',
        choices=[
            ('CLASS_BOOKING', 'CLASS_BOOKING'),
            ('STUDIO_ACCESS', 'STUDIO_ACCESS'),
        ]
    )
    valid_from = models.DateTimeField(default=timezone.now)
    valid_until = models.DateTimeField()
    status = models.CharField(
        max_length=30,
        default='ACTIVE',
        choices=[
            ('ACTIVE', 'ACTIVE'),
            ('CONSUMED_ENTERED', 'CONSUMED_ENTERED'),
            ('CONSUMED_EXITED', 'CONSUMED_EXITED'),
            ('CANCELLED', 'CANCELLED'),
            ('EXPIRED', 'EXPIRED'),
        ]
    )
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'access_passes'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['branch', 'status', 'valid_from']),
            models.Index(fields=['token_hash', 'status']),
            models.Index(fields=['user_profile', 'status']),
        ]

    def __str__(self):
        return f"AccessPass {self.pass_type} ({self.status}) for {self.user_profile}"
