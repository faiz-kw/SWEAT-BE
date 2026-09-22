"""
Generic, Tenant-Configurable Automation Engine.
Authoritative execution of trigger events, step workflows, conditions, durable waits, and actions.
"""
from typing import Dict, Any, List, Optional, Tuple, Set
import logging
from datetime import datetime, timedelta
from django.utils import timezone
from django.db import transaction, IntegrityError
from django.db.models import Q

from apps.tenant_core.models_automation import (
    AutomationWorkflow,
    AutomationWorkflowVersion,
    AutomationExecution,
    AutomationStepExecution,
)
from apps.tenant_core.automation.registry import (
    TriggerRegistry,
    ConditionOperatorRegistry,
    ConditionFieldRegistry,
    ActionRegistry,
)

logger = logging.getLogger(__name__)

MAX_EXECUTION_DEPTH = 5


class AutomationEngine:
    """
    Authoritative workflow validator and execution engine.
    """

    # =========================================================================
    # 1. VALIDATION BEFORE PUBLISH
    # =========================================================================

    @classmethod
    def validate_workflow_definition(
        cls,
        trigger_type: str,
        trigger_config: Dict[str, Any],
        steps_definition: List[Dict[str, Any]],
    ) -> Tuple[bool, str]:
        """
        Validates workflow completeness, configuration, and graph acyclicity.
        Must pass before a version can transition to PUBLISHED.
        """
        if not trigger_type or not TriggerRegistry.is_valid_trigger(trigger_type):
            return False, f"Invalid or missing trigger type: {trigger_type}"

        # Validate trigger conditions if present
        conditions = trigger_config.get('conditions', [])
        for c in conditions:
            field = c.get('field')
            operator = c.get('operator')
            field_def = ConditionFieldRegistry.get_field_def(field)
            if not field_def:
                return False, f"Unknown condition field: {field}"
            if operator not in field_def.get('allowed_operators', []):
                return False, f"Operator '{operator}' not permitted for field '{field}'"

        if not steps_definition or not isinstance(steps_definition, list):
            return False, "Workflow must contain at least one step"

        step_ids: Set[str] = set()
        adj_list: Dict[str, List[str]] = {}

        for step in steps_definition:
            sid = step.get('id')
            stype = step.get('type')
            if not sid:
                return False, "All steps must have a unique 'id'"
            if sid in step_ids:
                return False, f"Duplicate step ID: {sid}"
            step_ids.add(sid)

            if stype not in ['ACTION', 'CONDITION', 'WAIT', 'END']:
                return False, f"Invalid step type '{stype}' for step '{sid}'"

            # Validate Action
            if stype == 'ACTION':
                action_code = step.get('action_code')
                if not action_code or not ActionRegistry.is_valid_action(action_code):
                    return False, f"Invalid or unknown action_code '{action_code}' in step '{sid}'"
                handler = ActionRegistry.get_handler(action_code)
                is_valid, err = handler.validate_config(step.get('config', {}))
                if not is_valid:
                    return False, f"Step '{sid}' configuration error: {err}"
                next_id = step.get('next_step_id')
                adj_list[sid] = [next_id] if next_id else []

            # Validate Condition
            elif stype == 'CONDITION':
                cond = step.get('condition', {})
                field = cond.get('field')
                operator = cond.get('operator')
                if not field or not ConditionFieldRegistry.get_field_def(field):
                    return False, f"Step '{sid}' condition references unknown field: {field}"
                field_def = ConditionFieldRegistry.get_field_def(field)
                if operator not in field_def.get('allowed_operators', []):
                    return False, f"Step '{sid}' operator '{operator}' not allowed for field '{field}'"
                yes_step = step.get('yes_step_id')
                no_step = step.get('no_step_id')
                if not yes_step or not no_step:
                    return False, f"Condition step '{sid}' must specify both 'yes_step_id' and 'no_step_id'"
                adj_list[sid] = [yes_step, no_step]

            # Validate Wait
            elif stype == 'WAIT':
                duration_value = step.get('duration_value')
                duration_unit = step.get('duration_unit', 'MINUTES').upper()
                if not duration_value or int(duration_value) <= 0:
                    return False, f"Wait step '{sid}' must specify positive duration_value"
                if duration_unit not in ['MINUTES', 'HOURS', 'DAYS']:
                    return False, f"Invalid duration_unit '{duration_unit}' in step '{sid}'"
                next_id = step.get('next_step_id')
                adj_list[sid] = [next_id] if next_id else []

            # Validate End
            elif stype == 'END':
                adj_list[sid] = []

        # Validate that all transitions point to existing step IDs or terminal
        for sid, targets in adj_list.items():
            for t in targets:
                if t and t not in step_ids:
                    return False, f"Step '{sid}' targets nonexistent step ID: '{t}'"

        # Graph Cycle Detection (DFS)
        visited = set()
        rec_stack = set()

        def has_cycle(node: str) -> bool:
            visited.add(node)
            rec_stack.add(node)
            for neighbor in adj_list.get(node, []):
                if not neighbor:
                    continue
                if neighbor not in visited:
                    if has_cycle(neighbor):
                        return True
                elif neighbor in rec_stack:
                    return True
            rec_stack.remove(node)
            return False

        for sid in step_ids:
            if sid not in visited:
                if has_cycle(sid):
                    return False, "Cyclic workflow loops are not permitted"

        return True, ""

    # =========================================================================
    # 2. TRIGGER CONSUMPTION
    # =========================================================================

    @classmethod
    def handle_domain_event(
        cls,
        event: Any,
        db_alias: str = 'default',
    ) -> List[AutomationExecution]:
        """
        Consumes a committed DomainOutboxEvent.
        Evaluates active published workflows for the trigger, checks conditions,
        creates executions with DB idempotency, and begins execution.
        """
        event_type = event.event_type
        aggregate_id = event.aggregate_id
        org = event.organization
        payload = event.payload or {}

        # 1. Look up published workflow versions for active workflows matching trigger
        active_versions = list(
            AutomationWorkflowVersion.objects.using(db_alias).filter(
                workflow__organization=org,
                workflow__status='ACTIVE',
                status='PUBLISHED',
                trigger_type=event_type,
            ).select_related('workflow', 'workflow__organization')
        )

        if not active_versions:
            return []

        executions: List[AutomationExecution] = []

        for version in active_versions:
            # 2. Evaluate trigger conditions
            context = cls._build_initial_context(event, payload, db_alias=db_alias)
            if not cls._evaluate_trigger_conditions(version.trigger_config, context):
                logger.info(
                    "Workflow %s (v%s) skipped: trigger conditions not met for event %s",
                    version.workflow.name, version.version_number, event.id
                )
                continue

            # 3. Create AutomationExecution with DB-enforced idempotency
            execution_depth = int(payload.get('_execution_depth') or payload.get('execution_depth') or 0)
            if execution_depth >= MAX_EXECUTION_DEPTH:
                logger.warning(
                    "Workflow %s (v%s) aborted: execution depth limit (%d) reached to prevent recursion",
                    version.workflow.name, version.version_number, execution_depth
                )
                continue

            first_step_id = version.steps_definition[0]['id'] if version.steps_definition else ''

            try:
                with transaction.atomic(using=db_alias):
                    execution = AutomationExecution.objects.using(db_alias).create(
                        organization=org,
                        workflow=version.workflow,
                        workflow_version=version,
                        trigger_event_type=event_type,
                        trigger_event_id=event.id,
                        aggregate_type=event.aggregate_type,
                        aggregate_id=aggregate_id,
                        status='RUNNING',
                        current_step_id=first_step_id,
                        context_data=context,
                        execution_depth=execution_depth,
                    )
            except IntegrityError:
                # Deduplicated: execution already exists for this event + workflow version
                logger.info("AutomationExecution already exists for event %s and version %s", event.id, version.id)
                existing = AutomationExecution.objects.using(db_alias).filter(
                    organization=org, workflow_version=version, trigger_event_id=event.id
                ).first()
                if existing:
                    executions.append(existing)
                continue

            # 4. Advance execution through steps
            cls.run_execution_steps(execution, db_alias=db_alias)
            executions.append(execution)

        return executions

    # =========================================================================
    # 3. STEP RUNNER
    # =========================================================================

    @classmethod
    def run_execution_steps(
        cls,
        execution: AutomationExecution,
        db_alias: str = 'default',
    ):
        """
        Advances an execution synchronously until a WAIT or END step is reached.
        """
        steps_dict = {s['id']: s for s in execution.workflow_version.steps_definition}
        execution.attempt_count = (execution.attempt_count or 0) + 1
        execution.save(using=db_alias, update_fields=['attempt_count', 'updated_at'])

        while execution.status == 'RUNNING' and execution.current_step_id:
            step = steps_dict.get(execution.current_step_id)
            if not step:
                execution.status = 'FAILED'
                execution.error_code = 'STEP_NOT_FOUND'
                execution.error_message = f"Step '{execution.current_step_id}' not found in definition"
                execution.completed_at = timezone.now()
                execution.save(using=db_alias, update_fields=['status', 'error_code', 'error_message', 'completed_at', 'updated_at'])
                break

            step_type = step.get('type')
            sid = step.get('id')

            # Create or fetch StepExecution
            step_exec, _ = AutomationStepExecution.objects.using(db_alias).get_or_create(
                execution=execution,
                step_id=sid,
                attempt_count=1,
                defaults={
                    'step_type': step_type,
                    'status': 'RUNNING',
                    'input_data': execution.context_data,
                }
            )

            # A. ACTION STEP
            if step_type == 'ACTION':
                action_code = step.get('action_code')
                handler = ActionRegistry.get_handler(action_code)
                if not handler:
                    step_exec.status = 'FAILED'
                    step_exec.error_code = 'UNKNOWN_ACTION'
                    step_exec.error_message = f"No handler registered for action '{action_code}'"
                    step_exec.completed_at = timezone.now()
                    step_exec.save(using=db_alias)
                    execution.status = 'FAILED'
                    execution.error_code = 'UNKNOWN_ACTION'
                    execution.completed_at = timezone.now()
                    execution.save(using=db_alias, update_fields=['status', 'error_code', 'completed_at', 'updated_at'])
                    break

                try:
                    result = handler.execute(
                        execution=execution,
                        step_id=sid,
                        config=step.get('config', {}),
                        context=execution.context_data,
                        db_alias=db_alias,
                    )
                    if result.get('status') == 'FAILED':
                        step_exec.status = 'FAILED'
                        step_exec.error_code = result.get('error_code', 'ACTION_FAILED')
                        step_exec.error_message = result.get('error', 'Action failed')
                        step_exec.completed_at = timezone.now()
                        step_exec.save(using=db_alias)
                        execution.status = 'FAILED'
                        execution.error_code = result.get('error_code', 'ACTION_FAILED')
                        execution.error_message = result.get('error', 'Action failed')
                        execution.completed_at = timezone.now()
                        execution.save(using=db_alias, update_fields=['status', 'error_code', 'error_message', 'completed_at', 'updated_at'])
                        break

                    step_exec.status = 'COMPLETED'
                    step_exec.output_data = result
                    step_exec.completed_at = timezone.now()
                    step_exec.save(using=db_alias)

                    # Update context with output
                    execution.context_data[f"step_{sid}"] = result
                    execution.current_step_id = step.get('next_step_id') or ''
                    if not execution.current_step_id:
                        execution.status = 'COMPLETED'
                        execution.completed_at = timezone.now()
                    execution.save(using=db_alias, update_fields=['context_data', 'current_step_id', 'status', 'completed_at', 'updated_at'])
                except Exception as exc:
                    logger.error("Action step %s failed in execution %s: %s", sid, execution.id, exc)
                    step_exec.status = 'FAILED'
                    step_exec.error_code = 'ACTION_EXECUTION_ERROR'
                    step_exec.error_message = str(exc)
                    step_exec.completed_at = timezone.now()
                    step_exec.save(using=db_alias)
                    execution.status = 'FAILED'
                    execution.error_code = 'ACTION_EXECUTION_ERROR'
                    execution.error_message = str(exc)
                    execution.completed_at = timezone.now()
                    execution.save(using=db_alias, update_fields=['status', 'error_code', 'error_message', 'completed_at', 'updated_at'])
                    break

            # B. CONDITION STEP
            elif step_type == 'CONDITION':
                cond = step.get('condition', {})
                field = cond.get('field')
                operator = cond.get('operator')
                expected_val = cond.get('value')

                # Re-evaluate live entity state for safety
                refreshed_context = cls._refresh_live_context(execution, db_alias=db_alias)
                actual_val = refreshed_context.get(field)

                passed = ConditionOperatorRegistry.evaluate(operator, actual_val, expected_val)
                step_exec.status = 'COMPLETED'
                step_exec.output_data = {'evaluated_true': passed, 'actual_value': str(actual_val)}
                step_exec.completed_at = timezone.now()
                step_exec.save(using=db_alias)

                next_sid = step.get('yes_step_id') if passed else step.get('no_step_id')
                execution.current_step_id = next_sid or ''
                if not execution.current_step_id:
                    execution.status = 'COMPLETED'
                    execution.completed_at = timezone.now()
                execution.save(using=db_alias, update_fields=['current_step_id', 'status', 'completed_at', 'updated_at'])

            # C. WAIT STEP
            elif step_type == 'WAIT':
                val = int(step.get('duration_value', 1))
                unit = step.get('duration_unit', 'MINUTES').upper()
                multiplier = 60 if unit == 'MINUTES' else 3600 if unit == 'HOURS' else 86400
                resume_time = timezone.now() + timedelta(seconds=val * multiplier)

                step_exec.status = 'WAITING'
                step_exec.resume_at = resume_time
                step_exec.save(using=db_alias)

                execution.status = 'WAITING'
                execution.waiting_until = resume_time
                # Advance step pointer so when resumed it immediately runs next_step_id
                execution.current_step_id = step.get('next_step_id') or ''
                execution.save(using=db_alias, update_fields=['status', 'waiting_until', 'current_step_id', 'updated_at'])
                break

            # D. END STEP
            elif step_type == 'END':
                step_exec.status = 'COMPLETED'
                step_exec.completed_at = timezone.now()
                step_exec.save(using=db_alias)

                execution.status = 'COMPLETED'
                execution.current_step_id = sid
                execution.completed_at = timezone.now()
                execution.save(using=db_alias, update_fields=['status', 'current_step_id', 'completed_at', 'updated_at'])
                break

    # =========================================================================
    # 4. RESUME DUE WAITING EXECUTIONS
    # =========================================================================

    @classmethod
    def resume_due_waiting_executions(
        cls,
        now: Optional[datetime] = None,
        db_alias: str = 'default',
        limit: int = 50,
    ) -> List[AutomationExecution]:
        """
        Scans and resumes due WAITING executions using select_for_update for concurrency protection.
        Worker retries and restarts will not duplicate execution.
        """
        check_time = now or timezone.now()
        resumed_list: List[AutomationExecution] = []

        with transaction.atomic(using=db_alias):
            waiting_executions = list(
                AutomationExecution.objects.using(db_alias).select_for_update(skip_locked=True).filter(
                    status='WAITING',
                    waiting_until__lte=check_time,
                )[:limit]
            )

            for execution in waiting_executions:
                execution.status = 'RUNNING'
                execution.waiting_until = None

                # Complete the waiting step_execution if any
                waiting_steps = AutomationStepExecution.objects.using(db_alias).filter(
                    execution=execution, status='WAITING'
                )
                for ws in waiting_steps:
                    ws.status = 'COMPLETED'
                    ws.completed_at = timezone.now()
                    ws.save(using=db_alias, update_fields=['status', 'completed_at'])

                # If current_step_id is pointing to the WAIT step, advance to next_step_id
                steps_map = {s.get('id'): s for s in (execution.workflow_version.steps_definition or [])}
                curr_step = steps_map.get(execution.current_step_id)
                if curr_step and curr_step.get('type') == 'WAIT':
                    execution.current_step_id = curr_step.get('next_step_id') or ''

                execution.save(using=db_alias, update_fields=['status', 'waiting_until', 'current_step_id', 'updated_at'])

                cls.run_execution_steps(execution, db_alias=db_alias)
                resumed_list.append(execution)

        return resumed_list

    # =========================================================================
    # 5. CONTEXT & CONDITION HELPERS
    # =========================================================================

    @classmethod
    def _build_initial_context(cls, event: Any, payload: Dict[str, Any], db_alias: str = 'default') -> Dict[str, Any]:
        """Extracts and normalizes context fields from event payload and entity lookup."""
        context = dict(payload)
        lead_id = payload.get('lead_id')
        trial_id = payload.get('trial_id')
        if not lead_id and event.aggregate_type == 'Lead':
            lead_id = str(event.aggregate_id)
        if not trial_id and event.aggregate_type == 'TrialBooking':
            trial_id = str(event.aggregate_id)

        if lead_id:
            context['lead_id'] = str(lead_id)
            from apps.tenant_core.models_crm import Lead
            lead = Lead.objects.using(db_alias).filter(id=lead_id).first()
            if lead:
                context['lead.source'] = lead.lead_source.code if lead.lead_source else ''
                context['lead.stage'] = lead.current_status
                context['lead.branch'] = str(lead.branch_id) if lead.branch_id else ''
                context['lead.gender'] = lead.gender or ''
                context['lead.do_not_contact'] = lead.do_not_contact
                context['lead.program'] = str(lead.interested_program_id) if lead.interested_program_id else ''
                context['lead.assigned_agent'] = str(lead.assigned_sales_user_id) if lead.assigned_sales_user_id else ''

        if trial_id:
            context['trial_id'] = str(trial_id)
            from apps.tenant_core.models_crm import TrialBooking
            trial = TrialBooking.objects.using(db_alias).filter(id=trial_id).first()
            if trial:
                context['trial.status'] = trial.status
                context['trial.confirmation_status'] = trial.confirmation_status
                context['trial.branch'] = str(trial.branch_id) if trial.branch_id else ''

        return context

    @classmethod
    def _refresh_live_context(cls, execution: AutomationExecution, db_alias: str = 'default') -> Dict[str, Any]:
        """Fetches latest database state of entities so conditions test real-time state."""
        ctx = dict(execution.context_data)
        lead_id = ctx.get('lead_id')
        trial_id = ctx.get('trial_id')

        if lead_id:
            from apps.tenant_core.models_crm import Lead
            lead = Lead.objects.using(db_alias).filter(id=lead_id).first()
            if lead:
                ctx['lead.source'] = lead.lead_source.code if lead.lead_source else ''
                ctx['lead.stage'] = lead.current_status
                ctx['lead.branch'] = str(lead.branch_id) if lead.branch_id else ''
                ctx['lead.gender'] = lead.gender or ''
                ctx['lead.do_not_contact'] = lead.do_not_contact

                # Check if customer replied
                from apps.tenant_core.models_communication import CommunicationMessage
                has_reply = CommunicationMessage.objects.using(db_alias).filter(
                    lead=lead, direction='INBOUND'
                ).exists()
                ctx['communication.customer_replied'] = has_reply

        if trial_id:
            from apps.tenant_core.models_crm import TrialBooking
            trial = TrialBooking.objects.using(db_alias).filter(id=trial_id).first()
            if trial:
                ctx['trial.status'] = trial.status
                ctx['trial.confirmation_status'] = trial.confirmation_status

        return ctx

    @classmethod
    def _evaluate_trigger_conditions(cls, trigger_config: Dict[str, Any], context: Dict[str, Any]) -> bool:
        """Evaluates top-level trigger filter conditions."""
        conditions = trigger_config.get('conditions', [])
        if not conditions:
            return True

        for cond in conditions:
            field = cond.get('field')
            operator = cond.get('operator')
            expected = cond.get('value')
            actual = context.get(field)
            if not ConditionOperatorRegistry.evaluate(operator, actual, expected):
                return False

        return True
