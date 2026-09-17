import uuid
from decimal import Decimal
from django.db import models, transaction
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.db.models import F, Max

from .models_bookings import (
    BookingPolicySet,
    BookingCancellationRule,
    AttendancePolicySet,
    AttendancePenaltyRule,
    MemberAttendanceState,
    AttendancePolicyEvent,
    Booking,
    BookingStatusHistory,
    BookingReschedule,
    BookingCancellation,
    BookingWaitlistEvent,
)
from .models_attendance import AttendanceRecord, AccessEvent
from .models_classes import ClassOccurrence
from .models_crm import UserProfile
from .models_memberships import Membership, MembershipEntitlement
from .services_reliability import record_business_audit, enqueue_outbox_event
from .services_memberships import MembershipLifecycleService
from .context import get_current_tenant_db_alias


class BookingWaitlistAttendanceService:

    @classmethod
    def create_booking(
        cls,
        user_profile: UserProfile,
        occurrence: ClassOccurrence,
        booking_type: str = 'MEMBER',
        booking_source: str = 'WEB',
        membership: Membership = None,
        created_by_user=None,
        db_alias: str = None,
    ) -> Booking:
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        with transaction.atomic(using=alias):
            organization = occurrence.class_template.category.organization
            branch = occurrence.branch

            # Lock occurrence to serialize concurrent booking and capacity allocation
            occurrence = ClassOccurrence.objects.using(alias).select_for_update().get(id=occurrence.id)

            # 1. Prevent duplicate active booking
            existing = Booking.objects.using(alias).filter(
                user_profile=user_profile,
                occurrence=occurrence,
                status__in=['RESERVED', 'WAITLISTED', 'CONFIRMED']
            ).first()
            if existing:
                raise ValidationError(f"User already has an active booking ({existing.booking_number}) for this class occurrence.")

            # 2. Check Attendance Policy & Member State
            attendance_policy = AttendancePolicySet.objects.using(alias).filter(
                organization=organization,
                status='ACTIVE'
            ).first()

            member_state = None
            if attendance_policy:
                member_state = MemberAttendanceState.objects.using(alias).filter(
                    user_profile=user_profile,
                    attendance_policy_set=attendance_policy,
                ).first()
                if not member_state:
                    member_state = MemberAttendanceState(
                        user_profile=user_profile,
                        attendance_policy_set=attendance_policy,
                        current_max_advance_bookings=attendance_policy.normal_max_advance_bookings,
                        booking_mode='NORMAL'
                    )
                    member_state.save(using=alias)
                # Check restriction
                if member_state.booking_mode == 'SINGLE_BOOKING':
                    active_advance = Booking.objects.using(alias).filter(
                        user_profile=user_profile,
                        status__in=['CONFIRMED', 'RESERVED'],
                        occurrence__start_at__gt=timezone.now()
                    ).count()
                    if active_advance >= member_state.current_max_advance_bookings:
                        raise ValidationError(
                            f"Booking restricted due to previous no-shows. Maximum active advance bookings allowed: {member_state.current_max_advance_bookings}."
                        )

            # 3. Check Booking Policy
            booking_policy = BookingPolicySet.objects.using(alias).filter(
                organization=organization,
                status='ACTIVE'
            ).first()

            # Generate unique booking number
            booking_number = f"BK-{timezone.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"

            # 4. Check Occurrence Capacity dynamically
            enrolled_count = Booking.objects.using(alias).filter(
                occurrence=occurrence,
                status__in=['CONFIRMED', 'RESERVED', 'COMPLETED']
            ).count()
            is_capacity_available = enrolled_count < occurrence.capacity

            if is_capacity_available:
                # Consume entitlement if member booking
                entitlement = None
                if booking_type == 'MEMBER':
                    if not membership:
                        # Find active membership with available entitlement
                        membership = Membership.objects.using(alias).filter(
                            user_profile=user_profile,
                            status='ACTIVE',
                            end_date__gte=timezone.now().date()
                        ).first()
                    if membership:
                        ent = membership.entitlements.using(alias).filter(status='ACTIVE').filter(
                            models.Q(is_unlimited=True) | models.Q(allocated_units__gt=models.F('consumed_units'))
                        ).first()
                        if not ent:
                            raise ValidationError("No membership entitlement sessions available.")
                        MembershipLifecycleService.consume_entitlement(
                            membership=membership,
                            entitlement_type=ent.entitlement_type,
                            units=Decimal('1.00'),
                            reason_text="Class booking reservation",
                            created_by_user=created_by_user,
                            db_alias=alias,
                        )
                        entitlement = ent

                booking = Booking.objects.using(alias).create(
                    booking_number=booking_number,
                    user_profile=user_profile,
                    membership=membership,
                    entitlement=entitlement,
                    occurrence=occurrence,
                    branch=branch,
                    booking_type=booking_type,
                    booking_source=booking_source,
                    status='CONFIRMED',
                    booked_at=timezone.now(),
                    created_by_user=created_by_user,
                )

                BookingStatusHistory.objects.using(alias).create(
                    booking=booking,
                    from_status=None,
                    to_status='CONFIRMED',
                    reason_code='INITIAL_BOOKING',
                    reason_text='Confirmed booking created with session entitlement allocation',
                    changed_by_user=created_by_user,
                    changed_at=timezone.now()
                )

                record_business_audit(
                    organization=organization,
                    module='BOOKINGS',
                    action_code='BOOKING_CONFIRMED',
                    entity_type='Booking',
                    entity_id=booking.id,
                    branch=branch,
                    actor_user=created_by_user,
                    metadata={'booking_number': booking_number, 'occurrence_id': str(occurrence.id)},
                    db_alias=alias,
                )

                enqueue_outbox_event(
                    organization=organization,
                    event_type='BOOKING_CONFIRMED',
                    aggregate_type='Booking',
                    aggregate_id=booking.id,
                    payload={'booking_number': booking_number, 'user_id': str(user_profile.id), 'occurrence_id': str(occurrence.id)},
                    db_alias=alias,
                )

                return booking

            else:
                # Capacity is full: evaluate waitlist
                allow_waitlist = booking_policy.allow_waitlist if booking_policy else True
                waitlist_cap = booking_policy.waitlist_capacity if (booking_policy and booking_policy.waitlist_capacity) else 20

                current_waitlist_count = Booking.objects.using(alias).filter(occurrence=occurrence, status='WAITLISTED').count()
                if not allow_waitlist or current_waitlist_count >= waitlist_cap:
                    raise ValidationError("Class occurrence capacity and waitlist are completely full.")

                next_position = current_waitlist_count + 1

                booking = Booking.objects.using(alias).create(
                    booking_number=booking_number,
                    user_profile=user_profile,
                    membership=membership,
                    entitlement=None,
                    occurrence=occurrence,
                    branch=branch,
                    booking_type=booking_type,
                    booking_source=booking_source,
                    status='WAITLISTED',
                    waitlist_position=next_position,
                    booked_at=timezone.now(),
                    created_by_user=created_by_user,
                )

                BookingStatusHistory.objects.using(alias).create(
                    booking=booking,
                    from_status=None,
                    to_status='WAITLISTED',
                    reason_code='WAITLIST_JOINED',
                    reason_text=f'Joined waitlist at position {next_position}',
                    changed_by_user=created_by_user,
                    changed_at=timezone.now()
                )

                BookingWaitlistEvent.objects.using(alias).create(
                    booking=booking,
                    occurrence=occurrence,
                    event_type='JOINED',
                    new_position=next_position,
                    reason='Class capacity full at booking time',
                    triggered_by_type='USER' if created_by_user else 'SYSTEM',
                    triggered_by_user=created_by_user,
                    created_at=timezone.now()
                )

                return booking

    @classmethod
    @transaction.atomic
    def cancel_booking(
        cls,
        booking: Booking,
        cancelled_by_user=None,
        reason_code: str = 'MEMBER_REQUEST',
        reason_text: str = ''
    ) -> BookingCancellation:
        if booking.status not in ['CONFIRMED', 'WAITLISTED', 'RESERVED']:
            raise ValidationError(f"Cannot cancel booking in status '{booking.status}'.")

        occurrence = booking.occurrence
        organization = occurrence.class_template.category.organization
        branch = booking.branch
        now = timezone.now()

        diff_minutes = int((occurrence.start_at - now).total_seconds() / 60)
        old_status = booking.status

        # Find applied cancellation rule
        applied_rule = BookingCancellationRule.objects.filter(
            organization=organization,
            status='ACTIVE'
        ).order_by('priority').first()

        session_action = 'RESTORE'
        session_units = Decimal('1.00')
        treated_as_no_show = False
        within_cutoff = diff_minutes > 60

        if applied_rule:
            session_action = applied_rule.session_action
            if applied_rule.cancellation_type == 'TREATED_AS_NO_SHOW':
                treated_as_no_show = True

        if old_status == 'CONFIRMED':
            # Restore entitlement if configured and was consumed
            if session_action == 'RESTORE' and booking.membership and booking.entitlement:
                MembershipLifecycleService.restore_entitlement(
                    membership=booking.membership,
                    entitlement_type=booking.entitlement.entitlement_type,
                    units=session_units,
                    booking_id=booking.id,
                    reason_text=f"Cancellation refund ({reason_code})",
                    created_by_user=cancelled_by_user
                )

        elif old_status == 'WAITLISTED':
            session_action = 'NO_ACTION'
            session_units = Decimal('0.00')

            BookingWaitlistEvent.objects.create(
                booking=booking,
                occurrence=occurrence,
                event_type='MANUALLY_CANCELLED',
                old_position=booking.waitlist_position,
                reason=reason_text or reason_code,
                triggered_by_type='USER' if cancelled_by_user else 'SYSTEM',
                triggered_by_user=cancelled_by_user,
                created_at=now
            )

            # Re-index subsequent waitlist positions
            subsequent = Booking.objects.filter(
                occurrence=occurrence,
                status='WAITLISTED',
                waitlist_position__gt=booking.waitlist_position
            ).order_by('waitlist_position')
            for sub in subsequent:
                old_p = sub.waitlist_position
                new_p = old_p - 1
                sub.waitlist_position = new_p
                sub.save(update_fields=['waitlist_position'])
                BookingWaitlistEvent.objects.create(
                    booking=sub,
                    occurrence=occurrence,
                    event_type='POSITION_CHANGED',
                    old_position=old_p,
                    new_position=new_p,
                    reason='Preceding waitlist member cancelled',
                    triggered_by_type='SYSTEM',
                    created_at=now
                )

        booking.status = 'CANCELLED'
        booking.cancelled_at = now
        booking.save(update_fields=['status', 'cancelled_at', 'updated_at'])

        cancellation_record = BookingCancellation.objects.create(
            booking=booking,
            cancelled_by_user=cancelled_by_user,
            reason_code=reason_code,
            reason_text=reason_text,
            minutes_before_class=diff_minutes,
            booking_cancellation_rule=applied_rule,
            treated_as_no_show=treated_as_no_show,
            session_action_applied=session_action,
            session_units_applied=session_units,
            within_cutoff=within_cutoff,
            cancelled_at=now,
            created_at=now
        )

        BookingStatusHistory.objects.create(
            booking=booking,
            from_status=old_status,
            to_status='CANCELLED',
            reason_code=reason_code,
            reason_text=reason_text,
            changed_by_user=cancelled_by_user,
            changed_at=now
        )

        record_business_audit(
            organization=organization,
            module='BOOKINGS',
            action_code='BOOKING_CANCELLED',
            entity_type='Booking',
            entity_id=booking.id,
            branch=branch,
            actor_user=cancelled_by_user,
            metadata={'reason_code': reason_code, 'minutes_before_class': diff_minutes}
        )

        enqueue_outbox_event(
            organization=organization,
            event_type='BOOKING_CANCELLED',
            aggregate_type='Booking',
            aggregate_id=booking.id,
            payload={'booking_id': str(booking.id), 'reason_code': reason_code}
        )

        # If a confirmed booking cancelled, auto-promote top of waitlist!
        if old_status == 'CONFIRMED':
            cls.auto_promote_from_waitlist(occurrence)

        return cancellation_record

    @classmethod
    def auto_promote_from_waitlist(cls, occurrence: ClassOccurrence, db_alias: str = None) -> Booking:
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        with transaction.atomic(using=alias):
            occurrence = ClassOccurrence.objects.using(alias).select_for_update().get(id=occurrence.id)
            enrolled_count = Booking.objects.using(alias).filter(
                occurrence=occurrence,
                status__in=['CONFIRMED', 'RESERVED', 'COMPLETED']
            ).count()
            if enrolled_count >= occurrence.capacity:
                return None

            top_waitlisted = Booking.objects.using(alias).select_for_update().filter(
                occurrence=occurrence,
                status='WAITLISTED'
            ).order_by('waitlist_position', 'booked_at').first()

            if not top_waitlisted:
                return None

            # Check & consume entitlement
            membership = top_waitlisted.membership
            entitlement = None
            if top_waitlisted.booking_type == 'MEMBER':
                if not membership:
                    membership = Membership.objects.using(alias).filter(
                        user_profile=top_waitlisted.user_profile,
                        status='ACTIVE',
                        end_date__gte=timezone.now().date()
                    ).first()
                if membership:
                    ent = membership.entitlements.filter(status='ACTIVE').filter(
                        models.Q(is_unlimited=True) | models.Q(allocated_units__gt=models.F('consumed_units'))
                    ).first()
                    if ent:
                        MembershipLifecycleService.consume_entitlement(
                            membership=membership,
                            entitlement_type=ent.entitlement_type,
                            units=Decimal('1.00'),
                            booking_id=top_waitlisted.id,
                            reason_text="Promoted from waitlist to confirmed",
                            created_by_user=None,
                            db_alias=alias,
                        )
                        entitlement = ent

            old_position = top_waitlisted.waitlist_position
            top_waitlisted.status = 'CONFIRMED'
            top_waitlisted.waitlist_position = None
            top_waitlisted.membership = membership
            top_waitlisted.entitlement = entitlement
            top_waitlisted.save(using=alias)

            BookingWaitlistEvent.objects.using(alias).create(
                booking=top_waitlisted,
                occurrence=occurrence,
                event_type='PROMOTED',
                old_position=old_position,
                new_position=None,
                reason='Auto-promoted from waitlist into open class spot',
                triggered_by_type='SYSTEM',
                created_at=timezone.now()
            )

            BookingStatusHistory.objects.using(alias).create(
                booking=top_waitlisted,
                from_status='WAITLISTED',
                to_status='CONFIRMED',
                reason_code='AUTO_PROMOTION',
                reason_text='Promoted into open slot after cancellation',
                changed_by_user=None,
                changed_at=timezone.now()
            )

            # Shift remaining waitlisted bookings down
            remaining = Booking.objects.using(alias).filter(
                occurrence=occurrence,
                status='WAITLISTED'
            ).order_by('waitlist_position')
            new_pos = 1
            for b in remaining:
                if b.waitlist_position != new_pos:
                    b.waitlist_position = new_pos
                    b.save(using=alias, update_fields=['waitlist_position'])
                new_pos += 1

            return top_waitlisted

    @classmethod
    @transaction.atomic
    def record_attendance(
        cls,
        booking: Booking,
        status: str = 'PRESENT',
        check_in_method: str = 'QR',
        marked_by_user=None,
    ) -> AttendanceRecord:
        user_profile = booking.user_profile
        branch = booking.branch
        occurrence = booking.occurrence
        now = timezone.now()
        organization = occurrence.class_template.category.organization

        record, created = AttendanceRecord.objects.get_or_create(
            booking=booking,
            defaults={
                'user_profile': user_profile,
                'branch': branch,
                'occurrence': occurrence,
                'status': status,
                'check_in_status': 'SUCCESSFUL' if status in ['PRESENT', 'LATE'] else 'FAILED',
                'check_in_method': check_in_method,
                'check_in_at': now if status in ['PRESENT', 'LATE'] else None,
                'marked_by_user': marked_by_user,
            }
        )
        if not created:
            record.status = status
            record.check_in_status = 'SUCCESSFUL' if status in ['PRESENT', 'LATE'] else 'FAILED'
            record.check_in_method = check_in_method
            if status in ['PRESENT', 'LATE']:
                record.check_in_at = now
            record.marked_by_user = marked_by_user
            record.save()

        # Update booking lifecycle
        old_booking_status = booking.status
        if status in ['PRESENT', 'LATE']:
            booking.status = 'COMPLETED'
            booking.completed_at = now
            booking.save(update_fields=['status', 'completed_at', 'updated_at'])

            # Handle attendance policy & state reset
            policy = AttendancePolicySet.objects.filter(organization=organization, status='ACTIVE').first()
            if policy:
                member_state = MemberAttendanceState.objects.filter(
                    user_profile=user_profile,
                    attendance_policy_set=policy
                ).first()
                if member_state:
                    if policy.reset_on_successful_attendance:
                        member_state.consecutive_no_show_count = 0
                        member_state.booking_mode = 'NORMAL'
                        member_state.current_max_advance_bookings = policy.normal_max_advance_bookings
                        member_state.last_successful_attendance_at = now
                        member_state.save()

                        AttendancePolicyEvent.objects.create(
                            user_profile=user_profile,
                            booking=booking,
                            attendance_record=record,
                            attendance_policy_set=policy,
                            event_type='SUCCESSFUL_ATTENDANCE',
                            new_booking_mode='NORMAL',
                            reason='Successful attendance completed; restrictions reset',
                            triggered_by_type='SYSTEM',
                            created_at=now
                        )

        elif status == 'NO_SHOW':
            booking.status = 'NO_SHOW'
            booking.save(update_fields=['status', 'updated_at'])

            policy = AttendancePolicySet.objects.filter(organization=organization, status='ACTIVE').first()
            if policy:
                member_state, _ = MemberAttendanceState.objects.get_or_create(
                    user_profile=user_profile,
                    attendance_policy_set=policy,
                    defaults={
                        'current_max_advance_bookings': policy.normal_max_advance_bookings,
                        'booking_mode': 'NORMAL'
                    }
                )
                member_state.consecutive_no_show_count += 1
                member_state.active_no_show_count += 1
                member_state.last_no_show_at = now

                # Evaluate restriction threshold
                if member_state.consecutive_no_show_count >= policy.restriction_threshold:
                    old_mode = member_state.booking_mode
                    member_state.booking_mode = 'SINGLE_BOOKING'
                    member_state.current_max_advance_bookings = policy.restricted_max_advance_bookings
                    member_state.restriction_started_at = now
                    member_state.restriction_reason = f"Exceeded {policy.restriction_threshold} consecutive no-shows"

                    AttendancePolicyEvent.objects.create(
                        user_profile=user_profile,
                        booking=booking,
                        attendance_record=record,
                        attendance_policy_set=policy,
                        event_type='RESTRICTION_ACTIVATED',
                        no_show_sequence=member_state.consecutive_no_show_count,
                        old_booking_mode=old_mode,
                        new_booking_mode='SINGLE_BOOKING',
                        reason=member_state.restriction_reason,
                        triggered_by_type='SYSTEM',
                        created_at=now
                    )

                member_state.save()

        BookingStatusHistory.objects.create(
            booking=booking,
            from_status=old_booking_status,
            to_status=booking.status,
            reason_code=f'ATTENDANCE_{status}',
            reason_text=f'Attendance recorded as {status} via {check_in_method}',
            changed_by_user=marked_by_user,
            changed_at=now
        )

        return record

    @classmethod
    @transaction.atomic
    def log_access_event(
        cls,
        user_profile: UserProfile,
        branch,
        event_type: str = 'ENTRY',
        device_reference: str = None,
        booking: Booking = None,
        provider_reference: str = None,
    ) -> AccessEvent:
        now = timezone.now()
        event = AccessEvent.objects.create(
            user_profile=user_profile,
            booking=booking,
            branch=branch,
            event_type=event_type,
            device_reference=device_reference,
            provider_reference=provider_reference,
            event_at=now,
            created_at=now
        )

        # If ENTRY and valid booking exists today for this member, auto-check in
        if event_type == 'ENTRY':
            if not booking:
                # Find today's booking for user in this branch
                today = now.date()
                booking = Booking.objects.filter(
                    user_profile=user_profile,
                    branch=branch,
                    status='CONFIRMED',
                    occurrence__start_at__date=today
                ).first()
            if booking:
                cls.record_attendance(
                    booking=booking,
                    status='PRESENT',
                    check_in_method='ACCESS_DEVICE'
                )

        return event
