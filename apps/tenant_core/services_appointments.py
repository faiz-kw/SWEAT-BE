"""
apps/tenant_core/services_appointments.py — Business Services for Module F: Individual Appointments

Key Rules:
1. Appointment Creation:
   - Validates appointment type, member, branch, and time integrity (end_at > start_at).
   - Distinct from group class sessions (1-to-1 personal service).
2. Trainer Eligibility & Assignment:
   - Specialty qualification check with allow_individual=True.
   - Working shift & schedule exception checks via TrainerAvailabilityService.
   - Time conflict detection with class occurrences and other individual appointments.
3. Transactional business audit and domain outbox event emission.
"""

import uuid
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from django.db import transaction, models
from django.db.models import Q
from django.utils import timezone
from django.core.exceptions import ValidationError

from .models_appointments import (
    AppointmentType,
    Appointment,
    AppointmentTrainer,
    AppointmentTypeSpecialtyRequirement,
)
from .models_org import Branch
from .models_users import TenantUser
from .models_workforce import (
    UserProfile,
    TrainerProfile,
    TrainerSpecialtyAssignment,
)
from .models_classes import ClassOccurrenceTrainer
from .services_workforce import TrainerAvailabilityService
from .services_reliability import record_business_audit, enqueue_outbox_event


class AppointmentSchedulingService:
    """
    Atomic orchestration of individual appointments, trainer eligibility validation,
    and lifecycle status changes.
    """

    @classmethod
    @transaction.atomic
    def create_appointment(
        cls,
        appointment_type: AppointmentType,
        user_profile: UserProfile,
        branch: Branch,
        start_at: datetime,
        end_at: Optional[datetime] = None,
        delivery_mode: str = 'OFFLINE',
        online_join_url: Optional[str] = None,
        booking_source: str = 'ADMIN',
        notes: Optional[str] = None,
        membership_id: Optional[str] = None,
        entitlement_id: Optional[str] = None,
        created_by: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> Appointment:
        alias = db_alias or 'default'

        if appointment_type.status != 'ACTIVE':
            raise ValidationError(f"AppointmentType {appointment_type.code} is not active.")

        if branch.status != 'ACTIVE':
            raise ValidationError(f"Branch {branch.name} is not active.")

        computed_end = end_at or (start_at + timedelta(minutes=appointment_type.default_duration_minutes))
        if computed_end <= start_at:
            raise ValidationError("Appointment end_at must be strictly greater than start_at.")

        # Check for overlapping active appointments for this member
        overlapping_member = Appointment.objects.using(alias).filter(
            user_profile=user_profile,
            status__in=['RESERVED', 'CONFIRMED'],
            start_at__lt=computed_end,
            end_at__gt=start_at,
        ).exists()
        if overlapping_member:
            raise ValidationError(f"Member already has a conflicting appointment in this time window.")

        appointment = Appointment(
            appointment_type=appointment_type,
            user_profile=user_profile,
            branch=branch,
            start_at=start_at,
            end_at=computed_end,
            delivery_mode=delivery_mode or appointment_type.default_delivery_mode,
            online_join_url=online_join_url,
            status='CONFIRMED',
            booking_source=booking_source,
            notes=notes,
            membership_id=membership_id,
            entitlement_id=entitlement_id,
            created_by_user=created_by,
        )
        appointment.save(using=alias)

        record_business_audit(
            organization=branch.organization,
            branch=branch,
            module='appointments',
            action_code='APPOINTMENT_BOOKED',
            entity_type='Appointment',
            entity_id=appointment.id,
            actor_user=created_by,
            event_description=f"Booked {appointment_type.name} for member {user_profile.member_number or user_profile.id}",
            after_data={
                'appointment_type': appointment_type.code,
                'user_profile_id': str(user_profile.id),
                'start_at': start_at.isoformat(),
                'end_at': computed_end.isoformat(),
                'status': appointment.status,
            },
            db_alias=alias,
        )

        enqueue_outbox_event(
            organization=branch.organization,
            event_type='APPOINTMENT_BOOKED',
            aggregate_type='Appointment',
            aggregate_id=str(appointment.id),
            payload={
                'appointment_id': str(appointment.id),
                'user_profile_id': str(user_profile.id),
                'branch_id': str(branch.id),
                'start_at': start_at.isoformat(),
                'end_at': computed_end.isoformat(),
            },
            db_alias=alias,
        )

        return appointment

    @classmethod
    @transaction.atomic
    def assign_trainer_to_appointment(
        cls,
        appointment_id: str,
        trainer_profile_id: str,
        role: str = 'LEAD',
        actor: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> AppointmentTrainer:
        """
        Assigns a trainer to an individual appointment with full eligibility validation:
        1. Active trainer & employee status.
        2. Specialty requirement check with allow_individual=True.
        3. Working shift and leave exceptions check.
        4. Time conflict check with classes and other appointments.
        """
        alias = db_alias or 'default'
        appt = Appointment.objects.using(alias).select_for_update().get(id=appointment_id)
        trainer = TrainerProfile.objects.using(alias).select_related('employee_profile').get(id=trainer_profile_id)

        # 1. Active Status Check
        if trainer.trainer_status != 'ACTIVE' or trainer.employee_profile.employment_status != 'ACTIVE':
            raise ValidationError(f"Trainer {trainer.trainer_code} is not active.")

        # 2. Specialty Requirement Check (if defined for this appointment type)
        mandatory_reqs = AppointmentTypeSpecialtyRequirement.objects.using(alias).filter(
            appointment_type=appt.appointment_type,
            is_mandatory=True,
            status='ACTIVE',
        )
        if mandatory_reqs.exists() and not trainer.can_teach_all_specialties:
            required_specialties = [r.trainer_specialty_id for r in mandatory_reqs]
            has_spec = TrainerSpecialtyAssignment.objects.using(alias).filter(
                trainer_profile=trainer,
                trainer_specialty_id__in=required_specialties,
                allow_individual=True,
                status='ACTIVE',
            ).exists()
            if not has_spec:
                raise ValidationError(
                    f"Trainer {trainer.trainer_code} does not possess required individual specialty credentials for {appt.appointment_type.name}."
                )

        # 3. Schedule shift and leave check via TrainerAvailabilityService
        duration = int((appt.end_at - appt.start_at).total_seconds() // 60)
        is_avail, reason, _ = TrainerAvailabilityService.is_trainer_available(
            trainer=trainer,
            branch=appt.branch,
            start_datetime=appt.start_at,
            duration_minutes=duration,
            delivery_mode='INDIVIDUAL',
            db_alias=alias,
        )
        if not is_avail:
            raise ValidationError(f"Trainer availability check failed: {reason}")

        # 4. Conflict check with other appointments
        conflict_appt = AppointmentTrainer.objects.using(alias).filter(
            trainer_profile=trainer,
            status__in=['ASSIGNED', 'CONFIRMED'],
            appointment__start_at__lt=appt.end_at,
            appointment__end_at__gt=appt.start_at,
        ).exclude(appointment=appt).exists()
        if conflict_appt:
            raise ValidationError(f"Trainer {trainer.trainer_code} has a conflicting individual appointment.")

        # 5. Conflict check with group class occurrences
        conflict_class = ClassOccurrenceTrainer.objects.using(alias).filter(
            trainer_profile=trainer,
            status__in=['ASSIGNED', 'CONFIRMED'],
            occurrence__start_at__lt=appt.end_at,
            occurrence__end_at__gt=appt.start_at,
        ).exists()
        if conflict_class:
            raise ValidationError(f"Trainer {trainer.trainer_code} has a conflicting group class assignment.")

        # Create or update assignment
        assignment, _ = AppointmentTrainer.objects.using(alias).update_or_create(
            appointment=appt,
            trainer_profile=trainer,
            defaults={
                'role': role,
                'status': 'CONFIRMED',
                'assigned_by_user': actor,
                'assigned_at': timezone.now(),
            }
        )

        record_business_audit(
            organization=appt.branch.organization,
            branch=appt.branch,
            module='appointments',
            action_code='APPOINTMENT_TRAINER_ASSIGNED',
            entity_type='AppointmentTrainer',
            entity_id=assignment.id,
            actor_user=actor,
            event_description=f"Assigned trainer {trainer.trainer_code} to appointment {appt.id}",
            after_data={
                'appointment_id': str(appt.id),
                'trainer_code': trainer.trainer_code,
                'role': role,
            },
            db_alias=alias,
        )

        enqueue_outbox_event(
            organization=appt.branch.organization,
            event_type='APPOINTMENT_TRAINER_ASSIGNED',
            aggregate_type='Appointment',
            aggregate_id=str(appt.id),
            payload={
                'appointment_id': str(appt.id),
                'trainer_profile_id': str(trainer.id),
                'role': role,
            },
            db_alias=alias,
        )

        return assignment

    @classmethod
    @transaction.atomic
    def cancel_appointment(
        cls,
        appointment_id: str,
        reason: Optional[str] = None,
        actor: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> Appointment:
        alias = db_alias or 'default'
        appt = Appointment.objects.using(alias).select_for_update().get(id=appointment_id)
        if appt.status in ['CANCELLED', 'COMPLETED']:
            raise ValidationError(f"Cannot cancel appointment with status {appt.status}.")

        appt.status = 'CANCELLED'
        if reason:
            appt.notes = f"{appt.notes or ''}\nCancelled: {reason}".strip()
        appt.save(using=alias)

        # Cancel trainer assignments
        AppointmentTrainer.objects.using(alias).filter(appointment=appt).update(status='CANCELLED')

        record_business_audit(
            organization=appt.branch.organization,
            branch=appt.branch,
            module='appointments',
            action_code='APPOINTMENT_CANCELLED',
            entity_type='Appointment',
            entity_id=appt.id,
            actor_user=actor,
            event_description=f"Cancelled appointment {appt.id}: {reason or 'No reason provided'}",
            db_alias=alias,
        )

        enqueue_outbox_event(
            organization=appt.branch.organization,
            event_type='APPOINTMENT_CANCELLED',
            aggregate_type='Appointment',
            aggregate_id=str(appt.id),
            payload={'appointment_id': str(appt.id), 'reason': reason},
            db_alias=alias,
        )

        return appt

    @classmethod
    @transaction.atomic
    def complete_appointment(
        cls,
        appointment_id: str,
        actor: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> Appointment:
        alias = db_alias or 'default'
        appt = Appointment.objects.using(alias).select_for_update().get(id=appointment_id)
        if appt.status != 'CONFIRMED':
            raise ValidationError(f"Cannot complete appointment with status {appt.status}.")

        appt.status = 'COMPLETED'
        appt.save(using=alias)

        record_business_audit(
            organization=appt.branch.organization,
            branch=appt.branch,
            module='appointments',
            action_code='APPOINTMENT_COMPLETED',
            entity_type='Appointment',
            entity_id=appt.id,
            actor_user=actor,
            event_description=f"Completed appointment {appt.id}",
            db_alias=alias,
        )

        enqueue_outbox_event(
            organization=appt.branch.organization,
            event_type='APPOINTMENT_COMPLETED',
            aggregate_type='Appointment',
            aggregate_id=str(appt.id),
            payload={'appointment_id': str(appt.id)},
            db_alias=alias,
        )

        return appt
