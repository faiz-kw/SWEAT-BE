"""
apps/tenant_core/models_appointments.py — Layer 2 Module F: Individual Appointments

4 Domain Models:
  1. AppointmentType (appointment_types)
  2. Appointment (appointments)
  3. AppointmentTrainer (appointment_trainers)
  4. AppointmentTypeSpecialtyRequirement (appointment_type_specialty_requirements)

Key Rules:
- Distinct from Group Classes: One-to-one service between member and trainer(s).
- Validates trainer specialty with delivery_mode='INDIVIDUAL', working schedule, and buffer.
- Emits business audit and transactional outbox events.
"""

import uuid
from decimal import Decimal
from django.db import models
from django.utils import timezone
from .models_org import Organization, Branch
from .models_users import TenantUser
from .models_workforce import UserProfile, TrainerProfile, TrainerSpecialty


class AppointmentType(models.Model):
    """
    Tenant-configured individual service type: PT, assessment, nutrition, physio, etc.
    """
    DELIVERY_MODE_CHOICES = [
        ('OFFLINE', 'Offline / Studio'),
        ('ONLINE', 'Online Live Stream'),
        ('HYBRID', 'Hybrid'),
    ]
    STATUS_CHOICES = [
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name='appointment_types'
    )
    code = models.CharField(max_length=100)
    name = models.CharField(max_length=200)
    description = models.TextField(null=True, blank=True)
    default_duration_minutes = models.PositiveIntegerField(default=60)
    default_delivery_mode = models.CharField(max_length=20, choices=DELIVERY_MODE_CHOICES, default='OFFLINE')
    requires_trainer = models.BooleanField(default=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'appointment_types'
        unique_together = [('organization', 'code')]
        indexes = [
            models.Index(fields=['organization', 'status'], name='idx_appttype_org_status'),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"


class AppointmentTypeSpecialtyRequirement(models.Model):
    """
    Optional trainer specialty and minimum proficiency requirements for an appointment type.
    """
    PROFICIENCY_LEVELS = [
        ('BASIC', 'Basic'),
        ('INTERMEDIATE', 'Intermediate'),
        ('ADVANCED', 'Advanced'),
        ('EXPERT', 'Expert'),
    ]
    STATUS_CHOICES = [
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    appointment_type = models.ForeignKey(
        AppointmentType, on_delete=models.PROTECT, related_name='specialty_requirements'
    )
    trainer_specialty = models.ForeignKey(
        TrainerSpecialty, on_delete=models.PROTECT, related_name='appointment_type_requirements'
    )
    minimum_proficiency_level = models.CharField(
        max_length=20, choices=PROFICIENCY_LEVELS, default='BASIC'
    )
    is_mandatory = models.BooleanField(default=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'appointment_type_specialty_requirements'
        unique_together = [('appointment_type', 'trainer_specialty')]
        indexes = [
            models.Index(fields=['appointment_type', 'status'], name='idx_apptspec_type_st'),
        ]

    def __str__(self):
        return f"{self.appointment_type.name} -> {self.trainer_specialty.name} (Min: {self.minimum_proficiency_level})"


class Appointment(models.Model):
    """
    Individual member appointment for PT, assessment, or nutrition.
    """
    DELIVERY_MODE_CHOICES = [
        ('OFFLINE', 'Offline / Studio'),
        ('ONLINE', 'Online Live Stream'),
        ('HYBRID', 'Hybrid'),
    ]
    STATUS_CHOICES = [
        ('RESERVED', 'Reserved'),
        ('CONFIRMED', 'Confirmed'),
        ('COMPLETED', 'Completed'),
        ('CANCELLED', 'Cancelled'),
        ('NO_SHOW', 'No Show'),
    ]
    SOURCE_CHOICES = [
        ('WEB', 'Web Portal'),
        ('MOBILE_APP', 'Mobile App'),
        ('FRONT_DESK', 'Front Desk'),
        ('ADMIN', 'Admin Console'),
        ('API', 'External API'),
        ('OTHER', 'Other'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    appointment_type = models.ForeignKey(
        AppointmentType, on_delete=models.PROTECT, related_name='appointments'
    )
    user_profile = models.ForeignKey(
        UserProfile, on_delete=models.PROTECT, related_name='appointments'
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT, related_name='appointments'
    )
    membership_id = models.UUIDField(null=True, blank=True)
    entitlement_id = models.UUIDField(null=True, blank=True)
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    delivery_mode = models.CharField(max_length=20, choices=DELIVERY_MODE_CHOICES, default='OFFLINE')
    online_join_url = models.TextField(null=True, blank=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='CONFIRMED')
    booking_source = models.CharField(max_length=30, choices=SOURCE_CHOICES, default='ADMIN')
    notes = models.TextField(null=True, blank=True)
    created_by_user = models.ForeignKey(
        TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_appointments'
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'appointments'
        indexes = [
            models.Index(fields=['branch', 'start_at', 'status'], name='idx_appt_br_st_status'),
            models.Index(fields=['user_profile', 'start_at'], name='idx_appt_uprof_start'),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_at__gt=models.F('start_at')),
                name='chk_appointment_end_gt_start'
            ),
        ]

    def __str__(self):
        return f"{self.appointment_type.name} for {self.user_profile} on {self.start_at} ({self.status})"


class AppointmentTrainer(models.Model):
    """
    Trainer/provider assignment to an individual appointment.
    """
    ROLE_CHOICES = [
        ('LEAD', 'Lead Trainer'),
        ('ASSISTANT', 'Assistant Trainer'),
        ('SUBSTITUTE', 'Substitute Trainer'),
    ]
    STATUS_CHOICES = [
        ('ASSIGNED', 'Assigned'),
        ('CONFIRMED', 'Confirmed'),
        ('CANCELLED', 'Cancelled'),
        ('REPLACED', 'Replaced'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    appointment = models.ForeignKey(
        Appointment, on_delete=models.PROTECT, related_name='assigned_trainers'
    )
    trainer_profile = models.ForeignKey(
        TrainerProfile, on_delete=models.PROTECT, related_name='appointment_assignments'
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='LEAD')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ASSIGNED')
    assigned_by_user = models.ForeignKey(
        TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_appointment_trainers'
    )
    assigned_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'appointment_trainers'
        unique_together = [('appointment', 'trainer_profile')]
        indexes = [
            models.Index(fields=['appointment', 'status'], name='idx_appttrain_appt_status'),
            models.Index(fields=['trainer_profile', 'status'], name='idx_appttrain_prof_status'),
        ]

    def __str__(self):
        return f"{self.appointment} -> Trainer: {self.trainer_profile.trainer_code} ({self.role})"
