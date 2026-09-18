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
from datetime import date, datetime, timedelta, time
from typing import Optional, Dict, Any, List
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

        # Rule 11: Validate against branch weekly working hours
        day_names = {1: 'Monday', 2: 'Tuesday', 3: 'Wednesday', 4: 'Thursday', 5: 'Friday', 6: 'Saturday', 7: 'Sunday'}
        for dow in (days_of_week or []):
            wh = BranchWorkingHours.objects.using(alias).filter(branch=branch, day_of_week=dow).first()
            if wh:
                d_name = day_names.get(dow, f'Day {dow}')
                if not wh.is_open:
                    raise ValidationError(f"Branch '{branch.name}' is closed on {d_name}.")
                if not wh.is_24_hours:
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

        capacity = rule.capacity_override or rule.class_template.default_capacity
        trial_cap = rule.trial_capacity_override or rule.class_template.default_trial_capacity
        wait_cap = rule.waitlist_capacity_override or rule.class_template.default_waitlist_capacity

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
                    if eff.get('open_time') and rule.start_time < eff['open_time']:
                        curr += timedelta(days=1)
                        continue
                    if eff.get('close_time') and rule.end_time > eff['close_time']:
                        curr += timedelta(days=1)
                        continue

                # Construct timestamps
                start_dt = timezone.make_aware(datetime.combine(curr, rule.start_time))
                end_dt = timezone.make_aware(datetime.combine(curr, rule.end_time))

                # Check existence before inserting to prevent duplicate generation
                exists = ClassOccurrence.objects.using(alias).filter(
                    branch=rule.branch,
                    class_template=rule.class_template,
                    start_at=start_dt,
                ).exists()

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

        start_dt = timezone.make_aware(datetime.combine(occurrence_date, start_time))
        end_dt = timezone.make_aware(datetime.combine(occurrence_date, end_time))

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
            trainer = TrainerProfile.objects.using(alias).select_related('employee_profile').get(id=trainer_profile_id)

            # 1. Active status check
            if trainer.trainer_status != 'ACTIVE' or trainer.employee_profile.employment_status != 'ACTIVE':
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

            # 3. Schedule shift and leave check via TrainerAvailabilityService
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
            conflict = ClassOccurrenceTrainer.objects.using(alias).filter(
                trainer_profile=trainer,
                status__in=['ASSIGNED', 'CONFIRMED'],
                occurrence__start_at__lt=occ.end_at,
                occurrence__end_at__gt=occ.start_at,
            ).exclude(occurrence=occ).exists()

            if conflict:
                raise ValidationError("Trainer has an overlapping class assignment.")

            # Create assignment
            assignment = ClassOccurrenceTrainer(
                occurrence=occ,
                trainer_profile=trainer,
                trainer_role=trainer_role,
                status='CONFIRMED',
                assigned_by_user=actor,
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
                actor_user=actor,
                event_description=f"Assigned {trainer} to {occ} as {trainer_role}",
                after_data={'occurrence_id': str(occ.id), 'trainer_id': str(trainer.id), 'role': trainer_role},
                db_alias=alias,
            )
            return assignment


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
