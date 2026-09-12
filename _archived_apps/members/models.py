"""
Members & Client 360 Models for PerformanceOS (Phase 2).
Provides MembershipPlan, Member, MemberSubscription, Attendance, and ReferralLedger models.
"""

from django.db import models
from django.utils import timezone
from apps.tenants.models import TenantAwareModel, Location
from apps.users.models import User


class PlanCategory(models.TextChoices):
    MEMBERSHIP = 'Membership', 'Membership'
    PT = 'Personal Training', 'Personal Training'
    PILATES = 'Pilates', 'Pilates'
    NUTRITION = 'Nutrition', 'Nutrition'
    ALL_ACCESS = 'All-Access Elite', 'All-Access Elite'


class MemberStatus(models.TextChoices):
    ACTIVE = 'Active', 'Active'
    EXPIRING = 'Expiring', 'Expiring'
    FROZEN = 'Frozen', 'Frozen'
    LAPSED = 'Lapsed', 'Lapsed'
    CANCELLED = 'Cancelled', 'Cancelled'


class RiskLevel(models.TextChoices):
    LOW = 'Low', 'Low'
    MEDIUM = 'Medium', 'Medium'
    HIGH = 'High', 'High'


class Gender(models.TextChoices):
    MALE = 'Male', 'Male'
    FEMALE = 'Female', 'Female'
    OTHER = 'Other', 'Other'
    M = 'M', 'Male'
    F = 'F', 'Female'
    O = 'O', 'Other'


class AttendanceMethod(models.TextChoices):
    QR_CODE = 'QR Code', 'QR Code'
    BIOMETRIC = 'Biometric', 'Biometric'
    FRONT_DESK = 'Front Desk', 'Front Desk'
    APP = 'App', 'App'


class SubscriptionStatus(models.TextChoices):
    ACTIVE = 'Active', 'Active'
    EXPIRED = 'Expired', 'Expired'
    FROZEN = 'Frozen', 'Frozen'
    CANCELLED = 'Cancelled', 'Cancelled'


class ReferralEventType(models.TextChoices):
    REFERRAL_SIGNUP = 'Referral Signup', 'Referral Signup'
    RENEWAL_BONUS = 'Renewal Bonus', 'Renewal Bonus'
    BIRTHDAY_REWARD = 'Birthday Reward', 'Birthday Reward'
    POINTS_REDEMPTION = 'Points Redemption', 'Points Redemption'


class MembershipPlan(TenantAwareModel):
    """
    Membership and service plans catalog (e.g. 12-Week Transformation, Annual All-Access).
    """
    id = models.CharField(
        max_length=64,
        primary_key=True,
        help_text="Plan ID (e.g. PLN-001)"
    )
    name = models.CharField(max_length=255, help_text="Plan name (e.g. 12-Week Strength Transformation)")
    category = models.CharField(
        max_length=64,
        choices=PlanCategory.choices,
        default=PlanCategory.MEMBERSHIP
    )
    duration_months = models.IntegerField(default=1, help_text="Duration in months")
    price = models.DecimalField(max_digits=10, decimal_places=2)
    total_sessions = models.IntegerField(
        null=True,
        blank=True,
        help_text="Number of sessions included (null = unlimited)"
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'membership_plans'
        ordering = ['name']

    def __str__(self):
        return f"{self.name} - INR {self.price} ({self.id})"


class Member(TenantAwareModel):
    """
    Member profile representing gym clients with 360-degree telemetry.
    """
    id = models.CharField(
        max_length=64,
        primary_key=True,
        help_text="Unique Member ID (e.g. MEM-001)"
    )
    location = models.ForeignKey(
        Location,
        on_delete=models.CASCADE,
        related_name='members',
        help_text="Home studio / branch location"
    )
    name = models.CharField(max_length=255)
    phone = models.CharField(max_length=32)
    email = models.EmailField(blank=True, default='')
    gender = models.CharField(
        max_length=16,
        choices=Gender.choices,
        default=Gender.MALE
    )
    age = models.IntegerField(null=True, blank=True)

    status = models.CharField(
        max_length=32,
        choices=MemberStatus.choices,
        default=MemberStatus.ACTIVE
    )
    risk_level = models.CharField(
        max_length=16,
        choices=RiskLevel.choices,
        default=RiskLevel.LOW,
        help_text="Churn risk indicator"
    )
    health_score = models.IntegerField(default=85, help_text="Health score 0-100")
    performance_score = models.IntegerField(default=80, help_text="Strength/performance score 0-100")
    fitness_goal = models.CharField(max_length=255, blank=True, default='', help_text="Primary fitness objective")
    emergency_contact = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text="Emergency contact name and phone number"
    )
    from_lead_id = models.CharField(
        max_length=64,
        blank=True,
        default='',
        help_text="Original Lead ID if converted through sales pipeline"
    )

    primary_coach = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='coached_members',
        help_text="Assigned trainer or fitness coach"
    )
    joined_at = models.DateField(auto_now_add=True)

    class Meta:
        db_table = 'members'
        ordering = ['-joined_at']

    def __str__(self):
        return f"{self.name} ({self.id})"

    @property
    def reward_points_balance(self):
        """Total accumulated reward points from referrals and promotions."""
        return sum(entry.points for entry in self.referral_entries.all())


class MemberSubscription(TenantAwareModel):
    """
    Active or historical subscription mapping a member to a membership plan.
    """
    id = models.CharField(
        max_length=64,
        primary_key=True,
        help_text="Subscription ID (e.g. SUB-001)"
    )
    member = models.ForeignKey(
        Member,
        on_delete=models.CASCADE,
        related_name='subscriptions'
    )
    plan = models.ForeignKey(
        MembershipPlan,
        on_delete=models.PROTECT,
        related_name='subscriptions'
    )
    start_date = models.DateField()
    end_date = models.DateField()
    sessions_remaining = models.IntegerField(null=True, blank=True)
    status = models.CharField(
        max_length=32,
        choices=SubscriptionStatus.choices,
        default=SubscriptionStatus.ACTIVE
    )
    amount_paid = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)

    class Meta:
        db_table = 'member_subscriptions'
        ordering = ['-start_date']

    def __str__(self):
        return f"{self.member.name} - {self.plan.name} ({self.status})"


class Attendance(TenantAwareModel):
    """
    Attendance records capturing check-ins via QR code, biometric, or front desk.
    """
    id = models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')
    member = models.ForeignKey(
        Member,
        on_delete=models.CASCADE,
        related_name='attendance_records'
    )
    location = models.ForeignKey(
        Location,
        on_delete=models.CASCADE,
        related_name='attendance_logs'
    )
    check_in_time = models.DateTimeField(auto_now_add=True)
    method = models.CharField(
        max_length=32,
        choices=AttendanceMethod.choices,
        default=AttendanceMethod.FRONT_DESK
    )

    class Meta:
        db_table = 'member_attendance'
        ordering = ['-check_in_time']

    def __str__(self):
        return f"{self.member.name} @ {self.location.name} - {self.check_in_time}"


class ReferralLedger(TenantAwareModel):
    """
    Member referral points and rewards ledger.
    """
    id = models.CharField(
        max_length=64,
        primary_key=True,
        help_text="Ledger ID (e.g. REF-001)"
    )
    member = models.ForeignKey(
        Member,
        on_delete=models.CASCADE,
        related_name='referral_entries'
    )
    referred_member = models.ForeignKey(
        Member,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='referred_by_entries'
    )
    event_type = models.CharField(
        max_length=32,
        choices=ReferralEventType.choices,
        default=ReferralEventType.REFERRAL_SIGNUP
    )
    points = models.IntegerField(help_text="Positive for points earned, negative for redemptions")
    description = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        db_table = 'member_referral_ledger'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.member.name}: {self.points} pts ({self.event_type})"
