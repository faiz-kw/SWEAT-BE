"""
Master DB — Infrastructure & Control Plane Models (6 Tables)
Tables: tenant_data_sources, tenant_data_hosting_policies, tenant_data_source_health,
        tenant_provisioning, platform_settings, platform_audit_events
"""

import uuid
from django.db import models
from django.utils import timezone
from .models_tenant import Tenant
from .models_iam import PlatformUser


class TenantDataSource(models.Model):
    """
    Database connection record for each tenant's dedicated PostgreSQL database.
    This is how the DB router finds and connects to a tenant's database.
    """
    SOURCE_TYPE = [
        ('PLATFORM_MANAGED', 'Platform Managed (NeevCloud / Our Infrastructure)'),
        ('CUSTOMER_MANAGED', 'Customer Managed (AWS / GCP / Azure / On-Premise)'),
    ]
    STATUS = [
        ('PENDING', 'Pending Provisioning'),
        ('PROVISIONING', 'Provisioning in Progress'),
        ('ACTIVE', 'Active'),
        ('SUSPENDED', 'Suspended'),
        ('FAILED', 'Provisioning Failed'),
        ('DECOMMISSIONED', 'Decommissioned'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.OneToOneField(Tenant, on_delete=models.RESTRICT, related_name='data_source')
    hosting_mode = models.CharField(max_length=30, default='PLATFORM_MANAGED')
    database_engine = models.CharField(max_length=30, default='POSTGRESQL')
    database_name = models.CharField(max_length=150, default='')
    provider = models.CharField(max_length=50, default='PLATFORM')
    region = models.CharField(max_length=100, blank=True, null=True)
    network_mode = models.CharField(max_length=40, default='PRIVATE_NETWORK')
    ssl_mode = models.CharField(max_length=30, default='REQUIRE')
    secret_reference = models.CharField(max_length=255, blank=True, null=True, default='')
    schema_version = models.CharField(max_length=50, default='1.0')

    # Legacy fields maintained for backward compatibility
    source_type = models.CharField(max_length=30, choices=SOURCE_TYPE, default='PLATFORM_MANAGED')
    db_name = models.CharField(max_length=200, unique=True, help_text='Actual PostgreSQL database name')
    db_host = models.CharField(max_length=255, blank=True, default='')
    db_port = models.IntegerField(default=5432)
    db_user = models.CharField(max_length=200, blank=True, default='')
    db_password_secret_ref = models.CharField(max_length=255, blank=True, default='', help_text='Vault/Secret Manager path')
    db_schema_version = models.CharField(max_length=20, blank=True, default='', help_text='Last applied migration version')
    status = models.CharField(max_length=30, choices=STATUS, default='PENDING')
    is_platform_billable = models.BooleanField(default=True)
    provisioned_at = models.DateTimeField(null=True, blank=True)
    last_health_check_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'master'
        db_table = 'tenant_data_sources'
        ordering = ['tenant__slug']

    def __str__(self):
        return f"{self.tenant.slug} → {self.database_name or self.db_name} [{self.status}]"

    @property
    def database_host(self) -> str:
        """Read-only property alias for db_host (Sprint 14 reconciliation)."""
        return self.db_host

    @property
    def database_port(self) -> int:
        """Read-only property alias for db_port (Sprint 14 reconciliation)."""
        return self.db_port

    def save(self, *args, **kwargs):
        if not self.database_name and self.db_name:
            self.database_name = self.db_name
        elif not self.db_name and self.database_name:
            self.db_name = self.database_name
        elif not self._state.adding and self.database_name:
            self.db_name = self.database_name

        if not self.hosting_mode and self.source_type:
            self.hosting_mode = self.source_type
        elif self.hosting_mode:
            self.source_type = self.hosting_mode

        if self.schema_version and not self.db_schema_version:
            self.db_schema_version = self.schema_version
        elif self.db_schema_version and not self.schema_version:
            self.schema_version = self.db_schema_version

        if not self.secret_reference and self.db_password_secret_ref:
            self.secret_reference = self.db_password_secret_ref
        elif not self.db_password_secret_ref and self.secret_reference:
            self.db_password_secret_ref = self.secret_reference
        super().save(*args, **kwargs)


class TenantDataHostingPolicy(models.Model):
    """
    Data residency, retention, and backup policies per tenant.
    """
    REGION = [
        ('IN-MUMBAI', 'India — Mumbai (NeevCloud)'),
        ('IN-PUNE', 'India — Pune (NeevCloud)'),
        ('US-EAST', 'US East (Customer AWS)'),
        ('EU-WEST', 'EU West (Customer GCP)'),
        ('CUSTOM', 'Custom / On-Premise'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.OneToOneField(Tenant, on_delete=models.RESTRICT, related_name='hosting_policy')
    data_source = models.ForeignKey(TenantDataSource, on_delete=models.PROTECT, null=True, blank=True)
    preferred_region = models.CharField(max_length=30, choices=REGION, default='IN-MUMBAI')
    backup_retention_days = models.IntegerField(default=30)
    pitr_enabled = models.BooleanField(default=False, help_text='Point-in-Time Recovery')
    encryption_at_rest = models.BooleanField(default=True)
    data_residency_requirements = models.TextField(blank=True, default='')

    # Canonical ownership & reference fields
    backup_owner = models.CharField(max_length=30, default='PLATFORM')
    restore_owner = models.CharField(max_length=30, default='PLATFORM')
    patching_owner = models.CharField(max_length=30, default='PLATFORM')
    monitoring_owner = models.CharField(max_length=30, default='PLATFORM')
    migration_owner = models.CharField(max_length=30, default='PLATFORM')
    encryption_owner = models.CharField(max_length=30, default='PLATFORM')
    availability_sla = models.CharField(max_length=50, blank=True, null=True)
    backup_policy_reference = models.TextField(blank=True, null=True)
    dr_policy_reference = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'master'
        db_table = 'tenant_data_hosting_policies'

    def __str__(self):
        return f"{self.tenant.slug} → {self.preferred_region}"


class TenantDataSourceHealth(models.Model):
    """
    Health check results for tenant database connections.
    Populated by Celery health check workers.
    """
    HEALTH_STATUS = [
        ('HEALTHY', 'Healthy'),
        ('DEGRADED', 'Degraded'),
        ('UNREACHABLE', 'Unreachable'),
        ('SCHEMA_DRIFT', 'Schema Version Drift'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.RESTRICT, null=True, blank=True, related_name='health_checks')
    data_source = models.ForeignKey(TenantDataSource, on_delete=models.CASCADE, related_name='health_checks')
    status = models.CharField(max_length=30, choices=HEALTH_STATUS, default='HEALTHY')
    response_time_ms = models.IntegerField(null=True, blank=True)
    detected_schema_version = models.CharField(max_length=50, null=True, blank=True)
    error_code = models.CharField(max_length=100, null=True, blank=True)
    schema_version_found = models.CharField(max_length=20, blank=True, default='')
    error_message = models.TextField(blank=True, default='')
    checked_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = 'master'
        db_table = 'tenant_data_source_health'
        ordering = ['-checked_at']

    def __str__(self):
        db_identifier = self.data_source.db_name if self.data_source else (self.tenant.slug if self.tenant else 'Unknown')
        return f"{db_identifier} — {self.status} @ {self.checked_at}"

    def save(self, *args, **kwargs):
        if not self.tenant_id and self.data_source and getattr(self.data_source, 'tenant_id', None):
            self.tenant_id = self.data_source.tenant_id
        if self.detected_schema_version and not self.schema_version_found:
            self.schema_version_found = self.detected_schema_version[:20]
        elif self.schema_version_found and not self.detected_schema_version:
            self.detected_schema_version = self.schema_version_found
        super().save(*args, **kwargs)


class TenantProvisioning(models.Model):
    """
    Provisioning state machine record — tracks each step of tenant onboarding.
    """
    PROVISION_STATUS = [
        ('QUEUED', 'Queued'),
        ('IN_PROGRESS', 'In Progress'),
        ('COMPLETED', 'Completed'),
        ('FAILED', 'Failed'),
        ('ROLLED_BACK', 'Rolled Back'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.RESTRICT, related_name='provisioning_records')
    status = models.CharField(max_length=20, choices=PROVISION_STATUS, default='QUEUED')
    provisioning_status = models.CharField(max_length=30, default='PENDING')
    target_schema_version = models.CharField(max_length=50, default='1.0')
    database_created_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(null=True, blank=True)
    celery_task_id = models.CharField(max_length=255, blank=True, default='', db_index=True, help_text='Celery async task ID')
    current_step = models.CharField(max_length=100, blank=True, default='')
    total_steps = models.IntegerField(default=14)
    completed_steps = models.IntegerField(default=0)
    step_log = models.JSONField(default=list, help_text='Ordered list of completed step records')
    error_step = models.CharField(max_length=100, blank=True, default='')
    error_message = models.TextField(blank=True, default='')
    initiated_by = models.ForeignKey(
        PlatformUser, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='provisioning_initiated'
    )
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'master'
        db_table = 'tenant_provisioning'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.tenant.slug} provisioning [{self.status}] step {self.completed_steps}/{self.total_steps}"

    def save(self, *args, **kwargs):
        if not self.tenant_id:
            db = getattr(self, '_state', None) and getattr(self._state, 'db', None) or 'default'
            from .models_tenant import Tenant
            draft_slug = f"draft-prov-{uuid.uuid4().hex[:8]}"
            draft_tenant = Tenant.objects.using(db).create(
                name=f"Pending Provisioning ({self.id})",
                slug=draft_slug,
                code=draft_slug.upper()[:50],
                status='DRAFT',
            )
            self.tenant = draft_tenant
        if not self.provisioning_status and self.status:
            self.provisioning_status = self.status
        elif not self.status and self.provisioning_status:
            self.status = self.provisioning_status[:20]
        super().save(*args, **kwargs)

    def log_step(self, step_name: str, success: bool, message: str = ''):
        """Append a step result to the step_log JSON field."""
        self.step_log.append({
            'step': step_name,
            'success': success,
            'message': message,
            'timestamp': timezone.now().isoformat(),
        })
        self.completed_steps += 1
        self.current_step = step_name
        self.save(update_fields=['step_log', 'completed_steps', 'current_step', 'updated_at'])


class PlatformSetting(models.Model):
    """
    Global platform configuration key-value store.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=150, unique=True)
    value = models.JSONField(default=dict)
    category = models.CharField(max_length=100, default='GENERAL')
    is_active = models.BooleanField(default=True)
    data_type = models.CharField(max_length=20, default='string', help_text='string, integer, boolean, json')
    description = models.TextField(blank=True, default='')
    is_public = models.BooleanField(default=False, help_text='Exposed in public settings API')
    updated_by = models.ForeignKey(PlatformUser, on_delete=models.SET_NULL, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'master'
        db_table = 'platform_settings'
        ordering = ['key']

    def __str__(self):
        return f"{self.key} = {str(self.value)[:50]}"


class PlatformAuditEvent(models.Model):
    """
    Append-only audit log for platform-level actions.
    Records every action taken by platform staff on any entity.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(PlatformUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_events')
    actor_email = models.CharField(max_length=320, blank=True, default='')
    platform_user_id = models.UUIDField(null=True, blank=True)
    tenant_id = models.UUIDField(null=True, blank=True)
    event_name = models.CharField(max_length=150, default='SYSTEM_EVENT')
    action = models.CharField(max_length=50, help_text='e.g. CREATE, UPDATE, DELETE, LOGIN, SUSPEND')
    resource_type = models.CharField(max_length=100, help_text='e.g. Tenant, Subscription, PlatformUser')
    resource_id = models.UUIDField(null=True, blank=True, help_text='UUID of affected resource')
    before_data = models.JSONField(null=True, blank=True)
    after_data = models.JSONField(null=True, blank=True)
    reason = models.TextField(blank=True, null=True)
    source_application = models.CharField(max_length=100, default='control_plane')

    # Legacy fields
    description = models.TextField(blank=True, default='')
    before_state = models.JSONField(null=True, blank=True)
    after_state = models.JSONField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default='')
    request_id = models.UUIDField(null=True, blank=True)
    correlation_id = models.UUIDField(null=True, blank=True)
    tenant_context = models.ForeignKey(
        Tenant, on_delete=models.SET_NULL, null=True, blank=True,
        help_text='Set when platform staff acts in context of a specific tenant'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'master'
        db_table = 'platform_audit_events'
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.action}] {self.resource_type}/{self.resource_id} by {self.actor_email}"

    def save(self, *args, **kwargs):
        if not self.event_name and self.action:
            self.event_name = self.action
        if self.before_state and not self.before_data:
            self.before_data = self.before_state
        if self.after_state and not self.after_data:
            self.after_data = self.after_state
        super().save(*args, **kwargs)
