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
    def can_member_book(cls, user_profile: UserProfile, organization, db_alias: str = None) -> tuple:
        """
        Asks the attendance / no-show restriction service if the member is permitted to book.
        Returns (is_allowed: bool, reason: str).
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        attendance_policy = AttendancePolicySet.objects.using(alias).filter(
            organization=organization,
            status='ACTIVE'
        ).first()

        if not attendance_policy:
            return True, ""

        member_state = MemberAttendanceState.objects.using(alias).filter(
            user_profile=user_profile,
            attendance_policy_set=attendance_policy,
        ).first()

        if member_state and member_state.booking_mode == 'SINGLE_BOOKING':
            active_advance = Booking.objects.using(alias).filter(
                user_profile=user_profile,
                status__in=['CONFIRMED', 'RESERVED'],
                occurrence__start_at__gt=timezone.now()
            ).count()
            if active_advance >= member_state.current_max_advance_bookings:
                return (
                    False,
                    f"Booking restricted due to previous no-shows. Maximum active advance bookings allowed: {member_state.current_max_advance_bookings}."
                )

        return True, ""

    @classmethod
    def resolve_package_class_access(cls, package_version, occurrence: ClassOccurrence, db_alias: str = None) -> dict:
        """
        Validates class access against the member's exact package_version_id and
        the current PackageClassAccessRule implementation.
        Priority hierarchy:
        1. ClassTemplate + Branch
        2. ClassTemplate + Global (branch is null)
        3. ClassCategory + Branch
        4. ClassCategory + Global (branch is null)
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        from .models_classes import PackageClassAccessRule

        if not package_version:
            return {
                'allowed': True,
                'access_type': 'INCLUDED',
                'rule': None,
                'units': Decimal('1.00'),
                'entitlement_type': None
            }

        rules = PackageClassAccessRule.objects.using(alias).filter(
            package_version=package_version,
            status='ACTIVE'
        )

        if not rules.exists():
            # No specific restriction rules defined for this package version
            return {
                'allowed': True,
                'access_type': 'INCLUDED',
                'rule': None,
                'units': Decimal('1.00'),
                'entitlement_type': None
            }

        template = occurrence.class_template
        category = template.category if template else None
        branch = occurrence.branch

        # 1. Template + Branch
        rule = rules.filter(class_template=template, branch=branch).first()
        # 2. Template (all branches)
        if not rule:
            rule = rules.filter(class_template=template, branch__isnull=True).first()
        # 3. Category + Branch
        if not rule and category:
            rule = rules.filter(class_category=category, branch=branch).first()
        # 4. Category (all branches)
        if not rule and category:
            rule = rules.filter(class_category=category, branch__isnull=True).first()

        if not rule:
            tpl_name = template.name if template else 'Unknown'
            return {
                'allowed': False,
                'access_type': 'EXCLUDED',
                'rule': None,
                'reason': f"Class '{tpl_name}' is not included in package version {package_version.version_number}."
            }

        if rule.access_type == 'EXCLUDED':
            tpl_name = template.name if template else 'Unknown'
            return {
                'allowed': False,
                'access_type': 'EXCLUDED',
                'rule': rule,
                'reason': f"Class '{tpl_name}' is explicitly excluded for package version {package_version.version_number}."
            }
        elif rule.access_type in ('ADD_ON', 'PAY_PER_USE'):
            tpl_name = template.name if template else 'Unknown'
            return {
                'allowed': False,
                'access_type': rule.access_type,
                'rule': rule,
                'reason': f"Class '{tpl_name}' requires add-on or pay-per-use payment for package version {package_version.version_number}."
            }

        return {
            'allowed': True,
            'access_type': rule.access_type,
            'rule': rule,
            'units': rule.units_per_booking,
            'entitlement_type': rule.entitlement_type
        }

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
            # 1. Validate Member profile and account active
            if user_profile.member_status != 'ACTIVE':
                raise ValidationError("Member profile is not active.")
            if user_profile.user and user_profile.user.status != 'ACTIVE':
                raise ValidationError("User account is not active.")

            organization = occurrence.class_template.category.organization
            branch = occurrence.branch

            # Lock occurrence to serialize concurrent booking and capacity allocation
            occurrence = ClassOccurrence.objects.using(alias).select_for_update().get(id=occurrence.id)

            # 2. Prevent duplicate active booking
            existing = Booking.objects.using(alias).filter(
                user_profile=user_profile,
                occurrence=occurrence,
                status__in=['RESERVED', 'WAITLISTED', 'CONFIRMED']
            ).first()
            if existing:
                raise ValidationError(f"User already has an active booking ({existing.booking_number}) for this class occurrence.")

            # 3. Check Attendance / No-Show restrictions via restriction service
            can_book, restriction_reason = cls.can_member_book(user_profile, organization, db_alias=alias)
            if not can_book:
                raise ValidationError(restriction_reason)

            # 4. Check Booking Policy
            booking_policy = BookingPolicySet.objects.using(alias).filter(
                organization=organization,
                status='ACTIVE'
            ).first()

            if booking_policy:
                now = timezone.now()
                # Check max upcoming bookings
                upcoming_count = Booking.objects.using(alias).filter(
                    user_profile=user_profile,
                    status__in=['CONFIRMED', 'RESERVED'],
                    occurrence__start_at__gt=now
                ).count()
                if upcoming_count >= booking_policy.max_upcoming_bookings:
                    raise ValidationError(f"Maximum upcoming bookings ({booking_policy.max_upcoming_bookings}) exceeded.")

                # Check booking window
                diff_minutes = int((occurrence.start_at - now).total_seconds() / 60)
                if booking_policy.booking_open_minutes_before and diff_minutes > booking_policy.booking_open_minutes_before:
                    raise ValidationError("Booking window has not opened yet for this class occurrence.")
                if booking_policy.booking_close_minutes_before and diff_minutes < booking_policy.booking_close_minutes_before:
                    raise ValidationError("Booking window has closed for this class occurrence.")

                # Check trial allowed
                if booking_type == 'TRIAL':
                    if not booking_policy.allow_trial:
                        raise ValidationError("Trial bookings are not permitted by organization policy.")
                    if booking_policy.max_trial_bookings:
                        trials_used = Booking.objects.using(alias).filter(
                            user_profile=user_profile,
                            booking_type='TRIAL'
                        ).count()
                        if trials_used >= booking_policy.max_trial_bookings:
                            raise ValidationError(f"Maximum trial bookings ({booking_policy.max_trial_bookings}) exceeded.")

            # 5. Member validation & Package Class Access
            units_to_consume = Decimal('1.00')
            ent_type_filter = None
            if booking_type == 'MEMBER':
                if not membership:
                    membership = Membership.objects.using(alias).filter(
                        user_profile=user_profile,
                        status='ACTIVE',
                        end_date__gte=timezone.now().date()
                    ).first()

                if not membership:
                    raise ValidationError("Active membership required for member class booking.")

                if membership.status != 'ACTIVE':
                    raise ValidationError(f"Membership is in '{membership.status}' status (must be ACTIVE).")

                if membership.end_date < timezone.now().date():
                    raise ValidationError("Membership has expired.")

                # Validate PackageClassAccessRule against member's package_version
                access_res = cls.resolve_package_class_access(membership.package_version, occurrence, db_alias=alias)
                if not access_res['allowed']:
                    raise ValidationError(access_res['reason'])

                units_to_consume = access_res.get('units') or Decimal('1.00')
                ent_type_filter = access_res.get('entitlement_type')

            # Generate unique booking number
            booking_number = f"BK-{timezone.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"

            # 6. Check Occurrence Capacity dynamically
            enrolled_count = Booking.objects.using(alias).filter(
                occurrence=occurrence,
                status__in=['CONFIRMED', 'RESERVED', 'COMPLETED']
            ).count()
            is_capacity_available = enrolled_count < occurrence.capacity

            if is_capacity_available:
                entitlement = None
                if booking_type == 'MEMBER':
                    ent_qs = membership.entitlements.using(alias).filter(status='ACTIVE')
                    if ent_type_filter:
                        ent_qs = ent_qs.filter(entitlement_type=ent_type_filter)

                    ent = ent_qs.filter(
                        models.Q(is_unlimited=True) | models.Q(allocated_units__gt=models.F('consumed_units'))
                    ).first()
                    if not ent:
                        raise ValidationError("No membership entitlement sessions available.")

                    MembershipLifecycleService.consume_entitlement(
                        membership=membership,
                        entitlement_type=ent.entitlement_type,
                        units=units_to_consume,
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
                # Capacity is full: evaluate waitlist. WAITLIST CONSUMES ZERO ENTITLEMENT.
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
    def cancel_booking(
        cls,
        booking: Booking,
        cancelled_by_user=None,
        reason_code: str = 'MEMBER_REQUEST',
        reason_text: str = '',
        db_alias: str = None,
    ) -> BookingCancellation:
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        with transaction.atomic(using=alias):
            # Lock booking to prevent concurrent state transitions
            booking = Booking.objects.using(alias).select_for_update().get(id=booking.id)
            if booking.status not in ['CONFIRMED', 'WAITLISTED', 'RESERVED']:
                raise ValidationError(f"Cannot cancel booking in status '{booking.status}'.")

            occurrence = booking.occurrence
            organization = occurrence.class_template.category.organization
            branch = booking.branch
            now = timezone.now()

            diff_minutes = int((occurrence.start_at - now).total_seconds() / 60)
            old_status = booking.status

            # Find applied cancellation rule
            applied_rule = BookingCancellationRule.objects.using(alias).filter(
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

            if old_status in ['CONFIRMED', 'RESERVED']:
                # Restore entitlement if configured and was consumed
                if session_action == 'RESTORE' and booking.membership and booking.entitlement:
                    MembershipLifecycleService.reverse_entitlement(
                        membership=booking.membership,
                        entitlement_type=booking.entitlement.entitlement_type,
                        units=session_units,
                        booking_id=booking.id,
                        reason_text=f"Cancellation refund ({reason_code})",
                        created_by_user=cancelled_by_user,
                        db_alias=alias,
                    )

            elif old_status == 'WAITLISTED':
                session_action = 'NO_ACTION'
                session_units = Decimal('0.00')

                BookingWaitlistEvent.objects.using(alias).create(
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
                subsequent = Booking.objects.using(alias).filter(
                    occurrence=occurrence,
                    status='WAITLISTED',
                    waitlist_position__gt=booking.waitlist_position
                ).order_by('waitlist_position')
                for sub in subsequent:
                    old_p = sub.waitlist_position
                    new_p = old_p - 1
                    sub.waitlist_position = new_p
                    sub.save(using=alias, update_fields=['waitlist_position'])
                    BookingWaitlistEvent.objects.using(alias).create(
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
            booking.save(using=alias, update_fields=['status', 'cancelled_at', 'updated_at'])

            cancellation_record = BookingCancellation.objects.using(alias).create(
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

            BookingStatusHistory.objects.using(alias).create(
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
                metadata={'reason_code': reason_code, 'minutes_before_class': diff_minutes},
                db_alias=alias,
            )

            enqueue_outbox_event(
                organization=organization,
                event_type='BOOKING_CANCELLED',
                aggregate_type='Booking',
                aggregate_id=booking.id,
                payload={'booking_id': str(booking.id), 'reason_code': reason_code},
                db_alias=alias,
            )

            # Auto-promote top of waitlist after cancellation is committed in occurrence
            if old_status in ['CONFIRMED', 'RESERVED']:
                cls.auto_promote_from_waitlist(occurrence, db_alias=alias)

            return cancellation_record

    @classmethod
    def auto_promote_from_waitlist(cls, occurrence: ClassOccurrence, db_alias: str = None) -> Booking:
        """
        Safely auto-promotes the next eligible waitlisted member.
        Revalidates: member active, membership active, package access, entitlement availability,
        attendance/no-show restrictions, and occurrence capacity atomically.
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        with transaction.atomic(using=alias):
            occurrence = ClassOccurrence.objects.using(alias).select_for_update().get(id=occurrence.id)
            enrolled_count = Booking.objects.using(alias).filter(
                occurrence=occurrence,
                status__in=['CONFIRMED', 'RESERVED', 'COMPLETED']
            ).count()
            if enrolled_count >= occurrence.capacity:
                return None

            candidates = list(Booking.objects.using(alias).select_for_update().filter(
                occurrence=occurrence,
                status='WAITLISTED'
            ).order_by('waitlist_position', 'booked_at'))

            organization = occurrence.class_template.category.organization

            for candidate in candidates:
                cand_prof = candidate.user_profile
                # 1. Revalidate member active
                if cand_prof.member_status != 'ACTIVE' or (cand_prof.user and cand_prof.user.status != 'ACTIVE'):
                    candidate.status = 'CANCELLED'
                    candidate.waitlist_position = None
                    candidate.save(using=alias, update_fields=['status', 'waitlist_position', 'updated_at'])
                    BookingWaitlistEvent.objects.using(alias).create(
                        booking=candidate,
                        occurrence=occurrence,
                        event_type='AUTO_CANCELLED',
                        reason='Member profile or user account is no longer active at promotion time',
                        triggered_by_type='SYSTEM',
                        created_at=timezone.now()
                    )
                    continue

                # 2. Revalidate attendance restriction
                can_book, restriction_reason = cls.can_member_book(cand_prof, organization, db_alias=alias)
                if not can_book:
                    candidate.status = 'CANCELLED'
                    candidate.waitlist_position = None
                    candidate.save(using=alias, update_fields=['status', 'waitlist_position', 'updated_at'])
                    BookingWaitlistEvent.objects.using(alias).create(
                        booking=candidate,
                        occurrence=occurrence,
                        event_type='AUTO_CANCELLED',
                        reason=f'Promotion blocked by attendance restriction: {restriction_reason}',
                        triggered_by_type='SYSTEM',
                        created_at=timezone.now()
                    )
                    continue

                # 3. Revalidate membership & entitlement if MEMBER booking
                membership = candidate.membership
                entitlement = None
                if candidate.booking_type == 'MEMBER':
                    if not membership or membership.status != 'ACTIVE' or membership.end_date < timezone.now().date():
                        membership = Membership.objects.using(alias).filter(
                            user_profile=cand_prof,
                            status='ACTIVE',
                            end_date__gte=timezone.now().date()
                        ).first()

                    if not membership:
                        candidate.status = 'CANCELLED'
                        candidate.waitlist_position = None
                        candidate.save(using=alias, update_fields=['status', 'waitlist_position', 'updated_at'])
                        BookingWaitlistEvent.objects.using(alias).create(
                            booking=candidate,
                            occurrence=occurrence,
                            event_type='AUTO_CANCELLED',
                            reason='No active membership found at promotion time',
                            triggered_by_type='SYSTEM',
                            created_at=timezone.now()
                        )
                        continue

                    # Package class access check
                    access_res = cls.resolve_package_class_access(membership.package_version, occurrence, db_alias=alias)
                    if not access_res['allowed']:
                        candidate.status = 'CANCELLED'
                        candidate.waitlist_position = None
                        candidate.save(using=alias, update_fields=['status', 'waitlist_position', 'updated_at'])
                        BookingWaitlistEvent.objects.using(alias).create(
                            booking=candidate,
                            occurrence=occurrence,
                            event_type='AUTO_CANCELLED',
                            reason=f"Promotion blocked by package class access: {access_res.get('reason')}",
                            triggered_by_type='SYSTEM',
                            created_at=timezone.now()
                        )
                        continue

                    units_to_consume = access_res.get('units') or Decimal('1.00')
                    ent_type_filter = access_res.get('entitlement_type')

                    ent_qs = membership.entitlements.using(alias).filter(status='ACTIVE')
                    if ent_type_filter:
                        ent_qs = ent_qs.filter(entitlement_type=ent_type_filter)
                    ent = ent_qs.filter(
                        models.Q(is_unlimited=True) | models.Q(allocated_units__gt=models.F('consumed_units'))
                    ).first()

                    if not ent:
                        candidate.status = 'CANCELLED'
                        candidate.waitlist_position = None
                        candidate.save(using=alias, update_fields=['status', 'waitlist_position', 'updated_at'])
                        BookingWaitlistEvent.objects.using(alias).create(
                            booking=candidate,
                            occurrence=occurrence,
                            event_type='AUTO_CANCELLED',
                            reason='No available session entitlement at promotion time',
                            triggered_by_type='SYSTEM',
                            created_at=timezone.now()
                        )
                        continue

                    # Consume entitlement atomically
                    MembershipLifecycleService.consume_entitlement(
                        membership=membership,
                        entitlement_type=ent.entitlement_type,
                        units=units_to_consume,
                        booking_id=candidate.id,
                        reason_text="Promoted from waitlist to confirmed",
                        created_by_user=None,
                        db_alias=alias,
                    )
                    entitlement = ent

                # Candidate is verified and eligible! Confirm booking:
                old_position = candidate.waitlist_position
                candidate.status = 'CONFIRMED'
                candidate.waitlist_position = None
                candidate.membership = membership
                candidate.entitlement = entitlement
                candidate.save(using=alias)

                BookingWaitlistEvent.objects.using(alias).create(
                    booking=candidate,
                    occurrence=occurrence,
                    event_type='PROMOTED',
                    old_position=old_position,
                    new_position=None,
                    reason='Auto-promoted from waitlist into open class spot',
                    triggered_by_type='SYSTEM',
                    created_at=timezone.now()
                )

                BookingStatusHistory.objects.using(alias).create(
                    booking=candidate,
                    from_status='WAITLISTED',
                    to_status='CONFIRMED',
                    reason_code='AUTO_PROMOTION',
                    reason_text='Promoted into open slot after seat opened',
                    changed_by_user=None,
                    changed_at=timezone.now()
                )

                # Re-index remaining waitlisted bookings
                remaining = Booking.objects.using(alias).filter(
                    occurrence=occurrence,
                    status='WAITLISTED'
                ).order_by('waitlist_position', 'booked_at')
                for idx, b in enumerate(remaining, start=1):
                    if b.waitlist_position != idx:
                        b.waitlist_position = idx
                        b.save(using=alias, update_fields=['waitlist_position'])

                record_business_audit(
                    organization=organization,
                    module='BOOKINGS',
                    action_code='WAITLIST_PROMOTED',
                    entity_type='Booking',
                    entity_id=candidate.id,
                    branch=candidate.branch,
                    actor_user=None,
                    metadata={'booking_number': candidate.booking_number, 'occurrence_id': str(occurrence.id)},
                    db_alias=alias,
                )

                return candidate

            # Re-index remaining waitlisted if cancellations happened
            remaining = Booking.objects.using(alias).filter(
                occurrence=occurrence,
                status='WAITLISTED'
            ).order_by('waitlist_position', 'booked_at')
            for idx, b in enumerate(remaining, start=1):
                if b.waitlist_position != idx:
                    b.waitlist_position = idx
                    b.save(using=alias, update_fields=['waitlist_position'])

            return None

    @classmethod
    def reschedule_booking(
        cls,
        booking: Booking,
        to_occurrence: ClassOccurrence,
        rescheduled_by_user=None,
        reason_code: str = 'MEMBER_REQUEST',
        reason_text: str = '',
        db_alias: str = None,
    ) -> Booking:
        """
        Atomically reschedules a confirmed/reserved booking to a new class occurrence.
        All-or-nothing: Locks booking, old occurrence, and new occurrence.
        Retains already consumed session entitlement (zero double-consumption).
        Frees old occurrence seat and auto-promotes old occurrence waitlist.
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        with transaction.atomic(using=alias):
            # 1. Lock current booking
            booking = Booking.objects.using(alias).select_for_update().get(id=booking.id)
            if booking.status not in ['CONFIRMED', 'RESERVED']:
                raise ValidationError(f"Only confirmed or reserved bookings can be rescheduled. Current status: '{booking.status}'.")

            from_occurrence = booking.occurrence
            if from_occurrence.id == to_occurrence.id:
                raise ValidationError("Cannot reschedule to the exact same class occurrence.")

            # 2. Lock both occurrences deterministically
            occ_ids = sorted([from_occurrence.id, to_occurrence.id])
            locked_occs = {
                o.id: o for o in ClassOccurrence.objects.using(alias).select_for_update().filter(id__in=occ_ids)
            }
            from_occ_locked = locked_occs[from_occurrence.id]
            to_occ_locked = locked_occs[to_occurrence.id]

            # 3. Validate new occurrence
            if to_occ_locked.start_at <= timezone.now():
                raise ValidationError("Cannot reschedule to a past class occurrence.")

            # Check duplicate on target occurrence
            existing_target = Booking.objects.using(alias).filter(
                user_profile=booking.user_profile,
                occurrence=to_occ_locked,
                status__in=['RESERVED', 'WAITLISTED', 'CONFIRMED']
            ).first()
            if existing_target:
                raise ValidationError(f"User already has an active booking ({existing_target.booking_number}) for target class occurrence.")

            # Check capacity on target occurrence
            enrolled_target = Booking.objects.using(alias).filter(
                occurrence=to_occ_locked,
                status__in=['CONFIRMED', 'RESERVED', 'COMPLETED']
            ).count()
            if enrolled_target >= to_occ_locked.capacity:
                raise ValidationError("Target class occurrence capacity is full; cannot reschedule to a full class.")

            # 4. Check policy max_reschedules
            organization = to_occ_locked.class_template.category.organization
            policy = BookingPolicySet.objects.using(alias).filter(organization=organization, status='ACTIVE').first()
            if policy and policy.max_reschedules:
                prior_reschedules = BookingReschedule.objects.using(alias).filter(booking=booking).count()
                if prior_reschedules >= policy.max_reschedules:
                    raise ValidationError(f"Maximum allowed reschedules ({policy.max_reschedules}) reached for this booking.")

            # 5. Check Package Class Access for new occurrence
            if booking.membership:
                access_res = cls.resolve_package_class_access(booking.membership.package_version, to_occ_locked, db_alias=alias)
                if not access_res['allowed']:
                    raise ValidationError(f"Target class is not accessible under current package version: {access_res.get('reason')}")

            # 6. Create BookingReschedule audit
            reschedule_count = BookingReschedule.objects.using(alias).filter(booking=booking).count() + 1
            BookingReschedule.objects.using(alias).create(
                booking=booking,
                from_occurrence=from_occ_locked,
                to_occurrence=to_occ_locked,
                reschedule_number=reschedule_count,
                reason_code=reason_code,
                reason_text=reason_text,
                rescheduled_by_user=rescheduled_by_user,
                rescheduled_at=timezone.now(),
            )

            # 7. Update Booking pointer & branch (entitlement retained without double consumption)
            booking.occurrence = to_occ_locked
            booking.branch = to_occ_locked.branch
            booking.save(using=alias, update_fields=['occurrence', 'branch', 'updated_at'])

            # 8. Record Status History
            BookingStatusHistory.objects.using(alias).create(
                booking=booking,
                from_status=booking.status,
                to_status=booking.status,
                reason_code=reason_code or 'RESCHEDULED',
                reason_text=f"Rescheduled to {to_occ_locked.class_template.name} ({to_occ_locked.occurrence_date} {to_occ_locked.start_at.strftime('%H:%M')}). {reason_text}".strip(),
                changed_by_user=rescheduled_by_user,
                changed_at=timezone.now()
            )

            record_business_audit(
                organization=organization,
                module='BOOKINGS',
                action_code='BOOKING_RESCHEDULED',
                entity_type='Booking',
                entity_id=booking.id,
                branch=to_occ_locked.branch,
                actor_user=rescheduled_by_user,
                metadata={
                    'from_occurrence_id': str(from_occ_locked.id),
                    'to_occurrence_id': str(to_occ_locked.id),
                    'reschedule_number': reschedule_count
                },
                db_alias=alias,
            )

            enqueue_outbox_event(
                organization=organization,
                event_type='BOOKING_RESCHEDULED',
                aggregate_type='Booking',
                aggregate_id=booking.id,
                payload={'booking_id': str(booking.id), 'to_occurrence_id': str(to_occ_locked.id)},
                db_alias=alias,
            )

            # 9. Free seat on old occurrence: auto-promote any waitlisted members on old occurrence
            cls.auto_promote_from_waitlist(from_occ_locked, db_alias=alias)

            return booking

    @classmethod
    def record_attendance(
        cls,
        booking: Booking,
        status: str = 'PRESENT',
        check_in_method: str = 'QR',
        marked_by_user=None,
        db_alias: str = None,
    ) -> AttendanceRecord:
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        with transaction.atomic(using=alias):
            user_profile = booking.user_profile
            branch = booking.branch
            occurrence = booking.occurrence
            now = timezone.now()
            organization = occurrence.class_template.category.organization

            record = AttendanceRecord.objects.using(alias).filter(booking=booking).first()
            if not record:
                record = AttendanceRecord.objects.using(alias).create(
                    booking=booking,
                    user_profile=user_profile,
                    branch=branch,
                    occurrence=occurrence,
                    status=status,
                    check_in_status='SUCCESSFUL' if status in ['PRESENT', 'LATE'] else 'FAILED',
                    check_in_method=check_in_method,
                    check_in_at=now if status in ['PRESENT', 'LATE'] else None,
                    marked_by_user=marked_by_user,
                )
            else:
                record.status = status
                record.check_in_status = 'SUCCESSFUL' if status in ['PRESENT', 'LATE'] else 'FAILED'
                record.check_in_method = check_in_method
                if status in ['PRESENT', 'LATE']:
                    record.check_in_at = now
                record.marked_by_user = marked_by_user
                record.save(using=alias)

            # Update booking lifecycle
            old_booking_status = booking.status
            if status in ['PRESENT', 'LATE']:
                booking.status = 'COMPLETED'
                booking.completed_at = now
                booking.save(using=alias, update_fields=['status', 'completed_at', 'updated_at'])

                # Handle attendance policy & state reset
                policy = AttendancePolicySet.objects.using(alias).filter(organization=organization, status='ACTIVE').first()
                if policy:
                    member_state = MemberAttendanceState.objects.using(alias).filter(
                        user_profile=user_profile,
                        attendance_policy_set=policy
                    ).first()
                    if member_state:
                        if policy.reset_on_successful_attendance:
                            member_state.consecutive_no_show_count = 0
                            member_state.booking_mode = 'NORMAL'
                            member_state.current_max_advance_bookings = policy.normal_max_advance_bookings
                            member_state.last_successful_attendance_at = now
                            member_state.save(using=alias)

                            AttendancePolicyEvent.objects.using(alias).create(
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
                booking.save(using=alias, update_fields=['status', 'updated_at'])

                policy = AttendancePolicySet.objects.using(alias).filter(organization=organization, status='ACTIVE').first()
                if policy:
                    member_state = MemberAttendanceState.objects.using(alias).filter(
                        user_profile=user_profile,
                        attendance_policy_set=policy
                    ).first()
                    if not member_state:
                        member_state = MemberAttendanceState.objects.using(alias).create(
                            user_profile=user_profile,
                            attendance_policy_set=policy,
                            current_max_advance_bookings=policy.normal_max_advance_bookings,
                            booking_mode='NORMAL',
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

                        AttendancePolicyEvent.objects.using(alias).create(
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

                    member_state.save(using=alias)

            BookingStatusHistory.objects.using(alias).create(
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
    def log_access_event(
        cls,
        user_profile: UserProfile,
        branch,
        event_type: str = 'ENTRY',
        device_reference: str = None,
        booking: Booking = None,
        provider_reference: str = None,
        db_alias: str = None,
    ) -> AccessEvent:
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        with transaction.atomic(using=alias):
            now = timezone.now()
            event = AccessEvent.objects.using(alias).create(
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
                    today = now.date()
                    booking = Booking.objects.using(alias).filter(
                        user_profile=user_profile,
                        branch=branch,
                        status='CONFIRMED',
                        occurrence__start_at__date=today
                    ).first()
                if booking:
                    cls.record_attendance(
                        booking=booking,
                        status='PRESENT',
                        check_in_method='ACCESS_DEVICE',
                        db_alias=alias,
                    )

            return event
