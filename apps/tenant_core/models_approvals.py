import uuid
from django.db import models
from django.utils import timezone
from .models_users import TenantUser
from .models_org import Organization


class ApprovalRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name='approval_requests')
    request_type = models.CharField(max_length=100)
    entity_type = models.CharField(max_length=100)
    entity_id = models.UUIDField()
    requested_by_user = models.ForeignKey(TenantUser, on_delete=models.PROTECT, related_name='requested_approvals')
    requested_payload = models.JSONField(default=dict)
    status = models.CharField(
        max_length=20,
        default='PENDING',
        choices=[
            ('PENDING', 'PENDING'),
            ('APPROVED', 'APPROVED'),
            ('REJECTED', 'REJECTED'),
            ('CANCELLED', 'CANCELLED'),
        ]
    )
    required_approvals = models.IntegerField(default=1)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'approval_requests'
        ordering = ['-created_at']

    def __str__(self):
        return f"ApprovalRequest {self.request_type} for {self.entity_type}:{self.entity_id} ({self.status})"


class ApprovalAction(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    approval_request = models.ForeignKey(ApprovalRequest, on_delete=models.PROTECT, related_name='actions')
    approver_user = models.ForeignKey(TenantUser, on_delete=models.PROTECT, related_name='approval_actions')
    action = models.CharField(
        max_length=20,
        choices=[('APPROVED', 'APPROVED'), ('REJECTED', 'REJECTED')]
    )
    comment = models.TextField(null=True, blank=True)
    acted_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'approval_actions'
        ordering = ['-acted_at']

    def __str__(self):
        return f"ApprovalAction {self.action} by {self.approver_user} on {self.approval_request_id}"
