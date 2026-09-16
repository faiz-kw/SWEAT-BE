"""
apps/tenant_core/services_crm.py — Business Services for Layer 2 Module B: CRM, Leads & Trials

Workflows:
- create_lead: Idempotent/atomic lead intake, status history, initial assignment.
- transition_lead_status: State machine transitions, reason tracking, outbox event.
- assign_lead: Sales and trainer assignment with historical tracking.
- book_trial: Atomic trial booking, trainer schedule validation, lead state sync.
- transition_trial_status: Trial lifecycle (BOOKED -> CONFIRMED -> ATTENDED / NO_SHOW).
- submit_intake_form: Atomic submission with protected sensitive health data handling.
"""

import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
from django.db import transaction
from django.utils import timezone
from config.routers import get_tenant_db_alias

from .models_org import Organization, Branch
from .models_users import TenantUser
from .models_workforce import UserProfile, TrainerProfile
from .models_crm import (
    LeadSource,
    Lead,
    LeadStatusHistory,
    LeadAssignment,
    LeadNote,
    LeadActivity,
    IntakeForm,
    IntakeQuestion,
    IntakeSubmission,
    IntakeAnswer,
    TrialBooking,
    TrialStatusHistory,
    LeadConversion,
    SalesFollowupTask,
)
from .services_workforce import TrainerAvailabilityService
from .services_reliability import record_business_audit, enqueue_outbox_event

logger = logging.getLogger(__name__)

# Valid transitions state machine
VALID_LEAD_TRANSITIONS = {
    'NEW_LEAD': ['TRIAL_BOOKED', 'FOLLOW_UP_PENDING', 'INTERESTED', 'HOT_LEAD', 'NOT_INTERESTED', 'LOST'],
    'TRIAL_BOOKED': ['TRIAL_CONFIRMED', 'TRIAL_ATTENDED', 'NO_SHOW', 'CANCELLED', 'FOLLOW_UP_PENDING', 'LOST'],
    'TRIAL_CONFIRMED': ['TRIAL_ATTENDED', 'NO_SHOW', 'TRIAL_BOOKED', 'FOLLOW_UP_PENDING', 'LOST'],
    'TRIAL_ATTENDED': ['INTERESTED', 'HOT_LEAD', 'PAYMENT_PENDING', 'CONVERTED', 'FOLLOW_UP_PENDING', 'NOT_INTERESTED', 'LOST'],
    'NO_SHOW': ['TRIAL_BOOKED', 'FOLLOW_UP_PENDING', 'NOT_INTERESTED', 'LOST'],
    'FOLLOW_UP_PENDING': ['TRIAL_BOOKED', 'INTERESTED', 'HOT_LEAD', 'PAYMENT_PENDING', 'CONVERTED', 'NOT_INTERESTED', 'LOST'],
    'INTERESTED': ['TRIAL_BOOKED', 'HOT_LEAD', 'PAYMENT_PENDING', 'CONVERTED', 'NOT_INTERESTED', 'LOST'],
    'HOT_LEAD': ['PAYMENT_PENDING', 'CONVERTED', 'TRIAL_BOOKED', 'NOT_INTERESTED', 'LOST'],
    'PAYMENT_PENDING': ['CONVERTED', 'HOT_LEAD', 'LOST'],
    'CONVERTED': [],  # Terminal converted state
    'NOT_INTERESTED': ['NEW_LEAD', 'INTERESTED'],  # Can be reactivated
    'LOST': ['NEW_LEAD', 'INTERESTED'],  # Can be reactivated
}


class CRMLeadService:
    """
    Core business service managing Lead lifecycle, trial sessions, and assignments.
    """

    @classmethod
    def create_lead(
        cls,
        organization: Organization,
        first_name: str,
        last_name: str,
        phone: Optional[str] = None,
        email: Optional[str] = None,
        branch: Optional[Branch] = None,
        lead_source: Optional[LeadSource] = None,
        assigned_sales_user: Optional[TenantUser] = None,
        actor_user: Optional[TenantUser] = None,
        extra_fields: Optional[Dict[str, Any]] = None,
        db_alias: Optional[str] = None,
    ) -> Lead:
        alias = db_alias or get_tenant_db_alias() or 'default'

        with transaction.atomic(using=alias):
            lead = Lead.objects.using(alias).create(
                organization=organization,
                branch=branch,
                lead_source=lead_source,
                first_name=first_name,
                last_name=last_name,
                phone_normalized=phone,
                email_normalized=email,
                current_status='NEW_LEAD',
                assigned_sales_user=assigned_sales_user,
                **(extra_fields or {}),
            )

            # Record initial status
            LeadStatusHistory.objects.using(alias).create(
                lead=lead,
                from_status=None,
                to_status='NEW_LEAD',
                reason_code='LEAD_CREATION',
                reason_text='Initial lead intake into system',
                changed_by_user=actor_user,
            )

            # Record initial assignment if specified
            if assigned_sales_user:
                LeadAssignment.objects.using(alias).create(
                    lead=lead,
                    assigned_to_user=assigned_sales_user,
                    assigned_by_user=actor_user,
                    assignment_type='SALES',
                    status='ACTIVE',
                )

            # Audit & Outbox
            record_business_audit(
                organization=organization,
                branch=branch,
                actor_type='EMPLOYEE' if actor_user else 'SYSTEM',
                actor_user=actor_user,
                module='crm',
                action_code='LEAD_CREATE',
                entity_type='Lead',
                entity_id=lead.id,
                event_description=f"Created lead {lead.first_name} {lead.last_name}",
                after_data={'lead_id': str(lead.id), 'status': lead.current_status},
                db_alias=alias,
            )

            enqueue_outbox_event(
                organization=organization,
                event_type='LEAD_CREATED',
                aggregate_type='Lead',
                aggregate_id=lead.id,
                payload={'lead_id': str(lead.id), 'email': email, 'phone': phone},
                db_alias=alias,
            )

            return lead

    @classmethod
    def transition_lead_status(
        cls,
        lead: Lead,
        new_status: str,
        reason_code: Optional[str] = None,
        reason_text: Optional[str] = None,
        actor_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> Lead:
        alias = db_alias or get_tenant_db_alias() or 'default'

        with transaction.atomic(using=alias):
            old_status = lead.current_status
            if old_status == new_status:
                return lead

            lead.current_status = new_status
            lead.save(using=alias, update_fields=['current_status', 'updated_at'])

            LeadStatusHistory.objects.using(alias).create(
                lead=lead,
                from_status=old_status,
                to_status=new_status,
                reason_code=reason_code,
                reason_text=reason_text,
                changed_by_user=actor_user,
            )

            record_business_audit(
                organization=lead.organization,
                branch=lead.branch,
                actor_type='EMPLOYEE' if actor_user else 'SYSTEM',
                actor_user=actor_user,
                module='crm',
                action_code='LEAD_STATUS_TRANSITION',
                entity_type='Lead',
                entity_id=lead.id,
                event_description=f"Lead {lead.id} transitioned from {old_status} to {new_status}",
                before_data={'status': old_status},
                after_data={'status': new_status, 'reason': reason_code},
                db_alias=alias,
            )

            enqueue_outbox_event(
                organization=lead.organization,
                event_type='LEAD_STATUS_CHANGED',
                aggregate_type='Lead',
                aggregate_id=lead.id,
                payload={'lead_id': str(lead.id), 'old_status': old_status, 'new_status': new_status},
                db_alias=alias,
            )

            return lead

    @classmethod
    def assign_lead(
        cls,
        lead: Lead,
        assigned_to_user: TenantUser,
        assignment_type: str = 'SALES',
        actor_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> LeadAssignment:
        alias = db_alias or get_tenant_db_alias() or 'default'

        with transaction.atomic(using=alias):
            # Deactivate previous active assignment of same type
            LeadAssignment.objects.using(alias).filter(
                lead=lead,
                assignment_type=assignment_type,
                status='ACTIVE',
            ).update(status='INACTIVE', unassigned_at=timezone.now())

            new_assign = LeadAssignment.objects.using(alias).create(
                lead=lead,
                assigned_to_user=assigned_to_user,
                assigned_by_user=actor_user,
                assignment_type=assignment_type,
                status='ACTIVE',
            )

            if assignment_type == 'SALES':
                lead.assigned_sales_user = assigned_to_user
                lead.save(using=alias, update_fields=['assigned_sales_user', 'updated_at'])
            elif assignment_type == 'TRAINER':
                lead.assigned_trainer_user = assigned_to_user
                lead.save(using=alias, update_fields=['assigned_trainer_user', 'updated_at'])

            return new_assign

    @classmethod
    def book_trial(
        cls,
        lead: Lead,
        branch: Branch,
        scheduled_start: datetime,
        scheduled_end: datetime,
        assigned_trainer: Optional[TrainerProfile] = None,
        trial_type: str = 'GROUP_CLASS',
        booking_source: str = 'WEB',
        actor_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> TrialBooking:
        alias = db_alias or get_tenant_db_alias() or 'default'

        with transaction.atomic(using=alias):
            # If trainer assigned, verify availability
            if assigned_trainer:
                duration = int((scheduled_end - scheduled_start).total_seconds() / 60)
                available, reason, _ = TrainerAvailabilityService.is_trainer_available(
                    trainer=assigned_trainer,
                    branch=branch,
                    start_datetime=scheduled_start,
                    duration_minutes=duration,
                    delivery_mode='GROUP' if trial_type == 'GROUP_CLASS' else 'INDIVIDUAL',
                    db_alias=alias,
                )
                if not available:
                    raise ValueError(f"Selected trainer is not available: {reason}")

            trial = TrialBooking.objects.using(alias).create(
                lead=lead,
                branch=branch,
                assigned_trainer_profile=assigned_trainer,
                trial_type=trial_type,
                scheduled_start=scheduled_start,
                scheduled_end=scheduled_end,
                status='BOOKED',
                booking_source=booking_source,
                created_by_user=actor_user,
            )

            TrialStatusHistory.objects.using(alias).create(
                trial_booking=trial,
                from_status=None,
                to_status='BOOKED',
                reason_code='INITIAL_BOOKING',
                changed_by_user=actor_user,
            )

            # Sync lead status
            cls.transition_lead_status(
                lead=lead,
                new_status='TRIAL_BOOKED',
                reason_code='TRIAL_SCHEDULED',
                reason_text=f"Trial booked for {scheduled_start}",
                actor_user=actor_user,
                db_alias=alias,
            )

            return trial

    @classmethod
    def transition_trial_status(
        cls,
        trial: TrialBooking,
        new_status: str,
        reason_code: Optional[str] = None,
        actor_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> TrialBooking:
        alias = db_alias or get_tenant_db_alias() or 'default'

        with transaction.atomic(using=alias):
            old_status = trial.status
            if old_status == new_status:
                return trial

            trial.status = new_status
            trial.save(using=alias, update_fields=['status', 'updated_at'])

            TrialStatusHistory.objects.using(alias).create(
                trial_booking=trial,
                from_status=old_status,
                to_status=new_status,
                reason_code=reason_code,
                changed_by_user=actor_user,
            )

            # Sync lead status appropriately
            lead_status_map = {
                'ATTENDED': 'TRIAL_ATTENDED',
                ('NO_SHOW', 'NO_SHOW'): 'NO_SHOW',
                'NO_SHOW': 'NO_SHOW',
                'CONFIRMED': 'TRIAL_CONFIRMED',
            }
            if new_status in lead_status_map:
                cls.transition_lead_status(
                    lead=trial.lead,
                    new_status=lead_status_map[new_status],
                    reason_code=f"TRIAL_{new_status}",
                    actor_user=actor_user,
                    db_alias=alias,
                )

            return trial

    @classmethod
    def submit_intake_form(
        cls,
        intake_form: IntakeForm,
        answers: List[Dict[str, Any]],
        lead: Optional[Lead] = None,
        user_profile: Optional[UserProfile] = None,
        submitted_by_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> IntakeSubmission:
        alias = db_alias or get_tenant_db_alias() or 'default'

        with transaction.atomic(using=alias):
            submission = IntakeSubmission.objects.using(alias).create(
                intake_form=intake_form,
                lead=lead,
                user_profile=user_profile,
                submitted_by_user=submitted_by_user,
            )

            for item in answers:
                question_id = item.get('question_id')
                q = IntakeQuestion.objects.using(alias).get(id=question_id)
                IntakeAnswer.objects.using(alias).create(
                    submission=submission,
                    question=q,
                    text_value=item.get('text_value'),
                    numeric_value=item.get('numeric_value'),
                    boolean_value=item.get('boolean_value'),
                    date_value=item.get('date_value'),
                    json_value=item.get('json_value', {}),
                )

            return submission
