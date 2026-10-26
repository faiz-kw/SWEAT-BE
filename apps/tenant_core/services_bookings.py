import uuid
from datetime import timedelta
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
from .models_attendance import AttendanceRecord, AccessEvent, AccessPass
from .models_classes import ClassOccurrence
from .models_crm import UserProfile
from .models_memberships import Membership, MembershipEntitlement
from .services_reliability import record_business_audit, enqueue_outbox_event
from .services_memberships import MembershipLifecycleService
from .context import get_current_tenant_db_alias


class ParqRequiredValidationError(ValidationError):
    """
    Raised when class booking, waitlist entry, or rescheduling is blocked
    because the membership's PAR-Q is pending completion and digital signature.
    """
    def __init__(self, message, code='PARQ_REQUIRED', membership=None, form=None):
        super().__init__(message, code=code)
        self.code = code
        self.membership = membership
        self.form = form


class BookingWaitlistAttendanceService:

    @classmethod
    def resolve_booking_policy(cls, occurrence: ClassOccurrence, db_alias: str = None) -> BookingPolicySet:
        """
        Canonical resolution hierarchy:
        1. Occurrence override (occurrence=occurrence)
        2. ClassTemplate (class_template=occurrence.class_template)
        3. Program (program=occurrence.class_template.program)
        4. Branch (branch=occurrence.branch)
        5. Organization default (organization=occurrence.class_template.category.organization)
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        org = occurrence.class_template.category.organization

        # 1. Occurrence override
        policy = BookingPolicySet.objects.using(alias).filter(occurrence=occurrence, status='ACTIVE').first()
        if policy:
            return policy

        # 2. ClassTemplate
        policy = BookingPolicySet.objects.using(alias).filter(class_template=occurrence.class_template, status='ACTIVE').first()
        if policy:
            return policy

        # 3. Program
        if occurrence.class_template.program:
            policy = BookingPolicySet.objects.using(alias).filter(program=occurrence.class_template.program, status='ACTIVE').first()
            if policy:
                return policy

        # 4. Branch
        policy = BookingPolicySet.objects.using(alias).filter(branch=occurrence.branch, status='ACTIVE').first()
        if policy:
            return policy

        # 5. Organization default
        return (
            BookingPolicySet.objects.using(alias).filter(
                organization=org, branch__isnull=True, program__isnull=True,
                class_template__isnull=True, occurrence__isnull=True, status='ACTIVE'
            ).first()
            or BookingPolicySet.objects.using(alias).filter(organization=org, status='ACTIVE').first()
        )

    @classmethod
    def resolve_cancellation_rule(cls, booking: Booking, db_alias: str = None) -> BookingCancellationRule:
        """
        Canonical cancellation rule resolution:
        1. Applied policy set rules
        2. ClassTemplate rule
        3. Program rule
        4. Branch rule
        5. Organization default rule
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        occ = booking.occurrence
        org = occ.class_template.category.organization

        policy = cls.resolve_booking_policy(occ, db_alias=alias)
        if policy:
            rule = BookingCancellationRule.objects.using(alias).filter(booking_policy_set=policy, status='ACTIVE').order_by('priority').first()
            if rule:
                return rule

        rule = BookingCancellationRule.objects.using(alias).filter(class_template=occ.class_template, status='ACTIVE').order_by('priority').first()
        if rule:
            return rule

        if occ.class_template.program:
            rule = BookingCancellationRule.objects.using(alias).filter(program=occ.class_template.program, status='ACTIVE').order_by('priority').first()
            if rule:
                return rule

        rule = BookingCancellationRule.objects.using(alias).filter(branch=booking.branch, status='ACTIVE').order_by('priority').first()
        if rule:
            return rule

        return BookingCancellationRule.objects.using(alias).filter(organization=org, status='ACTIVE').order_by('priority').first()

    @classmethod
    def resolve_attendance_policy(cls, occurrence: ClassOccurrence, db_alias: str = None) -> AttendancePolicySet:
        """
        Canonical attendance policy resolution:
        1. ClassTemplate
        2. Program
        3. Branch
        4. Organization
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        org = occurrence.class_template.category.organization

        policy = AttendancePolicySet.objects.using(alias).filter(class_template=occurrence.class_template, status='ACTIVE').first()
        if policy:
            return policy

        if occurrence.class_template.program:
            policy = AttendancePolicySet.objects.using(alias).filter(program=occurrence.class_template.program, status='ACTIVE').first()
            if policy:
                return policy

        policy = AttendancePolicySet.objects.using(alias).filter(organization=org, status='ACTIVE').first()
        if policy:
            return policy

        # Auto-initialize active default organization AttendancePolicySet if none exists
        if org:
            policy, created = AttendancePolicySet.objects.using(alias).get_or_create(
                organization=org,
                name=f"{org.name} Standard Attendance Policy",
                defaults={
                    'rule_behavior': 'OPERATIONAL',
                    'normal_max_advance_bookings': 3,
                    'restricted_max_advance_bookings': 1,
                    'restriction_threshold': 2,
                    'reset_on_successful_attendance': True,
                    'no_show_mark_after_minutes': 0,
                    'status': 'ACTIVE',
                }
            )
            if created:
                AttendancePenaltyRule.objects.using(alias).create(
                    attendance_policy_set=policy,
                    rule_code='FIRST_NO_SHOW',
                    base_session_units=Decimal('1.00'),
                    additional_penalty_units=Decimal('0.00'),
                    priority=10,
                    status='ACTIVE',
                )
                AttendancePenaltyRule.objects.using(alias).create(
                    attendance_policy_set=policy,
                    rule_code='CONSECUTIVE_NO_SHOW',
                    base_session_units=Decimal('1.00'),
                    additional_penalty_units=Decimal('0.00'),
                    priority=20,
                    status='ACTIVE',
                )
                AttendancePenaltyRule.objects.using(alias).create(
                    attendance_policy_set=policy,
                    rule_code='RESTRICTED_NO_SHOW',
                    base_session_units=Decimal('1.00'),
                    additional_penalty_units=Decimal('1.00'),
                    priority=30,
                    status='ACTIVE',
                )
            return policy

        return None

    @classmethod
    def can_member_book(cls, user_profile: UserProfile, organization, occurrence: ClassOccurrence = None, db_alias: str = None) -> tuple:
        """
        Asks the attendance / no-show restriction service if the member is permitted to book.
        Returns (is_allowed: bool, reason: str).
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        if occurrence:
            attendance_policy = cls.resolve_attendance_policy(occurrence, db_alias=alias)
        else:
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
    def evaluate_occurrence_eligibility(
        cls,
        occurrence: ClassOccurrence,
        membership: Membership = None,
        user_profile: UserProfile = None,
        db_alias: str = None,
        booking_type: str = 'MEMBER',
    ) -> dict:
        """
        Authoritatively evaluates whether an occurrence is bookable for a specific membership and user.
        Returns:
            {'eligible': bool, 'reason_code': str | None, 'reason_message': str | None}
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        now = timezone.now()

        # 1. Occurrence Lifecycle Status
        if occurrence.status == 'CANCELLED':
            return {
                'eligible': False,
                'reason_code': 'OCCURRENCE_CANCELLED',
                'reason_message': 'This class session is cancelled.',
            }
        if occurrence.status == 'COMPLETED':
            return {
                'eligible': False,
                'reason_code': 'OCCURRENCE_COMPLETED',
                'reason_message': 'This class session is already completed.',
            }
        if occurrence.start_at and occurrence.start_at <= now:
            return {
                'eligible': False,
                'reason_code': 'PAST_OCCURRENCE',
                'reason_message': 'This class session has already started or ended.',
            }

        # 2. Program Branch Availability
        from .models_catalog import ProgramBranchAvailability
        if occurrence.class_template and occurrence.class_template.program:
            pba = ProgramBranchAvailability.objects.using(alias).filter(
                program=occurrence.class_template.program,
                branch=occurrence.branch
            ).first()
            if pba and not pba.is_active:
                return {
                    'eligible': False,
                    'reason_code': 'PROGRAM_UNAVAILABLE_AT_BRANCH',
                    'reason_message': f"Program is not permitted at branch '{occurrence.branch.name}'.",
                }

        # 3. Class Branch Availability
        from .models_classes import ClassBranchAvailability
        cba = ClassBranchAvailability.objects.using(alias).filter(
            class_template=occurrence.class_template,
            branch=occurrence.branch
        ).first()
        if cba and cba.status not in ('ACTIVE', 'ENABLED'):
            return {
                'eligible': False,
                'reason_code': 'CLASS_UNAVAILABLE_AT_BRANCH',
                'reason_message': f"Class is not active at branch '{occurrence.branch.name}'.",
            }

        # 4. Membership Specific Validations (if membership provided)
        if membership:
            if getattr(membership, 'parq_status', 'COMPLETED') != 'COMPLETED':
                return {
                    'eligible': False,
                    'reason_code': 'PARQ_REQUIRED',
                    'reason_message': f"A completed and digitally signed PAR-Q is required before booking classes under membership {membership.membership_number}.",
                }
            if membership.legacy_reference and 'PROVISIONAL_CASH_LIMIT_REACHED' in membership.legacy_reference:
                return {
                    'eligible': False,
                    'reason_code': 'PAYMENT_APPROVAL_PENDING_LIMIT_REACHED',
                    'reason_message': 'Cash payment approval is still pending and provisional session limit has been reached.',
                }
            from .services_memberships import MembershipLifecycleService
            MembershipLifecycleService.auto_sync_freeze_status(membership, db_alias=alias)

            occ_date = occurrence.start_at.date() if occurrence.start_at else now.date()
            from .models_memberships import MembershipFreeze
            matching_freeze = MembershipFreeze.objects.using(alias).filter(
                membership=membership,
                status__in=['ACTIVE', 'SCHEDULED'],
                freeze_from__lte=occ_date,
                freeze_until__gte=occ_date,
            ).first()
            if matching_freeze:
                return {
                    'eligible': False,
                    'reason_code': 'MEMBERSHIP_FROZEN',
                    'reason_message': f"Membership is frozen from {matching_freeze.freeze_from} to {matching_freeze.freeze_until}. No class bookings allowed during the freeze window.",
                }

            if membership.status not in ('ACTIVE', 'FROZEN'):
                return {
                    'eligible': False,
                    'reason_code': 'MEMBERSHIP_INACTIVE',
                    'reason_message': f"Selected membership is {membership.status}.",
                }
            if membership.end_date and membership.end_date < occ_date:
                return {
                    'eligible': False,
                    'reason_code': 'MEMBERSHIP_EXPIRED',
                    'reason_message': f"Selected membership expires on {membership.end_date}, before class date {occ_date}.",
                }
            if membership.end_date and occurrence.start_at and membership.end_date < occurrence.start_at.date():
                return {
                    'eligible': False,
                    'reason_code': 'MEMBERSHIP_EXPIRED_ON_DATE',
                    'reason_message': f"Membership will be expired on the class date ({occurrence.start_at.date()}).",
                }
            from .models_memberships import MembershipFreeze
            if occurrence.start_at and MembershipFreeze.objects.using(alias).filter(
                membership=membership,
                status__in=['ACTIVE', 'SCHEDULED'],
                freeze_from__lte=occurrence.start_at.date(),
                freeze_until__gte=occurrence.start_at.date()
            ).exists():
                return {
                    'eligible': False,
                    'reason_code': 'MEMBERSHIP_FROZEN_ON_DATE',
                    'reason_message': 'Class occurs during an active or scheduled membership freeze window.',
                }

            # Package Branch Availability
            from .models_catalog import PackageBranchAvailability
            pkg = membership.package_version.package if membership.package_version else membership.package
            if pkg:
                pba_qs = PackageBranchAvailability.objects.using(alias).filter(package=pkg)
                if pba_qs.exists() and not pba_qs.filter(branch=occurrence.branch, status='ENABLED').exists():
                    return {
                        'eligible': False,
                        'reason_code': 'BRANCH_NOT_PERMITTED',
                        'reason_message': f"Your package is not valid at '{occurrence.branch.name}'.",
                    }

            # Package Class Access Rule
            if membership.package_version:
                access_res = cls.resolve_package_class_access(membership.package_version, occurrence, db_alias=alias)
                if not access_res.get('allowed'):
                    return {
                        'eligible': False,
                        'reason_code': 'PACKAGE_CLASS_EXCLUDED',
                        'reason_message': access_res.get('reason') or "Membership package does not include this class.",
                    }

            # Cross-branch Entitlement
            is_cross_branch = bool(membership.home_branch and occurrence.branch_id != membership.home_branch_id)
            ent_qs = membership.entitlements.using(alias).filter(status='ACTIVE')
            if is_cross_branch:
                cross_ent = ent_qs.filter(
                    models.Q(entitlement_type='CROSS_BRANCH_SESSION') | models.Q(entitlement_type='CROSS_BRANCH'),
                    models.Q(is_unlimited=True) | models.Q(allocated_units__gt=models.F('consumed_units'))
                ).first()
                if not cross_ent:
                    return {
                        'eligible': False,
                        'reason_code': 'NO_CROSS_BRANCH_ENTITLEMENT',
                        'reason_message': f"No cross-branch session entitlements available for '{occurrence.branch.name}'.",
                    }
            else:
                home_ent = ent_qs.filter(
                    models.Q(is_unlimited=True) | models.Q(allocated_units__gt=models.F('consumed_units')),
                    entitlement_type='HOME_BRANCH_SESSION'
                ).first() or ent_qs.filter(
                    models.Q(is_unlimited=True) | models.Q(allocated_units__gt=models.F('consumed_units'))
                ).first()
                if not home_ent:
                    return {
                        'eligible': False,
                        'reason_code': 'NO_ENTITLEMENT_REMAINING',
                        'reason_message': 'No remaining sessions available in membership.',
                    }

        # 5. User Duplicate Booking Check
        if user_profile:
            has_booking = occurrence.bookings.using(alias).filter(
                user_profile=user_profile,
                status__in=['CONFIRMED', 'RESERVED', 'WAITLISTED']
            ).exists()
            if has_booking:
                return {
                    'eligible': False,
                    'reason_code': 'ALREADY_BOOKED',
                    'reason_message': 'Member already has an active booking or waitlist entry for this class.',
                }

        # 6. Capacity Check
        from .models_crm import TrialBooking
        booked_user_emails = set(
            occurrence.bookings.using(alias).filter(
                status__in=['CONFIRMED', 'RESERVED', 'COMPLETED']
            ).values_list('user_profile__user__email', flat=True)
        )
        trial_qs = TrialBooking.objects.using(alias).filter(
            class_occurrence_id=occurrence.id,
            status__in=['BOOKED', 'CONFIRMED', 'ATTENDED']
        )
        if booked_user_emails:
            trial_qs = trial_qs.exclude(lead__email_normalized__in=booked_user_emails)
        if user_profile and user_profile.user:
            trial_qs = trial_qs.exclude(
                models.Q(lead__email_normalized__iexact=user_profile.user.email) |
                (models.Q(lead__phone_normalized=user_profile.user.phone) if user_profile.user.phone else models.Q(pk=None))
            )
        trial_enrolled = trial_qs.count()
        enrolled_count = occurrence.bookings.using(alias).filter(
            status__in=['CONFIRMED', 'RESERVED', 'COMPLETED']
        ).count() + trial_enrolled

        if booking_type == 'TRIAL':
            effective_trial_cap = (
                occurrence.trial_capacity
                if occurrence.trial_capacity is not None
                else (occurrence.class_template.default_trial_capacity if occurrence.class_template else 0)
            )

            if effective_trial_cap <= 0:
                return {
                    'eligible': False,
                    'reason_code': 'TRIAL_NOT_PERMITTED',
                    'reason_message': 'Trial bookings are not enabled for this session.',
                }

            if enrolled_count >= occurrence.capacity:
                return {
                    'eligible': False,
                    'reason_code': 'TRIAL_CAPACITY_FULL',
                    'reason_message': 'This class session is fully booked. No trial slots available.',
                }

            existing_trials = occurrence.bookings.using(alias).filter(
                booking_type='TRIAL',
                status__in=['CONFIRMED', 'RESERVED', 'COMPLETED']
            ).count() + trial_enrolled

            if existing_trials >= effective_trial_cap:
                return {
                    'eligible': False,
                    'reason_code': 'TRIAL_CAPACITY_FULL',
                    'reason_message': 'Trial capacity reached for this session. This trial session is now full.',
                }
        else:
            if enrolled_count >= occurrence.capacity:
                booking_policy = cls.resolve_booking_policy(occurrence, db_alias=alias) if occurrence else None
                if booking_policy and booking_policy.allow_waitlist is not None:
                    allow_waitlist = booking_policy.allow_waitlist
                elif occurrence.class_template and occurrence.class_template.allow_waitlist is not None:
                    allow_waitlist = occurrence.class_template.allow_waitlist
                else:
                    allow_waitlist = True

                if booking_policy and booking_policy.waitlist_capacity is not None:
                    waitlist_cap = booking_policy.waitlist_capacity
                elif occurrence.waitlist_capacity is not None:
                    waitlist_cap = occurrence.waitlist_capacity
                elif occurrence.class_template and occurrence.class_template.default_waitlist_capacity is not None:
                    waitlist_cap = occurrence.class_template.default_waitlist_capacity
                else:
                    waitlist_cap = 5
                if not allow_waitlist or waitlist_cap <= 0:
                    return {
                        'eligible': False,
                        'reason_code': 'CLASS_FULL',
                        'reason_message': 'Class session is at full capacity.',
                    }
                wl_count = occurrence.bookings.using(alias).filter(status='WAITLISTED').count()
                if wl_count >= waitlist_cap:
                    return {
                        'eligible': False,
                        'reason_code': 'WAITLIST_FULL',
                        'reason_message': 'Class capacity and waitlist are both full.',
                    }

        return {
            'eligible': True,
            'reason_code': None,
            'reason_message': None,
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
        payment_status: str = 'PAID',
        confirmation_status: str = 'CONFIRMED',
        initial_status: str = None,
        db_alias: str = None,
    ) -> Booking:
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        with transaction.atomic(using=alias):
            # 1. Validate Member profile and account active
            if user_profile.member_status != 'ACTIVE':
                raise ValidationError("Member profile is not active.")
            if user_profile.user and user_profile.user.status != 'ACTIVE':
                raise ValidationError("User account is not active.")

            organization = (
                occurrence.class_template.category.organization
                if (occurrence.class_template and occurrence.class_template.category and occurrence.class_template.category.organization)
                else (occurrence.class_template.organization if (occurrence.class_template and occurrence.class_template.organization) else (occurrence.branch.organization if occurrence.branch else None))
            )
            branch = occurrence.branch

            # Lock occurrence to serialize concurrent booking and capacity allocation
            occurrence = ClassOccurrence.objects.using(alias).select_for_update().get(id=occurrence.id)

            # Validate occurrence lifecycle and class start/end boundaries
            if occurrence.status in ('CANCELLED', 'COMPLETED'):
                raise ValidationError(f"Cannot book a class occurrence that is {occurrence.status.lower()}.")

            now = timezone.now()
            effective_end_at = occurrence.end_at
            if not effective_end_at and occurrence.start_at:
                duration = occurrence.class_template.default_duration_minutes if occurrence.class_template else 50
                effective_end_at = occurrence.start_at + timedelta(minutes=duration)

            if effective_end_at and effective_end_at <= now:
                raise ValidationError("Cannot book a class occurrence that has already ended.")
            if occurrence.start_at and occurrence.start_at <= now:
                raise ValidationError("Cannot book a class occurrence that has already started.")

            # 2. Prevent duplicate active booking
            existing = Booking.objects.using(alias).filter(
                user_profile=user_profile,
                occurrence=occurrence,
                status__in=['RESERVED', 'WAITLISTED', 'CONFIRMED']
            ).first()
            if existing:
                raise ValidationError(f"User already has an active booking ({existing.booking_number}) for this class occurrence.")

            # 3. Check Attendance / No-Show restrictions via restriction service
            can_book, restriction_reason = cls.can_member_book(user_profile, organization, occurrence=occurrence, db_alias=alias)
            if not can_book:
                raise ValidationError(restriction_reason)

            # 4. Check Booking Policy via canonical hierarchy
            booking_policy = cls.resolve_booking_policy(occurrence, db_alias=alias)

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

            # 4b. Check Configurable BookingPolicyService for booking types & advance limits
            from .services_booking_policy import BookingPolicyService
            effective_bp = BookingPolicyService.get_effective_policy(organization, branch=branch, db_alias=alias)
            allowed_types = effective_bp.get('allowed_booking_types')
            if allowed_types and booking_type not in allowed_types:
                raise ValidationError(f"Booking type '{booking_type}' is not permitted by current booking policy.")

            # Check advance booking window limit
            adv_days_max = effective_bp.get('advance_booking_days_max')
            if adv_days_max and occurrence.occurrence_date:
                max_allowed_date = timezone.now().date() + timedelta(days=int(adv_days_max))
                if occurrence.occurrence_date > max_allowed_date:
                    raise ValidationError(f"Advance bookings beyond {adv_days_max} days are not permitted.")

            # 5. Member validation & Package Class Access
            units_to_consume = Decimal('1.00')
            ent_type_filter = None
            if booking_type == 'MEMBER':
                occ_date = occurrence.start_at.date() if occurrence.start_at else timezone.now().date()
                if not membership:
                    membership = Membership.objects.using(alias).filter(
                        user_profile=user_profile,
                        status__in=['ACTIVE', 'FROZEN'],
                        end_date__gte=occ_date
                    ).first()

                if not membership:
                    raise ValidationError("Active membership required for member class booking.")

                from .services_memberships import MembershipLifecycleService
                MembershipLifecycleService.auto_sync_freeze_status(membership, db_alias=alias)

                from .models_memberships import MembershipFreeze
                matching_freeze = MembershipFreeze.objects.using(alias).filter(
                    membership=membership,
                    status__in=['ACTIVE', 'SCHEDULED'],
                    freeze_from__lte=occ_date,
                    freeze_until__gte=occ_date,
                ).first()
                if matching_freeze:
                    raise ValidationError(
                        f"Membership is frozen from {matching_freeze.freeze_from} to {matching_freeze.freeze_until}. "
                        "No class bookings allowed during the freeze window."
                    )

                if membership.status not in ('ACTIVE', 'FROZEN'):
                    raise ValidationError(f"Membership is in '{membership.status}' status (must be ACTIVE).")

                if membership.end_date < occ_date:
                    raise ValidationError(f"Membership expires on {membership.end_date}, before class date {occ_date}.")

                if occurrence.start_at and membership.end_date < occurrence.start_at.date():
                    raise ValidationError(f"Membership expires on {membership.end_date}, before the class date {occurrence.start_at.date()}.")

                from .models_memberships import MembershipFreeze
                if occurrence.start_at and MembershipFreeze.objects.using(alias).filter(
                    membership=membership,
                    status__in=['ACTIVE', 'SCHEDULED'],
                    freeze_from__lte=occurrence.start_at.date(),
                    freeze_until__gte=occurrence.start_at.date()
                ).exists():
                    raise ValidationError("Cannot book class occurring during an active or scheduled membership freeze window.")

                # Gate: Verify PAR-Q completed and digitally signed for this specific membership
                parq_enforced = bool(
                    (booking_policy and booking_policy.require_parq)
                    or getattr(membership, 'parq_status', 'PENDING') != 'COMPLETED'
                )
                if parq_enforced and getattr(membership, 'parq_status', 'PENDING') != 'COMPLETED':
                    if not membership.parq_form:
                        from .models_crm import IntakeForm
                        org = (
                            getattr(membership.purchase_branch, 'organization', None)
                            or getattr(membership.home_branch, 'organization', None)
                            or getattr(membership.package, 'organization', None)
                        )
                        dyn_form = None
                        if org:
                            if membership.program:
                                dyn_form = IntakeForm.objects.using(alias).filter(
                                    organization=org, assigned_programs=membership.program, form_type='PAR_Q', status='ACTIVE'
                                ).first()
                            if not dyn_form:
                                dyn_form = IntakeForm.objects.using(alias).filter(
                                    organization=org, is_default_for_all_programs=True, form_type='PAR_Q', status='ACTIVE'
                                ).first()
                        if not dyn_form:
                            dyn_form = IntakeForm.objects.using(alias).filter(
                                form_type='PAR_Q', status='ACTIVE', is_default_for_all_programs=True
                            ).first() or IntakeForm.objects.using(alias).filter(
                                form_type='PAR_Q', status='ACTIVE'
                            ).first()
                        if dyn_form:
                            membership.parq_form = dyn_form
                            try:
                                membership.save(update_fields=['parq_form'], using=alias)
                            except Exception:
                                pass
                        else:
                            raise ParqRequiredValidationError(
                                "No active PAR-Q form is configured for this program or organization. Please contact gym administration before booking.",
                                code='PARQ_CONFIG_ERROR',
                                membership=membership,
                                form=None
                            )
                    raise ParqRequiredValidationError(
                        f"A completed and digitally signed PAR-Q is required before booking classes under membership {membership.membership_number}.",
                        code='PARQ_REQUIRED',
                        membership=membership,
                        form=membership.parq_form
                    )

                # Validate PackageClassAccessRule against member's package_version
                access_res = cls.resolve_package_class_access(membership.package_version, occurrence, db_alias=alias)
                if not access_res['allowed']:
                    raise ValidationError(access_res['reason'])

                units_to_consume = access_res.get('units') or Decimal('1.00')
                ent_type_filter = access_res.get('entitlement_type')

            # Generate unique booking number
            booking_number = f"BK-{timezone.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"

            # 6. Check Occurrence Capacity dynamically (shared across members and trials)
            enrolled_count = Booking.objects.using(alias).filter(
                occurrence=occurrence,
                status__in=['CONFIRMED', 'RESERVED', 'COMPLETED']
            ).count()
            from .models_crm import TrialBooking
            user_email = user_profile.user.email if (user_profile and user_profile.user) else None
            user_phone = user_profile.user.phone if (user_profile and user_profile.user) else None
            booked_user_emails = set(
                Booking.objects.using(alias).filter(
                    occurrence=occurrence,
                    status__in=['CONFIRMED', 'RESERVED', 'COMPLETED']
                ).values_list('user_profile__user__email', flat=True)
            )
            trial_qs = TrialBooking.objects.using(alias).filter(
                class_occurrence_id=occurrence.id,
                status__in=['BOOKED', 'CONFIRMED', 'ATTENDED']
            )
            if booked_user_emails:
                trial_qs = trial_qs.exclude(lead__email_normalized__in=booked_user_emails)
            if user_email or user_phone:
                trial_qs = trial_qs.exclude(
                    models.Q(lead__email_normalized__iexact=user_email) |
                    (models.Q(lead__phone_normalized=user_phone) if user_phone else models.Q(pk=None))
                )
            trial_enrolled = trial_qs.count()
            total_enrolled = enrolled_count + trial_enrolled
            is_capacity_available = total_enrolled < occurrence.capacity

            effective_trial_cap = (
                occurrence.trial_capacity
                if occurrence.trial_capacity is not None
                else (occurrence.class_template.default_trial_capacity if occurrence.class_template else 0)
            )

            if booking_type == 'TRIAL':
                if effective_trial_cap <= 0:
                    raise ValidationError("Trial bookings are not enabled for this session.")
                existing_trials = Booking.objects.using(alias).filter(
                    occurrence=occurrence,
                    booking_type='TRIAL',
                    status__in=['CONFIRMED', 'RESERVED', 'COMPLETED']
                ).count() + trial_enrolled
                if existing_trials >= effective_trial_cap:
                    is_capacity_available = False

            if is_capacity_available:
                entitlement = None
                if booking_type == 'MEMBER':
                    is_cross_branch = bool(membership.home_branch and occurrence.branch_id != membership.home_branch_id)
                    if is_cross_branch:
                        from .models_catalog import PackageBranchAvailability
                        pkg = membership.package_version.package if membership.package_version else membership.package
                        if pkg:
                            pba_qs = PackageBranchAvailability.objects.using(alias).filter(package=pkg)
                            if pba_qs.exists() and not pba_qs.filter(branch=occurrence.branch, status='ENABLED').exists():
                                raise ValidationError(f"Membership package does not permit access to '{occurrence.branch.name}'.")

                    ent_qs = membership.entitlements.using(alias).filter(status='ACTIVE')
                    if ent_type_filter:
                        ent_qs = ent_qs.filter(entitlement_type=ent_type_filter)

                    if is_cross_branch and not ent_type_filter:
                        cross_ent = ent_qs.filter(
                            models.Q(entitlement_type='CROSS_BRANCH_SESSION') | models.Q(entitlement_type='CROSS_BRANCH'),
                            models.Q(is_unlimited=True) | models.Q(allocated_units__gt=models.F('consumed_units'))
                        ).first()
                        if not cross_ent:
                            raise ValidationError("No cross-branch session entitlements available for booking outside home branch.")
                        ent = cross_ent
                    else:
                        if not ent_type_filter:
                            home_ent = ent_qs.filter(
                                models.Q(is_unlimited=True) | models.Q(allocated_units__gt=models.F('consumed_units')),
                                entitlement_type='HOME_BRANCH_SESSION'
                            ).first()
                            ent = home_ent or ent_qs.filter(
                                models.Q(is_unlimited=True) | models.Q(allocated_units__gt=models.F('consumed_units'))
                            ).first()
                        else:
                            ent = ent_qs.filter(
                                models.Q(is_unlimited=True) | models.Q(allocated_units__gt=models.F('consumed_units'))
                            ).first()

                    if not ent:
                        raise ValidationError("No membership entitlement sessions available.")

                    cls_title = occurrence.class_template.name if (occurrence and occurrence.class_template) else "Class"
                    consumption_ledger = MembershipLifecycleService.consume_entitlement(
                        membership=membership,
                        entitlement_type=ent.entitlement_type,
                        units=units_to_consume,
                        reason_text=f"Class booking reservation ({cls_title})",
                        created_by_user=created_by_user,
                        db_alias=alias,
                    )
                    entitlement = ent

                resolved_status = initial_status or ('CONFIRMED' if confirmation_status == 'CONFIRMED' else 'RESERVED')
                booking = Booking.objects.using(alias).create(
                    booking_number=booking_number,
                    user_profile=user_profile,
                    membership=membership,
                    entitlement=entitlement,
                    occurrence=occurrence,
                    branch=branch,
                    booking_type=booking_type,
                    booking_source=booking_source,
                    status=resolved_status,
                    payment_status=payment_status,
                    confirmation_status=confirmation_status,
                    confirmed_at=timezone.now() if confirmation_status == 'CONFIRMED' else None,
                    booked_at=timezone.now(),
                    created_by_user=created_by_user,
                )

                if entitlement and 'consumption_ledger' in locals() and consumption_ledger:
                    consumption_ledger.booking_id = booking.id
                    consumption_ledger.save(using=alias, update_fields=['booking_id'])

                BookingStatusHistory.objects.using(alias).create(
                    booking=booking,
                    from_status=None,
                    to_status=resolved_status,
                    reason_code='INITIAL_BOOKING',
                    reason_text=f'{resolved_status} booking created with session entitlement allocation',
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
                if booking_type == 'TRIAL':
                    raise ValidationError("Trial capacity reached for this session. This trial session is now full. Please select another slot.")

                if booking_policy and booking_policy.allow_waitlist is not None:
                    allow_waitlist = booking_policy.allow_waitlist
                elif occurrence.class_template and occurrence.class_template.allow_waitlist is not None:
                    allow_waitlist = occurrence.class_template.allow_waitlist
                else:
                    allow_waitlist = True

                if booking_policy and booking_policy.waitlist_capacity is not None:
                    waitlist_cap = booking_policy.waitlist_capacity
                elif occurrence.waitlist_capacity is not None:
                    waitlist_cap = occurrence.waitlist_capacity
                elif occurrence.class_template and occurrence.class_template.default_waitlist_capacity is not None:
                    waitlist_cap = occurrence.class_template.default_waitlist_capacity
                else:
                    waitlist_cap = 20

                current_waitlist_count = Booking.objects.using(alias).filter(occurrence=occurrence, status='WAITLISTED').count()
                if not allow_waitlist or current_waitlist_count >= waitlist_cap:
                    raise ValidationError("Class occurrence capacity and waitlist are completely full. No further bookings accepted.")

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

            # Evaluate cancellation policy and cutoffs via BookingPolicyService
            from .services_booking_policy import BookingPolicyService
            applied_rule = cls.resolve_cancellation_rule(booking, db_alias=alias)

            policy_eval = BookingPolicyService.validate_member_cancellation(booking, cancelled_by_user=cancelled_by_user, db_alias=alias)
            within_cutoff = not policy_eval['is_late']
            session_action = policy_eval['session_action']
            treated_as_no_show = policy_eval['treated_as_no_show']
            session_units = Decimal('1.00')

            # If an explicit BookingCancellationRule exists, it overrides default policy
            if applied_rule:
                if applied_rule.min_minutes_before is not None:
                    within_cutoff = diff_minutes >= applied_rule.min_minutes_before
                session_action = applied_rule.session_action
                if applied_rule.cancellation_type == 'TREATED_AS_NO_SHOW':
                    treated_as_no_show = True

            # If auto-cancelled due to non-payment, never consume membership credits
            if reason_code == 'AUTO_CANCELLED_UNPAID' or getattr(booking, 'payment_status', 'PAID') in ['UNPAID', 'PENDING']:
                session_action = 'NO_ACTION'

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

            # Policy resolution & promotion cutoff check
            policy = cls.resolve_booking_policy(occurrence, db_alias=alias)
            if policy:
                if not policy.auto_waitlist_promotion:
                    return None
                if policy.waitlist_close_minutes_before:
                    diff_minutes = (occurrence.start_at - timezone.now()).total_seconds() / 60
                    if diff_minutes < policy.waitlist_close_minutes_before:
                        return None

            from .models_crm import TrialBooking
            trial_enrolled = TrialBooking.objects.using(alias).filter(
                class_occurrence_id=occurrence.id,
                status__in=['BOOKED', 'CONFIRMED', 'ATTENDED']
            ).count()
            enrolled_count = Booking.objects.using(alias).filter(
                occurrence=occurrence,
                status__in=['CONFIRMED', 'RESERVED', 'COMPLETED']
            ).count() + trial_enrolled

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
                can_book, restriction_reason = cls.can_member_book(cand_prof, organization, occurrence=occurrence, db_alias=alias)
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

                    if occurrence.start_at and membership.end_date < occurrence.start_at.date():
                        candidate.status = 'CANCELLED'
                        candidate.waitlist_position = None
                        candidate.save(using=alias, update_fields=['status', 'waitlist_position', 'updated_at'])
                        BookingWaitlistEvent.objects.using(alias).create(
                            booking=candidate,
                            occurrence=occurrence,
                            event_type='AUTO_CANCELLED',
                            reason=f"Membership expired before class occurrence date ({occurrence.start_at.date()})",
                            triggered_by_type='SYSTEM',
                            created_at=timezone.now()
                        )
                        continue

                    from .models_memberships import MembershipFreeze
                    if occurrence.start_at and MembershipFreeze.objects.using(alias).filter(
                        membership=membership,
                        status__in=['ACTIVE', 'SCHEDULED'],
                        freeze_from__lte=occurrence.start_at.date(),
                        freeze_until__gte=occurrence.start_at.date()
                    ).exists():
                        candidate.status = 'CANCELLED'
                        candidate.waitlist_position = None
                        candidate.save(using=alias, update_fields=['status', 'waitlist_position', 'updated_at'])
                        BookingWaitlistEvent.objects.using(alias).create(
                            booking=candidate,
                            occurrence=occurrence,
                            event_type='AUTO_CANCELLED',
                            reason='Promotion blocked: class occurs during membership freeze window',
                            triggered_by_type='SYSTEM',
                            created_at=timezone.now()
                        )
                        continue

                    # Revalidate PAR-Q requirement at promotion time if required by policy
                    bp = cls.resolve_booking_policy(occurrence, db_alias=alias)
                    parq_enforced = bool(bp and bp.require_parq)
                    if parq_enforced and getattr(membership, 'parq_status', 'PENDING') != 'COMPLETED':
                        candidate.status = 'CANCELLED'
                        candidate.waitlist_position = None
                        candidate.save(using=alias, update_fields=['status', 'waitlist_position', 'updated_at'])
                        BookingWaitlistEvent.objects.using(alias).create(
                            booking=candidate,
                            occurrence=occurrence,
                            event_type='AUTO_CANCELLED',
                            reason='Promotion blocked: PAR-Q form is pending completion and signature',
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

                    is_cross_branch = bool(membership.home_branch and occurrence.branch_id != membership.home_branch_id)
                    ent_qs = membership.entitlements.using(alias).filter(status='ACTIVE')
                    if ent_type_filter:
                        ent_qs = ent_qs.filter(entitlement_type=ent_type_filter)

                    if is_cross_branch and not ent_type_filter:
                        cross_ent = ent_qs.filter(
                            models.Q(entitlement_type='CROSS_BRANCH_SESSION') | models.Q(entitlement_type='CROSS_BRANCH'),
                            models.Q(is_unlimited=True) | models.Q(allocated_units__gt=models.F('consumed_units'))
                        ).first()
                        ent = cross_ent
                    else:
                        if not ent_type_filter:
                            home_ent = ent_qs.filter(
                                models.Q(is_unlimited=True) | models.Q(allocated_units__gt=models.F('consumed_units')),
                                entitlement_type='HOME_BRANCH_SESSION'
                            ).first()
                            ent = home_ent or ent_qs.filter(
                                models.Q(is_unlimited=True) | models.Q(allocated_units__gt=models.F('consumed_units'))
                            ).first()
                        else:
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

            if not from_occ_locked.class_template.allow_reschedule:
                raise ValidationError("Rescheduling is not permitted for this class.")

            if not to_occ_locked.class_template.allow_reschedule:
                raise ValidationError("Rescheduling is not permitted for the target class.")

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

            # 4. Check policy max_reschedules and cutoffs via BookingPolicyService
            from .services_booking_policy import BookingPolicyService
            BookingPolicyService.validate_member_reschedule(booking, to_occ_locked, rescheduled_by_user=rescheduled_by_user, db_alias=alias)

            organization = to_occ_locked.class_template.category.organization
            policy = BookingPolicySet.objects.using(alias).filter(organization=organization, status='ACTIVE').first()
            if policy and policy.max_reschedules:
                prior_reschedules = BookingReschedule.objects.using(alias).filter(booking=booking).count()
                if prior_reschedules >= policy.max_reschedules:
                    raise ValidationError(f"Maximum allowed reschedules ({policy.max_reschedules}) reached for this booking.")

            # 5. Check Package Class Access for new occurrence
            if booking.membership:
                parq_enforced = bool(policy and policy.require_parq)
                if parq_enforced and getattr(booking.membership, 'parq_status', 'PENDING') != 'COMPLETED':
                    raise ParqRequiredValidationError(
                        f"A completed and digitally signed PAR-Q is required before rescheduling under membership {booking.membership.membership_number}.",
                        code='PARQ_REQUIRED',
                        membership=booking.membership,
                        form=booking.membership.parq_form
                    )
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

            # 7B. Sync CRM TrialBooking if this is a trial session
            if booking.booking_type == 'TRIAL':
                from .models_crm import TrialBooking
                tb = TrialBooking.objects.using(alias).filter(
                    class_occurrence_id=from_occ_locked.id
                ).filter(
                    models.Q(user_profile=booking.user_profile) |
                    models.Q(lead__email_normalized__iexact=rescheduled_by_user.email if rescheduled_by_user else '') |
                    (models.Q(lead__phone_normalized=rescheduled_by_user.phone) if rescheduled_by_user and rescheduled_by_user.phone else models.Q(pk=None))
                ).first()
                if not tb:
                    tb = TrialBooking.objects.using(alias).filter(
                        models.Q(user_profile=booking.user_profile) |
                        models.Q(lead__email_normalized__iexact=rescheduled_by_user.email if rescheduled_by_user else '')
                    ).order_by('-created_at').first()
                if tb:
                    tb.class_occurrence_id = to_occ_locked.id
                    tb.scheduled_start = to_occ_locked.start_at
                    tb.scheduled_end = to_occ_locked.end_at
                    tb.branch = to_occ_locked.branch
                    tb.status = 'CONFIRMED'
                    tb.confirmation_status = 'CONFIRMED'
                    tb.save(using=alias, update_fields=['class_occurrence_id', 'scheduled_start', 'scheduled_end', 'branch', 'status', 'confirmation_status', 'updated_at'])

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
        latitude=None,
        longitude=None,
        accuracy=None,
        distance_meters=None,
        face_verified: bool = False,
        liveness_score=None,
        liveness_method: str = '',
        selfie_image: str = '',
        challenges_passed=None,
        db_alias: str = None,
    ) -> AttendanceRecord:
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        with transaction.atomic(using=alias):
            user_profile = booking.user_profile
            branch = booking.branch
            occurrence = booking.occurrence
            now = timezone.now()
            organization = occurrence.class_template.category.organization

            # Geofence distance calculation & validation
            computed_distance = distance_meters
            if computed_distance is None and latitude is not None and longitude is not None and branch.latitude is not None and branch.longitude is not None:
                import math
                try:
                    R = 6371000  # Earth radius in meters
                    phi1 = math.radians(float(branch.latitude))
                    phi2 = math.radians(float(latitude))
                    delta_phi = math.radians(float(latitude) - float(branch.latitude))
                    delta_lambda = math.radians(float(longitude) - float(branch.longitude))
                    a = math.sin(delta_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
                    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
                    computed_distance = round(R * c, 1)
                except Exception:
                    computed_distance = None

            is_within_geofence = True
            if computed_distance is not None and branch.geofence_radius_meters:
                if computed_distance > branch.geofence_radius_meters:
                    is_within_geofence = False
                    if branch.geofence_enforcement == 'STRICT':
                        raise ValidationError(
                            f"Geofence validation failed: You are {int(computed_distance)}m away from {branch.name}. "
                            f"Attendance check-in is restricted to within {branch.geofence_radius_meters}m."
                        )

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
                    trainer_latitude=latitude,
                    trainer_longitude=longitude,
                    trainer_accuracy_meters=accuracy,
                    is_within_geofence=is_within_geofence,
                    geofence_distance_meters=computed_distance,
                    face_verified=face_verified,
                    liveness_score=liveness_score,
                    liveness_method=liveness_method or ('FACE_LIVENESS' if face_verified else ''),
                    trainer_selfie_url=selfie_image or '',
                    liveness_challenges_passed=challenges_passed or [],
                )
            else:
                record.status = status
                record.check_in_status = 'SUCCESSFUL' if status in ['PRESENT', 'LATE'] else 'FAILED'
                record.check_in_method = check_in_method
                if status in ['PRESENT', 'LATE']:
                    record.check_in_at = now
                record.marked_by_user = marked_by_user
                if latitude is not None:
                    record.trainer_latitude = latitude
                if longitude is not None:
                    record.trainer_longitude = longitude
                if accuracy is not None:
                    record.trainer_accuracy_meters = accuracy
                record.is_within_geofence = is_within_geofence
                if computed_distance is not None:
                    record.geofence_distance_meters = computed_distance
                if face_verified:
                    record.face_verified = True
                if liveness_score is not None:
                    record.liveness_score = liveness_score
                if liveness_method:
                    record.liveness_method = liveness_method
                if selfie_image:
                    record.trainer_selfie_url = selfie_image
                if challenges_passed:
                    record.liveness_challenges_passed = challenges_passed
                record.save(using=alias)

            # Update booking lifecycle
            old_booking_status = booking.status
            if status in ['PRESENT', 'LATE']:
                booking.status = 'COMPLETED'
                booking.completed_at = now
                booking.save(using=alias, update_fields=['status', 'completed_at', 'updated_at'])

                # Reconcile entitlement session consumption if not already debited
                if booking.membership:
                    from .models_memberships import MembershipEntitlementLedger
                    has_consumption = MembershipEntitlementLedger.objects.using(alias).filter(
                        booking_id=booking.id,
                        transaction_type='CONSUMPTION'
                    ).exists()
                    if not has_consumption:
                        ent = booking.entitlement or MembershipEntitlement.objects.using(alias).filter(
                            membership=booking.membership,
                            status='ACTIVE'
                        ).first()
                        if ent:
                            try:
                                cls_name = occurrence.class_template.name if (occurrence and occurrence.class_template) else "Class"
                                ledger = MembershipLifecycleService.consume_entitlement(
                                    membership=booking.membership,
                                    entitlement_type=ent.entitlement_type,
                                    units=Decimal('1.00'),
                                    booking_id=booking.id,
                                    reason_text=f"Class attendance deduction ({cls_name})",
                                    created_by_user=marked_by_user,
                                    db_alias=alias,
                                )
                                if not booking.entitlement:
                                    booking.entitlement = ent
                                    booking.save(using=alias, update_fields=['entitlement', 'updated_at'])
                            except Exception as e:
                                logger.warning(f"Failed to reconcile attendance entitlement consumption: {e}")

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

                policy = cls.resolve_attendance_policy(occurrence, db_alias=alias)
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

                    # Evaluate AttendancePenaltyRule for entitlement deduction / restoration
                    penalty_rule = AttendancePenaltyRule.objects.using(alias).filter(
                        attendance_policy_set=policy,
                        status='ACTIVE'
                    ).order_by('priority').first()

                    if penalty_rule:
                        if penalty_rule.additional_penalty_units and penalty_rule.additional_penalty_units > 0 and booking.membership and booking.entitlement:
                            MembershipLifecycleService.consume_entitlement(
                                membership=booking.membership,
                                entitlement_type=booking.entitlement.entitlement_type,
                                units=penalty_rule.additional_penalty_units,
                                reason_text=f"No-show policy penalty deduction ({penalty_rule.rule_code})",
                                created_by_user=marked_by_user,
                                db_alias=alias,
                            )
                        if penalty_rule.restore_booked_session and booking.membership and booking.entitlement:
                            MembershipLifecycleService.reverse_entitlement(
                                membership=booking.membership,
                                entitlement_type=booking.entitlement.entitlement_type,
                                units=Decimal('1.00'),
                                booking_id=booking.id,
                                reason_text="No-show session restored per policy rule",
                                created_by_user=marked_by_user,
                                db_alias=alias,
                            )

                        AttendancePolicyEvent.objects.using(alias).create(
                            user_profile=user_profile,
                            booking=booking,
                            attendance_record=record,
                            attendance_policy_set=policy,
                            attendance_penalty_rule=penalty_rule,
                            event_type='PENALTY_APPLIED',
                            no_show_sequence=member_state.consecutive_no_show_count,
                            sessions_deducted=penalty_rule.additional_penalty_units or Decimal('0.00'),
                            reason=f"No-show penalty rule '{penalty_rule.rule_code}' applied",
                            triggered_by_type='SYSTEM',
                            created_at=now
                        )

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
        event_id: uuid.UUID = None,
        scanned_at = None,
        direction: str = None,
        reconciliation_status: str = 'NORMAL',
        token_hash: str = None,
        db_alias: str = None,
    ) -> AccessEvent:
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        dir_value = direction or ('EXIT' if event_type == 'EXIT' else 'ENTRY')

        with transaction.atomic(using=alias):
            now = timezone.now()
            scan_time = scanned_at or now

            # 1. Idempotency Check: if event_id is supplied and already recorded, return immediately
            if event_id:
                existing_event = AccessEvent.objects.using(alias).filter(id=event_id).first()
                if existing_event:
                    return existing_event

            # 2. Resolve booking if not explicitly provided
            if not booking:
                today = scan_time.date() if hasattr(scan_time, 'date') else now.date()
                booking = Booking.objects.using(alias).filter(
                    user_profile=user_profile,
                    branch=branch,
                    status='CONFIRMED',
                    occurrence__start_at__date=today
                ).first()

            # 3. Single-Use and Anti-Replay Check for ENTRY
            effective_event_type = event_type
            effective_reconciliation = reconciliation_status

            if dir_value == 'ENTRY':
                if booking:
                    already_present = AttendanceRecord.objects.using(alias).filter(
                        booking=booking,
                        status__in=['PRESENT', 'LATE', 'LATE_SHOW', 'ATTENDED']
                    ).exists()
                    if already_present:
                        effective_event_type = 'DENIED'
                        effective_reconciliation = 'REPLAY_ATTEMPT'

            event_kwargs = {
                'user_profile': user_profile,
                'booking': booking,
                'branch': branch,
                'event_type': effective_event_type,
                'direction': dir_value,
                'reconciliation_status': effective_reconciliation,
                'token_hash': token_hash,
                'device_reference': device_reference,
                'provider_reference': provider_reference,
                'event_at': scan_time,
                'created_at': now,
            }
            if event_id:
                event_kwargs['id'] = event_id

            event = AccessEvent.objects.using(alias).create(**event_kwargs)

            # 4. Handle ENTRY Auto-Attendance
            if dir_value == 'ENTRY' and effective_event_type == 'ENTRY':
                if booking:
                    occ = booking.occurrence
                    start_time = occ.start_at if occ else None
                    end_time = occ.end_at if occ else None

                    att_status = 'PRESENT'
                    if start_time and end_time:
                        booking_policy = cls.resolve_booking_policy(occ, db_alias=alias) if occ else None
                        late_mins = booking_policy.late_entry_minutes if (booking_policy and booking_policy.late_entry_minutes is not None) else 20

                        late_threshold = start_time + timedelta(minutes=late_mins)
                        if scan_time <= late_threshold:
                            att_status = 'PRESENT'
                        elif scan_time <= end_time:
                            att_status = 'LATE'
                        else:
                            att_status = 'LATE_SHOW'

                    cls.record_attendance(
                        booking=booking,
                        status=att_status,
                        check_in_method='QR' if (token_hash or (provider_reference or '').lower().find('qr') >= 0) else 'ACCESS_DEVICE',
                        db_alias=alias,
                    )
                    # Mark local AccessPass consumed if one exists
                    AccessPass.objects.using(alias).filter(booking=booking, status='ACTIVE').update(
                        status='CONSUMED_ENTERED',
                        consumed_at=scan_time,
                        updated_at=now
                    )

            # 5. Handle EXIT Auto-Checkout
            elif dir_value == 'EXIT' and effective_event_type == 'EXIT':
                att_record = AttendanceRecord.objects.using(alias).filter(
                    user_profile=user_profile,
                    branch=branch,
                    status__in=['PRESENT', 'LATE'],
                    check_out_at__isnull=True,
                ).order_by('-check_in_at').first()

                if att_record:
                    att_record.check_out_at = scan_time
                    att_record.attendance_completed = True
                    att_record.completion_verified_at = now
                    att_record.updated_at = now
                    att_record.save(
                        update_fields=['check_out_at', 'attendance_completed', 'completion_verified_at', 'updated_at'],
                        using=alias
                    )

                if booking:
                    AccessPass.objects.using(alias).filter(booking=booking).exclude(status='CONSUMED_EXITED').update(
                        status='CONSUMED_EXITED',
                        updated_at=now
                    )

            return event

    @classmethod
    def reconcile_past_occurrences_attendance(
        cls,
        branch=None,
        target_date=None,
        occurrence_id=None,
        user_profile=None,
        db_alias: str = None,
    ) -> dict:
        """
        Automated Attendance Lifecycle & Reconciliation:
        Evaluates past class occurrences where the session has ended (occurrence.end_at <= now).
        Calculates and applies attendance policies, criteria, and discipline rules:
        - If a member checked in: synchronizes booking status to COMPLETED/ATTENDED.
        - If a member NEVER checked in and class has ended:
          * Automatically marks booking as NO_SHOW.
          * Creates AttendanceRecord(status='NO_SHOW', check_in_status='NOT_ATTEMPTED', check_in_method='SYSTEM').
          * Evaluates AttendancePenaltyRule and updates MemberAttendanceState.
          * Applies restrictions (SINGLE_BOOKING) if restriction_threshold reached.
          * Logs AttendancePolicyEvent and BookingStatusHistory.
        - Synchronizes un-attended past TrialBookings to NO_SHOW.
        """
        alias = db_alias or get_current_tenant_db_alias() or 'default'
        now = timezone.now()

        bookings_qs = (
            Booking.objects.using(alias)
            .filter(
                occurrence__end_at__lte=now,
                status__in=['CONFIRMED', 'RESERVED'],
            )
            .select_related(
                'occurrence',
                'occurrence__class_template',
                'occurrence__class_template__category__organization',
                'branch',
                'user_profile',
                'membership',
                'entitlement',
            )
        )

        if branch:
            if isinstance(branch, (str, uuid.UUID)):
                bookings_qs = bookings_qs.filter(branch_id=branch)
            else:
                bookings_qs = bookings_qs.filter(branch=branch)

        if occurrence_id:
            bookings_qs = bookings_qs.filter(occurrence_id=occurrence_id)

        if user_profile:
            bookings_qs = bookings_qs.filter(user_profile=user_profile)

        if target_date:
            bookings_qs = bookings_qs.filter(occurrence__occurrence_date=target_date)

        reconciled_no_show = 0
        reconciled_attended = 0

        for b in list(bookings_qs):
            occ = b.occurrence
            if not occ or not occ.end_at:
                continue

            policy = cls.resolve_attendance_policy(occ, db_alias=alias)
            grace_mins = policy.no_show_mark_after_minutes if policy else 0
            cutoff_time = occ.end_at + timedelta(minutes=grace_mins)

            if now < cutoff_time:
                continue

            # Check if an attendance record exists
            att = AttendanceRecord.objects.using(alias).filter(booking=b).first()
            if att and att.check_in_status == 'SUCCESSFUL':
                if b.status != 'COMPLETED':
                    b.status = 'COMPLETED'
                    b.completed_at = att.check_in_at or now
                    b.save(using=alias, update_fields=['status', 'completed_at', 'updated_at'])
                reconciled_attended += 1
            else:
                # Mark as NO_SHOW automatically
                cls.record_attendance(
                    booking=b,
                    status='NO_SHOW',
                    check_in_method='SYSTEM',
                    db_alias=alias,
                )
                reconciled_no_show += 1

        # Reconcile un-attended past TrialBookings
        from .models_crm import TrialBooking
        from .services_crm import CRMLeadService

        trials_qs = (
            TrialBooking.objects.using(alias)
            .filter(
                scheduled_end__lte=now,
                status__in=['BOOKED', 'CONFIRMED'],
            )
            .select_related('branch', 'lead')
        )
        if branch:
            if isinstance(branch, (str, uuid.UUID)):
                trials_qs = trials_qs.filter(branch_id=branch)
            else:
                trials_qs = trials_qs.filter(branch=branch)

        for t in list(trials_qs):
            try:
                CRMLeadService.mark_trial_no_show(trial=t, actor_user=None, db_alias=alias)
                reconciled_no_show += 1
            except Exception:
                pass

        return {
            'reconciled_no_show': reconciled_no_show,
            'reconciled_attended': reconciled_attended,
        }


BookingAttendanceService = BookingWaitlistAttendanceService

