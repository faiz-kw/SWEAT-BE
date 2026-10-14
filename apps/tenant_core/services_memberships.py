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
    MembershipExtension,
    MembershipPolicyConfig,
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

            # Resolve applicable active PAR-Q form for the program or organization
            from .models_crm import IntakeForm
            org = order.branch.organization if order.branch else (order.user_profile.preferred_branch.organization if order.user_profile and order.user_profile.preferred_branch else None)
            parq_form = None
            if org:
                if package and package.program:
                    parq_form = (
                        IntakeForm.objects.using(alias)
                        .filter(organization=org, status='ACTIVE', form_type__in=['PAR_Q', 'PARQ'], assigned_programs=package.program)
                        .order_by('-version_number')
                        .first()
                    )
                if not parq_form:
                    parq_form = (
                        IntakeForm.objects.using(alias)
                        .filter(organization=org, status='ACTIVE', form_type__in=['PAR_Q', 'PARQ'], is_default_for_all_programs=True)
                        .order_by('-version_number')
                        .first()
                    )

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
                parq_status='PENDING',
                parq_form=parq_form,
                parq_submission=None,
                parq_completed_at=None,
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

            # Resolve accepted terms document versions for order/member
            from .models_catalog import TermsAcceptance
            terms_acc_ids = list(TermsAcceptance.objects.using(alias).filter(order_id=order.id).values_list('terms_document_version_id', flat=True))
            if not terms_acc_ids and order.user_profile:
                terms_acc_ids = list(TermsAcceptance.objects.using(alias).filter(user_profile=order.user_profile).order_by('-accepted_at').values_list('terms_document_version_id', flat=True)[:1])

            # Create Immutable Contract Snapshot
            contract_snapshot = MembershipContractSnapshot.objects.using(alias).create(
                terms_document_version_ids=[str(t_id) for t_id in terms_acc_ids],
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

            if membership.legacy_reference and 'PROVISIONAL_CASH_LIMIT_REACHED' in membership.legacy_reference:
                raise ValidationError(
                    "PAYMENT_APPROVAL_PENDING_LIMIT_REACHED: Cash payment approval is still pending and provisional session limit has been reached.",
                    code="PAYMENT_APPROVAL_PENDING_LIMIT_REACHED"
                )

            ent = MembershipEntitlement.objects.using(alias).select_for_update().filter(
                membership=membership,
                entitlement_type=entitlement_type,
                status='ACTIVE',
            ).first()

            if not ent:
                if membership.legacy_reference and 'PROVISIONAL_CASH' in membership.legacy_reference:
                    membership.legacy_reference = "PROVISIONAL_CASH_LIMIT_REACHED:limit=provisional"
                    membership.save(using=alias, update_fields=['legacy_reference', 'updated_at'])
                    raise ValidationError(
                        "PAYMENT_APPROVAL_PENDING_LIMIT_REACHED: Cash payment approval is still pending and provisional session limit has been reached.",
                        code="PAYMENT_APPROVAL_PENDING_LIMIT_REACHED"
                    )
                raise ValidationError(f"No active entitlement of type '{entitlement_type}' found on this membership.")

            if not ent.is_unlimited:
                remaining = ent.remaining_units
                if remaining is not None and remaining < units:
                    if membership.legacy_reference and 'PROVISIONAL_CASH' in membership.legacy_reference:
                        membership.legacy_reference = f"PROVISIONAL_CASH_LIMIT_REACHED:limit={ent.allocated_units}"
                        membership.save(using=alias, update_fields=['legacy_reference', 'updated_at'])
                        raise ValidationError(
                            "PAYMENT_APPROVAL_PENDING_LIMIT_REACHED: Cash payment approval is still pending and provisional session limit has been reached.",
                            code="PAYMENT_APPROVAL_PENDING_LIMIT_REACHED"
                        )
                    raise ValidationError(f"Insufficient entitlement units. Required: {units}, Remaining: {remaining}.")

                ent.consumed_units += units
                if ent.allocated_units and ent.consumed_units >= ent.allocated_units:
                    ent.status = 'EXHAUSTED'
                    if membership.legacy_reference and 'PROVISIONAL_CASH' in membership.legacy_reference:
                        membership.legacy_reference = f"PROVISIONAL_CASH_LIMIT_REACHED:limit={ent.allocated_units}"
                        membership.save(using=alias, update_fields=['legacy_reference', 'updated_at'])
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
    def get_policy_config(
        cls,
        organization=None,
        db_alias: Optional[str] = None,
    ) -> MembershipPolicyConfig:
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        from .models_org import Organization
        org = organization
        if not org:
            org = Organization.objects.using(alias).filter(status='ACTIVE').order_by('-created_at').first()
        if not org:
            org = Organization.objects.using(alias).first()

        config, _ = MembershipPolicyConfig.objects.using(alias).get_or_create(
            organization=org,
            defaults={
                'extension_per_day_fee': Decimal('50.00'),
                'extension_activation_fee': Decimal('200.00'),
                'extension_min_days': 1,
                'extension_max_days': 90,
                'extension_requires_remaining_sessions': True,
                'freeze_per_day_fee': Decimal('25.00'),
                'freeze_min_days': 3,
                'freeze_max_days': 60,
                'freeze_max_count_per_cycle': 3,
            }
        )
        return config

    @classmethod
    def check_remaining_sessions(
        cls,
        membership: Membership,
        db_alias: Optional[str] = None,
    ) -> Tuple[bool, Decimal]:
        """
        Calculates remaining session units across active entitlements.
        Returns (has_remaining: bool, remaining_sessions: Decimal).
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        ents = membership.entitlements.using(alias).all()
        if not ents.exists():
            return False, Decimal('0.00')
        total = Decimal('0.00')
        has_unlimited = False
        for e in ents:
            if getattr(e, 'is_unlimited', False):
                has_unlimited = True
                break
            rem = e.remaining_units
            if rem and rem > Decimal('0.00'):
                total += rem
        if has_unlimited:
            return True, Decimal('999999')
        return (total > Decimal('0.00')), total

    @classmethod
    def calculate_freeze(
        cls,
        membership: Membership,
        freeze_from: date,
        freeze_until: date,
        db_alias: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Quotes and validates a prospective membership freeze window against tenant policy.
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        config = cls.get_policy_config(membership.organization, db_alias=alias)

        if freeze_until < freeze_from:
            return {
                'membership_id': str(membership.id),
                'can_freeze': False,
                'error': "Freeze end date must be on or after freeze start date.",
            }

        freeze_days = (freeze_until - freeze_from).days + 1

        if freeze_days < config.freeze_min_days:
            return {
                'membership_id': str(membership.id),
                'can_freeze': False,
                'error': f"Minimum freeze duration is {config.freeze_min_days} day(s). Selected: {freeze_days} day(s).",
            }

        if freeze_days > config.freeze_max_days:
            return {
                'membership_id': str(membership.id),
                'can_freeze': False,
                'error': f"Maximum freeze duration is {config.freeze_max_days} day(s). Selected: {freeze_days} day(s).",
            }

        past_freezes_count = MembershipFreeze.objects.using(alias).filter(
            membership=membership,
            status__in=['ACTIVE', 'COMPLETED', 'SCHEDULED']
        ).count()
        if past_freezes_count >= config.freeze_max_count_per_cycle:
            return {
                'membership_id': str(membership.id),
                'can_freeze': False,
                'error': f"Maximum allowed freezes ({config.freeze_max_count_per_cycle}) reached for this package.",
            }

        fee_per_day = config.freeze_per_day_fee
        total_fee = fee_per_day * Decimal(freeze_days)
        new_end_date = membership.end_date + timedelta(days=freeze_days)

        return {
            'membership_id': str(membership.id),
            'membership_number': membership.membership_number,
            'current_end_date': membership.end_date.isoformat(),
            'freeze_from': freeze_from.isoformat(),
            'freeze_until': freeze_until.isoformat(),
            'freeze_days': freeze_days,
            'fee_per_day': str(fee_per_day),
            'total_fee': str(total_fee),
            'new_end_date': new_end_date.isoformat(),
            'can_freeze': True,
            'error': None,
        }

    @classmethod
    def auto_sync_freeze_status(
        cls,
        membership: Membership,
        db_alias: Optional[str] = None,
    ) -> Membership:
        """
        Auto-reconciles freeze status:
        - If today > freeze_until for any ACTIVE freeze, marks it COMPLETED.
        - If today >= freeze_from and <= freeze_until for a SCHEDULED freeze, marks it ACTIVE and membership FROZEN.
        - If membership is FROZEN but has no active freezes today, restores status to ACTIVE.
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        today = timezone.now().date()

        # Mark past active freezes as COMPLETED
        past_freezes = MembershipFreeze.objects.using(alias).filter(
            membership=membership,
            status='ACTIVE',
            freeze_until__lt=today,
        )
        if past_freezes.exists():
            past_freezes.update(status='COMPLETED')

        # Activate scheduled freezes whose start date has arrived
        due_scheduled = MembershipFreeze.objects.using(alias).filter(
            membership=membership,
            status='SCHEDULED',
            freeze_from__lte=today,
            freeze_until__gte=today,
        )
        if due_scheduled.exists():
            due_scheduled.update(status='ACTIVE')
            if membership.status != 'FROZEN':
                old_status = membership.status
                membership.status = 'FROZEN'
                membership.save(using=alias, update_fields=['status', 'updated_at'])
                MembershipStatusHistory.objects.using(alias).create(
                    membership=membership,
                    from_status=old_status,
                    to_status='FROZEN',
                    reason_code='FREEZE_AUTO_ACTIVATED',
                    reason_text=f"Scheduled freeze became active on {today}.",
                )
            return membership

        # If membership is FROZEN but no freeze is active today, restore ACTIVE status
        if membership.status == 'FROZEN':
            active_freeze_today = MembershipFreeze.objects.using(alias).filter(
                membership=membership,
                status='ACTIVE',
                freeze_from__lte=today,
                freeze_until__gte=today,
            ).exists()
            if not active_freeze_today:
                old_status = membership.status
                membership.status = 'ACTIVE'
                membership.save(using=alias, update_fields=['status', 'updated_at'])
                MembershipStatusHistory.objects.using(alias).create(
                    membership=membership,
                    from_status=old_status,
                    to_status='ACTIVE',
                    reason_code='FREEZE_AUTO_RESUMED',
                    reason_text=f"Freeze window concluded on {today}. Booking and attendance access automatically resumed.",
                )

        return membership

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
        Applies a membership freeze, calculates freeze fee, extends membership end date,
        and enforces strict class booking lockout during the freeze window.
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        calc = cls.calculate_freeze(membership, freeze_from, freeze_until, db_alias=alias)
        if not calc.get('can_freeze'):
            raise ValidationError(calc.get('error') or "Cannot freeze membership.")

        freeze_days = calc['freeze_days']
        fee_per_day = Decimal(calc['fee_per_day'])
        total_fee = Decimal(calc['total_fee'])
        new_end = date.fromisoformat(calc['new_end_date'])

        # Extend membership end_date
        old_end = membership.end_date
        membership.end_date = new_end

        today = timezone.now().date()
        is_active_now = freeze_from <= today <= freeze_until

        old_status = membership.status
        if is_active_now:
            membership.status = 'FROZEN'

        membership.save(using=alias, update_fields=['end_date', 'status', 'updated_at'])

        # Extend active and expired entitlements valid_until by freeze_days
        end_dt = timezone.make_aware(datetime.combine(new_end, datetime.max.time()))
        membership.entitlements.using(alias).filter(status__in=['ACTIVE', 'EXPIRED']).update(
            valid_until=end_dt
        )

        freeze = MembershipFreeze.objects.using(alias).create(
            membership=membership,
            freeze_from=freeze_from,
            freeze_until=freeze_until,
            reason_code='MEMBER_REQUEST',
            reason_text=reason_text,
            extend_membership_days=freeze_days,
            fee_per_day=fee_per_day,
            total_fee=total_fee,
            payment_status='PAID',
            status='ACTIVE' if is_active_now else 'SCHEDULED',
            approved_by_user=approved_by_user,
        )

        if is_active_now and old_status != 'FROZEN':
            MembershipStatusHistory.objects.using(alias).create(
                membership=membership,
                from_status=old_status,
                to_status='FROZEN',
                reason_code='FREEZE_APPLIED',
                reason_text=(
                    f"Frozen from {freeze_from} to {freeze_until}. "
                    f"Extended end date by {freeze_days} days from {old_end} to {new_end}. "
                    f"Freeze fee: ₹{total_fee} (₹{fee_per_day}/day x {freeze_days} days)."
                ),
                changed_by_user=approved_by_user,
            )

        # Handle existing bookings falling within the freeze window
        if membership.user_profile:
            try:
                from .models_bookings import Booking
                from .services_bookings import BookingAttendanceService
                freeze_bookings = Booking.objects.using(alias).filter(
                    user_profile=membership.user_profile,
                    occurrence__start_at__date__gte=freeze_from,
                    occurrence__start_at__date__lte=freeze_until,
                    status__in=['CONFIRMED', 'RESERVED', 'WAITLISTED']
                )
                for bk in freeze_bookings:
                    BookingAttendanceService.cancel_booking(
                        booking=bk,
                        cancellation_source='ADMIN',
                        reason_text=f"Membership frozen from {freeze_from} to {freeze_until}.",
                        actor_user=approved_by_user,
                        db_alias=alias,
                    )
            except Exception as e:
                logger.warning(f"Error cancelling bookings during freeze: {e}")

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

            # Provision and adjust entitlements for upgraded target package version
            for ped in target_package_version.entitlement_definitions.using(alias).all():
                existing_ent = membership.entitlements.using(alias).filter(entitlement_type=ped.entitlement_type).first()
                if existing_ent:
                    if policy_rule.unused_session_handling == 'CARRY_FORWARD':
                        add_units = ped.allocated_units or Decimal('0.00')
                        if add_units > Decimal('0.00'):
                            cls.adjust_entitlement(
                                membership=membership,
                                entitlement_type=ped.entitlement_type,
                                units_delta=add_units,
                                reason_code='UPGRADE_ALLOCATION',
                                reason_text=f"Package upgrade to {target_package.name} (v{target_package_version.version_number})",
                                actor_user=actor_user,
                                db_alias=alias,
                            )
                    elif policy_rule.unused_session_handling == 'RESET':
                        rem = existing_ent.remaining_units or Decimal('0.00')
                        if rem > Decimal('0.00'):
                            MembershipEntitlementLedger.objects.using(alias).create(
                                membership_entitlement=existing_ent,
                                transaction_type='EXPIRY',
                                units=-rem,
                                reason_code='UPGRADE_RESET',
                                reason_text=f"Forfeited prior balance on upgrade to {target_package.name}",
                                balance_after=Decimal('0.00'),
                                created_by_user=actor_user,
                            )
                        existing_ent.allocated_units = ped.allocated_units
                        existing_ent.consumed_units = Decimal('0.00')
                        existing_ent.status = 'ACTIVE'
                        existing_ent.save(using=alias, update_fields=['allocated_units', 'consumed_units', 'status', 'updated_at'])
                        MembershipEntitlementLedger.objects.using(alias).create(
                            membership_entitlement=existing_ent,
                            transaction_type='ALLOCATION',
                            units=ped.allocated_units or Decimal('0.00'),
                            reason_code='UPGRADE_RESET_ALLOCATION',
                            reason_text=f"Reset allocation on upgrade to {target_package.name}",
                            balance_after=ped.allocated_units or Decimal('0.00'),
                            created_by_user=actor_user,
                        )
                else:
                    new_ent = MembershipEntitlement.objects.using(alias).create(
                        membership=membership,
                        source_definition=ped,
                        entitlement_type=ped.entitlement_type,
                        allocated_units=ped.allocated_units,
                        consumed_units=Decimal('0.00'),
                        is_unlimited=ped.is_unlimited,
                        valid_from=timezone.now(),
                        valid_until=timezone.make_aware(datetime.combine(membership.end_date, datetime.max.time())),
                        status='ACTIVE',
                    )
                    MembershipEntitlementLedger.objects.using(alias).create(
                        membership_entitlement=new_ent,
                        transaction_type='ALLOCATION',
                        units=ped.allocated_units or Decimal('0.00'),
                        reason_code='UPGRADE_NEW_ALLOCATION',
                        reason_text=f"Initial allocation on upgrade to {target_package.name}",
                        balance_after=ped.allocated_units or Decimal('0.00'),
                        created_by_user=actor_user,
                    )
        elif change_type == 'CANCELLATION':
            old_st = membership.status
            membership.status = 'CANCELLED'
            membership.cancelled_at = timezone.now()
            membership.save(using=alias, update_fields=['status', 'cancelled_at', 'updated_at'])

            # Expire entitlements
            for ent in membership.entitlements.filter(status='ACTIVE'):
                rem = ent.remaining_units or Decimal('0.00')
                if rem > Decimal('0.00'):
                    MembershipEntitlementLedger.objects.using(alias).create(
                        membership_entitlement=ent,
                        transaction_type='EXPIRY',
                        units=-rem,
                        reason_code='MEMBERSHIP_CANCELLED',
                        reason_text=reason_text or "Forfeited remaining units upon membership cancellation",
                        balance_after=Decimal('0.00'),
                        created_by_user=actor_user,
                    )
                ent.status = 'EXPIRED'
                ent.save(using=alias, update_fields=['status', 'updated_at'])

            MembershipStatusHistory.objects.using(alias).create(
                membership=membership,
                from_status=old_st,
                to_status='CANCELLED',
                reason_code='MEMBER_CANCELLATION',
                reason_text=reason or "Membership cancelled via change request",
                changed_by_user=actor_user,
            )

        return req

    @classmethod
    @transaction.atomic
    def unfreeze_membership(
        cls,
        membership: Membership,
        reason_text: Optional[str] = None,
        approved_by_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> Membership:
        """
        Prematurely or normally unfreezes a frozen membership, marking active freeze completed and restoring ACTIVE status.
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        freeze = MembershipFreeze.objects.using(alias).filter(
            membership=membership,
            status='ACTIVE'
        ).first()
        if freeze:
            freeze.status = 'COMPLETED'
            freeze.save(using=alias, update_fields=['status', 'updated_at'])

        old_status = membership.status
        membership.status = 'ACTIVE'
        membership.save(using=alias, update_fields=['status', 'updated_at'])

        MembershipStatusHistory.objects.using(alias).create(
            membership=membership,
            from_status=old_status,
            to_status='ACTIVE',
            reason_code='UNFREEZE_APPLIED',
            reason_text=reason_text or "Membership unfreezed by staff.",
            changed_by_user=approved_by_user,
        )
        return membership

    @classmethod
    def calculate_extension(
        cls,
        membership: Membership,
        days: int,
        db_alias: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Quotes and validates a prospective package validity extension against tenant policy.
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        config = cls.get_policy_config(membership.organization, db_alias=alias)

        if days <= 0:
            return {
                'membership_id': str(membership.id),
                'can_extend': False,
                'error': "Extension days must be greater than zero.",
            }

        if days < config.extension_min_days:
            return {
                'membership_id': str(membership.id),
                'can_extend': False,
                'error': f"Minimum extension duration is {config.extension_min_days} day(s).",
            }

        if days > config.extension_max_days:
            return {
                'membership_id': str(membership.id),
                'can_extend': False,
                'error': f"Maximum extension duration is {config.extension_max_days} day(s).",
            }

        # Check remaining sessions
        has_remaining, remaining_sessions = cls.check_remaining_sessions(membership, db_alias=alias)
        if config.extension_requires_remaining_sessions and not has_remaining:
            return {
                'membership_id': str(membership.id),
                'membership_number': membership.membership_number,
                'status': membership.status,
                'is_expired': (membership.status == 'EXPIRED' or membership.end_date < timezone.now().date()),
                'current_end_date': membership.end_date.isoformat(),
                'new_end_date': None,
                'days': days,
                'fee_per_day': str(config.extension_per_day_fee),
                'activation_fee': str(config.extension_activation_fee),
                'total_fee': '0.00',
                'remaining_sessions': str(remaining_sessions),
                'can_extend': False,
                'error': "Cannot extend package: no remaining sessions left for consumption. Members can only extend if they have unused sessions.",
            }

        today = timezone.now().date()
        is_expired = (membership.status == 'EXPIRED' or membership.end_date < today)
        fee_per_day = config.extension_per_day_fee
        activation_fee = config.extension_activation_fee if is_expired else Decimal('0.00')
        total_fee = activation_fee + (fee_per_day * Decimal(days))

        base_date = max(membership.end_date, today)
        new_end_date = base_date + timedelta(days=days)

        return {
            'membership_id': str(membership.id),
            'membership_number': membership.membership_number,
            'status': membership.status,
            'is_expired': is_expired,
            'current_end_date': membership.end_date.isoformat(),
            'new_end_date': new_end_date.isoformat(),
            'days': days,
            'fee_per_day': str(fee_per_day),
            'activation_fee': str(activation_fee),
            'total_fee': str(total_fee),
            'remaining_sessions': 'Unlimited' if remaining_sessions == Decimal('999999') else str(remaining_sessions),
            'can_extend': True,
            'error': None,
        }

    @classmethod
    @transaction.atomic
    def extend_membership(
        cls,
        membership: Membership,
        days: int,
        reason_text: Optional[str] = None,
        approved_by_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> Tuple[Membership, MembershipExtension]:
        """
        Extends the validity end date of a membership by N days.
        Enforces:
        - Prerequisite: Only works when there are sessions left for consumption.
        - If active: charges per-day fee * days.
        - If already expired: charges activation fee + (per-day fee * days), reactivates membership and entitlements.
        - Records MembershipExtension and full audit history.
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        calc = cls.calculate_extension(membership, days, db_alias=alias)
        if not calc.get('can_extend'):
            raise ValidationError(calc.get('error') or "Cannot extend membership.")

        old_end = membership.end_date
        old_status = membership.status
        was_expired = calc['is_expired']
        new_end = date.fromisoformat(calc['new_end_date'])
        fee_per_day = Decimal(calc['fee_per_day'])
        activation_fee = Decimal(calc['activation_fee'])
        total_fee = Decimal(calc['total_fee'])
        rem_sessions = Decimal(calc['remaining_sessions']) if calc['remaining_sessions'] != 'Unlimited' else Decimal('999999')

        membership.end_date = new_end
        if membership.status == 'EXPIRED' or was_expired:
            membership.status = 'ACTIVE'
        membership.save(using=alias, update_fields=['end_date', 'status', 'updated_at'])

        # Update valid_until on active and expired entitlements
        end_dt = timezone.make_aware(datetime.combine(new_end, datetime.max.time()))
        membership.entitlements.using(alias).filter(status__in=['ACTIVE', 'EXPIRED']).update(
            valid_until=end_dt,
            status='ACTIVE'
        )

        # Record append-only extension audit and fee entry
        extension = MembershipExtension.objects.using(alias).create(
            membership=membership,
            days=days,
            fee_per_day=fee_per_day,
            activation_fee=activation_fee,
            total_fee=total_fee,
            was_expired=was_expired,
            sessions_remaining_at_extension=rem_sessions,
            previous_end_date=old_end,
            new_end_date=new_end,
            reason_text=reason_text or f"Extended by {days} days from {old_end} to {new_end}.",
            created_by_user=approved_by_user,
        )

        # Status History
        MembershipStatusHistory.objects.using(alias).create(
            membership=membership,
            from_status=old_status,
            to_status=membership.status,
            reason_code='EXTENSION_APPLIED',
            reason_text=(
                f"Extended by {days} days from {old_end} to {new_end}. "
                f"Fee: ₹{total_fee} (Activation: ₹{activation_fee}, Per-day: ₹{fee_per_day} x {days}). "
                f"{reason_text or ''}".strip()
            ),
            changed_by_user=approved_by_user,
        )

        # Package History
        MembershipPackageHistory.objects.using(alias).create(
            membership=membership,
            from_package=membership.package,
            from_package_version=membership.package_version,
            to_package=membership.package,
            to_package_version=membership.package_version,
            change_type='EXTENSION',
            changed_by_user=approved_by_user,
            reason=f"Validity extended by {days} days to {new_end}. Total fee: ₹{total_fee}.",
        )

        return membership, extension

    @classmethod
    @transaction.atomic
    def transfer_home_branch(
        cls,
        membership: Membership,
        target_branch: Branch,
        reason_text: Optional[str] = None,
        actor_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> MembershipBranchHistory:
        """
        Transfers the home branch of a membership and updates the member's preferred branch
        with operational availability validation (active branch, program availability, package availability).
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        from .models_catalog import ProgramBranchAvailability, PackageBranchAvailability

        if getattr(target_branch, 'status', 'ACTIVE') != 'ACTIVE':
            raise ValidationError(f"Target branch '{target_branch.name}' is inactive and cannot receive membership transfers.")

        if membership.home_branch_id == target_branch.id:
            raise ValidationError(f"Member is already assigned to '{target_branch.name}' as home branch.")

        # Program branch availability check
        prog = membership.program or (membership.package.program if membership.package else None)
        if prog:
            prog_avail = ProgramBranchAvailability.objects.using(alias).filter(program=prog, branch=target_branch).first()
            if prog_avail and not prog_avail.is_active:
                raise ValidationError(f"Program '{prog.name}' is not operationally active at '{target_branch.name}'.")

        # Package branch availability check
        pkg = membership.package
        if pkg:
            pkg_avail = PackageBranchAvailability.objects.using(alias).filter(package=pkg, branch=target_branch).first()
            if pkg_avail and pkg_avail.status == 'DISABLED':
                raise ValidationError(f"Package '{pkg.name}' is disabled at '{target_branch.name}'.")

        from_branch = membership.home_branch
        membership.home_branch = target_branch
        membership.save(using=alias, update_fields=['home_branch', 'updated_at'])

        profile = membership.user_profile
        if profile:
            profile.preferred_branch = target_branch
            profile.save(using=alias, update_fields=['preferred_branch', 'updated_at'])

        hist = MembershipBranchHistory.objects.using(alias).create(
            membership=membership,
            from_branch=from_branch,
            to_branch=target_branch,
            change_type='TRANSFER',
            reason=reason_text or f"Transferred home branch from {from_branch.name if from_branch else 'None'} to {target_branch.name}",
            changed_by_user=actor_user,
        )
        return hist

    @classmethod
    @transaction.atomic
    def cancel_membership(
        cls,
        membership: Membership,
        reason_text: Optional[str] = None,
        actor_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> Membership:
        """
        Cancels an active membership, expires active entitlements, and records status history.
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        old_st = membership.status
        membership.status = 'CANCELLED'
        membership.cancelled_at = timezone.now()
        membership.save(using=alias, update_fields=['status', 'cancelled_at', 'updated_at'])

        # Expire entitlements and append forfeiture ledger records
        for ent in membership.entitlements.using(alias).filter(status='ACTIVE'):
            rem = ent.remaining_units or Decimal('0.00')
            if rem > Decimal('0.00'):
                MembershipEntitlementLedger.objects.using(alias).create(
                    membership_entitlement=ent,
                    transaction_type='EXPIRY',
                    units=-rem,
                    reason_code='MEMBERSHIP_CANCELLED',
                    reason_text=reason_text or "Forfeited remaining units upon membership cancellation",
                    balance_after=Decimal('0.00'),
                    created_by_user=actor_user,
                )
            ent.status = 'EXPIRED'
            ent.save(using=alias, update_fields=['status', 'updated_at'])

        # Cancel any upcoming bookings
        if membership.user_profile:
            try:
                from .models_bookings import Booking
                from .services_bookings import BookingAttendanceService
                now_dt = timezone.now()
                future_bookings = Booking.objects.using(alias).filter(
                    user_profile=membership.user_profile,
                    occurrence__start_at__gte=now_dt,
                    status__in=['CONFIRMED', 'RESERVED', 'WAITLISTED']
                )
                for bk in future_bookings:
                    BookingAttendanceService.cancel_booking(
                        booking=bk,
                        cancellation_source='ADMIN',
                        reason_text=f"Membership cancelled: {reason_text or 'Staff action'}",
                        actor_user=actor_user,
                        db_alias=alias,
                    )
            except Exception as e:
                logger.warning(f"Error cancelling bookings on membership cancellation: {e}")

        MembershipStatusHistory.objects.using(alias).create(
            membership=membership,
            from_status=old_st,
            to_status='CANCELLED',
            reason_code='MEMBER_CANCELLATION',
            reason_text=reason_text or "Membership cancelled by staff.",
            changed_by_user=actor_user,
        )
        return membership

    @classmethod
    @transaction.atomic
    def adjust_entitlement(
        cls,
        membership: Membership,
        entitlement_type: str,
        units_delta: Decimal,
        reason_code: str = 'ADMIN_ADJUSTMENT',
        reason_text: Optional[str] = None,
        actor_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> MembershipEntitlementLedger:
        """
        Staff controlled adjustment of entitlement units with immutable ledger entry and balance_after.
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        ent = MembershipEntitlement.objects.using(alias).select_for_update().filter(
            membership=membership,
            entitlement_type=entitlement_type,
        ).first()

        if not ent:
            now_dt = timezone.now()
            end_dt = timezone.make_aware(datetime.combine(membership.end_date, datetime.max.time()))
            ent = MembershipEntitlement.objects.using(alias).create(
                membership=membership,
                entitlement_type=entitlement_type,
                allocated_units=Decimal('0.00'),
                consumed_units=Decimal('0.00'),
                is_unlimited=False,
                valid_from=now_dt,
                valid_until=end_dt,
                status='ACTIVE',
            )

        if units_delta >= Decimal('0.00'):
            ent.allocated_units = (ent.allocated_units or Decimal('0.00')) + units_delta
            if ent.status == 'EXHAUSTED':
                ent.status = 'ACTIVE'
        else:
            reduction = abs(units_delta)
            min_allowed = ent.consumed_units or Decimal('0.00')
            if ent.allocated_units is not None and (ent.allocated_units - reduction) < min_allowed:
                raise ValidationError(
                    f"Cannot reduce allocation by {reduction} units. {ent.consumed_units} unit(s) have already been consumed. "
                    f"Maximum possible reduction is {ent.allocated_units - min_allowed} unit(s)."
                )
            ent.allocated_units = (ent.allocated_units or Decimal('0.00')) - reduction
            if ent.allocated_units <= min_allowed:
                ent.status = 'EXHAUSTED'

        ent.save(using=alias, update_fields=['allocated_units', 'consumed_units', 'status', 'updated_at'])
        balance_after = ent.remaining_units

        ledger = MembershipEntitlementLedger.objects.using(alias).create(
            membership_entitlement=ent,
            transaction_type='ADJUSTMENT',
            units=units_delta,
            reason_code=reason_code,
            reason_text=reason_text or f"Manual adjustment of {units_delta} units",
            balance_after=balance_after,
            created_by_user=actor_user,
        )
        return ledger
