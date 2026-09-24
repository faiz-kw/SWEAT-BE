import uuid
from typing import Optional, Set
from django.db import models
from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404
from django.utils import timezone

from .context import get_tenant_db_alias
from .permissions import RequireActiveTenantAndOrg, TenantRBACPermission
from .models_bookings import (
    BookingPolicySet,
    BookingCancellationRule,
    RewardRedemptionRule,
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
from .models_crm import UserProfile, TrialBooking
from .models_memberships import Membership
from .models_org import Branch
from .models_rbac import RoleAssignment
from .services_crm import CRMLeadService
from .serializers_bookings import (
    BookingPolicySetSerializer,
    BookingCancellationRuleSerializer,
    RewardRedemptionRuleSerializer,
    AttendancePolicySetSerializer,
    AttendancePenaltyRuleSerializer,
    MemberAttendanceStateSerializer,
    AttendancePolicyEventSerializer,
    BookingListSerializer,
    BookingSerializer,
    BookingStatusHistorySerializer,
    BookingRescheduleSerializer,
    BookingCancellationSerializer,
    BookingWaitlistEventSerializer,
    AttendanceRecordSerializer,
    AccessEventSerializer,
)
from .services_bookings import BookingWaitlistAttendanceService


def _get_db(request):
    return (
        get_tenant_db_alias()
        or getattr(getattr(request, 'user', None), '_db_alias', None)
        or getattr(request, '_tenant_db_alias', None)
        or 'default'
    )


def get_user_effective_branch_ids(user, db_alias: str = 'default') -> Optional[Set[str]]:
    """
    Derives the effective permitted branch IDs for the user based on active RoleAssignments,
    role scopes, and UserBranch records (Correction 1: role-name agnostic).
    Returns:
        None: Organization-wide access (all branches permitted)
        Set[str]: Exact set of branch UUID strings the user is permitted to access
    """
    if getattr(user, 'is_superuser', False):
        return None

    from .models_rbac import RoleAssignment
    from .models_users import UserBranch

    assignments = list(
        RoleAssignment.objects.using(db_alias)
        .filter(user=user, is_active=True)
        .select_related('role', 'branch')
    )

    # If user has no assignments, return empty set (restricted)
    if not assignments:
        # Check UserBranch fallback
        ub_ids = set(
            str(b) for b in UserBranch.objects.using(db_alias)
            .filter(user=user, is_active=True)
            .values_list('branch_id', flat=True)
            if b
        )
        return ub_ids

    # If any active assignment has ORG scope, user has org-wide scope
    for ra in assignments:
        if ra.role and ra.role.is_active and ra.role.scope == 'ORG':
            return None

    permitted = set()
    for ra in assignments:
        if ra.role and ra.role.is_active:
            if ra.branch_id:
                permitted.add(str(ra.branch_id))

    # Also merge UserBranch records
    for bid in UserBranch.objects.using(db_alias).filter(user=user, is_active=True).values_list('branch_id', flat=True):
        if bid:
            permitted.add(str(bid))

    return permitted


class BookingViewSet(viewsets.ModelViewSet):
    serializer_class = BookingSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'ops'
    required_submodule = 'bookings'
    required_permission = 'ops.bookings.view'
    permission_action_map = {
        'create': 'ops.bookings.create',
        'update': 'ops.bookings.edit',
        'partial_update': 'ops.bookings.edit',
        'destroy': 'ops.bookings.delete',
        'cancel': 'ops.bookings.edit',
        'reschedule': 'ops.bookings.edit',
        'record_attendance': 'ops.bookings.edit',
        'promote_waitlist': 'ops.bookings.edit',
    }

    def get_serializer_class(self):
        # Use lightweight list serializer for list actions to avoid heavy prefetch chains
        if self.action == 'list':
            return BookingListSerializer
        return BookingSerializer

    def get_queryset(self):
        alias = _get_db(self.request)

        # For list: only the joins needed by BookingListSerializer (no nested prefetch)
        # For retrieve/detail: add full prefetch_related for nested history/events
        base_select = [
            'user_profile', 'user_profile__user', 'occurrence',
            'occurrence__class_template', 'branch', 'membership', 'membership__package', 'entitlement'
        ]
        qs = Booking.objects.using(alias).select_related(*base_select)

        if getattr(self, 'action', 'list') != 'list':
            qs = qs.prefetch_related('reschedules', 'status_history', 'cancellations', 'waitlist_events')

        # Branch scope enforcement
        permitted_branches = get_user_effective_branch_ids(self.request.user, alias)
        if permitted_branches is not None:
            qs = qs.filter(branch_id__in=permitted_branches)

        # If user is a member (has MEMBER role and no staff roles), scope to own profile
        user_roles = set(
            RoleAssignment.objects.using(alias)
            .filter(user=self.request.user, is_active=True)
            .values_list('role__code', flat=True)
        )
        staff_roles = {'ORG_ADMIN', 'BRANCH_MANAGER', 'FRONT_DESK', 'TRAINER', 'SALES_REP', 'FINANCE_ADMIN'}
        if 'MEMBER' in user_roles and not (user_roles & staff_roles):
            qs = qs.filter(user_profile__user=self.request.user)

        status_filter = self.request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)

        branch_id = self.request.query_params.get('branch_id')
        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        occurrence_id = self.request.query_params.get('occurrence_id')
        if occurrence_id:
            qs = qs.filter(occurrence_id=occurrence_id)

        user_id = self.request.query_params.get('user_id') or self.request.query_params.get('user_profile_id')
        if user_id:
            qs = qs.filter(user_profile_id=user_id)

        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(
                models.Q(booking_number__icontains=search) |
                models.Q(user_profile__first_name_snapshot__icontains=search) |
                models.Q(user_profile__last_name_snapshot__icontains=search) |
                models.Q(user_profile__user__email__icontains=search) |
                models.Q(occurrence__class_template__name__icontains=search)
            )

        return qs.order_by('-booked_at')

    def list(self, request, *args, **kwargs):
        alias = _get_db(request)
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
        results = list(serializer.data)

        # Unified Operations View: Include TrialBookings as first-class rows
        include_trials = request.query_params.get('include_trials', 'true').lower() in ['true', '1', 'yes']
        user_roles = set(
            RoleAssignment.objects.using(alias)
            .filter(user=request.user, is_active=True)
            .values_list('role__code', flat=True)
        )
        staff_roles = {'ORG_ADMIN', 'BRANCH_MANAGER', 'FRONT_DESK', 'TRAINER', 'SALES_REP', 'FINANCE_ADMIN'}
        is_member_only = 'MEMBER' in user_roles and not (user_roles & staff_roles)

        if include_trials and not is_member_only:
            trial_qs = TrialBooking.objects.using(alias).select_related('lead', 'branch', 'assigned_trainer_profile')

            permitted_branches = get_user_effective_branch_ids(request.user, alias)
            if permitted_branches is not None:
                trial_qs = trial_qs.filter(branch_id__in=permitted_branches)

            branch_id = request.query_params.get('branch_id')
            if branch_id:
                trial_qs = trial_qs.filter(branch_id=branch_id)

            occurrence_id = request.query_params.get('occurrence_id')
            if occurrence_id:
                trial_qs = trial_qs.filter(class_occurrence_id=occurrence_id)

            status_filter = request.query_params.get('status')
            if status_filter:
                if status_filter == 'CONFIRMED':
                    trial_qs = trial_qs.filter(models.Q(status='CONFIRMED') | models.Q(confirmation_status='CONFIRMED'))
                else:
                    trial_qs = trial_qs.filter(status=status_filter)

            search = request.query_params.get('search')
            if search:
                trial_qs = trial_qs.filter(
                    models.Q(lead__first_name__icontains=search) |
                    models.Q(lead__last_name__icontains=search) |
                    models.Q(lead__email_normalized__icontains=search) |
                    models.Q(lead__phone_normalized__icontains=search) |
                    models.Q(notes__icontains=search)
                )

            occ_ids = [t.class_occurrence_id for t in trial_qs if t.class_occurrence_id]
            occ_map = {}
            if occ_ids:
                for occ in ClassOccurrence.objects.using(alias).filter(id__in=occ_ids).select_related('class_template'):
                    occ_map[occ.id] = {
                        'title': occ.class_template.name if occ.class_template else 'Class Session',
                        'date': occ.occurrence_date.isoformat() if occ.occurrence_date else None,
                        'start_at': occ.start_at.isoformat() if occ.start_at else None,
                        'end_at': occ.end_at.isoformat() if occ.end_at else None,
                    }

            for t in trial_qs:
                occ_info = occ_map.get(t.class_occurrence_id, {})
                title = occ_info.get('title')
                if search and title and search.lower() not in title.lower() and not (
                    search.lower() in (t.lead.first_name or '').lower() or
                    search.lower() in (t.lead.last_name or '').lower() or
                    search.lower() in (t.lead.email_normalized or '').lower() or
                    f"TRL-{str(t.id)[:8].upper()}".lower().find(search.lower()) != -1
                ):
                    continue

                trial_item = {
                    'id': str(t.id),
                    'is_trial': True,
                    'booking_number': f"TRL-{str(t.id)[:8].upper()}",
                    'status': t.status,
                    'confirmation_status': t.confirmation_status,
                    'booking_type': 'PROSPECT',
                    'booking_source': 'TRIAL',
                    'waitlist_position': None,
                    'booked_at': t.created_at.isoformat() if t.created_at else None,
                    'cancelled_at': t.updated_at.isoformat() if t.status == 'CANCELLED' else None,
                    'completed_at': t.updated_at.isoformat() if t.status == 'ATTENDED' else None,
                    'user_profile': None,
                    'user_profile_name': f"{t.lead.first_name} {t.lead.last_name}".strip() if t.lead else 'Prospect',
                    'user_profile_email': t.lead.email_normalized if t.lead else None,
                    'occurrence': str(t.class_occurrence_id) if t.class_occurrence_id else None,
                    'occurrence_title': occ_info.get('title', 'Trial Session'),
                    'occurrence_date': occ_info.get('date', t.scheduled_start.date().isoformat() if t.scheduled_start else None),
                    'occurrence_start_at': occ_info.get('start_at', t.scheduled_start.isoformat() if t.scheduled_start else None),
                    'occurrence_end_at': occ_info.get('end_at', t.scheduled_end.isoformat() if t.scheduled_end else None),
                    'branch': str(t.branch_id) if t.branch_id else None,
                    'branch_name': t.branch.name if t.branch else 'Main Studio',
                    'membership': None,
                    'package_name': 'Trial Session',
                    'entitlement': None,
                    'lead_id': str(t.lead_id) if t.lead_id else None,
                    'created_at': t.created_at.isoformat() if t.created_at else None,
                    'updated_at': t.updated_at.isoformat() if t.updated_at else None,
                }
                results.append(trial_item)

        results.sort(key=lambda x: x.get('booked_at') or x.get('created_at') or '', reverse=True)
        return Response(results)

    def retrieve(self, request, *args, **kwargs):
        alias = _get_db(request)
        pk = kwargs.get('pk')
        try:
            return super().retrieve(request, *args, **kwargs)
        except Exception:
            trial = TrialBooking.objects.using(alias).select_related('lead', 'branch', 'assigned_trainer_profile').filter(id=pk).first()
            if not trial:
                return Response({'detail': 'Booking not found.'}, status=status.HTTP_404_NOT_FOUND)

            occ = ClassOccurrence.objects.using(alias).filter(id=trial.class_occurrence_id).select_related('class_template').first() if trial.class_occurrence_id else None
            data = {
                'id': str(trial.id),
                'is_trial': True,
                'booking_number': f"TRL-{str(trial.id)[:8].upper()}",
                'status': trial.status,
                'confirmation_status': trial.confirmation_status,
                'booking_type': 'PROSPECT',
                'booking_source': 'TRIAL',
                'member_name': f"{trial.lead.first_name} {trial.lead.last_name}".strip() if trial.lead else 'Prospect',
                'member_email': trial.lead.email_normalized if trial.lead else None,
                'member_phone': trial.lead.phone_normalized if trial.lead else None,
                'member_number': 'PROSPECT',
                'user_profile_name': f"{trial.lead.first_name} {trial.lead.last_name}".strip() if trial.lead else 'Prospect',
                'user_profile_email': trial.lead.email_normalized if trial.lead else None,
                'occurrence': str(trial.class_occurrence_id) if trial.class_occurrence_id else None,
                'occurrence_title': occ.class_template.name if occ and occ.class_template else 'Trial Session',
                'class_name': occ.class_template.name if occ and occ.class_template else 'Trial Session',
                'occurrence_date': occ.occurrence_date.isoformat() if occ and occ.occurrence_date else (trial.scheduled_start.date().isoformat() if trial.scheduled_start else None),
                'occurrence_start_at': trial.scheduled_start.isoformat() if trial.scheduled_start else None,
                'occurrence_end_at': trial.scheduled_end.isoformat() if trial.scheduled_end else None,
                'start_at': trial.scheduled_start.isoformat() if trial.scheduled_start else None,
                'end_at': trial.scheduled_end.isoformat() if trial.scheduled_end else None,
                'start_time': trial.scheduled_start.isoformat() if trial.scheduled_start else None,
                'end_time': trial.scheduled_end.isoformat() if trial.scheduled_end else None,
                'branch': str(trial.branch_id) if trial.branch_id else None,
                'branch_name': trial.branch.name if trial.branch else 'Main Studio',
                'membership': None,
                'package_name': 'Trial Session',
                'lead_id': str(trial.lead_id) if trial.lead_id else None,
                'booked_at': trial.created_at.isoformat() if trial.created_at else None,
                'created_at': trial.created_at.isoformat() if trial.created_at else None,
                'updated_at': trial.updated_at.isoformat() if trial.updated_at else None,
                'reschedules': [],
                'status_history': [],
                'cancellations': [],
                'waitlist_events': [],
            }
            return Response(data)

    def create(self, request, *args, **kwargs):
        alias = _get_db(request)
        user_profile_id = request.data.get('user_profile')
        occurrence_id = request.data.get('occurrence')
        booking_type = request.data.get('booking_type', 'MEMBER')
        booking_source = request.data.get('booking_source', 'WEB')
        membership_id = request.data.get('membership')

        # Auto-resolve user_profile for logged-in member if not explicitly passed
        if not user_profile_id:
            profile = UserProfile.objects.using(alias).filter(user=request.user).first()
            if profile:
                user_profile_id = str(profile.id)

        if not user_profile_id or not occurrence_id:
            return Response({'detail': 'user_profile and occurrence are required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user_profile = UserProfile.objects.using(alias).select_related('user').get(id=user_profile_id)
        except UserProfile.DoesNotExist:
            return Response({'detail': f"UserProfile with id '{user_profile_id}' not found in tenant database."}, status=status.HTTP_404_NOT_FOUND)

        # Member self-booking safety: non-staff member cannot book on behalf of other users
        user_roles = set(
            RoleAssignment.objects.using(alias)
            .filter(user=request.user, is_active=True)
            .values_list('role__code', flat=True)
        )
        staff_roles = {'ORG_ADMIN', 'BRANCH_MANAGER', 'FRONT_DESK', 'TRAINER', 'SALES_REP', 'FINANCE_ADMIN'}
        if 'MEMBER' in user_roles and not (user_roles & staff_roles):
            if str(user_profile.user_id) != str(request.user.id):
                return Response(
                    {'detail': 'Members can only create bookings for their own profile.'},
                    status=status.HTTP_403_FORBIDDEN
                )

        try:
            occurrence = ClassOccurrence.objects.using(alias).select_related('class_template__category__organization', 'branch').get(id=occurrence_id)
        except ClassOccurrence.DoesNotExist:
            return Response({'detail': f"ClassOccurrence with id '{occurrence_id}' not found in tenant database."}, status=status.HTTP_404_NOT_FOUND)

        # Branch scope authorization check
        permitted_branches = get_user_effective_branch_ids(request.user, alias)
        if permitted_branches is not None and str(occurrence.branch_id) not in permitted_branches:
            return Response(
                {'detail': f"User lacks permission to book classes for branch '{occurrence.branch.name}'."},
                status=status.HTTP_403_FORBIDDEN
            )

        membership = None
        if membership_id:
            try:
                membership = Membership.objects.using(alias).get(id=membership_id)
            except Membership.DoesNotExist:
                return Response({'detail': f"Membership with id '{membership_id}' not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            booking = BookingWaitlistAttendanceService.create_booking(
                user_profile=user_profile,
                occurrence=occurrence,
                booking_type=booking_type,
                booking_source=booking_source,
                membership=membership,
                created_by_user=request.user,
                db_alias=alias,
            )
            serializer = self.get_serializer(booking)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        except ValidationError as e:
            detail = e.message_dict if hasattr(e, 'message_dict') else (e.messages[0] if hasattr(e, 'messages') and e.messages else str(e))
            return Response({'detail': detail}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='cancel')
    def cancel(self, request, pk=None):
        alias = _get_db(request)
        try:
            booking = Booking.objects.using(alias).select_related('occurrence__class_template__category__organization', 'branch').get(id=pk)
        except Booking.DoesNotExist:
            trial = TrialBooking.objects.using(alias).filter(id=pk).first()
            if trial:
                permitted_branches = get_user_effective_branch_ids(request.user, alias)
                if permitted_branches is not None and str(trial.branch_id) not in permitted_branches:
                    return Response({'detail': 'User lacks permission to modify bookings for this branch.'}, status=status.HTTP_403_FORBIDDEN)
                reason = request.data.get('reason_text') or request.data.get('reason_code') or 'Cancelled from Operations'
                try:
                    cancelled = CRMLeadService.cancel_trial(trial=trial, reason=reason, actor_user=request.user, db_alias=alias)
                    return Response({'status': cancelled.status, 'detail': 'Trial booking cancelled successfully.'}, status=status.HTTP_200_OK)
                except ValidationError as e:
                    return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
            return Response({'detail': 'Booking not found.'}, status=status.HTTP_404_NOT_FOUND)

        # Branch scope check
        permitted_branches = get_user_effective_branch_ids(request.user, alias)
        if permitted_branches is not None and str(booking.branch_id) not in permitted_branches:
            return Response({'detail': 'User lacks permission to modify bookings for this branch.'}, status=status.HTTP_403_FORBIDDEN)

        reason_code = request.data.get('reason_code', 'MEMBER_REQUEST')
        reason_text = request.data.get('reason_text', '')

        try:
            cancellation = BookingWaitlistAttendanceService.cancel_booking(
                booking=booking,
                cancelled_by_user=request.user,
                reason_code=reason_code,
                reason_text=reason_text,
                db_alias=alias,
            )
            serializer = BookingCancellationSerializer(cancellation)
            return Response(serializer.data, status=status.HTTP_200_OK)
        except ValidationError as e:
            detail = e.message_dict if hasattr(e, 'message_dict') else (e.messages[0] if hasattr(e, 'messages') and e.messages else str(e))
            return Response({'detail': detail}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='reschedule')
    def reschedule(self, request, pk=None):
        alias = _get_db(request)
        try:
            booking = Booking.objects.using(alias).select_related('occurrence', 'branch').get(id=pk)
        except Booking.DoesNotExist:
            trial = TrialBooking.objects.using(alias).filter(id=pk).first()
            if trial:
                permitted_branches = get_user_effective_branch_ids(request.user, alias)
                if permitted_branches is not None and str(trial.branch_id) not in permitted_branches:
                    return Response({'detail': 'User lacks permission to modify bookings for this branch.'}, status=status.HTTP_403_FORBIDDEN)
                to_occurrence_id = request.data.get('to_occurrence_id') or request.data.get('new_occurrence_id')
                reason = request.data.get('reason_text') or request.data.get('reason_code') or 'Rescheduled from Operations'
                try:
                    rescheduled = CRMLeadService.reschedule_trial(trial=trial, new_class_occurrence_id=to_occurrence_id, reason=reason, actor_user=request.user, db_alias=alias)
                    return Response({
                        'id': str(rescheduled.id),
                        'is_trial': True,
                        'status': rescheduled.status,
                        'occurrence': str(rescheduled.class_occurrence_id),
                        'scheduled_start': rescheduled.scheduled_start.isoformat(),
                        'detail': 'Trial booking rescheduled successfully.'
                    }, status=status.HTTP_200_OK)
                except ValidationError as e:
                    return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
            return Response({'detail': 'Booking not found.'}, status=status.HTTP_404_NOT_FOUND)

        # Branch scope check on current booking
        permitted_branches = get_user_effective_branch_ids(request.user, alias)
        if permitted_branches is not None and str(booking.branch_id) not in permitted_branches:
            return Response({'detail': 'User lacks permission to modify bookings for this branch.'}, status=status.HTTP_403_FORBIDDEN)

        to_occurrence_id = request.data.get('to_occurrence_id') or request.data.get('new_occurrence_id')
        if not to_occurrence_id:
            return Response({'detail': 'to_occurrence_id is required for rescheduling.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            to_occurrence = ClassOccurrence.objects.using(alias).select_related(
                'class_template__category__organization', 'branch'
            ).get(id=to_occurrence_id)
        except ClassOccurrence.DoesNotExist:
            return Response({'detail': f"Target class occurrence '{to_occurrence_id}' not found."}, status=status.HTTP_404_NOT_FOUND)

        # Branch scope check on target occurrence
        if permitted_branches is not None and str(to_occurrence.branch_id) not in permitted_branches:
            return Response({'detail': f"User lacks permission for target branch '{to_occurrence.branch.name}'."}, status=status.HTTP_403_FORBIDDEN)

        reason_code = request.data.get('reason_code', 'MEMBER_REQUEST')
        reason_text = request.data.get('reason_text', '')

        try:
            rescheduled_booking = BookingWaitlistAttendanceService.reschedule_booking(
                booking=booking,
                to_occurrence=to_occurrence,
                rescheduled_by_user=request.user,
                reason_code=reason_code,
                reason_text=reason_text,
                db_alias=alias,
            )
            serializer = self.get_serializer(rescheduled_booking)
            return Response(serializer.data, status=status.HTTP_200_OK)
        except ValidationError as e:
            detail = e.message_dict if hasattr(e, 'message_dict') else (e.messages[0] if hasattr(e, 'messages') and e.messages else str(e))
            return Response({'detail': detail}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='record-attendance')
    def record_attendance(self, request, pk=None):
        alias = _get_db(request)
        try:
            booking = Booking.objects.using(alias).select_related(
                'user_profile', 'branch', 'occurrence__class_template__category__organization'
            ).get(id=pk)
        except Booking.DoesNotExist:
            trial = TrialBooking.objects.using(alias).filter(id=pk).first()
            if trial:
                permitted_branches = get_user_effective_branch_ids(request.user, alias)
                if permitted_branches is not None and str(trial.branch_id) not in permitted_branches:
                    return Response({'detail': 'User lacks permission to modify bookings for this branch.'}, status=status.HTTP_403_FORBIDDEN)
                att_status = request.data.get('status', 'PRESENT')
                notes = request.data.get('notes')
                try:
                    if att_status in ['PRESENT', 'ATTENDED']:
                        attended = CRMLeadService.mark_trial_attended(trial=trial, notes=notes, actor_user=request.user, db_alias=alias)
                        return Response({'status': attended.status, 'detail': 'Trial marked as attended.'}, status=status.HTTP_200_OK)
                    elif att_status in ['NO_SHOW', 'ABSENT']:
                        noshow = CRMLeadService.mark_trial_no_show(trial=trial, notes=notes, actor_user=request.user, db_alias=alias)
                        return Response({'status': noshow.status, 'detail': 'Trial marked as no show.'}, status=status.HTTP_200_OK)
                    return Response({'detail': f"Unsupported attendance status '{att_status}' for trial."}, status=status.HTTP_400_BAD_REQUEST)
                except ValidationError as e:
                    return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
            return Response({'detail': 'Booking not found.'}, status=status.HTTP_404_NOT_FOUND)

        att_status = request.data.get('status', 'PRESENT')
        check_in_method = request.data.get('check_in_method', 'MANUAL')
        latitude = request.data.get('latitude') or request.data.get('trainer_latitude')
        longitude = request.data.get('longitude') or request.data.get('trainer_longitude')
        accuracy = request.data.get('accuracy') or request.data.get('trainer_accuracy_meters')
        distance_meters = request.data.get('distance_meters') or request.data.get('geofence_distance_meters')
        face_verified = bool(request.data.get('face_verified', False))
        liveness_score = request.data.get('liveness_score')
        liveness_method = request.data.get('liveness_method', '')
        selfie_image = request.data.get('selfie_image') or request.data.get('trainer_selfie_url', '')
        challenges_passed = request.data.get('challenges_passed') or request.data.get('liveness_challenges_passed', [])

        try:
            record = BookingWaitlistAttendanceService.record_attendance(
                booking=booking,
                status=att_status,
                check_in_method=check_in_method,
                marked_by_user=request.user,
                latitude=latitude,
                longitude=longitude,
                accuracy=accuracy,
                distance_meters=distance_meters,
                face_verified=face_verified,
                liveness_score=liveness_score,
                liveness_method=liveness_method,
                selfie_image=selfie_image,
                challenges_passed=challenges_passed,
                db_alias=alias,
            )
            serializer = AttendanceRecordSerializer(record)
            return Response(serializer.data, status=status.HTTP_200_OK)
        except ValidationError as e:
            detail = e.message_dict if hasattr(e, 'message_dict') else (e.messages[0] if hasattr(e, 'messages') and e.messages else str(e))
            return Response({'detail': detail}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='confirm')
    def confirm(self, request, pk=None):
        alias = _get_db(request)
        trial = TrialBooking.objects.using(alias).filter(id=pk).first()
        if trial:
            permitted_branches = get_user_effective_branch_ids(request.user, alias)
            if permitted_branches is not None and str(trial.branch_id) not in permitted_branches:
                return Response({'detail': 'User lacks permission to modify bookings for this branch.'}, status=status.HTTP_403_FORBIDDEN)
            channel = request.data.get('channel', 'MANUAL')
            notes = request.data.get('notes')
            try:
                confirmed = CRMLeadService.confirm_trial(trial=trial, channel=channel, notes=notes, actor_user=request.user, db_alias=alias)
                return Response({'status': confirmed.status, 'confirmation_status': confirmed.confirmation_status}, status=status.HTTP_200_OK)
            except ValidationError as e:
                return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'detail': 'Booking not found or not a trial.'}, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=['post'], url_path='promote-waitlist')
    def promote_waitlist(self, request):
        alias = _get_db(request)
        occurrence_id = request.data.get('occurrence_id')
        if not occurrence_id:
            return Response({'detail': 'occurrence_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            occurrence = ClassOccurrence.objects.using(alias).select_related(
                'class_template__category__organization', 'branch'
            ).get(id=occurrence_id)
        except ClassOccurrence.DoesNotExist:
            return Response({'detail': 'Class occurrence not found.'}, status=status.HTTP_404_NOT_FOUND)

        promoted = BookingWaitlistAttendanceService.auto_promote_from_waitlist(occurrence, db_alias=alias)
        if promoted:
            return Response(self.get_serializer(promoted).data, status=status.HTTP_200_OK)
        return Response({'detail': 'No eligible waitlisted members available for promotion or capacity full.'}, status=status.HTTP_200_OK)


class BookingPolicySetViewSet(viewsets.ModelViewSet):
    serializer_class = BookingPolicySetSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'ops'
    required_submodule = 'classes'
    required_permission = 'ops.classes.view'
    permission_action_map = {
        'create': 'ops.classes.create',
        'update': 'ops.classes.edit',
        'partial_update': 'ops.classes.edit',
        'destroy': 'ops.classes.delete',
    }

    def get_queryset(self):
        alias = _get_db(self.request)
        qs = BookingPolicySet.objects.using(alias).all()
        branch_id = self.request.query_params.get('branch_id')
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        status_param = self.request.query_params.get('status')
        if status_param:
            qs = qs.filter(status=status_param)
        return qs.order_by('-created_at')

    def perform_create(self, serializer):
        alias = _get_db(self.request)
        serializer.save(created_by_user=self.request.user)


class BookingCancellationRuleViewSet(viewsets.ModelViewSet):
    serializer_class = BookingCancellationRuleSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'ops'
    required_submodule = 'classes'
    required_permission = 'ops.classes.view'
    permission_action_map = {
        'create': 'ops.classes.create',
        'update': 'ops.classes.edit',
        'partial_update': 'ops.classes.edit',
        'destroy': 'ops.classes.delete',
    }

    def get_queryset(self):
        alias = _get_db(self.request)
        return BookingCancellationRule.objects.using(alias).all().order_by('priority', '-created_at')


class RewardRedemptionRuleViewSet(viewsets.ModelViewSet):
    serializer_class = RewardRedemptionRuleSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'ops'
    required_submodule = 'bookings'
    required_permission = 'ops.bookings.view'
    permission_action_map = {
        'create': 'ops.bookings.create',
        'update': 'ops.bookings.edit',
        'partial_update': 'ops.bookings.edit',
        'destroy': 'ops.bookings.delete',
    }

    def get_queryset(self):
        alias = _get_db(self.request)
        return RewardRedemptionRule.objects.using(alias).all().order_by('-created_at')


class AttendancePolicySetViewSet(viewsets.ModelViewSet):
    serializer_class = AttendancePolicySetSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'members'
    required_submodule = 'attendance'
    required_permission = 'members.attendance.view'
    permission_action_map = {
        'create': 'members.attendance.create',
        'update': 'members.attendance.edit',
        'partial_update': 'members.attendance.edit',
        'destroy': 'members.attendance.delete',
    }

    def get_queryset(self):
        alias = _get_db(self.request)
        return AttendancePolicySet.objects.using(alias).all().order_by('-created_at')


class AttendancePenaltyRuleViewSet(viewsets.ModelViewSet):
    serializer_class = AttendancePenaltyRuleSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'members'
    required_submodule = 'attendance'
    required_permission = 'members.attendance.view'
    permission_action_map = {
        'create': 'members.attendance.create',
        'update': 'members.attendance.edit',
        'partial_update': 'members.attendance.edit',
        'destroy': 'members.attendance.delete',
    }

    def get_queryset(self):
        alias = _get_db(self.request)
        return AttendancePenaltyRule.objects.using(alias).all().order_by('priority', '-created_at')


class MemberAttendanceStateViewSet(viewsets.ModelViewSet):
    serializer_class = MemberAttendanceStateSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'members'
    required_submodule = 'attendance'
    required_permission = 'members.attendance.view'
    permission_action_map = {
        'create': 'members.attendance.create',
        'update': 'members.attendance.edit',
        'partial_update': 'members.attendance.edit',
        'destroy': 'members.attendance.delete',
        'reset_restriction': 'members.attendance.edit',
    }

    def get_queryset(self):
        alias = _get_db(self.request)
        return MemberAttendanceState.objects.using(alias).select_related(
            'user_profile', 'user_profile__user', 'attendance_policy_set'
        ).all()

    @action(detail=True, methods=['post'], url_path='reset-restriction')
    def reset_restriction(self, request, pk=None):
        alias = _get_db(request)
        state = self.get_object()
        policy = state.attendance_policy_set
        state.booking_mode = 'NORMAL'
        state.current_max_advance_bookings = policy.normal_max_advance_bookings
        state.consecutive_no_show_count = 0
        state.save(using=alias)

        AttendancePolicyEvent.objects.using(alias).create(
            user_profile=state.user_profile,
            attendance_policy_set=policy,
            event_type='POLICY_RESET',
            new_booking_mode='NORMAL',
            reason=request.data.get('reason', 'Administrative reset'),
            triggered_by_type='ADMIN',
            triggered_by_user=request.user,
            created_at=timezone.now()
        )

        return Response(self.get_serializer(state).data, status=status.HTTP_200_OK)


class AttendanceRecordViewSet(viewsets.ModelViewSet):
    serializer_class = AttendanceRecordSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'members'
    required_submodule = 'attendance'
    required_permission = 'members.attendance.view'
    permission_action_map = {
        'create': 'members.attendance.create',
        'update': 'members.attendance.edit',
        'partial_update': 'members.attendance.edit',
        'destroy': 'members.attendance.delete',
    }

    def get_queryset(self):
        alias = _get_db(self.request)
        qs = AttendanceRecord.objects.using(alias).select_related(
            'user_profile', 'user_profile__user', 'occurrence', 'occurrence__class_template', 'branch', 'booking'
        ).all()
        branch_id = self.request.query_params.get('branch_id')
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        att_status = self.request.query_params.get('status')
        if att_status:
            qs = qs.filter(status=att_status)
        occurrence_id = self.request.query_params.get('occurrence_id') or self.request.query_params.get('occurrence')
        if occurrence_id:
            qs = qs.filter(occurrence_id=occurrence_id)
        return qs


class AccessEventViewSet(viewsets.ModelViewSet):
    serializer_class = AccessEventSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'members'
    required_submodule = 'attendance'
    required_permission = 'members.attendance.view'
    permission_action_map = {
        'create': 'members.attendance.create',
        'update': 'members.attendance.edit',
        'partial_update': 'members.attendance.edit',
        'destroy': 'members.attendance.delete',
    }

    def get_queryset(self):
        alias = _get_db(self.request)
        return AccessEvent.objects.using(alias).select_related('user_profile', 'user_profile__user', 'branch', 'booking').all().order_by('-created_at')

    def create(self, request, *args, **kwargs):
        alias = _get_db(request)
        user_profile_id = request.data.get('user_profile')
        branch_id = request.data.get('branch')
        event_type = request.data.get('event_type', 'ENTRY')
        device_reference = request.data.get('device_reference')
        provider_reference = request.data.get('provider_reference')

        user_profile = get_object_or_404(UserProfile.objects.using(alias), id=user_profile_id)
        branch = get_object_or_404(Branch.objects.using(alias), id=branch_id)

        event = BookingWaitlistAttendanceService.log_access_event(
            user_profile=user_profile,
            branch=branch,
            event_type=event_type,
            device_reference=device_reference,
            provider_reference=provider_reference,
            db_alias=alias,
        )
        serializer = self.get_serializer(event)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
