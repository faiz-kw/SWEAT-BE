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

        if action == 'REJECTED' and (not comment or not str(comment).strip()):
            raise ValidationError('Rejection reason is required when rejecting an approval request.')

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
            if not comment or not str(comment).strip():
                raise ValidationError("Rejection reason comment is required.", code="REJECTION_REASON_REQUIRED")
            approval_request.status = 'REJECTED'
            approval_request.resolved_at = timezone.now()
            approval_request.save(using=alias, update_fields=['status', 'resolved_at', 'updated_at'])
            if approval_request.request_type == 'CASH_PAYMENT_APPROVAL':
                cls._materialize_cash_payment_rejection(approval_request, action_record, alias=alias)
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

                # Automatic materialization for workforce schedule/leave exceptions
                if approval_request.request_type in ['TRAINER_LEAVE_REQUEST', 'SCHEDULE_EXCEPTION_REQUEST']:
                    cls._materialize_schedule_exception(approval_request, alias=alias)
                elif approval_request.request_type == 'CASH_PAYMENT_APPROVAL':
                    cls._materialize_cash_payment_approval(approval_request, action_record, alias=alias)

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

    @classmethod
    def _materialize_schedule_exception(cls, approval_request: ApprovalRequest, alias: str = 'default'):
        """
        Creates or updates an active EmployeeScheduleException when a workforce
        leave/exception approval request is granted.
        """
        from .models_workforce import EmployeeProfile, EmployeeScheduleException
        from .models_org import Branch

        payload = approval_request.requested_payload or {}
        emp_id = payload.get('employee_profile_id')
        emp = None
        if emp_id:
            emp = EmployeeProfile.objects.using(alias).filter(id=emp_id).first()
        if not emp:
            user = approval_request.requested_by_user
            emp = EmployeeProfile.objects.using(alias).filter(user_profile__user=user).first()

        if not emp:
            logger.warning(
                "Cannot materialize schedule exception for ApprovalRequest %s: EmployeeProfile not found.",
                approval_request.id
            )
            return

        branch_id = payload.get('branch_id')
        branch = Branch.objects.using(alias).filter(id=branch_id).first() if branch_id else None
        exception_date = payload.get('exception_date')
        if not exception_date:
            logger.warning(
                "Cannot materialize schedule exception for ApprovalRequest %s: exception_date missing.",
                approval_request.id
            )
            return

        exception_type = payload.get('exception_type', 'LEAVE')
        is_available = payload.get('is_available')
        if is_available is None:
            is_available = exception_type in ['WEEKLY_OFF_OVERRIDE', 'SPECIAL_SHIFT', 'TEMPORARY_AVAILABILITY']

        start_time = payload.get('start_time') or None
        end_time = payload.get('end_time') or None
        reason = payload.get('reason') or f"Approved {exception_type} request #{str(approval_request.id)[:8]}"

        EmployeeScheduleException.objects.using(alias).update_or_create(
            employee_profile=emp,
            exception_date=exception_date,
            defaults={
                'branch': branch,
                'exception_type': exception_type,
                'is_available': is_available,
                'start_time': start_time,
                'end_time': end_time,
                'reason': reason,
                'status': 'ACTIVE',
            }
        )


    @classmethod
    def _materialize_cash_payment_approval(cls, approval_request: ApprovalRequest, action_record: ApprovalAction, alias: str = 'default'):
        """
        Activates approved cash payment transaction, recalculates order balance,
        and conditionally triggers membership/conversion finalization.
        """
        from decimal import Decimal
        from .models_commerce import PaymentTransaction, Order, MemberInvoice
        from .services_payment_policy import PaymentPolicyService

        txn = PaymentTransaction.objects.using(alias).select_for_update().filter(id=approval_request.entity_id).first()
        if not txn:
            logger.error("PaymentTransaction %s not found for cash approval", approval_request.entity_id)
            return

        txn.status = 'SUCCESS'
        txn.paid_at = timezone.now()
        txn.metadata['approval_status'] = 'APPROVED'
        txn.metadata['approved_by'] = str(action_record.approver_user_id)
        txn.metadata['approved_by_name'] = getattr(action_record.approver_user, 'display_name', '')
        txn.metadata['approved_at'] = timezone.now().isoformat()
        txn.metadata['approval_comment'] = action_record.comment or ''
        txn.save(using=alias)

        order = Order.objects.using(alias).select_for_update().get(id=txn.order_id)
        total_amount, total_paid, outstanding, count = PaymentPolicyService.calculate_order_balance(order, db_alias=alias)

        if total_paid >= total_amount:
            order.status = 'PAID'
            order.save(using=alias)
            if not MemberInvoice.objects.using(alias).filter(order=order).exists():
                invoice_number = f"INV-{uuid.uuid4().hex[:8].upper()}"
                MemberInvoice.objects.using(alias).create(
                    invoice_number=invoice_number,
                    order=order,
                    user_profile=order.user_profile,
                    branch=order.branch,
                    subtotal=order.subtotal,
                    discount_amount=order.discount_amount,
                    reward_amount=order.reward_amount,
                    tax_amount=order.tax_amount,
                    total_amount=order.total_amount,
                    status='PAID',
                    issued_at=timezone.now(),
                )
        else:
            order.status = 'PARTIALLY_PAID'
            order.save(using=alias)

        # Release full entitlements if membership was provisional
        from .models_memberships import Membership, MembershipEntitlementLedger, MembershipStatusHistory
        from .models_catalog import PackageEntitlementDefinition
        membership = Membership.objects.using(alias).filter(source_order=order).first()
        if membership and membership.legacy_reference and 'PROVISIONAL_CASH' in membership.legacy_reference:
            ent_defs = {
                str(ed.id): ed
                for ed in PackageEntitlementDefinition.objects.using(alias).filter(package_version=membership.package_version)
            }
            for ent in membership.entitlements.using(alias).all():
                ed = ent.source_definition or ent_defs.get(str(ent.source_definition_id))
                if ed:
                    full_units = ed.allocated_units
                    current_alloc = ent.allocated_units or Decimal('0.00')
                    remaining_units = (full_units - current_alloc) if full_units else Decimal('0.00')
                    ent.allocated_units = full_units
                    ent.is_unlimited = ed.is_unlimited
                    ent.status = 'ACTIVE'
                    ent.save(using=alias, update_fields=['allocated_units', 'is_unlimited', 'status', 'updated_at'])
                    if remaining_units > Decimal('0.00'):
                        alloc_log_units = remaining_units if not ed.is_unlimited else Decimal('999999.00')
                        MembershipEntitlementLedger.objects.using(alias).create(
                            membership_entitlement=ent,
                            transaction_type='ALLOCATION',
                            units=alloc_log_units,
                            reason_code='CASH_APPROVAL_ENTITLEMENT_RELEASE',
                            reason_text="Full package entitlement released upon cash payment approval",
                            balance_after=ent.remaining_units or Decimal('999999.00'),
                            created_by_user=action_record.approver_user,
                        )
            membership.legacy_reference = 'CASH_APPROVED'
            membership.status = 'ACTIVE'
            membership.save(using=alias, update_fields=['legacy_reference', 'status', 'updated_at'])
            MembershipStatusHistory.objects.using(alias).create(
                membership=membership,
                from_status='ACTIVE',
                to_status='ACTIVE',
                reason_code='CASH_PAYMENT_APPROVED',
                reason_text="Provisional restrictions cleared upon cash payment approval",
                changed_by_user=action_record.approver_user,
            )

        # Check membership activation policy
        should_activate = PaymentPolicyService.should_activate_membership(order, total_paid, db_alias=alias)
        if should_activate and order.lead and order.lead.current_status != 'CONVERTED':
            from .services_crm import LeadConversionService
            LeadConversionService._finalize_conversion_for_order(order=order, actor_user=action_record.approver_user, db_alias=alias)

    @classmethod
    def _materialize_cash_payment_rejection(cls, approval_request: ApprovalRequest, action_record: ApprovalAction, alias: str = 'default'):
        """
        Marks rejected cash payment transaction as CANCELLED.
        Order balance and membership remain unaffected.
        """
        from .models_commerce import PaymentTransaction

        txn = PaymentTransaction.objects.using(alias).select_for_update().filter(id=approval_request.entity_id).first()
        if not txn:
            return

        txn.status = 'CANCELLED'
        txn.metadata['approval_status'] = 'REJECTED'
        txn.metadata['rejected_by'] = str(action_record.approver_user_id)
        txn.metadata['rejected_by_name'] = getattr(action_record.approver_user, 'display_name', '')
        txn.metadata['rejected_at'] = timezone.now().isoformat()
        txn.metadata['rejection_reason'] = action_record.comment or ''
        txn.save(using=alias)

        # Cancel provisional membership upon cash payment rejection
        from .models_memberships import Membership, MembershipStatusHistory
        membership = Membership.objects.using(alias).filter(source_order=txn.order).first()
        if membership and membership.legacy_reference and 'PROVISIONAL_CASH' in membership.legacy_reference:
            membership.status = 'CANCELLED'
            membership.legacy_reference = f"CASH_REJECTED:reason={action_record.comment or 'Payment rejected'}"
            membership.save(using=alias, update_fields=['status', 'legacy_reference', 'updated_at'])
            membership.entitlements.using(alias).update(status='INACTIVE')
            MembershipStatusHistory.objects.using(alias).create(
                membership=membership,
                from_status='ACTIVE',
                to_status='CANCELLED',
                reason_code='CASH_PAYMENT_REJECTED',
                reason_text=f"Cash payment rejected: {action_record.comment or 'Payment rejected'}",
                changed_by_user=action_record.approver_user,
            )
