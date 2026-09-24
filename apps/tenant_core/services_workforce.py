"""
apps/tenant_core/services_workforce.py — Business Services for Module A: Workforce & Trainers

Implements:
- TrainerAvailabilityService:
  Data-driven trainer eligibility & conflict resolution considering:
  - Trainer profile status (ACTIVE)
  - Employee profile status (ACTIVE)
  - Branch active status
  - Dynamic specialty assignments (not hardcoded booleans)
  - Delivery mode (GROUP, INDIVIDUAL, ONLINE)
  - Proficiency level hierarchy (BASIC -> INTERMEDIATE -> ADVANCED -> EXPERT)
  - Weekly recurring shifts (EmployeeWorkSchedule)
  - Single-day exceptions & leave (EmployeeScheduleException)
  - Buffer time (minimum_schedule_buffer_minutes)
"""

import logging
from datetime import datetime, date, time, timedelta
from typing import Optional, List, Dict, Tuple, Any
from django.db.models import Q
from config.routers import get_tenant_db_alias
from .models_org import Branch, Organization
from .models_workforce import (
    UserProfile,
    EmployeeProfile,
    TrainerProfile,
    EmployeeWorkSchedule,
    EmployeeScheduleException,
    TrainerSpecialty,
    TrainerSpecialtyAssignment,
)

logger = logging.getLogger(__name__)

PROFICIENCY_WEIGHTS = {
    'BASIC': 1,
    'INTERMEDIATE': 2,
    'ADVANCED': 3,
    'EXPERT': 4,
}


class TrainerAvailabilityService:
    """
    Evaluates dynamic trainer availability and qualification against class or
    appointment scheduling requirements.
    """

    @classmethod
    def is_trainer_available(
        cls,
        trainer: TrainerProfile,
        branch: Branch,
        start_datetime: datetime,
        duration_minutes: int,
        delivery_mode: str = 'GROUP',
        specialty_code: Optional[str] = None,
        min_proficiency: str = 'BASIC',
        db_alias: Optional[str] = None,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Check whether a specific trainer is eligible and available to teach.
        Returns: (is_available, reason_message, details_dict)
        """
        alias = db_alias or get_tenant_db_alias() or 'default'
        target_date = start_datetime.date()
        target_start_time = start_datetime.time()
        end_datetime = start_datetime + timedelta(minutes=duration_minutes)
        target_end_time = end_datetime.time()
        iso_day = target_date.isoweekday()  # 1=Monday ... 7=Sunday

        # 1. Active Status Checks
        if trainer.trainer_status != 'ACTIVE':
            return False, f"Trainer {trainer.trainer_code} is INACTIVE.", {'code': 'TRAINER_INACTIVE'}

        emp = trainer.employee_profile
        if emp.employment_status != 'ACTIVE':
            return False, f"Employee {emp.employee_code} status is {emp.employment_status}.", {'code': 'EMPLOYEE_INACTIVE'}

        if branch.status != 'ACTIVE':
            return False, f"Branch {branch.name} is {branch.status}.", {'code': 'BRANCH_INACTIVE'}

        # 2. Specialty Qualification Check
        if specialty_code and not trainer.can_teach_all_specialties:
            specialty_qs = TrainerSpecialtyAssignment.objects.using(alias).filter(
                trainer_profile=trainer,
                trainer_specialty__code__iexact=specialty_code,
                status='ACTIVE',
            ).filter(
                Q(branch__isnull=True) | Q(branch=branch)
            ).filter(
                Q(valid_from__isnull=True) | Q(valid_from__lte=target_date)
            ).filter(
                Q(valid_until__isnull=True) | Q(valid_until__gte=target_date)
            )

            mode_upper = delivery_mode.upper()
            if mode_upper == 'GROUP':
                specialty_qs = specialty_qs.filter(allow_group=True)
            elif mode_upper == 'INDIVIDUAL':
                specialty_qs = specialty_qs.filter(allow_individual=True)
            elif mode_upper == 'ONLINE':
                specialty_qs = specialty_qs.filter(allow_online=True)

            assignment = specialty_qs.first()
            if not assignment:
                return False, (
                    f"Trainer {trainer.trainer_code} lacks an active specialty assignment for '{specialty_code}' "
                    f"under mode '{delivery_mode}' at branch '{branch.name}'."
                ), {'code': 'SPECIALTY_NOT_ASSIGNED'}

            # Proficiency check
            req_weight = PROFICIENCY_WEIGHTS.get(min_proficiency.upper(), 1)
            act_weight = PROFICIENCY_WEIGHTS.get(assignment.proficiency_level.upper(), 1)
            if act_weight < req_weight:
                return False, (
                    f"Trainer proficiency {assignment.proficiency_level} is below required {min_proficiency}."
                ), {'code': 'INSUFFICIENT_PROFICIENCY'}

        # 3. Schedule Exceptions (Leave / Absence / Special Shift)
        exceptions = EmployeeScheduleException.objects.using(alias).filter(
            employee_profile=emp,
            exception_date=target_date,
            status='ACTIVE',
        ).filter(
            Q(branch__isnull=True) | Q(branch=branch)
        )

        has_special_shift = False
        for exc in exceptions:
            if not exc.is_available:
                # Full-day leave / unavailable
                if not exc.start_time or not exc.end_time:
                    return False, (
                        f"Trainer is on {exc.exception_type} on {target_date}: {exc.reason or 'No reason provided'}."
                    ), {'code': 'SCHEDULE_EXCEPTION_ABSENT', 'type': exc.exception_type}
                # Partial day absence overlapping slot
                if not (target_end_time <= exc.start_time or target_start_time >= exc.end_time):
                    return False, (
                        f"Trainer is unavailable from {exc.start_time} to {exc.end_time} on {target_date}."
                    ), {'code': 'SCHEDULE_EXCEPTION_PARTIAL_ABSENT'}
            else:
                # Special shift or availability override
                if exc.start_time and exc.end_time:
                    if target_start_time >= exc.start_time and target_end_time <= exc.end_time:
                        has_special_shift = True
                else:
                    # Full day override (e.g. WEEKLY_OFF_OVERRIDE or TEMPORARY_AVAILABILITY covering entire day)
                    has_special_shift = True

        # 4. Working Schedule Check (if no special shift override was applied)
        if not has_special_shift:
            regular_shifts = EmployeeWorkSchedule.objects.using(alias).filter(
                employee_profile=emp,
                branch=branch,
                day_of_week=iso_day,
                status='ACTIVE',
                valid_from__lte=target_date,
            ).filter(
                Q(valid_until__isnull=True) | Q(valid_until__gte=target_date)
            )

            covering_shift = None
            for shift in regular_shifts:
                if target_start_time >= shift.start_time and target_end_time <= shift.end_time:
                    covering_shift = shift
                    break

            if not covering_shift:
                return False, (
                    f"Trainer {trainer.trainer_code} does not have an active work shift covering "
                    f"{target_start_time}-{target_end_time} on weekday {iso_day} at branch {branch.name}."
                ), {'code': 'NO_MATCHING_SHIFT'}

        return True, "Trainer is qualified and available.", {
            'code': 'AVAILABLE',
            'trainer_id': str(trainer.id),
            'trainer_code': trainer.trainer_code,
            'branch_id': str(branch.id),
            'buffer_minutes': trainer.minimum_schedule_buffer_minutes,
        }

    @classmethod
    def find_eligible_trainers(
        cls,
        organization: Organization,
        branch: Branch,
        start_datetime: datetime,
        duration_minutes: int,
        delivery_mode: str = 'GROUP',
        specialty_code: Optional[str] = None,
        min_proficiency: str = 'BASIC',
        db_alias: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Scan all active trainers within the organization and return qualified, available trainers.
        """
        alias = db_alias or get_tenant_db_alias() or 'default'
        trainers = TrainerProfile.objects.using(alias).filter(
            trainer_status='ACTIVE',
            employee_profile__organization=organization,
            employee_profile__employment_status='ACTIVE',
        ).select_related('employee_profile', 'employee_profile__user_profile')

        eligible = []
        for trainer in trainers:
            available, reason, details = cls.is_trainer_available(
                trainer=trainer,
                branch=branch,
                start_datetime=start_datetime,
                duration_minutes=duration_minutes,
                delivery_mode=delivery_mode,
                specialty_code=specialty_code,
                min_proficiency=min_proficiency,
                db_alias=alias,
            )
            if available:
                user_prof = trainer.employee_profile.user_profile
                eligible.append({
                    'trainer_id': str(trainer.id),
                    'trainer_code': trainer.trainer_code,
                    'full_name': f"{user_prof.first_name_snapshot or ''} {user_prof.last_name_snapshot or ''}".strip(),
                    'email': user_prof.user.email if hasattr(user_prof, 'user') else '',
                    'buffer_minutes': trainer.minimum_schedule_buffer_minutes,
                    'details': details,
                })

        return eligible
