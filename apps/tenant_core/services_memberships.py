"""
apps/tenant_core/services_memberships.py — Business Services for Layer 2 Module I: Memberships & Entitlements

Implements:
1. Atomic Membership Activation from Order/Purchase.
2. Immutable Contract Snapshot and Initial Entitlement Allocation.
3. Append-only Entitlement Ledger (Consumption & Reversals with balance_after).
4. Membership Freeze Management with automated end-date extension.
5. Membership Change Quoting & Execution (Upgrades, Downgrades, Cancellations).
6. Full Audit Trail (Status History, Branch History, Package History, BusinessAuditEvent, DomainOutboxEvent).
"""

import uuid
import logging
from decimal import Decimal
from datetime import date, datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError

from .models_memberships import (
    Membership,
    MembershipContractSnapshot,
    MembershipEntitlement,
    MembershipEntitlementLedger,
    MembershipBranchHistory,
    MembershipStatusHistory,
    MembershipFreeze,
    MembershipRenewalPolicy,
    MembershipChangePolicy,
    MembershipChangePolicyRule,
    MembershipChangeRequest,
    MembershipPackageHistory,
)
from .models_org import Branch
from .models_users import TenantUser
from .models_workforce import UserProfile
from .models_catalog import Package, PackageVersion, PackagePrice, PackageEntitlementDefinition
from .models_commerce import Order, OrderItem
from .services_reliability import record_business_audit, enqueue_outbox_event
from .context import get_current_tenant_db_alias

logger = logging.getLogger(__name__)


class MembershipLifecycleService:
    """
    Core business logic engine managing the complete lifecycle of memberships and entitlements.
    """

    @classmethod
    def activate_membership_from_order(
        cls,
        order: Order,
        order_item: OrderItem,
        start_date: Optional[date] = None,
        db_alias: Optional[str] = None,
        created_by_user: Optional[TenantUser] = None,
    ) -> Membership:
        """
        Activates a new membership, creates immutable contract snapshot, and initializes entitlements.
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        with transaction.atomic(using=alias):
            # Lock order to serialize concurrent activation attempts
            order = Order.objects.using(alias).select_for_update().get(id=order.id)

            # Idempotency: duplicate activation cannot create multiple active contracts
            existing = Membership.objects.using(alias).filter(
                source_order_item=order_item,
                status='ACTIVE'
            ).first()
            if existing:
                return existing

            if not order.user_profile:
                raise ValidationError("Order must have a member UserProfile to activate a membership.")

            if not order_item.package or not order_item.package_version:
                raise ValidationError("OrderItem must be a PACKAGE with package_version.")

            package = order_item.package
            package_version = order_item.package_version
            package_price = order_item.package_price

            # Calculate start and end dates
            start = start_date or timezone.now().date()
            duration_value = package_version.duration_value or 1
            duration_unit = package_version.duration_unit or 'MONTH'

            if duration_unit == 'DAY':
                end = start + timedelta(days=duration_value)
            elif duration_unit == 'WEEK':
                end = start + timedelta(weeks=duration_value)
            elif duration_unit == 'MONTH':
                end = start + timedelta(days=duration_value * 30)
            elif duration_unit == 'YEAR':
                end = start + timedelta(days=duration_value * 365)
            else:
                end = start + timedelta(days=30)

            membership_number = f"MEM-{uuid.uuid4().hex[:8].upper()}"

            membership = Membership.objects.using(alias).create(
                user_profile=order.user_profile,
                program=package.program,
                package=package,
                package_version=package_version,
                package_price=package_price,
                source_order=order,
                source_order_item=order_item,
                purchase_branch=order.branch,
                home_branch=order.branch,
                membership_number=membership_number,
                start_date=start,
                end_date=end,
                status='ACTIVE',
                activated_at=timezone.now(),
            )

            # Snapshot entitlements definition
            entitlements_def_qs = PackageEntitlementDefinition.objects.using(alias).filter(
                package_version=package_version
            )
            entitlements_data = []
            for ed in entitlements_def_qs:
                entitlements_data.append({
                    'id': str(ed.id),
                    'entitlement_type': ed.entitlement_type,
                    'reference_type': ed.reference_type,
                    'reference_id': str(ed.reference_id) if ed.reference_id else None,
                    'allocated_units': str(ed.allocated_units) if ed.allocated_units else None,
                    'is_unlimited': ed.is_unlimited,
                })

            # Create Immutable Contract Snapshot
            contract_snapshot = MembershipContractSnapshot.objects.using(alias).create(
                membership=membership,
                package=package,
                package_version=package_version,
                package_price=package_price,
                package_name_snapshot=order_item.item_name_snapshot or package.name,
                purchase_price=order_item.unit_price_snapshot,
                discount_amount=order_item.discount_amount,
                tax_amount=order_item.tax_amount,
                final_amount=order_item.total_amount,
                currency=order.currency,
                duration_value=duration_value,
                duration_unit=duration_unit,
                start_date=start,
                end_date=end,
                entitlements_snapshot=entitlements_data,
                purchase_branch=order.branch,
                source_order=order,
                source_order_item=order_item,
            )

            # Instantiate Entitlements & Initial Ledger entries
            now_dt = timezone.now()
            end_dt = timezone.make_aware(datetime.combine(end, datetime.max.time()))

            for ed in entitlements_def_qs:
                ent = MembershipEntitlement.objects.using(alias).create(
                    membership=membership,
                    source_definition=ed,
                    entitlement_type=ed.entitlement_type,
                    reference_type=ed.reference_type,
                    reference_id=ed.reference_id,
                    allocated_units=ed.allocated_units,
                    consumed_units=Decimal('0.00'),
                    is_unlimited=ed.is_unlimited,
                    valid_from=now_dt,
                    valid_until=end_dt,
                    status='ACTIVE',
                )

                # Record ledger row
                alloc_units = ed.allocated_units if not ed.is_unlimited else Decimal('999999.00')
                MembershipEntitlementLedger.objects.using(alias).create(
                    membership_entitlement=ent,
                    transaction_type='ALLOCATION',
                    units=alloc_units,
                    reason_code='INITIAL_PURCHASE',
                    reason_text=f"Initial allocation from contract {contract_snapshot.id}",
                    balance_after=alloc_units,
                    created_by_user=created_by_user,
                )

            # Append Branch History
            MembershipBranchHistory.objects.using(alias).create(
                membership=membership,
                to_branch=order.branch,
                change_type='INITIAL',
                reason="Membership purchase at home branch",
                changed_by_user=created_by_user,
            )

            # Append Status History
            MembershipStatusHistory.objects.using(alias).create(
                membership=membership,
                from_status=None,
                to_status='ACTIVE',
                reason_code='PURCHASE_ACTIVATION',
                reason_text=f"Activated via Order {order.order_number}",
                changed_by_user=created_by_user,
            )

            # Append Package History
            MembershipPackageHistory.objects.using(alias).create(
                membership=membership,
                to_package=package,
                to_package_version=package_version,
                change_type='UPGRADE',
                order=order,
                changed_by_user=created_by_user,
                reason="Initial package activation",
            )

            # Auditing & Outbox
            record_business_audit(
                organization=order.branch.organization,
                module='membership',
                action_code='MEMBERSHIP_ACTIVATED',
                entity_type='Membership',
                entity_id=membership.id,
                branch=order.branch,
                actor_user=created_by_user,
                metadata={
                    'membership_number': membership.membership_number,
                    'package_id': str(package.id),
                    'order_id': str(order.id),
                },
                db_alias=alias,
            )

            enqueue_outbox_event(
                organization=order.branch.organization,
                event_type='membership.activated',
                aggregate_type='Membership',
                aggregate_id=membership.id,
                payload={
                    'membership_id': str(membership.id),
                    'membership_number': membership.membership_number,
                    'user_profile_id': str(order.user_profile_id),
                    'start_date': str(start),
                    'end_date': str(end),
                },
                db_alias=alias,
            )

            return membership

    @classmethod
    def consume_entitlement(
        cls,
        membership: Membership,
        entitlement_type: str,
        units: Decimal = Decimal('1.00'),
        booking_id: Optional[uuid.UUID] = None,
        reason_text: Optional[str] = None,
        created_by_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> MembershipEntitlementLedger:
        """
        Consumes units from an active membership entitlement and appends an immutable ledger record.
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        with transaction.atomic(using=alias):
            if membership.status != 'ACTIVE':
                raise ValidationError(f"Cannot consume sessions: Membership is {membership.status}.")

            ent = MembershipEntitlement.objects.using(alias).select_for_update().filter(
                membership=membership,
                entitlement_type=entitlement_type,
                status='ACTIVE',
            ).first()

            if not ent:
                raise ValidationError(f"No active entitlement of type '{entitlement_type}' found on this membership.")

            if not ent.is_unlimited:
                remaining = ent.remaining_units
                if remaining is not None and remaining < units:
                    raise ValidationError(f"Insufficient entitlement units. Required: {units}, Remaining: {remaining}.")

                ent.consumed_units += units
                if ent.allocated_units and ent.consumed_units >= ent.allocated_units:
                    ent.status = 'EXHAUSTED'
                ent.save(using=alias, update_fields=['consumed_units', 'status', 'updated_at'])
                balance_after = ent.remaining_units
            else:
                ent.consumed_units += units
                ent.save(using=alias, update_fields=['consumed_units', 'updated_at'])
                balance_after = Decimal('999999.00')

            ledger = MembershipEntitlementLedger.objects.using(alias).create(
                membership_entitlement=ent,
                transaction_type='CONSUMPTION',
                units=-units,
                booking_id=booking_id,
                reason_code='BOOKING_CONSUMPTION',
                reason_text=reason_text or f"Consumed {units} unit(s) for booking {booking_id}",
                balance_after=balance_after,
                created_by_user=created_by_user,
            )

            return ledger

    @classmethod
    def reverse_entitlement(
        cls,
        membership: Membership,
        entitlement_type: str,
        units: Decimal = Decimal('1.00'),
        booking_id: Optional[uuid.UUID] = None,
        reason_text: Optional[str] = None,
        created_by_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> MembershipEntitlementLedger:
        """
        Reverses consumed units back to an entitlement upon booking cancellation and writes an immutable ledger entry.
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        with transaction.atomic(using=alias):
            ent = MembershipEntitlement.objects.using(alias).select_for_update().filter(
                membership=membership,
                entitlement_type=entitlement_type,
            ).first()

            if not ent:
                raise ValidationError(f"Entitlement '{entitlement_type}' not found on this membership.")

            if not ent.is_unlimited:
                ent.consumed_units = max(Decimal('0.00'), ent.consumed_units - units)
                if ent.status == 'EXHAUSTED':
                    ent.status = 'ACTIVE'
                ent.save(using=alias, update_fields=['consumed_units', 'status', 'updated_at'])
                balance_after = ent.remaining_units
            else:
                ent.consumed_units = max(Decimal('0.00'), ent.consumed_units - units)
                ent.save(using=alias, update_fields=['consumed_units', 'updated_at'])
                balance_after = Decimal('999999.00')

            ledger = MembershipEntitlementLedger.objects.using(alias).create(
                membership_entitlement=ent,
                transaction_type='REVERSAL',
                units=units,
                booking_id=booking_id,
                reason_code='CANCELLATION_REVERSAL',
                reason_text=reason_text or f"Reversed {units} unit(s) for booking {booking_id}",
                balance_after=balance_after,
                created_by_user=created_by_user,
            )

            return ledger

    # Alias restore_entitlement to reverse_entitlement
    restore_entitlement = reverse_entitlement

    @classmethod
    @transaction.atomic
    def apply_freeze(
        cls,
        membership: Membership,
        freeze_from: date,
        freeze_until: date,
        reason_text: Optional[str] = None,
        approved_by_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> MembershipFreeze:
        """
        Applies a membership freeze, calculates extended days, and extends the membership end date.
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'

        if freeze_until <= freeze_from:
            raise ValidationError("Freeze end date must be after freeze start date.")

        freeze_days = (freeze_until - freeze_from).days

        # Extend membership end_date
        old_end = membership.end_date
        new_end = old_end + timedelta(days=freeze_days)
        membership.end_date = new_end

        today = timezone.now().date()
        is_active_now = freeze_from <= today <= freeze_until

        old_status = membership.status
        if is_active_now:
            membership.status = 'FROZEN'

        membership.save(using=alias, update_fields=['end_date', 'status', 'updated_at'])

        freeze = MembershipFreeze.objects.using(alias).create(
            membership=membership,
            freeze_from=freeze_from,
            freeze_until=freeze_until,
            reason_code='MEMBER_REQUEST',
            reason_text=reason_text,
            extend_membership_days=freeze_days,
            status='ACTIVE' if is_active_now else 'SCHEDULED',
            approved_by_user=approved_by_user,
        )

        if is_active_now and old_status != 'FROZEN':
            MembershipStatusHistory.objects.using(alias).create(
                membership=membership,
                from_status=old_status,
                to_status='FROZEN',
                reason_code='FREEZE_APPLIED',
                reason_text=f"Frozen from {freeze_from} to {freeze_until}. Extended end date by {freeze_days} days to {new_end}.",
                changed_by_user=approved_by_user,
            )

        return freeze

    @classmethod
    @transaction.atomic
    def quote_and_apply_change(
        cls,
        membership: Membership,
        policy_rule: MembershipChangePolicyRule,
        target_package: Optional[Package] = None,
        target_package_version: Optional[PackageVersion] = None,
        reason: Optional[str] = None,
        actor_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> MembershipChangeRequest:
        """
        Evaluates and applies a membership modification (upgrade, downgrade, cancellation).
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        policy = policy_rule.membership_change_policy
        change_type = policy_rule.change_type

        # Calculate remaining days snapshot
        today = timezone.now().date()
        remaining_days = max(0, (membership.end_date - today).days)

        # Calculate remaining sessions snapshot
        primary_ent = membership.entitlements.filter(status='ACTIVE').first()
        remaining_sessions = primary_ent.remaining_units if (primary_ent and not primary_ent.is_unlimited) else Decimal('0.00')

        # Compute financial quote
        contract = getattr(membership, 'contract_snapshot', None)
        orig_price = contract.final_amount if contract else Decimal('5000.00')

        # Rough proration calculation
        total_duration = max(1, (contract.end_date - contract.start_date).days if contract else 30)
        daily_rate = orig_price / Decimal(str(total_duration))
        original_remaining_val = (daily_rate * Decimal(str(remaining_days))).quantize(Decimal('0.01'))

        additional_amount = Decimal('0.00')
        penalty_amount = Decimal('0.00')
        refund_amount = Decimal('0.00')

        if change_type == 'UPGRADE' and target_package_version:
            target_price_obj = target_package_version.prices.filter(status='ACTIVE').first()
            target_price = target_price_obj.base_price if target_price_obj else Decimal('8000.00')
            additional_amount = max(Decimal('0.00'), target_price - original_remaining_val)
        elif change_type == 'CANCELLATION':
            if policy_rule.cancellation_fee_type == 'FIXED' and policy_rule.cancellation_fee_value:
                penalty_amount = policy_rule.cancellation_fee_value
            elif policy_rule.cancellation_fee_type == 'PERCENTAGE' and policy_rule.cancellation_fee_value:
                penalty_amount = (original_remaining_val * (policy_rule.cancellation_fee_value / Decimal('100.00'))).quantize(Decimal('0.01'))

            if policy_rule.refund_mode == 'PRORATED':
                refund_amount = max(Decimal('0.00'), original_remaining_val - penalty_amount)

        final_payable = additional_amount

        req = MembershipChangeRequest.objects.using(alias).create(
            membership=membership,
            membership_change_policy=policy,
            membership_change_policy_rule=policy_rule,
            policy_version_number=policy.version_number,
            change_type=change_type,
            current_package=membership.package,
            current_package_version=membership.package_version,
            target_package=target_package,
            target_package_version=target_package_version,
            effective_mode_applied=policy_rule.effective_mode,
            pricing_mode_applied=policy_rule.pricing_mode,
            remaining_sessions_snapshot=remaining_sessions,
            remaining_days_snapshot=remaining_days,
            original_remaining_value=original_remaining_val,
            penalty_amount=penalty_amount,
            refund_amount=refund_amount,
            additional_amount=additional_amount,
            final_amount_payable=final_payable,
            status='APPLIED',
            requested_by_user=actor_user,
            approved_by_user=actor_user,
            applied_at=timezone.now(),
            reason=reason,
        )

        # Apply membership changes
        if change_type in ['UPGRADE', 'DOWNGRADE'] and target_package and target_package_version:
            from_pkg = membership.package
            from_pkg_v = membership.package_version

            membership.package = target_package
            membership.package_version = target_package_version
            membership.save(using=alias, update_fields=['package', 'package_version', 'updated_at'])

            MembershipPackageHistory.objects.using(alias).create(
                membership=membership,
                from_package=from_pkg,
                from_package_version=from_pkg_v,
                to_package=target_package,
                to_package_version=target_package_version,
                change_type=change_type,
                membership_change_request=req,
                changed_by_user=actor_user,
                reason=reason or f"Executed {change_type}",
            )
        elif change_type == 'CANCELLATION':
            old_st = membership.status
            membership.status = 'CANCELLED'
            membership.cancelled_at = timezone.now()
            membership.save(using=alias, update_fields=['status', 'cancelled_at', 'updated_at'])

            # Expire entitlements
            membership.entitlements.filter(status='ACTIVE').update(status='EXPIRED')

            MembershipStatusHistory.objects.using(alias).create(
                membership=membership,
                from_status=old_st,
                to_status='CANCELLED',
                reason_code='MEMBER_CANCELLATION',
                reason_text=reason or "Membership cancelled via change request",
                changed_by_user=actor_user,
            )

        return req
