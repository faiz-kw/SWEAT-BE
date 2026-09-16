import uuid
from django.db import models
from django.utils import timezone
from .models_users import TenantUser
from .models_org import Organization, Branch
from .models_catalog import Program, Package
from .models_classes import ClassTemplate, ClassOccurrence
from .models_crm import UserProfile
from .models_memberships import Membership, MembershipEntitlement


class BookingPolicySet(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name='booking_policy_sets')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, null=True, blank=True, related_name='booking_policy_sets')
    program = models.ForeignKey(Program, on_delete=models.PROTECT, null=True, blank=True, related_name='booking_policy_sets')
    class_template = models.ForeignKey(ClassTemplate, on_delete=models.PROTECT, null=True, blank=True, related_name='booking_policy_sets')
    occurrence = models.ForeignKey(ClassOccurrence, on_delete=models.PROTECT, null=True, blank=True, related_name='booking_policy_sets')
    version_number = models.IntegerField(default=1)
    rule_behavior = models.CharField(
        max_length=20,
        default='OPERATIONAL',
        choices=[('CONTRACTUAL', 'CONTRACTUAL'), ('OPERATIONAL', 'OPERATIONAL'), ('TRANSACTIONAL', 'TRANSACTIONAL')]
    )
    effective_from = models.DateTimeField(default=timezone.now)
    effective_until = models.DateTimeField(null=True, blank=True)
    max_upcoming_bookings = models.IntegerField(default=3)
    booking_open_minutes_before = models.IntegerField(null=True, blank=True)
    booking_close_minutes_before = models.IntegerField(null=True, blank=True)
    allow_waitlist = models.BooleanField(default=False)
    waitlist_capacity = models.IntegerField(null=True, blank=True)
    waitlist_close_minutes_before = models.IntegerField(null=True, blank=True)
    waitlist_auto_cancel_minutes_before = models.IntegerField(null=True, blank=True)
    auto_waitlist_promotion = models.BooleanField(default=True)
    waitlist_promotion_mode = models.CharField(
        max_length=20,
        default='FIFO',
        choices=[('FIFO', 'FIFO'), ('PRIORITY', 'PRIORITY'), ('MANUAL', 'MANUAL')]
    )
    waitlist_confirmation_required = models.BooleanField(default=False)
    waitlist_confirmation_minutes = models.IntegerField(null=True, blank=True)
    max_reschedules = models.IntegerField(null=True, blank=True)
    late_entry_minutes = models.IntegerField(null=True, blank=True)
    allow_trial = models.BooleanField(default=False)
    max_trial_bookings = models.IntegerField(null=True, blank=True)
    allow_cross_branch = models.BooleanField(default=False)
    require_parq = models.BooleanField(default=False)
    status = models.CharField(
        max_length=20,
        default='ACTIVE',
        choices=[('DRAFT', 'DRAFT'), ('SCHEDULED', 'SCHEDULED'), ('ACTIVE', 'ACTIVE'), ('RETIRED', 'RETIRED')]
    )
    created_by_user = models.ForeignKey(TenantUser, on_delete=models.PROTECT, null=True, blank=True, related_name='created_booking_policy_sets')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'booking_policy_sets'
        ordering = ['-created_at']

    def __str__(self):
        return f"BookingPolicySet v{self.version_number} ({self.status})"


class BookingCancellationRule(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking_policy_set = models.ForeignKey(BookingPolicySet, on_delete=models.PROTECT, null=True, blank=True, related_name='cancellation_rules')
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name='cancellation_rules')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, null=True, blank=True, related_name='cancellation_rules')
    program = models.ForeignKey(Program, on_delete=models.PROTECT, null=True, blank=True, related_name='cancellation_rules')
    class_template = models.ForeignKey(ClassTemplate, on_delete=models.PROTECT, null=True, blank=True, related_name='cancellation_rules')
    rule_name = models.CharField(max_length=200)
    min_minutes_before = models.IntegerField(null=True, blank=True)
    max_minutes_before = models.IntegerField(null=True, blank=True)
    cancellation_type = models.CharField(
        max_length=30,
        choices=[
            ('EARLY_CANCEL', 'EARLY_CANCEL'),
            ('LATE_CANCEL_REWARDED', 'LATE_CANCEL_REWARDED'),
            ('LAST_MINUTE_CANCEL', 'LAST_MINUTE_CANCEL'),
            ('TREATED_AS_NO_SHOW', 'TREATED_AS_NO_SHOW'),
        ]
    )
    session_action = models.CharField(
        max_length=20,
        choices=[('RESTORE', 'RESTORE'), ('CONSUME', 'CONSUME'), ('NO_ACTION', 'NO_ACTION')]
    )
    reward_type = models.CharField(
        max_length=20,
        default='NONE',
        choices=[('NONE', 'NONE'), ('POINTS', 'POINTS'), ('CREDIT', 'CREDIT')]
    )
    reward_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    priority = models.IntegerField(default=100)
    valid_from = models.DateTimeField(default=timezone.now)
    valid_until = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, default='ACTIVE', choices=[('ACTIVE', 'ACTIVE'), ('INACTIVE', 'INACTIVE')])
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'booking_cancellation_rules'
        ordering = ['priority', '-created_at']

    def __str__(self):
        return f"{self.rule_name} ({self.cancellation_type})"


class RewardRedemptionRule(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name='reward_redemption_rules')
    reward_type = models.CharField(max_length=20, default='POINTS', choices=[('POINTS', 'POINTS'), ('CREDIT', 'CREDIT')])
    currency = models.CharField(max_length=3, default='USD')
    unit_conversion_rate = models.DecimalField(max_digits=14, decimal_places=4, default=1.0)
    max_wallet_percentage = models.DecimalField(max_digits=6, decimal_places=3, default=100.0)
    max_order_percentage = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)
    minimum_balance = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    minimum_redemption = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    maximum_redemption = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    allowed_order_type = models.CharField(
        max_length=30,
        default='ALL',
        choices=[
            ('NEW_MEMBERSHIP', 'NEW_MEMBERSHIP'),
            ('RENEWAL', 'RENEWAL'),
            ('EXTENSION', 'EXTENSION'),
            ('UPGRADE', 'UPGRADE'),
            ('REJOIN', 'REJOIN'),
            ('ALL', 'ALL'),
        ]
    )
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, null=True, blank=True, related_name='reward_redemption_rules')
    package = models.ForeignKey(Package, on_delete=models.PROTECT, null=True, blank=True, related_name='reward_redemption_rules')
    valid_from = models.DateTimeField(default=timezone.now)
    valid_until = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, default='ACTIVE', choices=[('ACTIVE', 'ACTIVE'), ('INACTIVE', 'INACTIVE')])
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'reward_redemption_rules'
        ordering = ['-created_at']

    def __str__(self):
        return f"RewardRedemptionRule {self.reward_type} ({self.status})"


class AttendancePolicySet(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name='attendance_policy_sets')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, null=True, blank=True, related_name='attendance_policy_sets')
    program = models.ForeignKey(Program, on_delete=models.PROTECT, null=True, blank=True, related_name='attendance_policy_sets')
    class_template = models.ForeignKey(ClassTemplate, on_delete=models.PROTECT, null=True, blank=True, related_name='attendance_policy_sets')
    name = models.CharField(max_length=200)
    version_number = models.IntegerField(default=1)
    rule_behavior = models.CharField(
        max_length=20,
        default='OPERATIONAL',
        choices=[('CONTRACTUAL', 'CONTRACTUAL'), ('OPERATIONAL', 'OPERATIONAL'), ('TRANSACTIONAL', 'TRANSACTIONAL')]
    )
    effective_from = models.DateTimeField(default=timezone.now)
    effective_until = models.DateTimeField(null=True, blank=True)
    normal_max_advance_bookings = models.IntegerField(default=3)
    restricted_max_advance_bookings = models.IntegerField(default=1)
    restriction_threshold = models.IntegerField(default=2)
    reset_on_successful_attendance = models.BooleanField(default=True)
    no_show_mark_after_minutes = models.IntegerField(default=0)
    status = models.CharField(
        max_length=20,
        default='ACTIVE',
        choices=[('DRAFT', 'DRAFT'), ('SCHEDULED', 'SCHEDULED'), ('ACTIVE', 'ACTIVE'), ('RETIRED', 'RETIRED')]
    )
    created_by_user = models.ForeignKey(TenantUser, on_delete=models.PROTECT, null=True, blank=True, related_name='created_attendance_policy_sets')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'attendance_policy_sets'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} v{self.version_number} ({self.status})"


class AttendancePenaltyRule(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    attendance_policy_set = models.ForeignKey(AttendancePolicySet, on_delete=models.PROTECT, related_name='penalty_rules')
    rule_code = models.CharField(
        max_length=40,
        choices=[
            ('FIRST_NO_SHOW', 'FIRST_NO_SHOW'),
            ('CONSECUTIVE_NO_SHOW', 'CONSECUTIVE_NO_SHOW'),
            ('RESTRICTED_NO_SHOW', 'RESTRICTED_NO_SHOW'),
            ('CUSTOM', 'CUSTOM'),
        ]
    )
    sequence_from = models.IntegerField(null=True, blank=True)
    sequence_to = models.IntegerField(null=True, blank=True)
    base_session_units = models.DecimalField(max_digits=12, decimal_places=2, default=1.0)
    additional_penalty_units = models.DecimalField(max_digits=12, decimal_places=2, default=0.0)
    max_total_units = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    restore_booked_session = models.BooleanField(default=False)
    reward_type = models.CharField(
        max_length=20,
        default='NONE',
        choices=[('NONE', 'NONE'), ('POINTS', 'POINTS'), ('CREDIT', 'CREDIT')]
    )
    reward_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    priority = models.IntegerField(default=100)
    status = models.CharField(max_length=20, default='ACTIVE', choices=[('ACTIVE', 'ACTIVE'), ('INACTIVE', 'INACTIVE')])
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'attendance_penalty_rules'
        ordering = ['priority', '-created_at']

    def __str__(self):
        return f"{self.rule_code} (Units: {self.base_session_units}+{self.additional_penalty_units})"


class MemberAttendanceState(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_profile = models.ForeignKey(UserProfile, on_delete=models.PROTECT, related_name='attendance_states')
    attendance_policy_set = models.ForeignKey(AttendancePolicySet, on_delete=models.PROTECT, related_name='member_states')
    consecutive_no_show_count = models.IntegerField(default=0)
    active_no_show_count = models.IntegerField(default=0)
    booking_mode = models.CharField(
        max_length=20,
        default='NORMAL',
        choices=[('NORMAL', 'NORMAL'), ('SINGLE_BOOKING', 'SINGLE_BOOKING')]
    )
    current_max_advance_bookings = models.IntegerField(default=3)
    restriction_started_at = models.DateTimeField(null=True, blank=True)
    restriction_reason = models.TextField(null=True, blank=True)
    last_no_show_at = models.DateTimeField(null=True, blank=True)
    last_successful_attendance_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, default='ACTIVE', choices=[('ACTIVE', 'ACTIVE'), ('INACTIVE', 'INACTIVE')])
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'member_attendance_states'
        unique_together = ('user_profile', 'attendance_policy_set')
        ordering = ['-updated_at']

    def __str__(self):
        return f"{self.user_profile} Attendance State ({self.booking_mode})"


class Booking(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking_number = models.CharField(max_length=100, unique=True)
    user_profile = models.ForeignKey(UserProfile, on_delete=models.PROTECT, related_name='bookings')
    membership = models.ForeignKey(Membership, on_delete=models.PROTECT, null=True, blank=True, related_name='bookings')
    entitlement = models.ForeignKey(MembershipEntitlement, on_delete=models.PROTECT, null=True, blank=True, related_name='bookings')
    occurrence = models.ForeignKey(ClassOccurrence, on_delete=models.PROTECT, related_name='bookings')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='bookings')
    booking_type = models.CharField(
        max_length=30,
        default='MEMBER',
        choices=[
            ('MEMBER', 'MEMBER'),
            ('TRIAL', 'TRIAL'),
            ('WALK_IN', 'WALK_IN'),
            ('COMPLIMENTARY', 'COMPLIMENTARY'),
            ('PAY_PER_USE', 'PAY_PER_USE'),
        ]
    )
    booking_source = models.CharField(
        max_length=30,
        default='WEB',
        choices=[
            ('WEB', 'WEB'),
            ('MOBILE_APP', 'MOBILE_APP'),
            ('FRONT_DESK', 'FRONT_DESK'),
            ('ADMIN', 'ADMIN'),
            ('KIOSK', 'KIOSK'),
            ('QR', 'QR'),
            ('API', 'API'),
            ('MIGRATION', 'MIGRATION'),
            ('OTHER', 'OTHER'),
        ]
    )
    status = models.CharField(
        max_length=30,
        default='CONFIRMED',
        choices=[
            ('RESERVED', 'RESERVED'),
            ('WAITLISTED', 'WAITLISTED'),
            ('CONFIRMED', 'CONFIRMED'),
            ('CANCELLED', 'CANCELLED'),
            ('RESCHEDULED', 'RESCHEDULED'),
            ('COMPLETED', 'COMPLETED'),
            ('NO_SHOW', 'NO_SHOW'),
        ]
    )
    waitlist_position = models.IntegerField(null=True, blank=True)
    booked_at = models.DateTimeField(default=timezone.now)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_by_user = models.ForeignKey(TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_bookings')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'bookings'
        ordering = ['-booked_at']
        indexes = [
            models.Index(fields=['occurrence', 'status']),
            models.Index(fields=['user_profile', 'status', 'booked_at']),
            models.Index(fields=['branch', 'status', 'booked_at']),
        ]

    def __str__(self):
        return f"Booking {self.booking_number} ({self.status})"


class BookingStatusHistory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking = models.ForeignKey(Booking, on_delete=models.PROTECT, related_name='status_history')
    from_status = models.CharField(max_length=30, null=True, blank=True)
    to_status = models.CharField(max_length=30)
    reason_code = models.CharField(max_length=100, null=True, blank=True)
    reason_text = models.TextField(null=True, blank=True)
    changed_by_user = models.ForeignKey(TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='booking_status_changes')
    changed_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'booking_status_history'
        ordering = ['-changed_at']


class BookingReschedule(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking = models.ForeignKey(Booking, on_delete=models.PROTECT, related_name='reschedules')
    from_occurrence = models.ForeignKey(ClassOccurrence, on_delete=models.PROTECT, related_name='reschedules_from')
    to_occurrence = models.ForeignKey(ClassOccurrence, on_delete=models.PROTECT, related_name='reschedules_to')
    reschedule_number = models.IntegerField(default=1)
    reason_code = models.CharField(max_length=100, null=True, blank=True)
    reason_text = models.TextField(null=True, blank=True)
    rescheduled_by_user = models.ForeignKey(TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='booking_reschedules')
    rescheduled_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'booking_reschedules'
        ordering = ['booking', 'reschedule_number']


class BookingCancellation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking = models.ForeignKey(Booking, on_delete=models.PROTECT, related_name='cancellations')
    cancelled_by_user = models.ForeignKey(TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='cancelled_bookings')
    reason_code = models.CharField(max_length=100)
    reason_text = models.TextField(null=True, blank=True)
    minutes_before_class = models.IntegerField()
    booking_cancellation_rule = models.ForeignKey(BookingCancellationRule, on_delete=models.PROTECT, null=True, blank=True, related_name='cancellations')
    attendance_policy_set = models.ForeignKey(AttendancePolicySet, on_delete=models.PROTECT, null=True, blank=True, related_name='cancellations')
    treated_as_no_show = models.BooleanField(default=False)
    session_action_applied = models.CharField(
        max_length=20,
        default='RESTORE',
        choices=[('RESTORE', 'RESTORE'), ('CONSUME', 'CONSUME'), ('NO_ACTION', 'NO_ACTION')]
    )
    session_units_applied = models.DecimalField(max_digits=12, decimal_places=2, default=0.0)
    reward_value_applied = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    reward_ledger = models.ForeignKey('RewardLedger', on_delete=models.PROTECT, null=True, blank=True, related_name='booking_cancellations')
    within_cutoff = models.BooleanField(default=False)
    cancelled_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'booking_cancellations'
        ordering = ['-cancelled_at']


class BookingWaitlistEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking = models.ForeignKey(Booking, on_delete=models.PROTECT, related_name='waitlist_events')
    occurrence = models.ForeignKey(ClassOccurrence, on_delete=models.PROTECT, related_name='waitlist_events')
    event_type = models.CharField(
        max_length=40,
        choices=[
            ('JOINED', 'JOINED'),
            ('POSITION_CHANGED', 'POSITION_CHANGED'),
            ('PROMOTION_OFFERED', 'PROMOTION_OFFERED'),
            ('PROMOTED', 'PROMOTED'),
            ('PROMOTION_ACCEPTED', 'PROMOTION_ACCEPTED'),
            ('PROMOTION_EXPIRED', 'PROMOTION_EXPIRED'),
            ('AUTO_CANCELLED', 'AUTO_CANCELLED'),
            ('MANUALLY_CANCELLED', 'MANUALLY_CANCELLED'),
        ]
    )
    old_position = models.IntegerField(null=True, blank=True)
    new_position = models.IntegerField(null=True, blank=True)
    reason = models.TextField(null=True, blank=True)
    triggered_by_type = models.CharField(max_length=20, default='SYSTEM', choices=[('SYSTEM', 'SYSTEM'), ('USER', 'USER'), ('ADMIN', 'ADMIN')])
    triggered_by_user = models.ForeignKey(TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='triggered_waitlist_events')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'booking_waitlist_events'
        ordering = ['-created_at']


class AttendancePolicyEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_profile = models.ForeignKey(UserProfile, on_delete=models.PROTECT, related_name='attendance_events')
    booking = models.ForeignKey(Booking, on_delete=models.PROTECT, null=True, blank=True, related_name='attendance_policy_events')
    attendance_record = models.ForeignKey('AttendanceRecord', on_delete=models.PROTECT, null=True, blank=True, related_name='policy_events')
    attendance_policy_set = models.ForeignKey(AttendancePolicySet, on_delete=models.PROTECT, related_name='policy_events')
    attendance_penalty_rule = models.ForeignKey(AttendancePenaltyRule, on_delete=models.PROTECT, null=True, blank=True, related_name='policy_events')
    event_type = models.CharField(
        max_length=40,
        choices=[
            ('NO_SHOW_RECORDED', 'NO_SHOW_RECORDED'),
            ('PENALTY_APPLIED', 'PENALTY_APPLIED'),
            ('RESTRICTION_ACTIVATED', 'RESTRICTION_ACTIVATED'),
            ('FUTURE_BOOKINGS_CANCELLED', 'FUTURE_BOOKINGS_CANCELLED'),
            ('SUCCESSFUL_ATTENDANCE', 'SUCCESSFUL_ATTENDANCE'),
            ('POLICY_RESET', 'POLICY_RESET'),
            ('MANUAL_OVERRIDE', 'MANUAL_OVERRIDE'),
        ]
    )
    no_show_sequence = models.IntegerField(null=True, blank=True)
    sessions_deducted = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    old_booking_mode = models.CharField(max_length=20, null=True, blank=True)
    new_booking_mode = models.CharField(max_length=20, null=True, blank=True)
    reason = models.TextField(null=True, blank=True)
    triggered_by_type = models.CharField(max_length=20, default='SYSTEM', choices=[('SYSTEM', 'SYSTEM'), ('USER', 'USER'), ('ADMIN', 'ADMIN')])
    triggered_by_user = models.ForeignKey(TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='triggered_attendance_events')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'attendance_policy_events'
        ordering = ['-created_at']
