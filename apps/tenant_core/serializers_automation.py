from rest_framework import serializers
from apps.tenant_core.models_automation import (
    AutomationWorkflow,
    AutomationWorkflowVersion,
    AutomationExecution,
    AutomationStepExecution,
)


class AutomationStepExecutionSerializer(serializers.ModelSerializer):
    class Meta:
        model = AutomationStepExecution
        fields = [
            'id', 'step_id', 'step_type', 'status', 'resume_at', 'attempt_count',
            'input_data', 'output_data', 'error_code', 'error_message',
            'started_at', 'completed_at', 'created_at',
        ]


class AutomationExecutionSerializer(serializers.ModelSerializer):
    workflow_name = serializers.CharField(source='workflow.name', read_only=True)
    version_number = serializers.IntegerField(source='workflow_version.version_number', read_only=True)

    class Meta:
        model = AutomationExecution
        fields = [
            'id', 'workflow', 'workflow_name', 'workflow_version', 'version_number',
            'trigger_event_type', 'trigger_event_id', 'aggregate_type', 'aggregate_id',
            'status', 'current_step_id', 'waiting_until', 'context_data', 'execution_depth',
            'attempt_count', 'error_code', 'error_message', 'started_at', 'completed_at',
            'created_at', 'updated_at',
        ]


class AutomationExecutionDetailSerializer(serializers.ModelSerializer):
    workflow_name = serializers.CharField(source='workflow.name', read_only=True)
    version_number = serializers.IntegerField(source='workflow_version.version_number', read_only=True)
    step_executions = AutomationStepExecutionSerializer(many=True, read_only=True)

    class Meta:
        model = AutomationExecution
        fields = [
            'id', 'workflow', 'workflow_name', 'workflow_version', 'version_number',
            'trigger_event_type', 'trigger_event_id', 'aggregate_type', 'aggregate_id',
            'status', 'current_step_id', 'waiting_until', 'context_data', 'execution_depth',
            'attempt_count', 'error_code', 'error_message', 'started_at', 'completed_at',
            'created_at', 'updated_at', 'step_executions',
        ]


class AutomationWorkflowVersionSerializer(serializers.ModelSerializer):
    published_by_name = serializers.SerializerMethodField()

    class Meta:
        model = AutomationWorkflowVersion
        fields = [
            'id', 'workflow', 'version_number', 'status', 'trigger_type',
            'trigger_config', 'steps_definition', 'published_at', 'published_by_name',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'workflow', 'version_number', 'status', 'published_at', 'published_by_name', 'created_at', 'updated_at']

    def get_published_by_name(self, obj):
        if obj.published_by_user:
            return f"{obj.published_by_user.first_name} {obj.published_by_user.last_name}".strip()
        return None


class AutomationWorkflowSerializer(serializers.ModelSerializer):
    current_version_detail = AutomationWorkflowVersionSerializer(source='current_version', read_only=True)
    draft_version_detail = serializers.SerializerMethodField()
    executions_count = serializers.SerializerMethodField()
    failures_count = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    trigger_type = serializers.CharField(source='current_version.trigger_type', read_only=True)

    class Meta:
        model = AutomationWorkflow
        fields = [
            'id', 'name', 'description', 'domain', 'status',
            'current_version', 'current_version_detail', 'draft_version_detail',
            'executions_count', 'failures_count', 'created_by_name', 'trigger_type',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'current_version', 'created_at', 'updated_at']

    def get_draft_version_detail(self, obj):
        alias = obj._state.db or 'default'
        draft = AutomationWorkflowVersion.objects.using(alias).filter(workflow=obj, status='DRAFT').first()
        if draft:
            return AutomationWorkflowVersionSerializer(draft).data
        return None

    def get_executions_count(self, obj):
        alias = obj._state.db or 'default'
        return AutomationExecution.objects.using(alias).filter(workflow=obj).count()

    def get_failures_count(self, obj):
        alias = obj._state.db or 'default'
        return AutomationExecution.objects.using(alias).filter(workflow=obj, status='FAILED').count()

    def get_created_by_name(self, obj):
        if obj.created_by_user:
            return f"{obj.created_by_user.first_name} {obj.created_by_user.last_name}".strip()
        return None


class AutomationWorkflowDetailSerializer(AutomationWorkflowSerializer):
    versions = AutomationWorkflowVersionSerializer(many=True, read_only=True)

    class Meta(AutomationWorkflowSerializer.Meta):
        fields = AutomationWorkflowSerializer.Meta.fields + ['versions']
