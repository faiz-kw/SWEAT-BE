import uuid
from django.db import models
from django.utils import timezone
from .models_org import Organization
from .models_users import TenantUser


class AutomationWorkflow(models.Model):
    """
    Tenant-configurable business automation workflow header.
    Contains overall lifecycle (ACTIVE / INACTIVE) and points to current published version.
    """
    STATUS_CHOICES = [
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name='automation_workflows')
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default='')
    domain = models.CharField(max_length=50, default='crm', help_text='Domain grouping e.g. crm, commerce, etc.')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE')
    current_version = models.ForeignKey(
        'AutomationWorkflowVersion',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='active_for_workflow'
    )
    created_by_user = models.ForeignKey(
        TenantUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_workflows'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'automation_workflows'
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['organization', 'status'], name='idx_wf_org_status'),
            models.Index(fields=['domain'], name='idx_wf_domain'),
        ]

    def __str__(self):
        return f"{self.name} ({self.status}) [v{self.current_version.version_number if self.current_version else 0}]"


class AutomationWorkflowVersion(models.Model):
    """
    Immutable version snapshot of an automation workflow.
    Once status is PUBLISHED, the version definition is frozen forever.
    Edits create or update a DRAFT version.
    """
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('PUBLISHED', 'Published'),
        ('RETIRED', 'Retired'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workflow = models.ForeignKey(AutomationWorkflow, on_delete=models.CASCADE, related_name='versions')
    version_number = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='DRAFT')
    trigger_type = models.CharField(max_length=100, help_text='Canonical trigger code e.g. LEAD_CREATED')
    trigger_config = models.JSONField(default=dict, blank=True, help_text='Trigger-level filters/conditions')
    steps_definition = models.JSONField(default=list, help_text='Ordered/graph step definitions')
    published_at = models.DateTimeField(null=True, blank=True)
    published_by_user = models.ForeignKey(
        TenantUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='published_workflow_versions'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'automation_workflow_versions'
        unique_together = ('workflow', 'version_number')
        ordering = ['-version_number']
        indexes = [
            models.Index(fields=['workflow', 'status'], name='idx_wfv_wf_status'),
            models.Index(fields=['trigger_type', 'status'], name='idx_wfv_trig_status'),
        ]

    def __str__(self):
        return f"{self.workflow.name} v{self.version_number} [{self.status}]"


class AutomationExecution(models.Model):
    """
    Durable record of a single workflow execution triggered by a domain event.
    Deduplicated at database level by (organization, workflow_version, trigger_event_id).
    """
    STATUS_CHOICES = [
        ('RUNNING', 'Running'),
        ('WAITING', 'Waiting'),
        ('COMPLETED', 'Completed'),
        ('FAILED', 'Failed'),
        ('CANCELLED', 'Cancelled'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name='automation_executions')
    workflow = models.ForeignKey(AutomationWorkflow, on_delete=models.PROTECT, related_name='executions')
    workflow_version = models.ForeignKey(AutomationWorkflowVersion, on_delete=models.PROTECT, related_name='executions')
    trigger_event_type = models.CharField(max_length=100)
    trigger_event_id = models.UUIDField(help_text='DomainOutboxEvent id or unique idempotency UUID')
    aggregate_type = models.CharField(max_length=100)
    aggregate_id = models.UUIDField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='RUNNING')
    current_step_id = models.CharField(max_length=100, blank=True, default='')
    waiting_until = models.DateTimeField(null=True, blank=True, db_index=True)
    context_data = models.JSONField(default=dict, blank=True)
    execution_depth = models.PositiveIntegerField(default=0, help_text='Tracks chain depth for recursion prevention')
    attempt_count = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=100, blank=True, default='')
    error_message = models.TextField(blank=True, default='')
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'automation_executions'
        ordering = ['-started_at']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'workflow_version', 'trigger_event_id'],
                name='uq_auto_exec_evt'
            ),
        ]
        indexes = [
            models.Index(fields=['status', 'waiting_until'], name='idx_auto_exec_wait'),
            models.Index(fields=['aggregate_type', 'aggregate_id'], name='idx_auto_exec_agg'),
        ]

    def __str__(self):
        return f"Exec:{self.workflow.name} [{self.status}] on {self.aggregate_type}:{self.aggregate_id}"


class AutomationStepExecution(models.Model):
    """
    Granular audit and resume record for each step executed in an AutomationExecution.
    Enforces deterministic step idempotency and records safe inputs/outputs.
    """
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('RUNNING', 'Running'),
        ('WAITING', 'Waiting'),
        ('COMPLETED', 'Completed'),
        ('FAILED', 'Failed'),
        ('SKIPPED', 'Skipped'),
    ]

    STEP_TYPE_CHOICES = [
        ('ACTION', 'Action'),
        ('CONDITION', 'Condition'),
        ('WAIT', 'Wait'),
        ('END', 'End'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    execution = models.ForeignKey(AutomationExecution, on_delete=models.CASCADE, related_name='step_executions')
    step_id = models.CharField(max_length=100)
    step_type = models.CharField(max_length=20, choices=STEP_TYPE_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    resume_at = models.DateTimeField(null=True, blank=True)
    attempt_count = models.PositiveIntegerField(default=1)
    input_data = models.JSONField(default=dict, blank=True)
    output_data = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=100, blank=True, default='')
    error_message = models.TextField(blank=True, default='')
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def attempt(self):
        return self.attempt_count

    @attempt.setter
    def attempt(self, value):
        self.attempt_count = value

    class Meta:
        app_label = 'tenant_core'
        db_table = 'automation_step_executions'
        ordering = ['created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['execution', 'step_id', 'attempt_count'],
                name='uq_step_exec_attempt'
            ),
        ]
        indexes = [
            models.Index(fields=['execution', 'step_id'], name='idx_step_exec_lookup'),
        ]

    def __str__(self):
        return f"StepExec:{self.step_id} ({self.step_type}) [{self.status}]"
