"""
apps/tenant_core/services_booking_policy.py — Configurable Booking Policy Service.

Implements:
1. Canonical policy resolution hierarchy:
   Branch-specific override -> Organization-wide default -> Robust system defaults.
2. Separate configurable auto-cancellation policies for unpaid and unconfirmed bookings:
   - Configurable enable/disable switch
   - Time limit from booking creation
   - Explicit admin choice on update: new bookings only vs existing bookings too
3. Configurable booking-type and eligibility rules.
4. Member cancellation and rescheduling cutoffs with late-cancellation penalty enforcement.
5. Safe background auto-cancellation engine:
   - Concurrency protection via select_for_update(skip_locked=True) & distributed locks
   - Retry safety & idempotency
   - Waitlist promotion upon seat vacancy
   - Omnichannel notifications via CommunicationService & InAppNotification
   - Business audit logging & status history
"""

import logging
import uuid
from decimal import Decimal
from datetime import timedelta
from typing import Dict, Any, Optional, Tuple, List
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.db import transaction, models

from apps.tenant_core.context import get_current_tenant_db_alias, tenant_database_context
from apps.tenant_core.locks import redis_distributed_lock
from apps.tenant_core.services_reliability import record_business_audit, enqueue_outbox_event
from apps.tenant_core.audit import emit_audit_event

logger = logging.getLogger(__name__)

DEFAULT_BOOKING_POLICY: Dict[str, Any] = {
    # 1. Auto-cancellation for unpaid bookings
    'auto_cancel_unpaid_enabled': True,
    'auto_cancel_unpaid_minutes': 15,

    # 2. Auto-cancellation for unconfirmed bookings
    'auto_cancel_unconfirmed_enabled': True,
    'auto_cancel_unconfirmed_minutes': 60,

    # 3. Cutoffs
    'cancellation_cutoff_hours': 2,
    'rescheduling_cutoff_hours': 2,
    'max_reschedules_per_booking': 2,

    # 4. Late cancellation policy
    'late_cancellation_allowed': True,
    'late_cancellation_session_action': 'CONSUME',  # CONSUME forfeits credit, RESTORE refunds it
    'late_cancellation_fee': '0.00',
    'late_cancellation_treated_as_no_show': False,

    # 5. Eligibility & Types
    'allowed_booking_types': ['MEMBER', 'TRIAL', 'PAY_PER_USE', 'WALK_IN', 'COMPLIMENTARY'],
    'require_active_membership': True,
    'max_upcoming_bookings': 5,
    'advance_booking_days_max': 14,

    # 6. Turnstile QR Access Configuration
    'qr_activation_minutes_before_class': 20,

    # 7. Scope
    'apply_to_existing_bookings': False,
    'policy_effective_at': None,

    # 7. Notifications
    'notify_unpaid_auto_cancellation': True,
    'notify_unconfirmed_auto_cancellation': True,
    'notify_late_cancellation': True,
    'notification_channels': ['WHATSAPP', 'SMS', 'EMAIL', 'IN_APP'],
}


class BookingPolicyService:
    """
    Authoritative booking policy service for tenant CRM operations.
    """

    @classmethod
    def get_effective_policy(
        cls,
        organization,
        branch=None,
        db_alias: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Resolves effective policy following hierarchy:
        Branch Override -> Organization Default -> Built-in Defaults.
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        policy = dict(DEFAULT_BOOKING_POLICY)

        from .models_bookings import BookingPolicyConfig
        from .models_govern import OrganizationSettings, BranchSettings

        # 1. Organization level default
        try:
            org_cfg = BookingPolicyConfig.objects.using(alias).filter(
                organization=organization,
                branch__isnull=True
            ).first()
            if org_cfg:
                cls._merge_model_into_dict(policy, org_cfg)
            else:
                # Fallback to OrganizationSettings.booking_config if present
                org_settings = OrganizationSettings.objects.using(alias).filter(organization=organization).first()
                if org_settings:
                    if org_settings.booking_cancellation_window_hours:
                        policy['cancellation_cutoff_hours'] = org_settings.booking_cancellation_window_hours
                    if org_settings.late_cancellation_fee:
                        policy['late_cancellation_fee'] = str(org_settings.late_cancellation_fee)
                    if isinstance(org_settings.booking_config, dict):
                        cls._deep_merge(policy, org_settings.booking_config)
        except Exception as e:
            logger.debug("Error loading organization booking policy config: %s", e)

        # 2. Branch level override
        if branch:
            try:
                branch_cfg = BookingPolicyConfig.objects.using(alias).filter(
                    organization=organization,
                    branch=branch
                ).first()
                if branch_cfg:
                    cls._merge_model_into_dict(policy, branch_cfg)
                else:
                    branch_settings = BranchSettings.objects.using(alias).filter(branch=branch).first()
                    if branch_settings:
                        if branch_settings.booking_cancellation_window_hours is not None:
                            policy['cancellation_cutoff_hours'] = branch_settings.booking_cancellation_window_hours
                        if branch_settings.late_cancellation_fee is not None:
                            policy['late_cancellation_fee'] = str(branch_settings.late_cancellation_fee)
                        if isinstance(branch_settings.booking_config, dict):
                            cls._deep_merge(policy, branch_settings.booking_config)
            except Exception as e:
                logger.debug("Error loading branch booking policy config: %s", e)

        return policy

    @classmethod
    def save_policy(
        cls,
        organization,
        policy_data: Dict[str, Any],
        branch=None,
        apply_to_existing: bool = False,
        actor_user=None,
        db_alias: Optional[str] = None,
    ):
        """
        Persists booking policy configuration for an organization or branch override.
        Captures explicit choice: new bookings only vs existing eligible bookings.
        Emits immutable business and platform audit records.
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        from .models_bookings import BookingPolicyConfig

        with transaction.atomic(using=alias):
            cfg, _ = BookingPolicyConfig.objects.using(alias).select_for_update().get_or_create(
                organization=organization,
                branch=branch,
                defaults={'organization': organization, 'branch': branch}
            )

            # Map allowed fields
            field_names = [
                'auto_cancel_unpaid_enabled',
                'auto_cancel_unpaid_minutes',
                'auto_cancel_unconfirmed_enabled',
                'auto_cancel_unconfirmed_minutes',
                'cancellation_cutoff_hours',
                'rescheduling_cutoff_hours',
                'max_reschedules_per_booking',
                'late_cancellation_allowed',
                'late_cancellation_session_action',
                'late_cancellation_fee',
                'late_cancellation_treated_as_no_show',
                'allowed_booking_types',
                'require_active_membership',
                'max_upcoming_bookings',
                'advance_booking_days_max',
                'qr_activation_minutes_before_class',
                'notify_unpaid_auto_cancellation',
                'notify_unconfirmed_auto_cancellation',
                'notify_late_cancellation',
                'notification_channels',
            ]

            before_snapshot = {}
            for field in field_names:
                before_snapshot[field] = getattr(cfg, field, None)
                if field in policy_data:
                    val = policy_data[field]
                    if field == 'late_cancellation_fee' and val is not None:
                        val = Decimal(str(val))
                    setattr(cfg, field, val)

            cfg.apply_to_existing_bookings = bool(apply_to_existing)
            cfg.policy_effective_at = timezone.now()
            cfg.updated_by_user = actor_user
            cfg.save(using=alias)

            # Sync mirrors in OrganizationSettings / BranchSettings for cross-module compatibility
            try:
                if branch:
                    from .models_govern import BranchSettings
                    b_settings, _ = BranchSettings.objects.using(alias).get_or_create(branch=branch)
                    b_settings.booking_cancellation_window_hours = cfg.cancellation_cutoff_hours
                    b_settings.late_cancellation_fee = cfg.late_cancellation_fee
                    b_settings.booking_config = cls._model_to_dict(cfg)
                    b_settings.save(using=alias, update_fields=['booking_cancellation_window_hours', 'late_cancellation_fee', 'booking_config', 'updated_at'])
                else:
                    from .models_govern import OrganizationSettings
                    o_settings, _ = OrganizationSettings.objects.using(alias).get_or_create(organization=organization)
                    o_settings.booking_cancellation_window_hours = cfg.cancellation_cutoff_hours
                    o_settings.late_cancellation_fee = cfg.late_cancellation_fee
                    o_settings.booking_config = cls._model_to_dict(cfg)
                    o_settings.save(using=alias, update_fields=['booking_cancellation_window_hours', 'late_cancellation_fee', 'booking_config', 'updated_at'])
            except Exception as e:
                logger.warning("Failed syncing governance settings mirror: %s", e)

            # Record business audit
            target_desc = f"Branch '{branch.name}'" if branch else f"Tenant '{organization.name}'"
            record_business_audit(
                organization=organization,
                module='BOOKINGS',
                action_code='BOOKING_POLICY_UPDATED',
                entity_type='BookingPolicyConfig',
                entity_id=cfg.id,
                branch=branch,
                actor_user=actor_user,
                metadata={
                    'target': target_desc,
                    'apply_to_existing_bookings': apply_to_existing,
                    'policy_effective_at': cfg.policy_effective_at.isoformat(),
                    'auto_cancel_unpaid_minutes': cfg.auto_cancel_unpaid_minutes,
                    'auto_cancel_unconfirmed_minutes': cfg.auto_cancel_unconfirmed_minutes,
                    'cancellation_cutoff_hours': cfg.cancellation_cutoff_hours,
                    'rescheduling_cutoff_hours': cfg.rescheduling_cutoff_hours,
                },
                db_alias=alias,
            )

            return cfg

    @classmethod
    def is_booking_eligible_for_auto_cancellation(
        cls,
        booking,
        policy: Dict[str, Any],
        now=None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Evaluates whether a Booking or TrialBooking has passed its auto-cancellation deadline.
        Returns: (is_eligible, cancellation_reason_code, human_reason_text)
        """
        current_time = now or timezone.now()
        created_time = getattr(booking, 'booked_at', None) or getattr(booking, 'created_at', None)
        if not created_time:
            return False, None, None

        # Check existing booking policy scope choice:
        # If apply_to_existing_bookings is False, bookings created before policy_effective_at are exempt
        policy_effective_at = policy.get('policy_effective_at')
        if not policy.get('apply_to_existing_bookings', False) and policy_effective_at:
            if isinstance(policy_effective_at, str):
                from django.utils.dateparse import parse_datetime
                policy_effective_at = parse_datetime(policy_effective_at)
            if policy_effective_at and created_time < policy_effective_at:
                return False, None, None

        # 1. Evaluate Unpaid Auto-Cancellation
        if policy.get('auto_cancel_unpaid_enabled', True):
            payment_status = getattr(booking, 'payment_status', 'PAID')
            is_unpaid = payment_status in ['UNPAID', 'PENDING']
            if is_unpaid and booking.status in ['RESERVED', 'BOOKED', 'CONFIRMED']:
                unpaid_deadline = created_time + timedelta(minutes=int(policy.get('auto_cancel_unpaid_minutes', 15)))
                if current_time >= unpaid_deadline:
                    minutes = policy.get('auto_cancel_unpaid_minutes', 15)
                    return True, 'AUTO_CANCELLED_UNPAID', f"Auto-cancelled: Payment not completed within {minutes} minutes."

        # 2. Evaluate Unconfirmed Auto-Cancellation (T-60 mins before session or creation fallback)
        if policy.get('auto_cancel_unconfirmed_enabled', True):
            confirmation_status = getattr(booking, 'confirmation_status', 'CONFIRMED')
            is_unconfirmed = (confirmation_status in ['PENDING', 'RESCHEDULE_REQUESTED']) or (booking.status == 'RESERVED')
            if is_unconfirmed and booking.status in ['RESERVED', 'BOOKED']:
                start_time = getattr(booking, 'scheduled_start', None) or (
                    getattr(booking, 'occurrence', None).start_at if getattr(booking, 'occurrence', None) else None
                )
                minutes = int(policy.get('auto_cancel_unconfirmed_minutes', 60))
                if start_time:
                    unconfirmed_deadline = start_time - timedelta(minutes=minutes)
                else:
                    unconfirmed_deadline = created_time + timedelta(minutes=minutes)

                if current_time >= unconfirmed_deadline:
                    return True, 'AUTO_CANCELLED_UNCONFIRMED', f"Auto-cancelled: Booking not confirmed {minutes} minutes before session."

        return False, None, None

    @classmethod
    def process_auto_cancellations(
        cls,
        tenant_id: Optional[str] = None,
        organization=None,
        branch=None,
        dry_run: bool = False,
        db_alias: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Background task / on-demand processor:
        Safely scans, evaluates, and auto-cancels expired unpaid or unconfirmed bookings.
        Uses SELECT FOR UPDATE SKIP LOCKED to prevent race conditions across parallel workers.
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        from .models_bookings import Booking, BookingCancellation, BookingStatusHistory
        from .models_crm import TrialBooking
        from .models_org import Organization
        from .services_bookings import BookingWaitlistAttendanceService

        if not organization:
            organization = Organization.objects.using(alias).filter(status='ACTIVE').first()
            if not organization:
                return {'status': 'SKIPPED', 'reason': 'No active organization found'}

        now = timezone.now()
        results = {
            'evaluated_count': 0,
            'auto_cancelled_count': 0,
            'cancelled_unpaid_count': 0,
            'cancelled_unconfirmed_count': 0,
            'skipped_count': 0,
            'notifications_dispatched': 0,
            'cancelled_booking_ids': [],
        }

        # Concurrency protection via lock key per organization
        lock_key = f"lock:booking_auto_cancel:{organization.id}:{branch.id if branch else 'all'}"
        with redis_distributed_lock(lock_key, timeout_seconds=180, blocking=False):
            # A. Process Class Bookings
            booking_qs = Booking.objects.using(alias).filter(
                status__in=['RESERVED', 'BOOKED', 'CONFIRMED'],
            )
            if branch:
                booking_qs = booking_qs.filter(branch=branch)
            else:
                booking_qs = booking_qs.filter(occurrence__class_template__category__organization=organization)

            # Filter candidates that could be unpaid or unconfirmed
            candidate_bookings = list(
                booking_qs.filter(
                    models.Q(payment_status__in=['UNPAID', 'PENDING']) |
                    models.Q(confirmation_status='PENDING') |
                    models.Q(status='RESERVED')
                ).select_related('occurrence__class_template__category__organization', 'branch', 'user_profile__user', 'membership', 'entitlement')
            )

            for b in candidate_bookings:
                results['evaluated_count'] += 1
                policy = cls.get_effective_policy(organization, branch=b.branch, db_alias=alias)
                eligible, reason_code, reason_text = cls.is_booking_eligible_for_auto_cancellation(b, policy, now=now)

                if not eligible:
                    results['skipped_count'] += 1
                    continue

                if dry_run:
                    results['cancelled_booking_ids'].append(str(b.id))
                    if reason_code == 'AUTO_CANCELLED_UNPAID':
                        results['cancelled_unpaid_count'] += 1
                    else:
                        results['cancelled_unconfirmed_count'] += 1
                    continue

                # Execute atomic cancellation with lock on booking
                with transaction.atomic(using=alias):
                    locked_b = Booking.objects.using(alias).select_for_update(skip_locked=True).filter(id=b.id).first()
                    if not locked_b or locked_b.status in ['CANCELLED', 'COMPLETED', 'NO_SHOW']:
                        results['skipped_count'] += 1
                        continue

                    # Re-verify eligibility under atomic lock to guard against concurrent payment or confirmation
                    fresh_eligible, f_code, f_text = cls.is_booking_eligible_for_auto_cancellation(locked_b, policy, now=now)
                    if not fresh_eligible:
                        logger.info("Booking %s payment or confirmation resolved concurrently; skipping cancellation.", locked_b.id)
                        results['skipped_count'] += 1
                        continue

                    # Cancel booking via BookingWaitlistAttendanceService (handles credit refund & waitlist promotion)
                    cancellation = BookingWaitlistAttendanceService.cancel_booking(
                        booking=locked_b,
                        cancelled_by_user=None,
                        reason_code=f_code or reason_code,
                        reason_text=f_text or reason_text,
                        db_alias=alias,
                    )
                    if (f_code or reason_code) == 'AUTO_CANCELLED_UNCONFIRMED':
                        locked_b.confirmation_status = 'DECLINED'
                        locked_b.save(using=alias, update_fields=['confirmation_status', 'updated_at'])

                    if reason_code == 'AUTO_CANCELLED_UNPAID':
                        results['cancelled_unpaid_count'] += 1
                    else:
                        results['cancelled_unconfirmed_count'] += 1
                    results['cancelled_booking_ids'].append(str(locked_b.id))

                    # Emit business audit log for auto-cancellation
                    record_business_audit(
                        organization=organization,
                        module='BOOKINGS',
                        action_code='BOOKING_AUTO_CANCELLED',
                        entity_type='Booking',
                        entity_id=locked_b.id,
                        branch=locked_b.branch,
                        actor_user=None,
                        metadata={'reason': f_code or reason_code, 'booking_number': locked_b.booking_number},
                        db_alias=alias,
                    )

                    # Dispatch notification
                    dispatched = cls._dispatch_auto_cancellation_notification(
                        booking=locked_b,
                        policy=policy,
                        reason_code=f_code or reason_code,
                        reason_text=f_text or reason_text,
                        organization=organization,
                        db_alias=alias,
                    )
                    if dispatched:
                        results['notifications_dispatched'] += 1

            # B. Process Trial Bookings
            trial_qs = TrialBooking.objects.using(alias).filter(
                status__in=['BOOKED'],
                confirmation_status__in=['PENDING', 'RESCHEDULE_REQUESTED']
            )
            if branch:
                trial_qs = trial_qs.filter(branch=branch)
            else:
                trial_qs = trial_qs.filter(branch__organization=organization)

            candidate_trials = list(trial_qs.select_related('lead', 'branch'))
            for t in candidate_trials:
                results['evaluated_count'] += 1
                policy = cls.get_effective_policy(organization, branch=t.branch, db_alias=alias)
                eligible, reason_code, reason_text = cls.is_booking_eligible_for_auto_cancellation(t, policy, now=now)

                if not eligible:
                    results['skipped_count'] += 1
                    continue

                if dry_run:
                    results['cancelled_booking_ids'].append(str(t.id))
                    results['cancelled_unconfirmed_count'] += 1
                    continue

                with transaction.atomic(using=alias):
                    locked_t = TrialBooking.objects.using(alias).select_for_update(skip_locked=True).filter(id=t.id).first()
                    if not locked_t or locked_t.status in ['CANCELLED', 'ATTENDED', 'CONVERTED']:
                        results['skipped_count'] += 1
                        continue

                    fresh_eligible, f_code, f_text = cls.is_booking_eligible_for_auto_cancellation(locked_t, policy, now=now)
                    if not fresh_eligible:
                        results['skipped_count'] += 1
                        continue

                    locked_t.status = 'CANCELLED'
                    locked_t.confirmation_status = 'CANCELLED'
                    locked_t.cancellation_reason = f_text or reason_text
                    locked_t.save(using=alias, update_fields=['status', 'confirmation_status', 'cancellation_reason', 'updated_at'])

                    results['cancelled_unconfirmed_count'] += 1
                    results['cancelled_booking_ids'].append(str(locked_t.id))

                    # Emit audit log for trial cancellation
                    record_business_audit(
                        organization=organization,
                        module='CRM',
                        action_code='TRIAL_AUTO_CANCELLED',
                        entity_type='TrialBooking',
                        entity_id=locked_t.id,
                        branch=locked_t.branch,
                        actor_user=None,
                        metadata={'reason_code': f_code or reason_code, 'notes': f_text or reason_text},
                        db_alias=alias,
                    )

                    # Trigger WhatsApp auto-cancel notice with 1-tap rebook link (t3_b_trial_class_auto_cancelled)
                    try:
                        from apps.tenant_core.communication.whatsapp_automation import WhatsAppAutomationService
                        WhatsAppAutomationService.send_trial_auto_cancelled_notice(locked_t, db_alias=alias)
                    except Exception as notify_err:
                        logger.warning("Failed to dispatch WhatsApp auto-cancel notice for trial %s: %s", locked_t.id, notify_err)

        results['auto_cancelled_count'] = results['cancelled_unpaid_count'] + results['cancelled_unconfirmed_count']
        logger.info(
            "Booking auto-cancellation process completed for org %s: evaluated=%d, cancelled_unpaid=%d, cancelled_unconfirmed=%d",
            organization.id, results['evaluated_count'], results['cancelled_unpaid_count'], results['cancelled_unconfirmed_count']
        )
        return results

    @classmethod
    def validate_member_cancellation(
        cls,
        booking,
        cancelled_by_user=None,
        db_alias: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Validates member cancellation against effective policy cutoffs.
        Determines:
        - whether the cancellation is within the free cancellation window, or late.
        - whether late cancellation is allowed at all.
        - what session action applies (RESTORE vs CONSUME).
        - what penalty fee applies.
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        occ = booking.occurrence
        org = occ.class_template.category.organization
        policy = cls.get_effective_policy(org, branch=booking.branch, db_alias=alias)

        now = timezone.now()
        diff_minutes = int((occ.start_at - now).total_seconds() / 60)
        cutoff_hours = policy.get('cancellation_cutoff_hours', 2)
        cutoff_minutes = cutoff_hours * 60

        is_late = diff_minutes < cutoff_minutes

        if is_late:
            if not policy.get('late_cancellation_allowed', True):
                raise ValidationError(
                    f"Cancellations are locked within {cutoff_hours} hours of class start time."
                )

            session_action = policy.get('late_cancellation_session_action', 'CONSUME')
            fee = Decimal(str(policy.get('late_cancellation_fee', '0.00')))
            treated_as_no_show = policy.get('late_cancellation_treated_as_no_show', False)
        else:
            session_action = 'RESTORE'
            fee = Decimal('0.00')
            treated_as_no_show = False

        return {
            'is_late': is_late,
            'diff_minutes': diff_minutes,
            'cutoff_hours': cutoff_hours,
            'session_action': session_action,
            'fee': fee,
            'treated_as_no_show': treated_as_no_show,
            'policy': policy,
        }

    @classmethod
    def validate_member_reschedule(
        cls,
        booking,
        to_occurrence,
        rescheduled_by_user=None,
        db_alias: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Validates member rescheduling request against policy cutoffs and limits.
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        from .models_bookings import BookingReschedule

        from_occ = booking.occurrence
        org = from_occ.class_template.category.organization
        policy = cls.get_effective_policy(org, branch=booking.branch, db_alias=alias)

        now = timezone.now()
        diff_minutes = int((from_occ.start_at - now).total_seconds() / 60)
        cutoff_hours = policy.get('rescheduling_cutoff_hours', 2)
        cutoff_minutes = cutoff_hours * 60

        if diff_minutes < cutoff_minutes:
            raise ValidationError(
                f"Rescheduling is not permitted within {cutoff_hours} hours of class start time."
            )

        max_reschedules = policy.get('max_reschedules_per_booking', 2)
        prior_count = max(getattr(booking, 'reschedule_count', 0), BookingReschedule.objects.using(alias).filter(booking=booking).count())
        if prior_count >= max_reschedules:
            raise ValidationError(
                f"Maximum allowed reschedules ({max_reschedules}) reached for this booking."
            )

        return {
            'diff_minutes': diff_minutes,
            'cutoff_hours': cutoff_hours,
            'prior_reschedules': prior_count,
            'max_reschedules': max_reschedules,
            'policy': policy,
        }

    @classmethod
    def _dispatch_auto_cancellation_notification(
        cls,
        booking,
        policy: Dict[str, Any],
        reason_code: str,
        reason_text: str,
        organization,
        db_alias: str,
    ) -> bool:
        """
        Dispatches omnichannel notifications to member using existing CommunicationService & InAppNotification.
        """
        try:
            channels = policy.get('notification_channels') or ['WHATSAPP', 'SMS', 'EMAIL', 'IN_APP']
            user = booking.user_profile.user if (booking.user_profile and booking.user_profile.user) else None
            recipient_email = user.email if user else None
            recipient_phone = user.phone if user else None

            # 1. In-App Notification (always instant and persistent)
            if 'IN_APP' in channels and user:
                from .models_govern import InAppNotification
                title = "Booking Auto-Cancelled" if reason_code == 'AUTO_CANCELLED_UNPAID' else "Reservation Expired"
                InAppNotification.objects.using(db_alias).create(
                    organization=organization,
                    user=user,
                    title=title,
                    message=f"Your booking for {booking.occurrence.class_template.name} ({booking.booking_number}) was cancelled: {reason_text}",
                    notification_type='BOOKING_CANCELLED',
                    deep_link=f"/portal?booking={booking.id}",
                )

            # 2. Outbound communications via CommunicationService
            from apps.tenant_core.communication.service import CommunicationService
            class_name = booking.occurrence.class_template.name if booking.occurrence and booking.occurrence.class_template else 'Class'
            context_data = {
                'member_name': booking.user_profile.first_name_snapshot or (user.first_name if user else 'Member'),
                'class_name': class_name,
                'booking_number': booking.booking_number,
                'reason': reason_text,
            }

            for channel in channels:
                if channel in ['WHATSAPP', 'SMS'] and recipient_phone:
                    idempotency_key = f"notify_autocancel_{booking.id}_{channel}_{reason_code}"
                    try:
                        CommunicationService.send_communication(
                            organization=organization,
                            channel=channel,
                            recipient=recipient_phone,
                            purpose='TRANSACTIONAL',
                            idempotency_key=idempotency_key,
                            trigger_type='SYSTEM',
                            subject="Booking Cancellation Notice",
                            body=f"Hi {context_data['member_name']}, your booking {booking.booking_number} for {class_name} was auto-cancelled: {reason_text}",
                            context_data=context_data,
                        )
                    except Exception as comm_err:
                        logger.debug("Failed sending %s auto-cancellation notification: %s", channel, comm_err)

                elif channel == 'EMAIL' and recipient_email:
                    idempotency_key = f"notify_autocancel_{booking.id}_email_{reason_code}"
                    try:
                        CommunicationService.send_communication(
                            organization=organization,
                            channel='EMAIL',
                            recipient=recipient_email,
                            purpose='TRANSACTIONAL',
                            idempotency_key=idempotency_key,
                            trigger_type='SYSTEM',
                            subject=f"SWEAT · Booking {booking.booking_number} Cancelled",
                            body=f"Hello {context_data['member_name']},\n\nYour reservation {booking.booking_number} for {class_name} has been cancelled.\nReason: {reason_text}\n\nSWEAT Team",
                            context_data=context_data,
                        )
                    except Exception as comm_err:
                        logger.debug("Failed sending EMAIL auto-cancellation notification: %s", comm_err)

            return True
        except Exception as e:
            logger.error("Failed dispatching auto-cancellation notifications for booking %s: %s", booking.id, e)
            return False

    @staticmethod
    def _merge_model_into_dict(target: dict, model_instance):
        for field in model_instance._meta.fields:
            if field.name in ['id', 'organization', 'branch', 'created_at', 'updated_at', 'updated_by_user']:
                continue
            val = getattr(model_instance, field.name, None)
            if val is not None:
                if isinstance(val, Decimal):
                    target[field.name] = str(val)
                elif hasattr(val, 'isoformat'):
                    target[field.name] = val.isoformat()
                else:
                    target[field.name] = val

    @staticmethod
    def _model_to_dict(model_instance) -> dict:
        data = {}
        for field in model_instance._meta.fields:
            if field.name in ['id', 'organization', 'branch', 'created_at', 'updated_at', 'updated_by_user']:
                continue
            val = getattr(model_instance, field.name, None)
            if isinstance(val, Decimal):
                data[field.name] = str(val)
            elif hasattr(val, 'isoformat'):
                data[field.name] = val.isoformat()
            else:
                data[field.name] = val
        return data

    @staticmethod
    def _deep_merge(target: dict, source: dict):
        for k, v in source.items():
            if k in target and isinstance(target[k], dict) and isinstance(v, dict):
                BookingPolicyService._deep_merge(target[k], v)
            elif v is not None:
                target[k] = v
