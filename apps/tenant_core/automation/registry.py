"""
Automation Registries: Triggers, Conditions, Operators, and Actions.
Designed to be generic, provider-agnostic, and future-integration ready.
Modules (CRM, Commerce, AI, etc.) register their capabilities here.
"""
from typing import Dict, Any, List, Optional, Tuple, Type
import logging
from django.utils import timezone
from datetime import timedelta

logger = logging.getLogger(__name__)


# =============================================================================
# CONDITION OPERATOR REGISTRY
# =============================================================================

class ConditionOperatorRegistry:
    OPERATORS = {
        'EQUALS': {'label': 'Equals', 'applies_to': ['string', 'number', 'boolean', 'enum', 'id']},
        'NOT_EQUALS': {'label': 'Does Not Equal', 'applies_to': ['string', 'number', 'boolean', 'enum', 'id']},
        'IN': {'label': 'Is Any Of', 'applies_to': ['string', 'enum', 'id']},
        'NOT_IN': {'label': 'Is Not Any Of', 'applies_to': ['string', 'enum', 'id']},
        'IS_EMPTY': {'label': 'Is Empty', 'applies_to': ['string', 'number', 'id']},
        'IS_NOT_EMPTY': {'label': 'Is Not Empty', 'applies_to': ['string', 'number', 'id']},
        'GREATER_THAN': {'label': 'Greater Than', 'applies_to': ['number', 'date', 'datetime']},
        'LESS_THAN': {'label': 'Less Than', 'applies_to': ['number', 'date', 'datetime']},
        'CONTAINS': {'label': 'Contains', 'applies_to': ['string']},
    }

    @classmethod
    def get_metadata(cls) -> List[Dict[str, Any]]:
        return [{'code': k, **v} for k, v in cls.OPERATORS.items()]

    @classmethod
    def list_operators(cls) -> List[Dict[str, Any]]:
        return cls.get_metadata()

    @classmethod
    def evaluate(cls, operator: str, actual_value: Any, expected_value: Any) -> bool:
        """
        Safely evaluates an operator without eval(), exec(), or arbitrary code execution.
        """
        op = operator.upper()
        if op == 'IS_EMPTY':
            return actual_value is None or actual_value == '' or actual_value == [] or actual_value == {}
        if op == 'IS_NOT_EMPTY':
            return actual_value is not None and actual_value != '' and actual_value != [] and actual_value != {}

        if actual_value is None:
            return False

        # Normalize string comparisons
        if isinstance(actual_value, str) and isinstance(expected_value, str):
            actual_str = actual_value.strip().lower()
            expected_str = expected_value.strip().lower()
            if op == 'EQUALS':
                return actual_str == expected_str
            if op == 'NOT_EQUALS':
                return actual_str != expected_str
            if op == 'CONTAINS':
                return expected_str in actual_str

        if op == 'EQUALS':
            return str(actual_value).strip().lower() == str(expected_value).strip().lower()
        elif op == 'NOT_EQUALS':
            return str(actual_value).strip().lower() != str(expected_value).strip().lower()
        elif op == 'IN':
            if isinstance(expected_value, (list, tuple, set)):
                norm_expected = [str(x).strip().lower() for x in expected_value]
                return str(actual_value).strip().lower() in norm_expected
            return str(actual_value).strip().lower() == str(expected_value).strip().lower()
        elif op == 'NOT_IN':
            if isinstance(expected_value, (list, tuple, set)):
                norm_expected = [str(x).strip().lower() for x in expected_value]
                return str(actual_value).strip().lower() not in norm_expected
            return str(actual_value).strip().lower() != str(expected_value).strip().lower()
        elif op == 'GREATER_THAN':
            try:
                return float(actual_value) > float(expected_value)
            except (ValueError, TypeError):
                return False
        elif op == 'LESS_THAN':
            try:
                return float(actual_value) < float(expected_value)
            except (ValueError, TypeError):
                return False

        return False


# =============================================================================
# CONDITION FIELD REGISTRY
# =============================================================================

class ConditionFieldRegistry:
    """
    Allowlist of safe queryable fields per entity/context.
    Never exposes arbitrary database attributes to tenant input.
    """
    FIELDS = {
        'lead.source': {
            'label': 'Lead Source',
            'type': 'enum',
            'value_source': 'lead_sources',
            'allowed_operators': ['EQUALS', 'NOT_EQUALS', 'IN', 'NOT_IN'],
        },
        'lead.stage': {
            'label': 'Lead Stage',
            'type': 'enum',
            'value_source': 'lead_stages',
            'allowed_operators': ['EQUALS', 'NOT_EQUALS', 'IN', 'NOT_IN'],
        },
        'lead.branch': {
            'label': 'Branch',
            'type': 'id',
            'value_source': 'branches',
            'allowed_operators': ['EQUALS', 'NOT_EQUALS', 'IN', 'NOT_IN'],
        },
        'lead.program': {
            'label': 'Interested Program',
            'type': 'id',
            'value_source': 'programs',
            'allowed_operators': ['EQUALS', 'NOT_EQUALS', 'IN', 'NOT_IN', 'IS_EMPTY', 'IS_NOT_EMPTY'],
        },
        'lead.assigned_agent': {
            'label': 'Assigned Sales Agent',
            'type': 'id',
            'value_source': 'users',
            'allowed_operators': ['EQUALS', 'NOT_EQUALS', 'IS_EMPTY', 'IS_NOT_EMPTY'],
        },
        'lead.gender': {
            'label': 'Gender',
            'type': 'enum',
            'value_source': 'genders',
            'allowed_operators': ['EQUALS', 'NOT_EQUALS'],
        },
        'lead.do_not_contact': {
            'label': 'Do Not Contact',
            'type': 'boolean',
            'allowed_operators': ['EQUALS'],
        },
        'trial.status': {
            'label': 'Trial Status',
            'type': 'enum',
            'value_source': 'trial_statuses',
            'allowed_operators': ['EQUALS', 'NOT_EQUALS', 'IN', 'NOT_IN'],
        },
        'trial.confirmation_status': {
            'label': 'Trial Confirmation Status',
            'type': 'enum',
            'value_source': 'trial_confirmation_statuses',
            'allowed_operators': ['EQUALS', 'NOT_EQUALS'],
        },
        'trial.branch': {
            'label': 'Trial Branch',
            'type': 'id',
            'value_source': 'branches',
            'allowed_operators': ['EQUALS', 'NOT_EQUALS'],
        },
        'communication.channel': {
            'label': 'Communication Channel',
            'type': 'enum',
            'value_source': 'communication_channels',
            'allowed_operators': ['EQUALS', 'NOT_EQUALS', 'IN'],
        },
        'communication.status': {
            'label': 'Communication Status',
            'type': 'enum',
            'value_source': 'communication_statuses',
            'allowed_operators': ['EQUALS', 'NOT_EQUALS'],
        },
        'communication.customer_replied': {
            'label': 'Customer Replied',
            'type': 'boolean',
            'allowed_operators': ['EQUALS'],
        },
    }

    @classmethod
    def get_field_def(cls, field_name: str) -> Optional[Dict[str, Any]]:
        return cls.FIELDS.get(field_name)

    @classmethod
    def get_metadata(cls) -> List[Dict[str, Any]]:
        return [{'field': k, **v} for k, v in cls.FIELDS.items()]

    @classmethod
    def list_fields(cls) -> List[Dict[str, Any]]:
        return cls.get_metadata()


# =============================================================================
# TRIGGER REGISTRY
# =============================================================================

class TriggerRegistry:
    """
    Registry of canonical system event triggers.
    Exposes safe metadata to the frontend builder.
    """
    TRIGGERS = {
        'LEAD_CREATED': {
            'display_name': 'Lead Created',
            'domain': 'crm',
            'description': 'Fires whenever a new Lead is registered through any source.',
            'supported_fields': ['lead.source', 'lead.stage', 'lead.branch', 'lead.program', 'lead.assigned_agent', 'lead.gender'],
        },
        'LEAD_STAGE_CHANGED': {
            'display_name': 'Lead Stage Changed',
            'domain': 'crm',
            'description': 'Fires when a Lead moves from one status stage to another.',
            'supported_fields': ['lead.stage', 'lead.source', 'lead.branch', 'lead.assigned_agent'],
        },
        'LEAD_ASSIGNED': {
            'display_name': 'Lead Assigned to Agent',
            'domain': 'crm',
            'description': 'Fires when an agent is assigned to or reassigned on a Lead.',
            'supported_fields': ['lead.assigned_agent', 'lead.branch', 'lead.stage'],
        },
        'TRIAL_BOOKED': {
            'display_name': 'Trial Booked',
            'domain': 'crm',
            'description': 'Fires when a new TrialBooking is created.',
            'supported_fields': ['trial.status', 'trial.confirmation_status', 'trial.branch', 'lead.source', 'lead.branch'],
        },
        'TRIAL_CONFIRMED': {
            'display_name': 'Trial Confirmed',
            'domain': 'crm',
            'description': 'Fires when a member/lead confirms their upcoming trial session.',
            'supported_fields': ['trial.status', 'trial.confirmation_status', 'trial.branch'],
        },
        'TRIAL_RESCHEDULED': {
            'display_name': 'Trial Rescheduled',
            'domain': 'crm',
            'description': 'Fires when a trial is rescheduled to a new class slot.',
            'supported_fields': ['trial.status', 'trial.branch'],
        },
        'TRIAL_CANCELLED': {
            'display_name': 'Trial Cancelled',
            'domain': 'crm',
            'description': 'Fires when a trial booking is cancelled.',
            'supported_fields': ['trial.status', 'trial.branch'],
        },
        'TRIAL_ATTENDED': {
            'display_name': 'Trial Attended',
            'domain': 'crm',
            'description': 'Fires when a lead completes attendance at their booked trial.',
            'supported_fields': ['trial.status', 'trial.branch'],
        },
        'TRIAL_NO_SHOW': {
            'display_name': 'Trial No-Show',
            'domain': 'crm',
            'description': 'Fires when a lead fails to attend a booked trial slot.',
            'supported_fields': ['trial.status', 'trial.branch'],
        },
        'FOLLOWUP_CREATED': {
            'display_name': 'Follow-up Task Created',
            'domain': 'crm',
            'description': 'Fires when a new sales follow-up task is scheduled.',
            'supported_fields': ['lead.branch', 'lead.stage'],
        },
        'FOLLOWUP_COMPLETED': {
            'display_name': 'Follow-up Task Completed',
            'domain': 'crm',
            'description': 'Fires when a sales agent marks a follow-up task completed.',
            'supported_fields': ['lead.branch', 'lead.stage'],
        },
        'COMMUNICATION_RECEIVED': {
            'display_name': 'Inbound Message Received',
            'domain': 'communication',
            'description': 'Fires when an inbound WhatsApp, SMS, or email is received.',
            'supported_fields': ['communication.channel', 'communication.customer_replied', 'lead.branch'],
        },
        'COMMUNICATION_DELIVERED': {
            'display_name': 'Message Delivered',
            'domain': 'communication',
            'description': 'Fires upon provider delivery confirmation.',
            'supported_fields': ['communication.channel', 'communication.status'],
        },
        'COMMUNICATION_FAILED': {
            'display_name': 'Message Delivery Failed',
            'domain': 'communication',
            'description': 'Fires when an outbound communication permanently fails delivery.',
            'supported_fields': ['communication.channel', 'communication.status'],
        },
        'CRM_LEAD_SLA_BREACHED': {
            'display_name': 'Lead Stage SLA Breached',
            'domain': 'crm',
            'description': 'Fires when an active lead stage SLA target response window elapses without transition.',
            'supported_fields': ['lead.stage', 'lead.branch', 'lead.assigned_agent', 'lead.source'],
        },
    }

    @classmethod
    def get_metadata(cls) -> List[Dict[str, Any]]:
        return [{'code': k, **v} for k, v in cls.TRIGGERS.items()]

    @classmethod
    def list_triggers(cls) -> List[Dict[str, Any]]:
        return cls.get_metadata()

    @classmethod
    def is_valid_trigger(cls, code: str) -> bool:
        return code in cls.TRIGGERS


# =============================================================================
# ACTION HANDLER BASE & REGISTRY
# =============================================================================

class BaseActionHandler:
    code = ""
    domain = ""
    display_name = ""
    config_schema: Dict[str, Any] = {}

    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> Tuple[bool, str]:
        """Validates action configuration structure before workflow publishing."""
        return True, ""

    @classmethod
    def execute(
        cls,
        execution: Any,
        step_id: str,
        config: Dict[str, Any],
        context: Dict[str, Any],
        db_alias: str = 'default'
    ) -> Dict[str, Any]:
        """Executes the action via authoritative domain service."""
        raise NotImplementedError


class AssignLeadAction(BaseActionHandler):
    code = "ASSIGN_LEAD"
    domain = "crm"
    display_name = "Assign Lead to Agent"
    config_schema = {
        'strategy': {'type': 'enum', 'options': ['SPECIFIC_USER', 'ROUND_ROBIN', 'BRANCH_DEFAULT'], 'required': True},
        'assigned_to_user_id': {'type': 'uuid', 'required': False},
    }

    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> Tuple[bool, str]:
        strategy = config.get('strategy')
        if not strategy or strategy not in ['SPECIFIC_USER', 'ROUND_ROBIN', 'BRANCH_DEFAULT']:
            return False, "Strategy must be SPECIFIC_USER, ROUND_ROBIN, or BRANCH_DEFAULT"
        if strategy == 'SPECIFIC_USER' and not (config.get('assigned_to_user_id') or config.get('specific_user_id')):
            return False, "assigned_to_user_id is required when strategy is SPECIFIC_USER"
        return True, ""

    @classmethod
    def execute(cls, execution=None, step_id='step', config=None, context=None, db_alias='default', **kwargs):
        config = config or kwargs.get('config', {})
        context = context or kwargs.get('context', {})
        from apps.tenant_core.services_crm import CRMLeadService
        from apps.tenant_core.models_crm import Lead
        from apps.tenant_core.models_users import TenantUser

        lead_id = context.get('lead_id')
        if not lead_id:
            return {'status': 'SKIPPED', 'reason': 'No lead_id in context'}

        lead = Lead.objects.using(db_alias).filter(id=lead_id).first()
        if not lead:
            return {'status': 'FAILED', 'error': f"Lead {lead_id} not found"}

        if lead.current_status == 'CONVERTED':
            return {'status': 'FAILED', 'error': 'Lead already converted', 'reason': 'LEAD_ALREADY_CONVERTED'}

        strategy = config.get('strategy')
        target_user = None
        if strategy == 'SPECIFIC_USER':
            uid = config.get('assigned_to_user_id') or config.get('specific_user_id')
            target_user = TenantUser.objects.using(db_alias).filter(id=uid).first()
            if not target_user:
                return {'status': 'FAILED', 'error': f"Target user {uid} not found"}
        elif strategy in ['ROUND_ROBIN', 'BRANCH_DEFAULT']:
            from apps.tenant_core.models_users import UserBranch
            ub = UserBranch.objects.using(db_alias).filter(branch=lead.branch, is_active=True).select_related('user').first()
            if ub:
                target_user = ub.user

        if target_user:
            actor = getattr(getattr(execution, 'workflow', None), 'created_by_user', None)
            CRMLeadService.assign_lead(
                lead=lead,
                assigned_to_user=target_user,
                assignment_type='SALES',
                actor_user=actor,
                db_alias=db_alias,
            )
            return {'status': 'SUCCESS', 'assigned_to_user_id': str(target_user.id)}

        return {'status': 'SKIPPED', 'reason': 'No eligible agent found for assignment'}


class CreateFollowupAction(BaseActionHandler):
    code = "CREATE_FOLLOWUP"
    domain = "crm"
    display_name = "Create Follow-up Task"
    config_schema = {
        'task_type': {'type': 'enum', 'options': ['CALL', 'WHATSAPP', 'EMAIL', 'MEETING', 'OTHER'], 'required': True},
        'priority': {'type': 'enum', 'options': ['LOW', 'NORMAL', 'HIGH', 'URGENT'], 'default': 'NORMAL'},
        'delay_minutes': {'type': 'integer', 'default': 60},
        'notes': {'type': 'string', 'default': ''},
        'assigned_to': {'type': 'enum', 'options': ['LEAD_OWNER', 'SPECIFIC_USER'], 'default': 'LEAD_OWNER'},
        'assigned_to_user_id': {'type': 'uuid', 'required': False},
    }

    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> Tuple[bool, str]:
        if not config.get('task_type') and not config.get('title'):
            return False, "task_type or title is required"
        return True, ""

    @classmethod
    def execute(cls, execution=None, step_id='step', config=None, context=None, db_alias='default', **kwargs):
        config = config or kwargs.get('config', {})
        context = context or kwargs.get('context', {})
        from apps.tenant_core.models_crm import Lead, SalesFollowupTask
        from apps.tenant_core.models_users import TenantUser

        lead_id = context.get('lead_id')
        if not lead_id:
            return {'status': 'SKIPPED', 'reason': 'No lead_id in context'}

        lead = Lead.objects.using(db_alias).filter(id=lead_id).first()
        if not lead:
            return {'status': 'FAILED', 'error': f"Lead {lead_id} not found"}

        assigned_user = None
        if config.get('assigned_to') == 'SPECIFIC_USER' and config.get('assigned_to_user_id'):
            assigned_user = TenantUser.objects.using(db_alias).filter(id=config.get('assigned_to_user_id')).first()
        if not assigned_user:
            actor = getattr(getattr(execution, 'workflow', None), 'created_by_user', None)
            assigned_user = lead.assigned_sales_user or actor or TenantUser.objects.using(db_alias).filter(organization=lead.organization).first()

        if not assigned_user:
            return {'status': 'FAILED', 'error': 'No user available to assign follow-up task'}

        delay = int(config.get('delay_minutes') or (int(config.get('offset_hours', 1)) * 60 if config.get('offset_hours') else 60))
        due_at = timezone.now() + timedelta(minutes=delay)
        exec_id = getattr(execution, 'id', None) or context.get('execution_id') or str(uuid.uuid4())
        ext_ref = f"AUTO_EXEC_{exec_id}"

        # DB idempotency check
        existing = SalesFollowupTask.objects.using(db_alias).filter(
            lead=lead, external_reference=ext_ref
        ).first()
        if existing:
            return {'status': 'ALREADY_EXISTS', 'task_id': str(existing.id), 'followup_id': str(existing.id)}

        task = SalesFollowupTask.objects.using(db_alias).create(
            lead=lead,
            assigned_to_user=assigned_user,
            created_by_user=assigned_user,
            task_type=config.get('task_type', 'CALL'),
            priority=config.get('priority', 'NORMAL'),
            due_at=due_at,
            outcome=config.get('title') or config.get('notes') or 'Follow-up Task',
            external_reference=ext_ref,
        )
        return {'status': 'SUCCESS', 'task_id': str(task.id), 'followup_id': str(task.id)}


class SendCommunicationAction(BaseActionHandler):
    code = "SEND_COMMUNICATION"
    domain = "communication"
    display_name = "Send Message (WhatsApp/Email/SMS)"
    config_schema = {
        'channel': {'type': 'enum', 'options': ['WHATSAPP', 'EMAIL', 'SMS'], 'required': True},
        'template_id': {'type': 'uuid', 'required': False},
        'body': {'type': 'string', 'required': False},
        'purpose': {'type': 'enum', 'options': ['TRANSACTIONAL', 'MARKETING'], 'default': 'TRANSACTIONAL'},
    }

    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> Tuple[bool, str]:
        if not config.get('channel'):
            return False, "Channel is required (WHATSAPP, EMAIL, SMS)"
        if not config.get('template_id') and not config.get('body'):
            return False, "Either template_id or body must be configured"
        return True, ""

    @classmethod
    def execute(cls, execution=None, step_id='step', config=None, context=None, db_alias='default', **kwargs):
        config = config or kwargs.get('config', {})
        context = context or kwargs.get('context', {})
        from apps.tenant_core.communication.service import CommunicationService
        from apps.tenant_core.models_crm import Lead, TrialBooking
        from apps.tenant_core.models_govern import NotificationTemplate

        # Cancelled trial guard
        trial_id = context.get('trial_id')
        if trial_id:
            trial = TrialBooking.objects.using(db_alias).filter(id=trial_id).first()
            if trial and (trial.status == 'CANCELLED' or getattr(trial, 'confirmation_status', None) == 'CANCELLED'):
                return {'status': 'FAILED', 'error': 'Trial is cancelled', 'reason': 'TRIAL_CANCELLED'}

        lead_id = context.get('lead_id')
        lead = Lead.objects.using(db_alias).filter(id=lead_id).first() if lead_id else None
        if not lead:
            return {'status': 'SKIPPED', 'reason': 'No valid lead found in context for communication'}

        channel = config.get('channel', 'WHATSAPP').upper()
        purpose = config.get('purpose', 'TRANSACTIONAL').upper()
        recipient = lead.phone_normalized if channel in ['WHATSAPP', 'SMS'] else lead.email_normalized
        if not recipient:
            return {'status': 'SKIPPED', 'reason': f"Lead has no valid {channel} recipient contact"}

        template = None
        if config.get('template_id'):
            template = NotificationTemplate.objects.using(db_alias).filter(id=config.get('template_id')).first()

        exec_id = getattr(execution, 'id', None) or context.get('execution_id') or str(uuid.uuid4())
        idempotency_key = f"AUTO_MSG_{exec_id}_{step_id}"

        # Context interpolation
        merged_context = {
            'first_name': lead.first_name,
            'last_name': lead.last_name,
            'lead_name': f"{lead.first_name} {lead.last_name}".strip(),
            'branch_name': lead.branch.name if lead.branch else 'Fitness Club',
            **context,
        }

        body = config.get('body', '')
        if body:
            for k, v in merged_context.items():
                body = body.replace(f"{{{{{k}}}}}", str(v))

        org = getattr(execution, 'organization', None) or lead.organization
        actor = getattr(getattr(execution, 'workflow', None), 'created_by_user', None)

        msg = CommunicationService.send_communication(
            organization=org,
            channel=channel,
            recipient=recipient,
            lead=lead,
            template=template,
            context_data=merged_context,
            body=body,
            purpose=purpose,
            idempotency_key=idempotency_key,
            created_by_user=actor,
            trigger_type='AUTOMATION',
        )

        return {
            'status': 'SUCCESS' if msg.status in ['QUEUED', 'SENT', 'DELIVERED', 'READ'] else 'FAILED',
            'message_id': str(msg.id),
            'delivery_status': msg.status,
            'failure_code': msg.failure_code,
        }


class ChangeLeadStageAction(BaseActionHandler):
    code = "CHANGE_LEAD_STAGE"
    domain = "crm"
    display_name = "Change Lead Stage"
    config_schema = {
        'new_stage': {'type': 'enum', 'value_source': 'lead_stages', 'required': True},
        'reason': {'type': 'string', 'default': 'Automated workflow transition'},
    }

    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> Tuple[bool, str]:
        if not config.get('new_stage') and not config.get('target_stage'):
            return False, "new_stage is required"
        return True, ""

    @classmethod
    def execute(cls, execution=None, step_id='step', config=None, context=None, db_alias='default', **kwargs):
        config = config or kwargs.get('config', {})
        context = context or kwargs.get('context', {})
        from apps.tenant_core.services_crm import CRMLeadService
        from apps.tenant_core.models_crm import Lead

        lead_id = context.get('lead_id')
        lead = Lead.objects.using(db_alias).filter(id=lead_id).first() if lead_id else None
        if not lead:
            return {'status': 'SKIPPED', 'reason': 'No lead_id in context'}

        new_stage = config.get('new_stage') or config.get('target_stage')
        valid_stages = [c[0] for c in Lead.STATUSES]
        if new_stage not in valid_stages:
            return {'status': 'FAILED', 'error': f"Invalid target stage: {new_stage}"}

        reason = config.get('reason', 'Automated workflow transition')
        actor = getattr(getattr(execution, 'workflow', None), 'created_by_user', None)

        try:
            CRMLeadService.transition_lead_status(
                lead=lead,
                new_status=new_stage,
                actor_user=actor,
                reason_text=reason,
                db_alias=db_alias,
            )
            return {'status': 'SUCCESS', 'new_stage': new_stage}
        except Exception as exc:
            return {'status': 'FAILED', 'error': str(exc)}


class AddLeadNoteAction(BaseActionHandler):
    code = "ADD_LEAD_NOTE"
    domain = "crm"
    display_name = "Add Lead Activity Note"
    config_schema = {
        'note_text': {'type': 'string', 'required': True},
    }

    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> Tuple[bool, str]:
        if not config.get('note_text'):
            return False, "note_text is required"
        return True, ""

    @classmethod
    def execute(cls, execution=None, step_id='step', config=None, context=None, db_alias='default', **kwargs):
        config = config or kwargs.get('config', {})
        context = context or kwargs.get('context', {})
        from apps.tenant_core.models_crm import Lead, LeadNote

        lead_id = context.get('lead_id')
        lead = Lead.objects.using(db_alias).filter(id=lead_id).first() if lead_id else None
        if not lead:
            return {'status': 'SKIPPED', 'reason': 'No lead_id in context'}

        note_text = config.get('note_text', '')
        actor = getattr(getattr(execution, 'workflow', None), 'created_by_user', None) or lead.assigned_sales_user
        if not actor:
            from apps.tenant_core.models_users import TenantUser
            actor = TenantUser.objects.using(db_alias).filter(organization=lead.organization).first()

        note = LeadNote.objects.using(db_alias).create(
            lead=lead,
            created_by_user=actor,
            note_text=note_text,
        )
        return {'status': 'SUCCESS', 'note_id': str(note.id)}


class ActionRegistry:
    """
    Central action registry.
    Future platform modules (Commerce, AI Vision, Assessment, Calendar)
    can plug new actions in without altering the automation engine.
    """
    _HANDLERS: Dict[str, Type[BaseActionHandler]] = {
        'ASSIGN_LEAD': AssignLeadAction,
        'CREATE_FOLLOWUP': CreateFollowupAction,
        'SEND_COMMUNICATION': SendCommunicationAction,
        'CHANGE_LEAD_STAGE': ChangeLeadStageAction,
        'ADD_LEAD_NOTE': AddLeadNoteAction,
    }

    @classmethod
    def register(cls, handler_cls: Type[BaseActionHandler]):
        cls._HANDLERS[handler_cls.code] = handler_cls

    @classmethod
    def get_handler(cls, code: str) -> Optional[Type[BaseActionHandler]]:
        return cls._HANDLERS.get(code)

    @classmethod
    def is_valid_action(cls, code: str) -> bool:
        return code in cls._HANDLERS

    @classmethod
    def get_metadata(cls) -> List[Dict[str, Any]]:
        return [
            {
                'code': h.code,
                'domain': h.domain,
                'display_name': h.display_name,
                'config_schema': h.config_schema,
            }
            for h in cls._HANDLERS.values()
        ]

    @classmethod
    def list_actions(cls) -> List[Dict[str, Any]]:
        return cls.get_metadata()


class RecommendationActionRegistry:
    """
    Phase 7 Next Best Action registry.
    Separates recommendation advisory capabilities from executable automation handlers.
    Can reference both executable ActionRegistry actions and user-confirmed UI operations.
    """
    _RECOMMENDATIONS = {
        'ASSIGN_LEAD': {
            'code': 'ASSIGN_LEAD',
            'display_name': 'Assign Sales Agent',
            'domain': 'crm',
            'is_automation_executable': True,
            'target_ui_action': 'ASSIGN_AGENT',
            'required_permission': 'crm.leads.edit',
            'description': 'Assign an eligible active sales agent to this lead.',
        },
        'CREATE_FOLLOWUP': {
            'code': 'CREATE_FOLLOWUP',
            'display_name': 'Schedule Follow-up',
            'domain': 'crm',
            'is_automation_executable': True,
            'target_ui_action': 'SCHEDULE_FOLLOWUP',
            'required_permission': 'crm.leads.edit',
            'description': 'Schedule a targeted follow-up task with due date.',
        },
        'SEND_COMMUNICATION': {
            'code': 'SEND_COMMUNICATION',
            'display_name': 'Send Follow-up Message',
            'domain': 'communication',
            'is_automation_executable': True,
            'target_ui_action': 'SEND_MESSAGE',
            'required_permission': 'crm.communication.send',
            'description': 'Send WhatsApp, SMS, or email follow-up message.',
        },
        'CHANGE_LEAD_STAGE': {
            'code': 'CHANGE_LEAD_STAGE',
            'display_name': 'Update Lead Stage',
            'domain': 'crm',
            'is_automation_executable': True,
            'target_ui_action': 'UPDATE_STAGE',
            'required_permission': 'crm.leads.edit',
            'description': 'Advance or update the commercial pipeline stage.',
        },
        'CALL_LEAD': {
            'code': 'CALL_LEAD',
            'display_name': 'Call Lead',
            'domain': 'crm',
            'is_automation_executable': False,  # Manual outreach / UI confirmation
            'target_ui_action': 'LOG_ACTIVITY',
            'required_permission': 'crm.leads.edit',
            'description': 'Conduct phone outreach and record interaction notes.',
        },
        'BOOK_TRIAL': {
            'code': 'BOOK_TRIAL',
            'display_name': 'Book Trial Workout',
            'domain': 'trial',
            'is_automation_executable': False,  # Requires scheduling & trainer validation
            'target_ui_action': 'BOOK_TRIAL',
            'required_permission': 'crm.trials.book',
            'description': 'Schedule and reserve a trial workout session slot.',
        },
        'CONFIRM_TRIAL': {
            'code': 'CONFIRM_TRIAL',
            'display_name': 'Confirm Trial Attendance',
            'domain': 'trial',
            'is_automation_executable': False,  # Operational confirmation
            'target_ui_action': 'CONFIRM_TRIAL',
            'required_permission': 'crm.trials.manage',
            'description': 'Verify member attendance readiness for upcoming trial.',
        },
        'RESCHEDULE_TRIAL': {
            'code': 'RESCHEDULE_TRIAL',
            'display_name': 'Reschedule Trial',
            'domain': 'trial',
            'is_automation_executable': False,
            'target_ui_action': 'RESCHEDULE_TRIAL',
            'required_permission': 'crm.trials.manage',
            'description': 'Move trial booking to an alternate date or time slot.',
        },
        'POST_TRIAL_FOLLOWUP': {
            'code': 'POST_TRIAL_FOLLOWUP',
            'display_name': 'Post-Trial Sales Follow-up',
            'domain': 'crm',
            'is_automation_executable': False,
            'target_ui_action': 'SCHEDULE_FOLLOWUP',
            'required_permission': 'crm.leads.edit',
            'description': 'Follow up with attendee for membership enrollment.',
        },
        'REVIEW_LEAD': {
            'code': 'REVIEW_LEAD',
            'display_name': 'Review Lead Activity',
            'domain': 'crm',
            'is_automation_executable': False,
            'target_ui_action': 'VIEW_LEAD_360',
            'required_permission': 'crm.leads.view',
            'description': 'Review prospect touchpoint history and pipeline status.',
        },
        'SEND_OFFER': {
            'code': 'SEND_OFFER',
            'display_name': 'Send Promotional Offer',
            'domain': 'crm',
            'is_automation_executable': False,
            'target_ui_action': 'VIEW_OFFERS',
            'required_permission': 'crm.leads.view',
            'description': 'Present eligible promotional discount or coupon code to lead.',
        },
    }

    @classmethod
    def get_recommendation_meta(cls, code: str) -> Optional[Dict[str, Any]]:
        return cls._RECOMMENDATIONS.get(code)

    @classmethod
    def is_valid_recommendation(cls, code: str) -> bool:
        return code in cls._RECOMMENDATIONS

    @classmethod
    def list_recommendations(cls) -> List[Dict[str, Any]]:
        return list(cls._RECOMMENDATIONS.values())

