import logging
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.utils import timezone
from django.db import transaction

from config.routers import get_tenant_db_alias
from .models_org import Organization, Branch
from .models_users import TenantUser
from .models_crm import LeadSource, Lead, CRMStageSlaPolicy
from .models_govern import NotificationTemplate
from .models_automation import (
    AutomationWorkflow,
    AutomationWorkflowVersion,
    AutomationExecution,
    AutomationStepExecution,
)
from .serializers_automation import (
    AutomationWorkflowSerializer,
    AutomationWorkflowDetailSerializer,
    AutomationWorkflowVersionSerializer,
    AutomationExecutionSerializer,
    AutomationExecutionDetailSerializer,
)
from .automation.registry import (
    TriggerRegistry,
    ConditionOperatorRegistry,
    ConditionFieldRegistry,
    ActionRegistry,
)
from .automation.engine import AutomationEngine
from .services_reliability import record_business_audit
from .permissions import RequireActiveTenantAndOrg, TenantRBACPermission

logger = logging.getLogger(__name__)


def _get_db(request):
    return (
        get_tenant_db_alias()
        or getattr(getattr(request, 'user', None), '_db_alias', None)
        or getattr(request, '_tenant_db_alias', None)
        or 'default'
    )


def _get_org(request):
    org = getattr(request, 'organization', None)
    if not org:
        user = getattr(request, 'user', None)
        if user and hasattr(user, 'organization') and user.organization:
            return user.organization
        alias = _get_db(request)
        if user and getattr(user, 'organization_id', None):
            return Organization.objects.using(alias).filter(id=user.organization_id).first()
        return Organization.objects.using(alias).filter(status='ACTIVE').first()
    return org


class AutomationWorkflowViewSet(viewsets.ModelViewSet):
    """
    CRUD and lifecycle management for Tenant Automation Workflows.
    Enforces strict immutability on published versions:
    Edits must occur on DRAFT versions.
    """
    serializer_class = AutomationWorkflowSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'automation'
    required_submodule = 'workflows'
    required_permission = 'automation.workflows.view'
    permission_action_map = {
        'list': 'automation.workflows.view',
        'retrieve': 'automation.workflows.view',
        'create': 'automation.workflows.manage',
        'update': 'automation.workflows.manage',
        'partial_update': 'automation.workflows.manage',
        'destroy': 'automation.workflows.manage',
        'publish': 'automation.workflows.publish',
        'activate': 'automation.workflows.manage',
        'deactivate': 'automation.workflows.manage',
        'duplicate': 'automation.workflows.manage',
        'create_draft': 'automation.workflows.manage',
        'update_draft': 'automation.workflows.manage',
        'metadata': 'automation.workflows.view',
    }

    def get_serializer_class(self):
        if self.action in ['retrieve', 'update_draft']:
            return AutomationWorkflowDetailSerializer
        return AutomationWorkflowSerializer

    def get_queryset(self):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        qs = AutomationWorkflow.objects.using(alias).select_related('current_version', 'created_by_user').all()
        if org:
            qs = qs.filter(organization=org)
        
        trigger = self.request.query_params.get('trigger') or self.request.query_params.get('trigger_type')
        if trigger:
            qs = qs.filter(current_version__trigger_type=trigger)
            
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            active_val = is_active.lower() == 'true'
            qs = qs.filter(status='ACTIVE' if active_val else 'INACTIVE')
            
        status_param = self.request.query_params.get('status')
        if status_param:
            qs = qs.filter(status=status_param.upper())

        return qs.order_by('-updated_at')

    def create(self, request, *args, **kwargs):
        alias = _get_db(request)
        org = _get_org(request)
        if not org:
            return Response({'error': 'Organization context missing'}, status=status.HTTP_400_BAD_REQUEST)

        name = request.data.get('name')
        if not name:
            return Response({'error': 'Workflow name is required'}, status=status.HTTP_400_BAD_REQUEST)

        trigger_type = request.data.get('trigger_type') or request.data.get('trigger_event')
        if not trigger_type or not TriggerRegistry.is_valid_trigger(trigger_type):
            return Response({'error': f"Valid trigger_type is required. Available: {list(TriggerRegistry._registry.keys())}"}, status=status.HTTP_400_BAD_REQUEST)

        description = request.data.get('description', '')
        domain = request.data.get('domain', 'crm')
        
        # Format trigger_config
        trigger_config = request.data.get('trigger_config')
        if trigger_config is None:
            conditions = request.data.get('trigger_conditions', [])
            trigger_config = {'conditions': conditions} if isinstance(conditions, list) else conditions

        steps = request.data.get('steps_definition') or request.data.get('steps', [])

        user = getattr(request, 'user', None)

        with transaction.atomic(using=alias):
            workflow = AutomationWorkflow.objects.using(alias).create(
                organization=org,
                name=name,
                description=description,
                domain=domain,
                status='INACTIVE',
                created_by_user=user if user and user.is_authenticated else None,
            )

            version = AutomationWorkflowVersion.objects.using(alias).create(
                workflow=workflow,
                version_number=1,
                status='DRAFT',
                trigger_type=trigger_type,
                trigger_config=trigger_config or {},
                steps_definition=steps or [],
            )

            workflow.current_version = version
            workflow.save(using=alias, update_fields=['current_version'])

            record_business_audit(
                organization=org,
                module='automation',
                action_code='WORKFLOW_CREATED',
                entity_type='AutomationWorkflow',
                entity_id=workflow.id,
                actor_user=user,
                metadata={'workflow_name': workflow.name, 'trigger_type': trigger_type, 'version': 1},
                db_alias=alias,
            )

        serializer = AutomationWorkflowDetailSerializer(workflow)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='create-draft')
    def create_draft(self, request, pk=None):
        """Creates a new DRAFT version for an existing workflow."""
        alias = _get_db(request)
        workflow = self.get_object()
        user = getattr(request, 'user', None)

        existing_draft = workflow.versions.filter(status='DRAFT').first()
        if existing_draft:
            serializer = AutomationWorkflowVersionSerializer(existing_draft)
            return Response(serializer.data, status=status.HTTP_200_OK)

        last_version = workflow.versions.order_by('-version_number').first()
        next_ver_num = (last_version.version_number + 1) if last_version else 1

        source_version = workflow.current_version or last_version
        trigger_type = source_version.trigger_type if source_version else 'LEAD_CREATED'
        trigger_config = source_version.trigger_config if source_version else {}
        steps = source_version.steps_definition if source_version else []

        draft = AutomationWorkflowVersion.objects.using(alias).create(
            workflow=workflow,
            version_number=next_ver_num,
            status='DRAFT',
            trigger_type=trigger_type,
            trigger_config=trigger_config,
            steps_definition=steps,
        )

        serializer = AutomationWorkflowVersionSerializer(draft)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='update-draft')
    def update_draft(self, request, pk=None):
        """Updates the current draft version of the workflow."""
        alias = _get_db(request)
        workflow = self.get_object()

        draft = workflow.versions.filter(status='DRAFT').order_by('-version_number').first()
        if not draft:
            if workflow.current_version and workflow.current_version.status == 'DRAFT':
                draft = workflow.current_version
            else:
                return Response({'error': 'No editable DRAFT version found. Call create-draft first.'}, status=status.HTTP_400_BAD_REQUEST)

        name = request.data.get('name')
        if name:
            workflow.name = name
        desc = request.data.get('description')
        if desc is not None:
            workflow.description = desc
        workflow.save(using=alias, update_fields=['name', 'description', 'updated_at'])

        trigger_type = request.data.get('trigger_type') or request.data.get('trigger_event')
        if trigger_type:
            if not TriggerRegistry.is_valid_trigger(trigger_type):
                return Response({'error': f"Unknown trigger_type: {trigger_type}"}, status=status.HTTP_400_BAD_REQUEST)
            draft.trigger_type = trigger_type

        if 'trigger_config' in request.data:
            draft.trigger_config = request.data['trigger_config']
        elif 'trigger_conditions' in request.data:
            draft.trigger_config = {'conditions': request.data['trigger_conditions']}

        if 'steps_definition' in request.data:
            draft.steps_definition = request.data['steps_definition']
        elif 'steps' in request.data:
            draft.steps_definition = request.data['steps']

        draft.save(using=alias, update_fields=['trigger_type', 'trigger_config', 'steps_definition', 'updated_at'])

        serializer = AutomationWorkflowDetailSerializer(workflow)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='publish')
    def publish(self, request, pk=None):
        """
        Validates graph and publishes the draft version.
        Published versions become strictly immutable.
        """
        alias = _get_db(request)
        org = _get_org(request)
        workflow = self.get_object()
        user = getattr(request, 'user', None)

        draft = workflow.versions.filter(status='DRAFT').order_by('-version_number').first()
        if not draft:
            if workflow.current_version and workflow.current_version.status == 'DRAFT':
                draft = workflow.current_version
            else:
                return Response({'error': 'No DRAFT version found to publish.'}, status=status.HTTP_400_BAD_REQUEST)

        # Graph and step validation
        is_valid, error_msg = AutomationEngine.validate_workflow_definition(
            trigger_type=draft.trigger_type,
            trigger_config=draft.trigger_config,
            steps_definition=draft.steps_definition,
        )

        if not is_valid:
            return Response({
                'error': 'Workflow validation failed before publish.',
                'validation_error': error_msg,
            }, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic(using=alias):
            draft.status = 'PUBLISHED'
            draft.published_at = timezone.now()
            draft.published_by_user = user if user and user.is_authenticated else None
            draft.save(using=alias, update_fields=['status', 'published_at', 'published_by_user', 'updated_at'])

            workflow.current_version = draft
            workflow.save(using=alias, update_fields=['current_version', 'updated_at'])

            record_business_audit(
                organization=org,
                module='automation',
                action_code='WORKFLOW_PUBLISHED',
                entity_type='AutomationWorkflow',
                entity_id=workflow.id,
                actor_user=user,
                metadata={'workflow_name': workflow.name, 'version_number': draft.version_number},
                db_alias=alias,
            )

        serializer = AutomationWorkflowDetailSerializer(workflow)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='activate')
    def activate(self, request, pk=None):
        alias = _get_db(request)
        org = _get_org(request)
        workflow = self.get_object()
        user = getattr(request, 'user', None)

        if not workflow.current_version or workflow.current_version.status != 'PUBLISHED':
            return Response({'error': 'Cannot activate workflow without a published version.'}, status=status.HTTP_400_BAD_REQUEST)

        workflow.status = 'ACTIVE'
        workflow.save(using=alias, update_fields=['status', 'updated_at'])

        record_business_audit(
            organization=org,
            module='automation',
            action_code='WORKFLOW_ACTIVATED',
            entity_type='AutomationWorkflow',
            entity_id=workflow.id,
            actor_user=user,
            metadata={'workflow_name': workflow.name},
            db_alias=alias,
        )

        return Response({'status': 'ACTIVE', 'is_active': True})

    @action(detail=True, methods=['post'], url_path='deactivate')
    def deactivate(self, request, pk=None):
        alias = _get_db(request)
        org = _get_org(request)
        workflow = self.get_object()
        user = getattr(request, 'user', None)

        workflow.status = 'INACTIVE'
        workflow.save(using=alias, update_fields=['status', 'updated_at'])

        record_business_audit(
            organization=org,
            module='automation',
            action_code='WORKFLOW_DEACTIVATED',
            entity_type='AutomationWorkflow',
            entity_id=workflow.id,
            actor_user=user,
            metadata={'workflow_name': workflow.name},
            db_alias=alias,
        )

        return Response({'status': 'INACTIVE', 'is_active': False})

    @action(detail=True, methods=['post'], url_path='duplicate')
    def duplicate(self, request, pk=None):
        """Duplicates workflow and its steps into a new draft workflow."""
        alias = _get_db(request)
        org = _get_org(request)
        workflow = self.get_object()
        user = getattr(request, 'user', None)

        source_version = workflow.current_version or workflow.versions.order_by('-version_number').first()

        with transaction.atomic(using=alias):
            new_wf = AutomationWorkflow.objects.using(alias).create(
                organization=org,
                name=f"{workflow.name} (Copy)",
                description=workflow.description,
                domain=workflow.domain,
                status='INACTIVE',
                created_by_user=user if user and user.is_authenticated else None,
            )

            new_ver = AutomationWorkflowVersion.objects.using(alias).create(
                workflow=new_wf,
                version_number=1,
                status='DRAFT',
                trigger_type=source_version.trigger_type if source_version else 'LEAD_CREATED',
                trigger_config=source_version.trigger_config if source_version else {},
                steps_definition=source_version.steps_definition if source_version else [],
            )

            new_wf.current_version = new_ver
            new_wf.save(using=alias, update_fields=['current_version'])

            record_business_audit(
                organization=org,
                module='automation',
                action_code='WORKFLOW_DUPLICATED',
                entity_type='AutomationWorkflow',
                entity_id=new_wf.id,
                actor_user=user,
                metadata={'original_workflow_id': str(workflow.id), 'new_workflow_id': str(new_wf.id)},
                db_alias=alias,
            )

        serializer = AutomationWorkflowDetailSerializer(new_wf)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['get'], url_path='metadata')
    def metadata(self, request):
        """
        Authoritative backend-driven metadata for frontend automation builder.
        Frontend MUST NOT hardcode dropdowns, operators, or triggers.
        """
        alias = _get_db(request)
        org = _get_org(request)

        sources = list(LeadSource.objects.using(alias).filter(organization=org, status='ACTIVE').values('id', 'code', 'name')) if org else []
        branches = list(Branch.objects.using(alias).filter(organization=org, status='ACTIVE').values('id', 'code', 'name')) if org else []
        users_qs = TenantUser.objects.using(alias).filter(organization=org, status='ACTIVE') if org else []
        users = [
            {'id': str(u.id), 'email': u.email, 'name': f"{u.first_name} {u.last_name}".strip() or u.email}
            for u in users_qs
        ]
        
        stages = [
            {'code': code, 'label': label}
            for code, label in Lead.STATUSES
        ]

        templates = list(NotificationTemplate.objects.using(alias).filter(organization=org, is_active=True).values('id', 'name', 'channel', 'event_type', 'body')) if org else []

        return Response({
            'triggers': TriggerRegistry.list_triggers(),
            'operators': ConditionOperatorRegistry.list_operators(),
            'condition_fields': ConditionFieldRegistry.list_fields(),
            'actions': ActionRegistry.list_actions(),
            'wait_units': [{'code': 'MINUTES', 'label': 'Minutes'}, {'code': 'HOURS', 'label': 'Hours'}, {'code': 'DAYS', 'label': 'Days'}],
            'channels': [{'code': 'WHATSAPP', 'label': 'WhatsApp'}, {'code': 'EMAIL', 'label': 'Email'}, {'code': 'SMS', 'label': 'SMS'}],
            'dynamic_values': {
                'lead_sources': sources,
                'branches': branches,
                'users': users,
                'lead_stages': stages,
                'templates': templates,
            }
        })


class AutomationExecutionViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only inspection and observability of automation runs.
    Tenant-isolated and preserves historical execution steps.
    """
    serializer_class = AutomationExecutionSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'automation'
    required_submodule = 'executions'
    required_permission = 'automation.executions.view'
    permission_action_map = {
        'list': 'automation.executions.view',
        'retrieve': 'automation.executions.view',
        'retry': 'automation.workflows.manage',
    }

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return AutomationExecutionDetailSerializer
        return AutomationExecutionSerializer

    def get_queryset(self):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        qs = AutomationExecution.objects.using(alias).select_related('workflow', 'workflow_version').all()
        if org:
            qs = qs.filter(organization=org)

        workflow_id = self.request.query_params.get('workflow_id')
        if workflow_id:
            qs = qs.filter(workflow_id=workflow_id)

        status_param = self.request.query_params.get('status')
        if status_param:
            qs = qs.filter(status=status_param)

        return qs.order_by('-created_at')

    @action(detail=True, methods=['post'], url_path='retry')
    def retry(self, request, pk=None):
        """
        Manually retry a FAILED execution safely.
        Resumes from the failed step without re-executing completed idempotent actions.
        """
        alias = _get_db(request)
        org = _get_org(request)
        execution = self.get_object()
        user = getattr(request, 'user', None)

        if execution.status != 'FAILED':
            return Response({'error': f"Cannot retry execution with status '{execution.status}'. Only FAILED executions can be retried."}, status=status.HTTP_400_BAD_REQUEST)

        failed_step = execution.step_executions.filter(status='FAILED').order_by('-attempt').first()
        if failed_step:
            failed_step.status = 'WAITING'
            failed_step.save(using=alias, update_fields=['status', 'updated_at'])

        execution.status = 'RUNNING'
        execution.error_code = None
        execution.error_message = None
        execution.save(using=alias, update_fields=['status', 'error_code', 'error_message', 'updated_at'])

        record_business_audit(
            organization=org,
            module='automation',
            action_code='AUTOMATION_EXECUTION_RETRIED',
            entity_type='AutomationExecution',
            entity_id=execution.id,
            actor_user=user,
            metadata={'workflow_id': str(execution.workflow_id), 'step_id': failed_step.step_id if failed_step else None},
            db_alias=alias,
        )

        AutomationEngine.run_execution_steps(execution, db_alias=alias)
        serializer = AutomationExecutionDetailSerializer(execution)
        return Response(serializer.data, status=status.HTTP_200_OK)
