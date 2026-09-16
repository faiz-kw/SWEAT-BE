"""
apps/tenant_core/services_approvals.py — Business Services for Layer 2 Module N: Admin Approvals

Implements:
1. Multi-Step Approval Workflows (Refunds, Discounts, Overrides, Custom Exceptions).
2. Segregation of Duties Enforcement (Requester cannot approve their own request).
3. Immutable Audit Trails and Outbox Event Dispatch.
"""

import uuid
import logging
from typing import Optional, Dict, Any
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError

from .models_approvals import ApprovalRequest, ApprovalAction
from .models_org import Organization
from .models_users import TenantUser
from .services_reliability import record_business_audit, enqueue_outbox_event
from .context import get_current_tenant_db_alias

logger = logging.getLogger(__name__)


class AdminApprovalService:
    """
    Core engine managing operational approval gates, multi-approver quorum, and audit verification.
    """

    @classmethod
    @transaction.atomic
    def create_approval_request(
        cls,
        organization: Organization,
        request_type: str,
        entity_type: str,
        entity_id: uuid.UUID,
        requested_by_user: TenantUser,
        requested_payload: Optional[Dict[str, Any]] = None,
        required_approvals: int = 1,
        db_alias: Optional[str] = None,
    ) -> ApprovalRequest:
        alias = db_alias or get_current_tenant_db_alias() or 'default'

        req = ApprovalRequest.objects.using(alias).create(
            organization=organization,
            request_type=request_type,
            entity_type=entity_type,
            entity_id=entity_id,
            requested_by_user=requested_by_user,
            requested_payload=requested_payload or {},
            status='PENDING',
            required_approvals=max(1, required_approvals),
        )

        record_business_audit(
            organization=organization,
            module='approvals',
            action_code='APPROVAL_REQUEST_CREATED',
            entity_type='ApprovalRequest',
            entity_id=req.id,
            actor_user=requested_by_user,
            metadata={
                'request_type': request_type,
                'target_entity_type': entity_type,
                'target_entity_id': str(entity_id),
                'required_approvals': required_approvals,
            },
            db_alias=alias,
        )

        enqueue_outbox_event(
            organization=organization,
            event_type='approvals.requested',
            aggregate_type='ApprovalRequest',
            aggregate_id=req.id,
            payload={
                'approval_request_id': str(req.id),
                'request_type': request_type,
                'entity_type': entity_type,
                'entity_id': str(entity_id),
                'requested_by_user_id': str(requested_by_user.id),
            },
            db_alias=alias,
        )

        return req

    @classmethod
    @transaction.atomic
    def process_action(
        cls,
        approval_request: ApprovalRequest,
        approver_user: TenantUser,
        action: str,
        comment: Optional[str] = None,
        allow_self_approval: bool = False,
        db_alias: Optional[str] = None,
    ) -> ApprovalAction:
        alias = db_alias or get_current_tenant_db_alias() or 'default'

        if approval_request.status != 'PENDING':
            raise ValidationError(
                f"Cannot act on approval request: Current status is {approval_request.status}."
            )

        if not allow_self_approval and approval_request.requested_by_user_id == approver_user.id:
            raise ValidationError("Segregation of duties violation: Requesters cannot approve their own requests.")

        if action not in ['APPROVED', 'REJECTED']:
            raise ValidationError(f"Invalid approval action '{action}'. Must be APPROVED or REJECTED.")

        # Check if approver already acted
        prior = ApprovalAction.objects.using(alias).filter(
            approval_request=approval_request,
            approver_user=approver_user
        ).first()
        if prior:
            raise ValidationError(f"User has already submitted an approval action ({prior.action}).")

        action_record = ApprovalAction.objects.using(alias).create(
            approval_request=approval_request,
            approver_user=approver_user,
            action=action,
            comment=comment,
            acted_at=timezone.now(),
        )

        if action == 'REJECTED':
            approval_request.status = 'REJECTED'
            approval_request.resolved_at = timezone.now()
            approval_request.save(using=alias, update_fields=['status', 'resolved_at', 'updated_at'])
        else:
            # Count approvals
            approval_count = ApprovalAction.objects.using(alias).filter(
                approval_request=approval_request,
                action='APPROVED'
            ).count()

            if approval_count >= approval_request.required_approvals:
                approval_request.status = 'APPROVED'
                approval_request.resolved_at = timezone.now()
                approval_request.save(using=alias, update_fields=['status', 'resolved_at', 'updated_at'])

        record_business_audit(
            organization=approval_request.organization,
            module='approvals',
            action_code=f"APPROVAL_ACTION_{action}",
            entity_type='ApprovalRequest',
            entity_id=approval_request.id,
            actor_user=approver_user,
            metadata={
                'action': action,
                'comment': comment,
                'resulting_status': approval_request.status,
            },
            db_alias=alias,
        )

        enqueue_outbox_event(
            organization=approval_request.organization,
            event_type=f"approvals.{action.lower()}",
            aggregate_type='ApprovalRequest',
            aggregate_id=approval_request.id,
            payload={
                'approval_request_id': str(approval_request.id),
                'action': action,
                'status': approval_request.status,
                'approver_user_id': str(approver_user.id),
            },
            db_alias=alias,
        )

        return action_record
