"""
apps/tenant_core/services_stage_automation.py — Authoritative CRM Stage Automation & SLA Service

Handles:
1. Configurable event-driven lead stage transitions (FOLLOWUP_COMPLETED, TRIAL_BOOKED,
   TRIAL_RESCHEDULED, TRIAL_ATTENDED, TRIAL_NO_SHOW, TRIAL_CANCELLED, LEAD_CONVERTED).
2. Separation of Response SLA (frozen upon first qualifying contact) and Stage SLA
   (duration in current stage, resets on stage transition).
3. Idempotent execution preventing duplicate history or infinite loops.
4. Canonical defaults seeding and tenant-isolated configuration.
"""

import re
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
from django.db import transaction, models
from django.utils import timezone
from django.core.exceptions import ValidationError

from config.routers import get_tenant_db_alias
from .models_org import Organization
from .models_users import TenantUser
from .models_crm import (
    Lead,
    LeadStatusHistory,
    SalesFollowupTask,
    CRMStageSlaPolicy,
    CRMStageAutomationRule,
)
from .services_reliability import record_business_audit, enqueue_outbox_event


DEFAULT_POSITIVE_KEYWORDS = [
    'interested', 'trial', 'proceed', 'confirm', 'spoke', 'connected',
    'agreed', 'yes', 'scheduled', 'booked', 'sign', 'membership',
    'consult', 'positive', 'wants', 'joining', 'visit'
]

DEFAULT_UNSUCCESSFUL_KEYWORDS = [
    'no answer', 'did not pick', 'not reachable', 'switched off',
    'busy', 'wrong number', 'declined', 'callback later', 'call back later',
    'left voicemail', 'voicemail', 'not interested', 'invalid'
]


class CRMStageAutomationService:
    """
    Authoritative service governing configurable stage automation and SLA semantics.
    """

    @classmethod
    def seed_default_rules(
        cls,
        organization: Organization,
        db_alias: Optional[str] = None,
    ) -> List[CRMStageAutomationRule]:
        alias = db_alias or get_tenant_db_alias() or 'default'
        created_rules = []

        defaults = [
            {
                'name': 'Successful Follow-up to Interested',
                'trigger_event': 'FOLLOWUP_COMPLETED',
                'from_stage': 'NEW_LEAD',
                'to_stage': 'INTERESTED',
                'conditions': {
                    'positive_outcome_only': True,
                    'outcome_keywords': DEFAULT_POSITIVE_KEYWORDS,
                    'unsuccessful_keywords': DEFAULT_UNSUCCESSFUL_KEYWORDS,
                },
                'priority': 10,
                'is_enabled': True,
            },
            {
                'name': 'Trial Booking to Trial Booked',
                'trigger_event': 'TRIAL_BOOKED',
                'from_stage': 'ANY',
                'to_stage': 'TRIAL_BOOKED',
                'conditions': {},
                'priority': 20,
                'is_enabled': True,
            },
            {
                'name': 'Trial Reschedule Maintains Trial Booked',
                'trigger_event': 'TRIAL_RESCHEDULED',
                'from_stage': 'ANY',
                'to_stage': 'TRIAL_BOOKED',
                'conditions': {},
                'priority': 25,
                'is_enabled': True,
            },
            {
                'name': 'Trial Attended Stage Transition',
                'trigger_event': 'TRIAL_ATTENDED',
                'from_stage': 'ANY',
                'to_stage': 'TRIAL_ATTENDED',
                'conditions': {},
                'priority': 30,
                'is_enabled': True,
            },
            {
                'name': 'Trial No-Show Stage Transition',
                'trigger_event': 'TRIAL_NO_SHOW',
                'from_stage': 'ANY',
                'to_stage': 'NO_SHOW',
                'conditions': {},
                'priority': 40,
                'is_enabled': True,
            },
            {
                'name': 'Trial Cancelled to Follow-up Pending',
                'trigger_event': 'TRIAL_CANCELLED',
                'from_stage': 'ANY',
                'to_stage': 'FOLLOW_UP_PENDING',
                'conditions': {},
                'priority': 50,
                'is_enabled': True,
            },
            {
                'name': 'Commercial Conversion to Converted Member',
                'trigger_event': 'LEAD_CONVERTED',
                'from_stage': 'ANY',
                'to_stage': 'CONVERTED',
                'conditions': {},
                'priority': 60,
                'is_enabled': True,
            },
        ]

        with transaction.atomic(using=alias):
            for d in defaults:
                rule, _ = CRMStageAutomationRule.objects.using(alias).get_or_create(
                    organization=organization,
                    name=d['name'],
                    defaults=d,
                )
                created_rules.append(rule)

        return created_rules

    @classmethod
    def get_rules_for_event(
        cls,
        organization: Organization,
        trigger_event: str,
        db_alias: Optional[str] = None,
    ) -> List[CRMStageAutomationRule]:
        alias = db_alias or get_tenant_db_alias() or 'default'
        # Check if tenant has any rules configured; if none, auto-seed canonical defaults
        if not CRMStageAutomationRule.objects.using(alias).filter(organization=organization).exists():
            cls.seed_default_rules(organization, db_alias=alias)

        return list(
            CRMStageAutomationRule.objects.using(alias)
            .filter(
                organization=organization,
                trigger_event=trigger_event,
                is_enabled=True,
            )
            .order_by('priority', 'created_at')
        )

    @classmethod
    def record_response_sla(
        cls,
        lead: Lead,
        responding_user: Optional[TenantUser] = None,
        response_at: Optional[datetime] = None,
        db_alias: Optional[str] = None,
    ) -> Lead:
        """
        Freezes the response SLA once the first qualifying response occurs.
        A qualifying response is an outbound contact / completed follow-up task.
        Guarantees that the response time is permanently recorded and never increments further.
        """
        alias = db_alias or get_tenant_db_alias() or 'default'
        if lead.response_sla_status in ('MET', 'BREACHED') and lead.first_response_at:
            return lead  # Already frozen historically

        now = response_at or timezone.now()
        lead.first_response_at = now
        elapsed_seconds = max(0, int((now - lead.created_at).total_seconds()))
        lead.first_response_time_seconds = elapsed_seconds

        # Look up response target policy for NEW_LEAD
        policy = CRMStageSlaPolicy.objects.using(alias).filter(
            organization=lead.organization,
            canonical_stage='NEW_LEAD',
            is_enabled=True,
        ).first()

        if policy:
            val = policy.response_target_value
            unit = policy.response_target_unit
            multiplier = 60 if unit == 'MINUTES' else (3600 if unit == 'HOURS' else 86400)
            target_seconds = val * multiplier
            lead.response_sla_status = 'MET' if elapsed_seconds <= target_seconds else 'BREACHED'
        else:
            # Default 15 minutes if not configured
            lead.response_sla_status = 'MET' if elapsed_seconds <= (15 * 60) else 'BREACHED'

        lead.save(using=alias, update_fields=[
            'first_response_at',
            'first_response_time_seconds',
            'response_sla_status',
            'updated_at',
        ])

        record_business_audit(
            organization=lead.organization,
            branch=lead.branch,
            actor_type='EMPLOYEE' if responding_user else 'SYSTEM',
            actor_user=responding_user,
            module='crm',
            action_code='CRM_RESPONSE_SLA_RECORDED',
            entity_type='Lead',
            entity_id=lead.id,
            event_description=f"Response SLA recorded: {lead.response_sla_status} ({elapsed_seconds}s)",
            after_data={
                'response_sla_status': lead.response_sla_status,
                'first_response_time_seconds': elapsed_seconds,
                'first_response_at': now.isoformat(),
            },
            db_alias=alias,
        )

        return lead

    @classmethod
    def evaluate_and_transition(
        cls,
        lead: Lead,
        trigger_event: str,
        context: Optional[Dict[str, Any]] = None,
        actor_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> Tuple[bool, Optional[CRMStageAutomationRule], Lead]:
        """
        Evaluates stage automation rules for the given event and transitions the lead if conditions match.
        Returns: (did_transition, matched_rule, updated_lead)
        """
        alias = db_alias or get_tenant_db_alias() or 'default'
        ctx = context or {}

        # Converted leads are terminal and never transition via standard stage rules
        if lead.current_status == 'CONVERTED' and trigger_event != 'LEAD_CONVERTED':
            return False, None, lead

        rules = cls.get_rules_for_event(lead.organization, trigger_event, db_alias=alias)
        if not rules:
            return False, None, lead

        for rule in rules:
            # 1. From-stage check
            if rule.from_stage != 'ANY' and rule.from_stage != lead.current_status:
                continue

            # 2. Idempotency check: if lead is already in target stage, do nothing
            if lead.current_status == rule.to_stage:
                return False, rule, lead

            # 3. Condition checks
            conditions = rule.conditions or {}

            # Follow-up task type filter
            allowed_task_types = conditions.get('task_types')
            if allowed_task_types and allowed_task_types != 'ANY':
                current_task_type = ctx.get('task_type')
                if isinstance(allowed_task_types, list):
                    if current_task_type not in allowed_task_types:
                        continue
                elif str(allowed_task_types).upper() != str(current_task_type).upper():
                    continue

            # Follow-up outcome checks
            outcome_text = str(ctx.get('outcome') or '').lower().strip()
            if conditions.get('positive_outcome_only'):
                unsuccessful_keywords = conditions.get('unsuccessful_keywords') or DEFAULT_UNSUCCESSFUL_KEYWORDS
                is_unsuccessful = any(re.search(rf"\b{re.escape(k)}\b", outcome_text) or (k in outcome_text) for k in unsuccessful_keywords)

                outcome_keywords = conditions.get('outcome_keywords') or DEFAULT_POSITIVE_KEYWORDS
                has_positive = any(re.search(rf"\b{re.escape(k)}\b", outcome_text) or (k in outcome_text) for k in outcome_keywords)

                if is_unsuccessful and not has_positive:
                    continue  # Negative / unsuccessful outcome does not transition
                if not has_positive and outcome_keywords:
                    continue  # No positive signal found

            # 4. Execute transition
            from .services_crm import CRMLeadService
            updated_lead = CRMLeadService.transition_lead_status(
                lead=lead,
                new_status=rule.to_stage,
                reason_code='STAGE_AUTOMATION',
                reason_text=f"Automatic transition via rule: '{rule.name}' (Trigger: {rule.get_trigger_event_display()})",
                actor_user=actor_user,
                db_alias=alias,
            )

            return True, rule, updated_lead

        return False, None, lead
