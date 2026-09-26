"""
apps/tenant_core/models_workforce.py — Layer 2 Module A: People, Employee & Trainer Profiles

8 Domain Models:
  1. UserProfile (user_profiles)
  2. EmployeeProfile (employee_profiles)
  3. TrainerProfile (trainer_profiles)
  4. SalesProfile (sales_profiles)
  5. EmployeeWorkSchedule (employee_work_schedules)
  6. EmployeeScheduleException (employee_schedule_exceptions)
  7. TrainerSpecialty (trainer_specialties)
  8. TrainerSpecialtyAssignment (trainer_specialty_assignments)

All models operate strictly within the dedicated tenant database (app_label='tenant_core').
"""

import uuid
from decimal import Decimal
from django.db import models
from django.utils import timezone
from .models_org import Organization, Branch
from .models_users import TenantUser
from .models_infra import File


class UserProfile(models.Model):
    """
    Layer 2 business/person profile for a Layer 1 login user.
    Member fields are optional for employee-only users.
    """
    MEMBER_TYPES = [
        ('MEMBER', 'Member'),
        ('TRIAL', 'Trial'),
        ('GUEST', 'Guest'),
    ]
    MEMBER_STATUSES = [
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        TenantUser,
        on_delete=models.PROTECT,
        related_name='profile',
        db_column='user_id',
    )
    member_number = models.CharField(max_length=100, null=True, blank=True)
    first_name_snapshot = models.CharField(max_length=150, null=True, blank=True)
    last_name_snapshot = models.CharField(max_length=150, null=True, blank=True)
    gender = models.CharField(max_length=30, null=True, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    profile_file = models.ForeignKey(
        File,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column='profile_file_id',
        related_name='user_profiles',
    )
    address_json = models.JSONField(default=dict, blank=True, null=True)
    emergency_contact_json = models.JSONField(default=dict, blank=True, null=True)
    preferred_language = models.CharField(max_length=20, default='en', blank=True, null=True)
    preferred_branch = models.ForeignKey(
        Branch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column='preferred_branch_id',
        related_name='preferred_by_users',
    )
    joining_date = models.DateField(null=True, blank=True)
    member_type = models.CharField(max_length=30, choices=MEMBER_TYPES, default='MEMBER', null=True, blank=True)
    acquisition_source = models.CharField(max_length=30, null=True, blank=True)
    member_status = models.CharField(max_length=20, choices=MEMBER_STATUSES, default='ACTIVE', null=True, blank=True)
    legacy_reference = models.CharField(max_length=150, null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'user_profiles'
        indexes = [
            models.Index(fields=['member_number'], name='idx_uprof_member_num'),
            models.Index(fields=['member_status'], name='idx_uprof_status'),
            models.Index(fields=['preferred_branch'], name='idx_uprof_pref_branch'),
        ]

    @property
    def is_customer_member(self) -> bool:
        """
        Positive verification if this profile represents an actual customer/member.
        Staff-only profiles return False.
        """
        if self.member_number and str(self.member_number).strip():
            return True
        if self.acquisition_source and str(self.acquisition_source).strip():
            return True
        alias = self._state.db or 'default'
        try:
            if self.memberships.using(alias).exists():
                return True
        except Exception:
            pass
        try:
            if self.lead_conversions.using(alias).exists():
                return True
        except Exception:
            pass
        try:
            if self.orders.using(alias).exists():
                return True
        except Exception:
            pass
        return False

    def __str__(self):
        return f"UserProfile({self.user.email} - {self.member_number or 'No Member#'})"


class EmployeeProfile(models.Model):
    """
    Employee record linking a person to an organization with employment terms.
    """
    EMPLOYMENT_TYPES = [
        ('FULL_TIME', 'Full Time'),
        ('PART_TIME', 'Part Time'),
        ('CONTRACT', 'Contract'),
        ('CONSULTANT', 'Consultant'),
        ('INTERN', 'Intern'),
    ]
    EMPLOYMENT_STATUSES = [
        ('ACTIVE', 'Active'),
        ('NOTICE_PERIOD', 'Notice Period'),
        ('RESIGNED', 'Resigned'),
        ('TERMINATED', 'Terminated'),
        ('INACTIVE', 'Inactive'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_profile = models.OneToOneField(
        UserProfile,
        on_delete=models.PROTECT,
        related_name='employee_profile',
        db_column='user_profile_id',
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name='employee_profiles',
        db_column='organization_id',
    )
    employee_code = models.CharField(max_length=100)
    joining_date = models.DateField(null=True, blank=True)
    hire_date = models.DateField(null=True, blank=True)
    exit_date = models.DateField(null=True, blank=True)
    employment_type = models.CharField(max_length=30, choices=EMPLOYMENT_TYPES, default='FULL_TIME')
    designation = models.CharField(max_length=150, null=True, blank=True)
    reporting_manager = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='direct_reports',
        db_column='reporting_manager_id',
    )
    employment_status = models.CharField(max_length=30, choices=EMPLOYMENT_STATUSES, default='ACTIVE')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'employee_profiles'
        constraints = [
            models.UniqueConstraint(fields=['organization', 'employee_code'], name='uq_emp_org_code'),
        ]
        indexes = [
            models.Index(fields=['organization', 'employment_status'], name='idx_emp_org_status'),
            models.Index(fields=['employee_code'], name='idx_emp_code'),
        ]

    def __str__(self):
        return f"EmployeeProfile({self.employee_code} - {self.designation or 'Staff'})"


class TrainerProfile(models.Model):
    """
    Trainer-specific capabilities, bio, experience, and scheduling buffer rules.
    """
    TRAINER_STATUSES = [
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    employee_profile = models.OneToOneField(
        EmployeeProfile,
        on_delete=models.PROTECT,
        related_name='trainer_profile',
        db_column='employee_profile_id',
    )
    trainer_code = models.CharField(max_length=100)
    bio = models.TextField(null=True, blank=True)
    experience_years = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    trainer_status = models.CharField(max_length=20, choices=TRAINER_STATUSES, default='ACTIVE')
    can_teach_all_specialties = models.BooleanField(default=False)
    minimum_schedule_buffer_minutes = models.IntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'trainer_profiles'
        indexes = [
            models.Index(fields=['trainer_status'], name='idx_trainer_status'),
            models.Index(fields=['trainer_code'], name='idx_trainer_code'),
        ]

    def __str__(self):
        return f"TrainerProfile({self.trainer_code} - Status: {self.trainer_status})"


class SalesProfile(models.Model):
    """
    Sales agent profile for CRM lead assignment and quota tracking.
    """
    SALES_TYPES = [
        ('INSIDE_SALES', 'Inside Sales'),
        ('FIELD_SALES', 'Field Sales'),
        ('FRONT_DESK', 'Front Desk'),
        ('CORPORATE_SALES', 'Corporate Sales'),
        ('CHANNEL_SALES', 'Channel Sales'),
    ]
    SALES_STATUSES = [
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    employee_profile = models.OneToOneField(
        EmployeeProfile,
        on_delete=models.PROTECT,
        related_name='sales_profile',
        db_column='employee_profile_id',
    )
    sales_code = models.CharField(max_length=100)
    sales_type = models.CharField(max_length=40, choices=SALES_TYPES, default='INSIDE_SALES')
    target_enabled = models.BooleanField(default=False)
    lead_assignment_enabled = models.BooleanField(default=True)
    sales_status = models.CharField(max_length=20, choices=SALES_STATUSES, default='ACTIVE')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'sales_profiles'
        indexes = [
            models.Index(fields=['sales_status', 'lead_assignment_enabled'], name='idx_sales_active_assign'),
            models.Index(fields=['sales_code'], name='idx_sales_code'),
        ]

    def __str__(self):
        return f"SalesProfile({self.sales_code} - {self.sales_type})"


class EmployeeWorkSchedule(models.Model):
    """
    Recurring weekly work shifts for employees at specific branches.
    day_of_week: 1=Monday ... 7=Sunday
    """
    SCHEDULE_TYPES = [
        ('REGULAR', 'Regular'),
        ('TEMPORARY', 'Temporary'),
    ]
    STATUSES = [
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    employee_profile = models.ForeignKey(
        EmployeeProfile,
        on_delete=models.PROTECT,
        related_name='work_schedules',
        db_column='employee_profile_id',
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.PROTECT,
        related_name='employee_schedules',
        db_column='branch_id',
    )
    day_of_week = models.SmallIntegerField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    valid_from = models.DateField()
    valid_until = models.DateField(null=True, blank=True)
    schedule_type = models.CharField(max_length=20, choices=SCHEDULE_TYPES, default='REGULAR')
    status = models.CharField(max_length=20, choices=STATUSES, default='ACTIVE')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'employee_work_schedules'
        constraints = [
            models.CheckConstraint(
                condition=models.Q(day_of_week__gte=1) & models.Q(day_of_week__lte=7),
                name='chk_work_sched_day_range',
            ),
        ]
        indexes = [
            models.Index(fields=['employee_profile', 'branch', 'day_of_week'], name='idx_emp_sched_lookup'),
            models.Index(fields=['status'], name='idx_emp_sched_status'),
        ]

    def __str__(self):
        return f"WorkSchedule(Emp:{self.employee_profile.employee_code} Day:{self.day_of_week} {self.start_time}-{self.end_time})"


class EmployeeScheduleException(models.Model):
    """
    Single-day schedule exceptions (leave, sick, weekly off, or special shifts).
    is_available: False for absence/leave, True for special extra shifts.
    """
    EXCEPTION_TYPES = [
        ('LEAVE', 'Leave'),
        ('WEEKLY_OFF', 'Weekly Off'),
        ('WEEKLY_OFF_OVERRIDE', 'Weekly Off Override'),
        ('SPECIAL_SHIFT', 'Special Shift'),
        ('UNAVAILABLE', 'Unavailable'),
        ('TEMPORARY_AVAILABILITY', 'Temporary Availability'),
        ('OTHER', 'Other'),
    ]
    STATUSES = [
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    employee_profile = models.ForeignKey(
        EmployeeProfile,
        on_delete=models.PROTECT,
        related_name='schedule_exceptions',
        db_column='employee_profile_id',
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='schedule_exceptions',
        db_column='branch_id',
    )
    exception_date = models.DateField()
    exception_type = models.CharField(max_length=30, choices=EXCEPTION_TYPES)
    is_available = models.BooleanField()
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    reason = models.TextField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUSES, default='ACTIVE')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'employee_schedule_exceptions'
        indexes = [
            models.Index(fields=['employee_profile', 'exception_date'], name='idx_emp_exc_date'),
            models.Index(fields=['status'], name='idx_emp_exc_status'),
        ]

    def __str__(self):
        return f"ScheduleException(Emp:{self.employee_profile.employee_code} Date:{self.exception_date} Type:{self.exception_type})"


class TrainerSpecialty(models.Model):
    """
    Master organization-level specialty catalog (e.g. HIIT, Yoga, Pilates, Strength).
    """
    STATUSES = [
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name='trainer_specialties',
        db_column='organization_id',
    )
    code = models.CharField(max_length=100)
    name = models.CharField(max_length=150)
    category = models.CharField(max_length=100, null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUSES, default='ACTIVE')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'trainer_specialties'
        constraints = [
            models.UniqueConstraint(fields=['organization', 'code'], name='uq_spec_org_code'),
        ]
        indexes = [
            models.Index(fields=['organization', 'status'], name='idx_spec_org_status'),
        ]

    def __str__(self):
        return f"Specialty({self.code} - {self.name})"


class TrainerSpecialtyAssignment(models.Model):
    """
    Assignment of specialties to a trainer profile, declaring delivery modes
    and proficiency level.
    """
    PROFICIENCY_LEVELS = [
        ('BASIC', 'Basic'),
        ('INTERMEDIATE', 'Intermediate'),
        ('ADVANCED', 'Advanced'),
        ('EXPERT', 'Expert'),
    ]
    STATUSES = [
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    trainer_profile = models.ForeignKey(
        TrainerProfile,
        on_delete=models.PROTECT,
        related_name='specialty_assignments',
        db_column='trainer_profile_id',
    )
    trainer_specialty = models.ForeignKey(
        TrainerSpecialty,
        on_delete=models.PROTECT,
        related_name='trainer_assignments',
        db_column='trainer_specialty_id',
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='trainer_specialty_assignments',
        db_column='branch_id',
    )
    proficiency_level = models.CharField(max_length=20, choices=PROFICIENCY_LEVELS, default='INTERMEDIATE')
    allow_group = models.BooleanField(default=False)
    allow_individual = models.BooleanField(default=False)
    allow_online = models.BooleanField(default=False)
    is_primary = models.BooleanField(default=False)
    valid_from = models.DateField(null=True, blank=True)
    valid_until = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUSES, default='ACTIVE')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'trainer_specialty_assignments'
        indexes = [
            models.Index(fields=['trainer_profile', 'status'], name='idx_tr_spec_assign_status'),
            models.Index(fields=['trainer_specialty', 'status'], name='idx_tr_spec_spec_status'),
        ]

    def __str__(self):
        return f"SpecialtyAssignment(Trainer:{self.trainer_profile.trainer_code} -> {self.trainer_specialty.code})"
