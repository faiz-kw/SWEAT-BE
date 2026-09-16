import uuid
from django.db import models
from django.utils import timezone
from .models_users import TenantUser
from .models_org import Organization, Branch
from .models_catalog import Package, PackageVersion


class ReferralProgram(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name='referral_programs')
    code = models.CharField(max_length=100)
    name = models.CharField(max_length=200)
    description = models.TextField(null=True, blank=True)
    valid_from = models.DateTimeField(default=timezone.now)
    valid_until = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        default='ACTIVE',
        choices=[('DRAFT', 'DRAFT'), ('ACTIVE', 'ACTIVE'), ('PAUSED', 'PAUSED'), ('INACTIVE', 'INACTIVE')]
    )
    configuration = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'referral_programs'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.code})"


class ReferralIdentifier(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name='referral_identifiers')
    owner_user = models.ForeignKey(TenantUser, on_delete=models.PROTECT, related_name='referral_identifiers')
    referral_program = models.ForeignKey(ReferralProgram, on_delete=models.PROTECT, null=True, blank=True, related_name='identifiers')
    identifier_type = models.CharField(
        max_length=30,
        default='MEMBER_REFERRAL_CODE',
        choices=[
            ('MEMBER_REFERRAL_CODE', 'MEMBER_REFERRAL_CODE'),
            ('TRAINER_CODE', 'TRAINER_CODE'),
            ('EMPLOYEE_CODE', 'EMPLOYEE_CODE'),
            ('SALES_CODE', 'SALES_CODE'),
        ]
    )
    identifier_value = models.CharField(max_length=150, unique=True)
    source_profile_type = models.CharField(
        max_length=20,
        default='MEMBER',
        choices=[('MEMBER', 'MEMBER'), ('TRAINER', 'TRAINER'), ('EMPLOYEE', 'EMPLOYEE'), ('SALES', 'SALES')]
    )
    source_profile_id = models.UUIDField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        default='ACTIVE',
        choices=[('ACTIVE', 'ACTIVE'), ('INACTIVE', 'INACTIVE'), ('EXPIRED', 'EXPIRED')]
    )
    valid_from = models.DateTimeField(default=timezone.now)
    valid_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'referral_identifiers'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.identifier_type}: {self.identifier_value}"


class Referral(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    referral_program = models.ForeignKey(ReferralProgram, on_delete=models.PROTECT, related_name='referrals')
    referral_identifier = models.ForeignKey(ReferralIdentifier, on_delete=models.PROTECT, related_name='referrals')
    referrer_user = models.ForeignKey(TenantUser, on_delete=models.PROTECT, related_name='referrals_sent')
    referred_user = models.ForeignKey(TenantUser, on_delete=models.PROTECT, null=True, blank=True, related_name='referrals_received')
    identifier_used = models.CharField(max_length=150)
    referrer_type = models.CharField(
        max_length=20,
        default='MEMBER',
        choices=[('MEMBER', 'MEMBER'), ('TRAINER', 'TRAINER'), ('EMPLOYEE', 'EMPLOYEE'), ('SALES', 'SALES')]
    )
    referral_context = models.CharField(
        max_length=20,
        default='FRIEND',
        choices=[('FRIEND', 'FRIEND'), ('MEMBER', 'MEMBER'), ('TRAINER', 'TRAINER'), ('EMPLOYEE', 'EMPLOYEE'), ('SALES', 'SALES')]
    )
    referred_email = models.EmailField(null=True, blank=True)
    referred_phone = models.CharField(max_length=32, null=True, blank=True)
    source = models.CharField(
        max_length=20,
        default='WEB',
        choices=[
            ('MOBILE_APP', 'MOBILE_APP'),
            ('WEB', 'WEB'),
            ('FRONT_DESK', 'FRONT_DESK'),
            ('QR', 'QR'),
            ('LINK', 'LINK'),
            ('MANUAL', 'MANUAL'),
            ('CAMPAIGN', 'CAMPAIGN'),
        ]
    )
    status = models.CharField(
        max_length=20,
        default='INVITED',
        choices=[
            ('INVITED', 'INVITED'),
            ('REGISTERED', 'REGISTERED'),
            ('QUALIFIED', 'QUALIFIED'),
            ('REWARDED', 'REWARDED'),
            ('REJECTED', 'REJECTED'),
            ('EXPIRED', 'EXPIRED'),
        ]
    )
    registered_at = models.DateTimeField(null=True, blank=True)
    qualified_at = models.DateTimeField(null=True, blank=True)
    rewarded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'referrals'
        ordering = ['-created_at']

    def __str__(self):
        return f"Referral {self.identifier_used} -> {self.referred_email or self.referred_user} ({self.status})"


class ReferralQualificationRule(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    referral_program = models.ForeignKey(ReferralProgram, on_delete=models.PROTECT, related_name='qualification_rules')
    qualification_event = models.CharField(
        max_length=30,
        default='FIRST_PURCHASE',
        choices=[
            ('REGISTRATION', 'REGISTRATION'),
            ('FIRST_PURCHASE', 'FIRST_PURCHASE'),
            ('PAYMENT_SUCCESS', 'PAYMENT_SUCCESS'),
            ('MEMBERSHIP_ACTIVATED', 'MEMBERSHIP_ACTIVATED'),
            ('FIRST_CLASS_ATTENDED', 'FIRST_CLASS_ATTENDED'),
            ('CUSTOM', 'CUSTOM'),
        ]
    )
    minimum_order_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    package = models.ForeignKey(Package, on_delete=models.PROTECT, null=True, blank=True, related_name='referral_qual_rules')
    package_version = models.ForeignKey(PackageVersion, on_delete=models.PROTECT, null=True, blank=True, related_name='referral_qual_rules')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, null=True, blank=True, related_name='referral_qual_rules')
    valid_from = models.DateTimeField(default=timezone.now)
    valid_until = models.DateTimeField(null=True, blank=True)
    priority = models.IntegerField(default=100)
    status = models.CharField(max_length=20, default='ACTIVE', choices=[('ACTIVE', 'ACTIVE'), ('INACTIVE', 'INACTIVE')])
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'referral_qualification_rules'
        ordering = ['priority', '-created_at']

    def __str__(self):
        return f"Qualification Rule ({self.qualification_event})"


class ReferralBenefitRule(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    referral_program = models.ForeignKey(ReferralProgram, on_delete=models.PROTECT, related_name='benefit_rules')
    referrer_type = models.CharField(
        max_length=20,
        default='ANY',
        choices=[('MEMBER', 'MEMBER'), ('TRAINER', 'TRAINER'), ('EMPLOYEE', 'EMPLOYEE'), ('SALES', 'SALES'), ('ANY', 'ANY')]
    )
    beneficiary = models.CharField(
        max_length=20,
        default='BOTH',
        choices=[('REFERRER', 'REFERRER'), ('REFEREE', 'REFEREE'), ('BOTH', 'BOTH')]
    )
    benefit_type = models.CharField(
        max_length=30,
        default='POINTS',
        choices=[
            ('PERCENTAGE_DISCOUNT', 'PERCENTAGE_DISCOUNT'),
            ('FIXED_DISCOUNT', 'FIXED_DISCOUNT'),
            ('POINTS', 'POINTS'),
            ('CREDIT', 'CREDIT'),
            ('COUPON', 'COUPON'),
            ('FREE_SESSION', 'FREE_SESSION'),
            ('INCENTIVE', 'INCENTIVE'),
        ]
    )
    benefit_value = models.DecimalField(max_digits=14, decimal_places=4, default=0.0)
    max_discount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    minimum_order_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    package = models.ForeignKey(Package, on_delete=models.PROTECT, null=True, blank=True, related_name='referral_benefit_rules')
    package_version = models.ForeignKey(PackageVersion, on_delete=models.PROTECT, null=True, blank=True, related_name='referral_benefit_rules')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, null=True, blank=True, related_name='referral_benefit_rules')
    valid_from = models.DateTimeField(default=timezone.now)
    valid_until = models.DateTimeField(null=True, blank=True)
    priority = models.IntegerField(default=100)
    status = models.CharField(max_length=20, default='ACTIVE', choices=[('ACTIVE', 'ACTIVE'), ('INACTIVE', 'INACTIVE')])
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'referral_benefit_rules'
        ordering = ['priority', '-created_at']

    def __str__(self):
        return f"Benefit {self.beneficiary} -> {self.benefit_type} ({self.benefit_value})"
