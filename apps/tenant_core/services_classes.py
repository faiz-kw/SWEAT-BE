"""
Layer 2 Business Services: Module E (Group Classes, Scheduling, Content Studio & Demand Planning)

Key Rules Enforced:
1. Schedule Occurrence Generation: Generates dated occurrences according to weekly schedule rules,
   local times, capacity overrides, and operating rules.
2. Trainer Eligibility & Assignment:
   - Validates branch access, specialty proficiency, delivery mode, work shift, schedule exceptions,
     conflicts, and buffer before assigning trainer to occurrence.
3. Content Studio Rotation:
   - Enforces NO_REPEAT_UNTIL_EXHAUSTED rotation pattern.
   - Deterministically cycles through eligible pool items by display_order.
   - Automatically increments rotation cycle when all pool items are exhausted.
4. Demand Event Tracking & Planning Recommendations.
5. Transactional audit and domain outbox event emission.
"""

import uuid
from decimal import Decimal
from datetime import date, datetime, timedelta, time, timezone as dt_timezone
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any, List, Union
from django.db import transaction, models
from django.db.models import Q
from django.utils import timezone
from django.core.exceptions import ValidationError

from .models_classes import (
    ClassCategory, ClassTemplate, ClassPrice, ClassBranchAvailability,
    ClassScheduleRule, ClassOccurrence, ClassOccurrenceTrainer,
    PackageClassAccessRule, ClassSpecialtyRequirement,
    ClassContentItem, ClassContentMapping, ClassContentAssignment,
    ClassDemandEvent, ClassDemandPlanningRun, ClassScheduleRecommendation,
)
from .models_org import Organization, Branch
from .models_govern import BranchWorkingHours, BranchOperatingException
from .models_users import TenantUser
from .models_workforce import TrainerProfile, TrainerSpecialtyAssignment
from .services_workforce import TrainerAvailabilityService
from .services_schedule import BranchScheduleService
from .services_reliability import record_business_audit, enqueue_outbox_event


class ClassSchedulingService:
    """
    Service managing Class Templates, Schedule Rules, Occurrence Generation,
    and Trainer Assignment with strict eligibility validation.
    """

    @classmethod
    @transaction.atomic
    def create_class_template(
        cls,
        organization: Organization,
        code: str,
        name: str,
        category: Optional[ClassCategory] = None,
        program_id: Optional[str] = None,
        description: Optional[str] = None,
        default_duration_minutes: int = 60,
        default_capacity: int = 20,
        default_trial_capacity: int = 0,
        default_waitlist_capacity: int = 0,
        default_delivery_mode: str = 'OFFLINE',
        allow_booking: bool = True,
        allow_trial: bool = False,
        allow_waitlist: bool = False,
        status: str = 'ACTIVE',
        actor: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> ClassTemplate:
        alias = db_alias or 'default'
        tpl = ClassTemplate(
            organization=organization,
            category=category,
            program_id=program_id,
            code=code.strip().upper(),
            name=name.strip(),
            description=description,
            default_duration_minutes=default_duration_minutes,
            default_capacity=default_capacity,
            default_trial_capacity=default_trial_capacity,
            default_waitlist_capacity=default_waitlist_capacity,
            default_delivery_mode=default_delivery_mode,
            allow_booking=allow_booking,
            allow_trial=allow_trial,
            allow_waitlist=allow_waitlist,
            status=status,
        )
        tpl.save(using=alias)

        record_business_audit(
            organization=organization,
            module='classes',
            action_code='CLASS_TEMPLATE_CREATED',
            entity_type='ClassTemplate',
            entity_id=tpl.id,
            actor_user=actor,
            event_description=f"Created class template {tpl.name} ({tpl.code})",
            after_data={'code': tpl.code, 'name': tpl.name, 'capacity': tpl.default_capacity},
            db_alias=alias,
        )
        return tpl

    @classmethod
    @transaction.atomic
    def create_schedule_rule(
        cls,
        class_template: ClassTemplate,
        branch: Branch,
        start_time: time,
        end_time: time,
        valid_from: date,
        days_of_week: List[int],
        valid_until: Optional[date] = None,
        delivery_mode: str = 'OFFLINE',
        capacity_override: Optional[int] = None,
        trial_capacity_override: Optional[int] = None,
        waitlist_capacity_override: Optional[int] = None,
        status: str = 'ACTIVE',
        actor: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> ClassScheduleRule:
        alias = db_alias or 'default'
        if end_time <= start_time:
            raise ValidationError("end_time must be strictly greater than start_time")

        if branch.status != 'ACTIVE':
            raise ValidationError(f"Branch '{branch.name}' is not ACTIVE.")

        # Validate program active and available at branch
        if class_template.program:
            if class_template.program.status != 'ACTIVE':
                raise ValidationError(f"Cannot schedule class '{class_template.name}' because its program '{class_template.program.name}' is not ACTIVE.")
            from .models_catalog import ProgramBranchAvailability
            pba_qs = ProgramBranchAvailability.objects.using(alias).filter(program=class_template.program)
            if pba_qs.exists():
                is_pba_active = pba_qs.filter(branch=branch, is_active=True).exists()
                if not is_pba_active:
                    raise ValidationError(f"Cannot schedule class '{class_template.name}' at '{branch.name}' because its program '{class_template.program.name}' is not active at this branch.")

        # Rule 11: Validate against branch weekly working hours (including split shifts)
        day_names = {1: 'Monday', 2: 'Tuesday', 3: 'Wednesday', 4: 'Thursday', 5: 'Friday', 6: 'Saturday', 7: 'Sunday'}
        for dow in (days_of_week or []):
            wh = BranchWorkingHours.objects.using(alias).filter(branch=branch, day_of_week=dow).first()
            if wh:
                d_name = day_names.get(dow, f'Day {dow}')
                if not wh.is_open:
                    raise ValidationError(f"Branch '{branch.name}' is closed on {d_name}.")
                if not wh.is_24_hours:
                    fits_s1 = bool(wh.open_time and wh.close_time and start_time >= wh.open_time and end_time <= wh.close_time)
                    fits_s2 = bool(
                        getattr(wh, 'has_split_shift', False) and
                        getattr(wh, 'open_time_2', None) and
                        getattr(wh, 'close_time_2', None) and
                        start_time >= wh.open_time_2 and
                        end_time <= wh.close_time_2
                    )
                    if getattr(wh, 'has_split_shift', False) and getattr(wh, 'open_time_2', None) and getattr(wh, 'close_time_2', None):
                        if not (fits_s1 or fits_s2):
                            raise ValidationError(
                                f"Class time ({start_time.strftime('%H:%M')}–{end_time.strftime('%H:%M')}) falls outside branch operating shifts "
                                f"({wh.open_time.strftime('%H:%M')}–{wh.close_time.strftime('%H:%M')} & "
                                f"{wh.open_time_2.strftime('%H:%M')}–{wh.close_time_2.strftime('%H:%M')}) on {d_name}."
                            )
                    else:
                        if wh.open_time and start_time < wh.open_time:
                            raise ValidationError(
                                f"Class start time {start_time.strftime('%H:%M')} is before branch opening time {wh.open_time.strftime('%H:%M')} on {d_name}."
                            )
                        if wh.close_time and end_time > wh.close_time:
                            raise ValidationError(
                                f"Class end time {end_time.strftime('%H:%M')} is after branch closing time {wh.close_time.strftime('%H:%M')} on {d_name}."
                            )

        rule = ClassScheduleRule(
            class_template=class_template,
            branch=branch,
            start_time=start_time,
            end_time=end_time,
            valid_from=valid_from,
            valid_until=valid_until,
            days_of_week=days_of_week,
            delivery_mode=delivery_mode,
            capacity_override=capacity_override,
            trial_capacity_override=trial_capacity_override,
            waitlist_capacity_override=waitlist_capacity_override,
            status=status,
        )
        rule.save(using=alias)

        record_business_audit(
            organization=branch.organization,
            branch=branch,
            module='classes',
            action_code='SCHEDULE_RULE_CREATED',
            entity_type='ClassScheduleRule',
            entity_id=rule.id,
            actor_user=actor,
            event_description=f"Created recurring schedule rule for {class_template.name} at {branch.name} ({start_time}-{end_time})",
            after_data={'template_id': str(class_template.id), 'branch_id': str(branch.id), 'days': days_of_week},
            db_alias=alias,
        )
        return rule

    @classmethod
    @transaction.atomic
    def generate_occurrences_from_rule(
        cls,
        rule: ClassScheduleRule,
        from_date: date,
        to_date: date,
        actor: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> List[ClassOccurrence]:
        """
        Generates class occurrences for all matching weekdays between from_date and to_date.
        Prevents duplicate generation for the same slot and respects branch exceptions.
        """
        alias = db_alias or 'default'
        created_occurrences = []

        curr = max(from_date, rule.valid_from)
        end = min(to_date, rule.valid_until) if rule.valid_until else to_date

        branch_avail = ClassBranchAvailability.objects.using(alias).filter(
            class_template=rule.class_template,
            branch=rule.branch,
            status='ENABLED',
        ).first()

        capacity = (
            rule.capacity_override
            or (branch_avail.capacity_override if branch_avail and branch_avail.capacity_override is not None else None)
            or rule.class_template.default_capacity
        )
        trial_cap = (
            rule.trial_capacity_override
            or (branch_avail.trial_capacity_override if branch_avail and branch_avail.trial_capacity_override is not None else None)
            or rule.class_template.default_trial_capacity
        )
        wait_cap = (
            rule.waitlist_capacity_override
            or (branch_avail.waitlist_capacity_override if branch_avail and branch_avail.waitlist_capacity_override is not None else None)
            or rule.class_template.default_waitlist_capacity
        )

        while curr <= end:
            # Python weekday: Mon=0 .. Sun=6, our spec uses 1..7 (Mon=1 .. Sun=7)
            day_num = curr.weekday() + 1
            if day_num in (rule.days_of_week or []):
                # Verify branch operating exception or closure for this exact date
                eff = BranchScheduleService.get_effective_schedule_for_date(rule.branch, curr, alias)
                if eff.get('source') in ('EXCEPTION', 'WEEKLY_SCHEDULE') and not eff.get('is_open'):
                    curr += timedelta(days=1)
                    continue

                if eff.get('is_open') and not eff.get('is_24_hours'):
                    fits_shift_1 = bool(eff.get('open_time') and eff.get('close_time') and rule.start_time >= eff['open_time'] and rule.end_time <= eff['close_time'])
                    fits_shift_2 = bool(
                        eff.get('has_split_shift') and
                        eff.get('open_time_2') and
                        eff.get('close_time_2') and
                        rule.start_time >= eff['open_time_2'] and
                        rule.end_time <= eff['close_time_2']
                    )
                    if eff.get('has_split_shift') and eff.get('open_time_2') and eff.get('close_time_2'):
                        if not (fits_shift_1 or fits_shift_2):
                            curr += timedelta(days=1)
                            continue
                    else:
                        if eff.get('open_time') and rule.start_time < eff['open_time']:
                            curr += timedelta(days=1)
                            continue
                        if eff.get('close_time') and rule.end_time > eff['close_time']:
                            curr += timedelta(days=1)
                            continue

                # Construct timestamps localized to branch timezone then stored as UTC
                tz_name = getattr(rule.branch, 'timezone', None) or 'Asia/Kolkata'
                try:
                    branch_tz = ZoneInfo(tz_name)
                except Exception:
                    branch_tz = ZoneInfo('Asia/Kolkata')

                local_start = datetime.combine(curr, rule.start_time).replace(tzinfo=branch_tz)
                start_dt = local_start.astimezone(dt_timezone.utc)
                local_end = datetime.combine(curr, rule.end_time).replace(tzinfo=branch_tz)
                end_dt = local_end.astimezone(dt_timezone.utc)

                # Check existence before inserting to prevent duplicate generation
                exists = ClassOccurrence.objects.using(alias).filter(
                    branch=rule.branch,
                    class_template=rule.class_template,
                    start_at=start_dt,
                ).exclude(status__in=['CANCELLED', 'DEACTIVATED']).exists()

                if not exists and rule.class_template.program_id:
                    exists = ClassOccurrence.objects.using(alias).filter(
                        branch=rule.branch,
                        class_template__program_id=rule.class_template.program_id,
                        start_at=start_dt,
                    ).exclude(status__in=['CANCELLED', 'DEACTIVATED']).exists()

                if not exists:
                    occ = ClassOccurrence(
                        class_template=rule.class_template,
                        schedule_rule=rule,
                        branch=rule.branch,
                        occurrence_date=curr,
                        start_at=start_dt,
                        end_at=end_dt,
                        delivery_mode=rule.delivery_mode,
                        capacity=capacity,
                        trial_capacity=trial_cap,
                        waitlist_capacity=wait_cap,
                        status='OPEN',
                    )
                    occ.save(using=alias)
                    created_occurrences.append(occ)

            curr += timedelta(days=1)

        return created_occurrences

    @classmethod
    @transaction.atomic
    def create_one_off_occurrence(
        cls,
        class_template: ClassTemplate,
        branch: Branch,
        occurrence_date: date,
        start_time: time,
        end_time: time,
        delivery_mode: str = 'OFFLINE',
        capacity: Optional[int] = None,
        trial_capacity: Optional[int] = None,
        waitlist_capacity: Optional[int] = None,
        actor: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> ClassOccurrence:
        alias = db_alias or 'default'
        if end_time <= start_time:
            raise ValidationError("end_time must be strictly greater than start_time")

        if branch.status != 'ACTIVE':
            raise ValidationError(f"Branch '{branch.name}' is not ACTIVE.")

        # Validate program active and available at branch
        if class_template.program:
            if class_template.program.status != 'ACTIVE':
                raise ValidationError(f"Cannot create occurrence for class '{class_template.name}' because its program '{class_template.program.name}' is not ACTIVE.")
            from .models_catalog import ProgramBranchAvailability
            is_pba_active = ProgramBranchAvailability.objects.using(alias).filter(
                program=class_template.program,
                branch=branch,
                is_active=True
            ).exists()
            if not is_pba_active:
                raise ValidationError(f"Cannot create occurrence for class '{class_template.name}' at '{branch.name}' because its program '{class_template.program.name}' is not active at this branch.")

        eff = BranchScheduleService.get_effective_schedule_for_date(branch, occurrence_date, alias)
        if eff.get('source') in ('EXCEPTION', 'WEEKLY_SCHEDULE') and not eff.get('is_open'):
            raise ValidationError(f"Branch '{branch.name}' is closed on {occurrence_date} ({eff.get('reason') or 'Closed'}).")

        if eff.get('is_open') and not eff.get('is_24_hours'):
            if eff.get('open_time') and start_time < eff['open_time']:
                raise ValidationError(
                    f"Class start time {start_time.strftime('%H:%M')} is before branch opening time {eff['open_time'].strftime('%H:%M')}."
                )
            if eff.get('close_time') and end_time > eff['close_time']:
                raise ValidationError(
                    f"Class end time {end_time.strftime('%H:%M')} is after branch closing time {eff['close_time'].strftime('%H:%M')}."
                )

        tz_name = getattr(branch, 'timezone', None) or 'Asia/Kolkata'
        try:
            branch_tz = ZoneInfo(tz_name)
        except Exception:
            branch_tz = ZoneInfo('Asia/Kolkata')

        local_start = datetime.combine(occurrence_date, start_time).replace(tzinfo=branch_tz)
        start_dt = local_start.astimezone(dt_timezone.utc)
        local_end = datetime.combine(occurrence_date, end_time).replace(tzinfo=branch_tz)
        end_dt = local_end.astimezone(dt_timezone.utc)

        occ = ClassOccurrence(
            class_template=class_template,
            branch=branch,
            occurrence_date=occurrence_date,
            start_at=start_dt,
            end_at=end_dt,
            delivery_mode=delivery_mode,
            capacity=capacity or class_template.default_capacity,
            trial_capacity=trial_capacity if trial_capacity is not None else class_template.default_trial_capacity,
            waitlist_capacity=waitlist_capacity if waitlist_capacity is not None else class_template.default_waitlist_capacity,
            status='OPEN',
            is_manual=True,
        )
        occ.save(using=alias)

        record_business_audit(
            organization=branch.organization,
            branch=branch,
            module='classes',
            action_code='ONE_OFF_CLASS_OCCURRENCE_CREATED',
            entity_type='ClassOccurrence',
            entity_id=occ.id,
            actor_user=actor,
            event_description=f"Created one-off class session for {class_template.name} at {branch.name} on {occurrence_date}",
            after_data={'template_id': str(class_template.id), 'date': str(occurrence_date), 'start_time': str(start_time)},
            db_alias=alias,
        )
        return occ

    @classmethod
    def assign_trainer_to_occurrence(
        cls,
        occurrence_id: str,
        trainer_profile_id: str,
        trainer_role: str = 'LEAD',
        actor: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> ClassOccurrenceTrainer:
        """
        Assigns a trainer to a class occurrence with complete eligibility checks:
        1. Active trainer & employee profile.
        2. Working shift & schedule exception verification.
        3. Specialty requirement check (if mandatory specialties exist for template).
        4. Group delivery mode permission (allow_group=True).
        5. Time conflict & buffer checks against other assignments.
        """
        alias = db_alias or 'default'
        with transaction.atomic(using=alias):
            occ = ClassOccurrence.objects.using(alias).select_for_update().get(id=occurrence_id)
            try:
                trainer = TrainerProfile.objects.using(alias).select_for_update().select_related('employee_profile').get(id=trainer_profile_id)
            except (TrainerProfile.DoesNotExist, ValueError):
                trainer = TrainerProfile.objects.using(alias).select_for_update().select_related('employee_profile').filter(
                    employee_profile__user_profile__user_id=trainer_profile_id
                ).first()
                if not trainer:
                    raise ValidationError(f"Trainer profile '{trainer_profile_id}' not found.")

            # 1. Active status check
            emp_active = True
            if hasattr(trainer, 'employee_profile') and trainer.employee_profile:
                emp_active = (getattr(trainer.employee_profile, 'employment_status', 'ACTIVE') == 'ACTIVE')
            if trainer.trainer_status != 'ACTIVE' or not emp_active:
                raise ValidationError(f"Trainer {trainer.trainer_code} is not active.")

            # 2. Specialty & Group delivery check
            reqs = ClassSpecialtyRequirement.objects.using(alias).filter(
                class_template=occ.class_template,
                status='ACTIVE',
                is_mandatory=True,
            )
            if reqs.exists() and not trainer.can_teach_all_specialties:
                # Verify trainer has at least one of the required specialties with allow_group=True
                specialty_ids = [r.trainer_specialty_id for r in reqs]
                has_spec = TrainerSpecialtyAssignment.objects.using(alias).filter(
                    trainer_profile=trainer,
                    trainer_specialty_id__in=specialty_ids,
                    allow_group=True,
                    status='ACTIVE',
                ).exists()
                if not has_spec:
                    raise ValidationError(
                        f"Trainer {trainer.trainer_code} does not have required specialty credentials for {occ.class_template.name}."
                    )

            # 3. Schedule shift and leave check (Admins/Managers can schedule/assign outside strict shifts)
            is_admin_override = True
            if actor:
                actor_role = str(getattr(actor, 'role', '')).upper().replace(' ', '_').replace('-', '_')
                role_codes = {str(r).upper().replace(' ', '_').replace('-', '_') for r in (getattr(actor, '_role_codes', set()) or set())}
                role_list = [str(r).upper().replace(' ', '_').replace('-', '_') for r in getattr(actor, '_roles', [])]
                actor_user_type = str(getattr(actor, 'user_type', '')).upper()

                admin_roles = {
                    'ADMIN', 'SUPER_ADMIN', 'ORG_ADMIN', 'BRANCH_MANAGER', 'MANAGER', 'STAFF',
                    'ORGANIZATION_ADMIN', 'ORGANIZATION_ADMINISTRATOR'
                }

                is_admin_override = (
                    getattr(actor, 'is_staff', False)
                    or getattr(actor, 'is_superuser', False)
                    or actor_user_type in ['STAFF', 'ADMIN', 'ORGANIZATION_ADMIN', 'ORGANIZATION_ADMINISTRATOR']
                    or actor_role in admin_roles
                    or bool(role_codes.intersection(admin_roles))
                    or any(r in admin_roles for r in role_list)
                )

                if not is_admin_override and hasattr(actor, 'id'):
                    try:
                        from .models_rbac import RoleAssignment
                        assigned_roles = RoleAssignment.objects.using(alias).filter(user_id=actor.id).select_related('role')
                        for ra in assigned_roles:
                            r_code = (ra.role.code or '').upper().replace(' ', '_').replace('-', '_')
                            r_name = (ra.role.name or '').upper().replace(' ', '_').replace('-', '_')
                            if r_code in admin_roles or r_name in admin_roles:
                                is_admin_override = True
                                break
                    except Exception:
                        pass

            if not is_admin_override:
                duration = int((occ.end_at - occ.start_at).total_seconds() // 60)
                is_avail, reason, _ = TrainerAvailabilityService.is_trainer_available(
                    trainer=trainer,
                    branch=occ.branch,
                    start_datetime=occ.start_at,
                    duration_minutes=duration,
                    delivery_mode='GROUP',
                    db_alias=alias,
                )
                if not is_avail:
                    raise ValidationError(f"Trainer availability check failed: {reason}")

            # 4. Conflict check with other class occurrences
            # At the same branch, back-to-back classes (end_at == next_start_at) are permitted (zero gap).
            # Across different branches, travel buffer (minimum_schedule_buffer_minutes) is enforced.
            buffer_min = getattr(trainer, 'minimum_schedule_buffer_minutes', 0) or 0
            from datetime import timedelta
            from django.db.models import Q
            buffer_delta = timedelta(minutes=buffer_min)
            target_start = occ.start_at - buffer_delta
            target_end = occ.end_at + buffer_delta

            if occ.branch_id:
                time_filter = (
                    (Q(occurrence__branch_id=occ.branch_id) & Q(occurrence__start_at__lt=occ.end_at, occurrence__end_at__gt=occ.start_at)) |
                    (~Q(occurrence__branch_id=occ.branch_id) & Q(occurrence__start_at__lt=target_end, occurrence__end_at__gt=target_start))
                )
            else:
                time_filter = Q(occurrence__start_at__lt=target_end, occurrence__end_at__gt=target_start)

            conflict = ClassOccurrenceTrainer.objects.using(alias).filter(
                Q(trainer_profile=trainer) &
                Q(status__in=['ASSIGNED', 'CONFIRMED']) &
                time_filter
            ).exclude(occurrence=occ).exclude(occurrence__status__in=['CANCELLED', 'DEACTIVATED']).exists()

            if conflict:
                raise ValidationError("Trainer has an overlapping class assignment.")

            # Resolve tenant_actor for assigned_by_user ForeignKey
            tenant_actor = None
            if actor:
                if getattr(actor, '__class__', None).__name__ == 'TenantUser':
                    tenant_actor = actor
                else:
                    try:
                        from .models_users import TenantUser
                        actor_email = getattr(actor, 'email', None)
                        actor_id = getattr(actor, 'id', None)
                        tenant_actor = TenantUser.objects.using(alias).filter(
                            Q(id=actor_id) | (Q(email__iexact=actor_email) if actor_email else Q(pk__isnull=True))
                        ).first()
                    except Exception:
                        tenant_actor = None

            # Check if this trainer is already assigned to this occurrence (update role if exists)
            existing_assignment = ClassOccurrenceTrainer.objects.using(alias).filter(
                occurrence=occ,
                trainer_profile=trainer,
            ).first()
            if existing_assignment:
                if trainer_role == 'LEAD':
                    ClassOccurrenceTrainer.objects.using(alias).filter(
                        occurrence=occ,
                        trainer_role='LEAD',
                        status__in=['ASSIGNED', 'CONFIRMED'],
                    ).exclude(id=existing_assignment.id).update(status='REPLACED')

                existing_assignment.trainer_role = trainer_role
                if existing_assignment.status not in ['CONFIRMED', 'ASSIGNED']:
                    existing_assignment.status = 'ASSIGNED'
                existing_assignment.assigned_by_user = tenant_actor
                existing_assignment.assigned_at = timezone.now()
                existing_assignment.save(using=alias)
                assignment = existing_assignment
            else:
                if trainer_role == 'LEAD':
                    ClassOccurrenceTrainer.objects.using(alias).filter(
                        occurrence=occ,
                        trainer_role='LEAD',
                        status__in=['ASSIGNED', 'CONFIRMED'],
                    ).update(status='REPLACED')

                # Create new assignment in ASSIGNED (pending check-in) status
                assignment = ClassOccurrenceTrainer(
                    occurrence=occ,
                    trainer_profile=trainer,
                    trainer_role=trainer_role,
                    status='ASSIGNED',
                    assigned_by_user=tenant_actor,
                    assigned_at=timezone.now(),
                )
                assignment.save(using=alias)

            record_business_audit(
                organization=occ.branch.organization,
                branch=occ.branch,
                module='classes',
                action_code='TRAINER_ASSIGNED',
                entity_type='ClassOccurrenceTrainer',
                entity_id=assignment.id,
                actor_user=tenant_actor,
                event_description=f"Assigned {trainer} to {occ} as {trainer_role}",
                after_data={'occurrence_id': str(occ.id), 'trainer_id': str(trainer.id), 'role': trainer_role},
                db_alias=alias,
            )
            return assignment

    @classmethod
    def release_trainers_for_occurrences(
        cls,
        occurrence_ids: Union[List[Union[str, uuid.UUID]], Any],
        reason: str = "Class session disabled/cancelled",
        actor: Optional[Any] = None,
        db_alias: Optional[str] = None,
    ) -> int:
        """
        Robustly and immediately releases all active trainer assignments for the specified
        class occurrence(s) when the session is cancelled, deactivated, or disabled.

        Guarantees:
        1. Transitions all active assignments (ASSIGNED / CONFIRMED) to CANCELLED.
        2. Frees up trainer availability instantly across group classes and PT appointments.
        3. Logs immutable business audit records (TRAINER_RELEASED) with actor context.
        4. Returns the count of released trainer assignments.
        """
        alias = db_alias or 'default'
        if not occurrence_ids:
            return 0

        # Handle list, set, or QuerySet of IDs
        if isinstance(occurrence_ids, (list, tuple, set)):
            clean_ids = [str(oid) for oid in occurrence_ids if oid]
        elif hasattr(occurrence_ids, 'values_list'):
            clean_ids = list(occurrence_ids.values_list('id', flat=True))
        else:
            clean_ids = [str(occurrence_ids)]

        if not clean_ids:
            return 0

        with transaction.atomic(using=alias):
            active_assignments = list(
                ClassOccurrenceTrainer.objects.using(alias).select_for_update().filter(
                    occurrence_id__in=clean_ids,
                    status__in=['ASSIGNED', 'CONFIRMED']
                ).select_related(
                    'occurrence',
                    'trainer_profile',
                    'occurrence__branch',
                    'occurrence__branch__organization'
                )
            )

            if not active_assignments:
                return 0

            target_ids = [a.id for a in active_assignments]
            ClassOccurrenceTrainer.objects.using(alias).filter(id__in=target_ids).update(
                status='CANCELLED'
            )

            # Resolve tenant_actor for audit logging
            tenant_actor = None
            if actor:
                if getattr(actor, '__class__', None).__name__ == 'TenantUser':
                    tenant_actor = actor
                else:
                    try:
                        from .models_users import TenantUser
                        actor_email = getattr(actor, 'email', None)
                        actor_id = getattr(actor, 'id', None)
                        tenant_actor = TenantUser.objects.using(alias).filter(
                            Q(id=actor_id) | (Q(email__iexact=actor_email) if actor_email else Q(pk__isnull=True))
                        ).first()
                    except Exception:
                        tenant_actor = None

            from .services_reliability import record_business_audit
            for assignment in active_assignments:
                occ = assignment.occurrence
                trainer = assignment.trainer_profile
                org = occ.branch.organization if (occ and occ.branch) else None
                if org:
                    try:
                        record_business_audit(
                            organization=org,
                            branch=occ.branch,
                            module='classes',
                            action_code='TRAINER_RELEASED',
                            entity_type='ClassOccurrenceTrainer',
                            entity_id=assignment.id,
                            actor_user=tenant_actor,
                            event_description=f"Released trainer {getattr(trainer, 'trainer_code', 'Trainer')} from cancelled/disabled class session on {getattr(occ, 'occurrence_date', '')}: {reason}",
                            before_data={'status': assignment.status, 'role': assignment.trainer_role},
                            after_data={'status': 'CANCELLED', 'reason': reason},
                            metadata={
                                'occurrence_id': str(occ.id),
                                'trainer_id': str(trainer.id),
                                'trainer_code': getattr(trainer, 'trainer_code', ''),
                                'release_reason': reason,
                            },
                            db_alias=alias,
                        )
                    except Exception as audit_err:
                        logger.warning(f"Failed to record audit log for released trainer: {audit_err}")

            return len(active_assignments)


class ContentStudioService:
    """
    Content Studio rotation engine enforcing NO_REPEAT_UNTIL_EXHAUSTED.
    Deterministic pool cycling by display_order without random repetition.
    """

    @classmethod
    def rotate_and_assign_content_to_occurrence(
        cls,
        occurrence_id: str,
        actor: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> Optional[ClassContentAssignment]:
        """
        Selects next content item from the eligible pool using NO_REPEAT_UNTIL_EXHAUSTED rotation.
        """
        alias = db_alias or 'default'
        with transaction.atomic(using=alias):
            occ = ClassOccurrence.objects.using(alias).select_for_update().get(id=occurrence_id)

            # 1. Fetch eligible content items mapped to this template or category
            mappings = ClassContentMapping.objects.using(alias).filter(
                status='ACTIVE'
            ).filter(
                models.Q(class_template=occ.class_template) |
                models.Q(class_category=occ.class_template.category)
            ).select_related('content_item')

            if not mappings.exists():
                return None

            # Pool sorted by display_order, title
            pool_items = list({
                m.content_item for m in mappings if m.content_item.status == 'ACTIVE'
            })
            pool_items.sort(key=lambda x: (x.display_order, x.title))

            if not pool_items:
                return None

            # 2. Determine latest rotation cycle for this template
            latest_assignment = ClassContentAssignment.objects.using(alias).filter(
                occurrence__class_template=occ.class_template,
                status='ACTIVE',
            ).order_by('-rotation_cycle_number', '-rotation_position').first()

            current_cycle = latest_assignment.rotation_cycle_number if latest_assignment else 1

            # 3. Find items already used in current cycle
            used_item_ids = set(
                ClassContentAssignment.objects.using(alias).filter(
                    occurrence__class_template=occ.class_template,
                    rotation_cycle_number=current_cycle,
                    status='ACTIVE',
                ).values_list('content_item_id', flat=True)
            )

            # 4. Find unused items in pool
            unused_items = [item for item in pool_items if item.id not in used_item_ids]

            if not unused_items:
                # Pool exhausted! Increment cycle to current_cycle + 1 and restart from item 0
                current_cycle += 1
                selected_item = pool_items[0]
                new_position = 1
            else:
                selected_item = unused_items[0]
                new_position = len(used_item_ids) + 1

            # 5. Create assignment record
            assignment = ClassContentAssignment(
                occurrence=occ,
                content_item=selected_item,
                rotation_cycle_number=current_cycle,
                rotation_position=new_position,
                assignment_method='ROTATION',
                assigned_by_user=actor,
                assigned_at=timezone.now(),
                status='ACTIVE',
            )
            assignment.save(using=alias)

            record_business_audit(
                organization=occ.branch.organization,
                branch=occ.branch,
                module='classes',
                action_code='CONTENT_ASSIGNED',
                entity_type='ClassContentAssignment',
                entity_id=assignment.id,
                actor_user=actor,
                event_description=f"Assigned content '{selected_item.title}' (Cycle {current_cycle}, Pos {new_position}) to {occ}",
                after_data={
                    'content_item_id': str(selected_item.id),
                    'cycle': current_cycle,
                    'position': new_position,
                },
                db_alias=alias,
            )
            return assignment
