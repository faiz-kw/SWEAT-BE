import uuid
from datetime import datetime, timedelta
from typing import Optional, Set
from django.db import models, transaction
from rest_framework import viewsets, status, permissions
from rest_framework.views import APIView
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
from .services_bookings import BookingWaitlistAttendanceService, ParqRequiredValidationError


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


from rest_framework.pagination import PageNumberPagination


class BookingsPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 500

    def get_page_size(self, request):
        # If querying bookings for a single class occurrence (roster modal) without explicit page_size, allow up to 200
        if request.query_params.get('occurrence_id') and not request.query_params.get('page_size'):
            return 200
        return super().get_page_size(request)

    def get_paginated_response(self, data):
        summary = getattr(self, '_bookings_summary', None) or {}
        return Response({
            'count': self.page.paginator.count,
            'total_pages': self.page.paginator.num_pages,
            'current_page': self.page.number,
            'page_size': self.get_page_size(self.request),
            'next': self.get_next_link(),
            'previous': self.get_previous_link(),
            'summary': summary,
            'results': data,
        })


class BookingViewSet(viewsets.ModelViewSet):
    serializer_class = BookingSerializer
    pagination_class = BookingsPagination
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
        'reconcile_attendance': 'ops.bookings.view',
        'promote_waitlist': 'ops.bookings.edit',
    }

    def get_serializer_class(self):
        # Use lightweight list serializer for list actions to avoid heavy prefetch chains
        if self.action == 'list':
            return BookingListSerializer
        return BookingSerializer

    def get_queryset(self):
        from .models_crm import IntakeSubmission
        alias = _get_db(self.request)

        # Trigger automatic attendance lifecycle reconciliation for past occurrences
        if getattr(self, 'action', 'list') == 'list':
            try:
                b_id = self.request.query_params.get('branch_id')
                BookingWaitlistAttendanceService.reconcile_past_occurrences_attendance(
                    branch=b_id if b_id and b_id != 'all' else None,
                    occurrence_id=self.request.query_params.get('occurrence_id'),
                    target_date=self.request.query_params.get('occurrence_date'),
                    db_alias=alias,
                )
            except Exception:
                pass

        base_select = [
            'user_profile', 'user_profile__user', 'occurrence',
            'occurrence__class_template', 'occurrence__class_template__program',
            'occurrence__class_template__category', 'branch', 'membership',
            'membership__package', 'membership__package__program', 'entitlement',
            'created_by_user'
        ]
        qs = Booking.objects.using(alias).select_related(*base_select)

        if getattr(self, 'action', 'list') == 'list':
            qs = qs.annotate(
                has_parq_submission=models.Exists(
                    IntakeSubmission.objects.using(alias).filter(user_profile_id=models.OuterRef('user_profile_id'))
                )
            ).prefetch_related('attendance_records')
        else:
            qs = qs.prefetch_related('reschedules', 'status_history', 'cancellations', 'waitlist_events', 'attendance_records')

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

        branch_id = self.request.query_params.get('branch_id')
        if branch_id and branch_id != 'all' and branch_id != 'ALL':
            qs = qs.filter(branch_id=branch_id)

        program_id = self.request.query_params.get('program_id') or self.request.query_params.get('program')
        if program_id and program_id != 'all' and program_id != 'ALL':
            from .models_catalog import Program
            matching_ids = [program_id]
            try:
                prog_obj = Program.objects.using(alias).filter(id=program_id).first()
                if prog_obj:
                    sibling_ids = list(Program.objects.using(alias).filter(name__iexact=prog_obj.name).values_list('id', flat=True))
                    matching_ids = list(set(matching_ids + [str(s) for s in sibling_ids]))
            except Exception:
                pass

            qs = qs.filter(
                models.Q(occurrence__class_template__program_id__in=matching_ids) |
                models.Q(occurrence__class_template__category_id__in=matching_ids) |
                models.Q(membership__package__program_id__in=matching_ids) |
                models.Q(occurrence__class_template__program__name__iexact=program_id)
            )

        booking_type = self.request.query_params.get('booking_type')
        is_trial = self.request.query_params.get('is_trial')
        if is_trial is not None and str(is_trial).lower() in ['true', '1', 'yes']:
            qs = qs.filter(booking_type='TRIAL')
        elif is_trial is not None and str(is_trial).lower() in ['false', '0', 'no']:
            qs = qs.exclude(booking_type='TRIAL')
        elif booking_type and booking_type.upper() != 'ALL':
            if booking_type.upper() == 'TRIAL':
                qs = qs.filter(booking_type='TRIAL')
            elif booking_type.upper() == 'MEMBER':
                qs = qs.exclude(booking_type='TRIAL')
            else:
                qs = qs.filter(booking_type=booking_type.upper())

        occurrence_id = self.request.query_params.get('occurrence_id')
        if occurrence_id:
            qs = qs.filter(occurrence_id=occurrence_id)

        user_id = self.request.query_params.get('user_id') or self.request.query_params.get('user_profile_id')
        if user_id:
            qs = qs.filter(user_profile_id=user_id)

        occ_date = self.request.query_params.get('occurrence_date')
        if occ_date:
            qs = qs.filter(occurrence__occurrence_date=occ_date)

        from_date = self.request.query_params.get('from_date')
        if from_date:
            qs = qs.filter(occurrence__occurrence_date__gte=from_date)

        to_date = self.request.query_params.get('to_date')
        if to_date:
            qs = qs.filter(occurrence__occurrence_date__lte=to_date)

        # Compute KPI summary on branch/date-scoped queryset before status/search filters
        if getattr(self, 'action', 'list') == 'list' and getattr(self, 'paginator', None) is not None and not occurrence_id:
            self.paginator._bookings_summary = qs.aggregate(
                total_count=models.Count('id'),
                confirmed_count=models.Count('id', filter=models.Q(status='CONFIRMED')),
                waitlisted_count=models.Count('id', filter=models.Q(status='WAITLISTED')),
                attended_count=models.Count('id', filter=models.Q(status__in=['ATTENDED', 'COMPLETED', 'LATE', 'LATE_ENTRY'])),
                no_show_count=models.Count('id', filter=models.Q(status='NO_SHOW')),
                cancelled_count=models.Count('id', filter=models.Q(status__in=['CANCELLED', 'LATE_CANCELLED'])),
            )

        status_filter = self.request.query_params.get('status')
        if status_filter and status_filter.upper() != 'ALL':
            qs = qs.filter(status=status_filter)

        search = (self.request.query_params.get('search') or '').strip()
        if search:
            for token in search.split():
                qs = qs.filter(
                    models.Q(booking_number__icontains=token) |
                    models.Q(user_profile__first_name_snapshot__icontains=token) |
                    models.Q(user_profile__last_name_snapshot__icontains=token) |
                    models.Q(user_profile__user__email__icontains=token) |
                    models.Q(user_profile__user__phone__icontains=token) |
                    models.Q(occurrence__class_template__name__icontains=token) |
                    models.Q(branch__name__icontains=token)
                )

        return qs.order_by('-booked_at', '-created_at')

    def list(self, request, *args, **kwargs):
        alias = _get_db(request)
        queryset = self.filter_queryset(self.get_queryset())

        # Unified Operations View: Include TrialBookings as first-class rows on page 1 or occurrence roster
        include_trials = request.query_params.get('include_trials', 'true').lower() in ['true', '1', 'yes']
        user_roles = set(
            RoleAssignment.objects.using(alias)
            .filter(user=request.user, is_active=True)
            .values_list('role__code', flat=True)
        )
        staff_roles = {'ORG_ADMIN', 'BRANCH_MANAGER', 'FRONT_DESK', 'TRAINER', 'SALES_REP', 'FINANCE_ADMIN'}
        is_member_only = 'MEMBER' in user_roles and not (user_roles & staff_roles)

        booking_type_param = (request.query_params.get('booking_type') or '').upper()
        is_trial_param = request.query_params.get('is_trial')
        if booking_type_param == 'MEMBER' or (is_trial_param is not None and str(is_trial_param).lower() in ['false', '0', 'no']):
            include_trials = False

        trial_items = []
        current_page_num = int(request.query_params.get('page', 1) or 1)
        if include_trials and not is_member_only and current_page_num == 1:
            trial_qs = TrialBooking.objects.using(alias).select_related('lead', 'branch', 'assigned_trainer_profile', 'created_by_user')

            permitted_branches = get_user_effective_branch_ids(request.user, alias)
            if permitted_branches is not None:
                trial_qs = trial_qs.filter(branch_id__in=permitted_branches)

            branch_id = request.query_params.get('branch_id')
            if branch_id and branch_id != 'all' and branch_id != 'ALL':
                trial_qs = trial_qs.filter(branch_id=branch_id)

            occurrence_id = request.query_params.get('occurrence_id')
            if occurrence_id:
                trial_qs = trial_qs.filter(class_occurrence_id=occurrence_id)

            status_filter = request.query_params.get('status')
            if status_filter and status_filter.upper() != 'ALL':
                if status_filter == 'CONFIRMED':
                    trial_qs = trial_qs.filter(models.Q(status='CONFIRMED') | models.Q(confirmation_status='CONFIRMED'))
                else:
                    trial_qs = trial_qs.filter(status=status_filter)

            # Program Filter for Trials
            program_id = request.query_params.get('program_id') or request.query_params.get('program')
            if program_id and program_id != 'all' and program_id != 'ALL':
                from .models_catalog import Program
                matching_ids = [program_id]
                try:
                    prog_obj = Program.objects.using(alias).filter(id=program_id).first()
                    if prog_obj:
                        sibling_ids = list(Program.objects.using(alias).filter(name__iexact=prog_obj.name).values_list('id', flat=True))
                        matching_ids = list(set(matching_ids + [str(s) for s in sibling_ids]))
                except Exception:
                    pass

                matching_occ_ids = list(
                    ClassOccurrence.objects.using(alias).filter(
                        models.Q(class_template__program_id__in=matching_ids) |
                        models.Q(class_template__category_id__in=matching_ids) |
                        models.Q(class_template__program__name__iexact=program_id)
                    ).values_list('id', flat=True)
                )
                trial_qs = trial_qs.filter(class_occurrence_id__in=matching_occ_ids)

            # Date Filters for Trials
            occ_date = request.query_params.get('occurrence_date')
            if occ_date:
                trial_qs = trial_qs.filter(scheduled_start__date=occ_date)

            from_date = request.query_params.get('from_date')
            if from_date:
                trial_qs = trial_qs.filter(scheduled_start__date__gte=from_date)

            to_date = request.query_params.get('to_date')
            if to_date:
                trial_qs = trial_qs.filter(scheduled_start__date__lte=to_date)

            search = (request.query_params.get('search') or '').strip()
            if search:
                trial_qs = trial_qs.filter(
                    models.Q(lead__first_name__icontains=search) |
                    models.Q(lead__last_name__icontains=search) |
                    models.Q(lead__email_normalized__icontains=search) |
                    models.Q(lead__phone_normalized__icontains=search) |
                    models.Q(notes__icontains=search)
                )

            trial_list = list(trial_qs.order_by('-created_at')[:50])
            occ_ids = [t.class_occurrence_id for t in trial_list if t.class_occurrence_id]
            occ_map = {}
            if occ_ids:
                for occ in ClassOccurrence.objects.using(alias).filter(id__in=occ_ids).select_related('class_template', 'class_template__program'):
                    prog_name = None
                    if occ.class_template and occ.class_template.program:
                        prog_name = occ.class_template.program.name
                    occ_map[occ.id] = {
                        'title': occ.class_template.name if occ.class_template else 'Class Session',
                        'program_name': prog_name,
                        'date': occ.occurrence_date.isoformat() if occ.occurrence_date else None,
                        'start_at': occ.start_at.isoformat() if occ.start_at else None,
                        'end_at': occ.end_at.isoformat() if occ.end_at else None,
                    }

            for t in trial_list:
                occ_info = occ_map.get(t.class_occurrence_id, {})
                title = occ_info.get('title')
                if search and title and search.lower() not in title.lower() and not (
                    search.lower() in (t.lead.first_name or '').lower() or
                    search.lower() in (t.lead.last_name or '').lower() or
                    search.lower() in (t.lead.email_normalized or '').lower() or
                    f"TRL-{str(t.id)[:8].upper()}".lower().find(search.lower()) != -1
                ):
                    continue

                staff_name = ''
                if t.created_by_user:
                    staff_name = f"{t.created_by_user.first_name or ''} {t.created_by_user.last_name or ''}".strip() or t.created_by_user.email
                else:
                    staff_name = 'Front Desk / Staff'

                trial_items.append({
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
                    'user_profile_phone': t.lead.phone_normalized if t.lead else None,
                    'phone': t.lead.phone_normalized if t.lead else None,
                    'occurrence': str(t.class_occurrence_id) if t.class_occurrence_id else None,
                    'occurrence_title': occ_info.get('title', 'Trial Session'),
                    'class_name': occ_info.get('title', 'Trial Session'),
                    'program_name': occ_info.get('program_name'),
                    'booked_by_name': staff_name,
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
                })

        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            results = list(serializer.data)
            if trial_items:
                results = trial_items + results
                results.sort(key=lambda x: x.get('booked_at') or x.get('created_at') or '', reverse=True)
            response = self.get_paginated_response(results)
            if trial_items and hasattr(response, 'data') and isinstance(response.data, dict):
                response.data['count'] = (response.data.get('count') or 0) + len(trial_items)
                if 'summary' in response.data and isinstance(response.data['summary'], dict):
                    sum_dict = response.data['summary']
                    sum_dict['total_count'] = (sum_dict.get('total_count') or 0) + len(trial_items)
                    for t in trial_items:
                        st = t.get('status')
                        if st == 'CONFIRMED' or t.get('confirmation_status') == 'CONFIRMED':
                            sum_dict['confirmed_count'] = (sum_dict.get('confirmed_count') or 0) + 1
                        elif st in ['ATTENDED', 'COMPLETED']:
                            sum_dict['attended_count'] = (sum_dict.get('attended_count') or 0) + 1
                        elif st == 'NO_SHOW':
                            sum_dict['no_show_count'] = (sum_dict.get('no_show_count') or 0) + 1
                        elif st in ['CANCELLED', 'LATE_CANCELLED']:
                            sum_dict['cancelled_count'] = (sum_dict.get('cancelled_count') or 0) + 1
            return response

        serializer = self.get_serializer(queryset[:200], many=True)
        results = trial_items + list(serializer.data)
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
        except ParqRequiredValidationError as e:
            return Response({
                'code': e.code,
                'detail': str(e.message if hasattr(e, 'message') else e),
                'error': str(e.message if hasattr(e, 'message') else e),
                'membership_id': str(e.membership.id) if e.membership else None,
                'form_id': str(e.form.id) if e.form else None,
                'form_title': e.form.name if e.form else None,
            }, status=status.HTTP_400_BAD_REQUEST)
        except ValidationError as e:
            if getattr(e, 'code', None) in ['PARQ_REQUIRED', 'PARQ_CONFIG_ERROR']:
                return Response({
                    'code': getattr(e, 'code', 'PARQ_REQUIRED'),
                    'detail': str(e.messages[0] if hasattr(e, 'messages') and e.messages else e),
                    'error': str(e.messages[0] if hasattr(e, 'messages') and e.messages else e),
                }, status=status.HTTP_400_BAD_REQUEST)
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
        except ParqRequiredValidationError as e:
            return Response({
                'code': e.code,
                'detail': str(e.message if hasattr(e, 'message') else e),
                'error': str(e.message if hasattr(e, 'message') else e),
                'membership_id': str(e.membership.id) if e.membership else None,
                'form_id': str(e.form.id) if e.form else None,
                'form_title': e.form.name if e.form else None,
            }, status=status.HTTP_400_BAD_REQUEST)
        except ValidationError as e:
            if getattr(e, 'code', None) in ['PARQ_REQUIRED', 'PARQ_CONFIG_ERROR']:
                return Response({
                    'code': getattr(e, 'code', 'PARQ_REQUIRED'),
                    'detail': str(e.messages[0] if hasattr(e, 'messages') and e.messages else e),
                    'error': str(e.messages[0] if hasattr(e, 'messages') and e.messages else e),
                }, status=status.HTTP_400_BAD_REQUEST)
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

    @action(detail=False, methods=['post'], url_path='reconcile-attendance')
    def reconcile_attendance(self, request):
        alias = _get_db(request)
        branch_id = request.data.get('branch_id') or request.query_params.get('branch_id')
        res = BookingWaitlistAttendanceService.reconcile_past_occurrences_attendance(
            branch=branch_id if branch_id and branch_id != 'all' else None,
            target_date=request.data.get('occurrence_date'),
            db_alias=alias,
        )
        return Response({
            'success': True,
            'message': 'Past occurrences attendance reconciled successfully.',
            **res,
        }, status=status.HTTP_200_OK)

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

        booking = Booking.objects.using(alias).filter(id=pk).first()
        if booking:
            permitted_branches = get_user_effective_branch_ids(request.user, alias)
            if permitted_branches is not None and str(booking.branch_id) not in permitted_branches:
                return Response({'detail': 'User lacks permission to modify bookings for this branch.'}, status=status.HTTP_403_FORBIDDEN)
            booking.confirmation_status = 'CONFIRMED'
            booking.confirmed_at = timezone.now()
            if booking.status == 'RESERVED':
                booking.status = 'CONFIRMED'
            booking.save(using=alias, update_fields=['confirmation_status', 'confirmed_at', 'status', 'updated_at'])

            BookingStatusHistory.objects.using(alias).create(
                booking=booking,
                from_status='RESERVED',
                to_status=booking.status,
                reason_code='MANUAL_CONFIRMED',
                reason_text=request.data.get('notes', 'Confirmed by staff'),
                changed_by_user=request.user,
                changed_at=timezone.now()
            )
            return Response({
                'id': str(booking.id),
                'status': booking.status,
                'confirmation_status': booking.confirmation_status,
                'detail': 'Booking confirmed successfully.'
            }, status=status.HTTP_200_OK)

        return Response({'detail': 'Booking not found.'}, status=status.HTTP_404_NOT_FOUND)

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
        org = serializer.validated_data.get('organization')
        if not org:
            branch = serializer.validated_data.get('branch')
            if branch and branch.organization:
                org = branch.organization
        if not org:
            from .models_org import Organization
            org = Organization.objects.using(alias).first()
        user = self.request.user
        created_by_user = user if hasattr(user, 'organization') else None
        if not created_by_user and hasattr(user, 'email'):
            from .models_users import TenantUser
            created_by_user = TenantUser.objects.using(alias).filter(email=user.email).first()
        serializer.save(organization=org, created_by_user=created_by_user)


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
    pagination_class = BookingsPagination
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
        if getattr(self, 'action', 'list') == 'list':
            try:
                b_id = self.request.query_params.get('branch_id')
                BookingWaitlistAttendanceService.reconcile_past_occurrences_attendance(
                    branch=b_id if b_id and b_id != 'all' else None,
                    occurrence_id=self.request.query_params.get('occurrence_id') or self.request.query_params.get('occurrence'),
                    db_alias=alias,
                )
            except Exception:
                pass
        qs = AttendanceRecord.objects.using(alias).select_related(
            'user_profile', 'user_profile__user', 'occurrence', 'occurrence__class_template', 'branch', 'booking'
        ).all()
        branch_id = self.request.query_params.get('branch_id')
        if branch_id and branch_id != 'all':
            qs = qs.filter(branch_id=branch_id)
        att_status = self.request.query_params.get('status')
        if att_status and att_status.upper() != 'ALL':
            qs = qs.filter(status=att_status)
        occurrence_id = self.request.query_params.get('occurrence_id') or self.request.query_params.get('occurrence')
        if occurrence_id:
            qs = qs.filter(occurrence_id=occurrence_id)
        search = (self.request.query_params.get('search') or '').strip()
        if search:
            qs = qs.filter(
                models.Q(booking__booking_number__icontains=search) |
                models.Q(user_profile__first_name_snapshot__icontains=search) |
                models.Q(user_profile__last_name_snapshot__icontains=search) |
                models.Q(user_profile__user__email__icontains=search) |
                models.Q(occurrence__class_template__name__icontains=search)
            )
        return qs.order_by('-check_in_at', '-created_at')


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


def _validate_edge_service_request(request) -> bool:
    import os
    import secrets
    from django.conf import settings
    expected_key = getattr(settings, 'EDGE_SERVICE_API_KEY', None) or os.getenv('EDGE_SERVICE_API_KEY')
    is_debug = getattr(settings, 'DEBUG', False)

    # In production (DEBUG=False), the edge API key is strictly MANDATORY.
    # Fail-closed immediately if unconfigured or empty.
    if not is_debug:
        if not expected_key or not str(expected_key).strip():
            return False

    if expected_key and str(expected_key).strip():
        api_key = request.headers.get('X-Edge-Api-Key') or request.META.get('HTTP_X_EDGE_API_KEY')
        if not api_key:
            return False
        return secrets.compare_digest(str(api_key).strip(), str(expected_key).strip())

    return True


class AccessControlBulkIngestView(APIView):
    """
    POST /api/v1/access-control/events/bulk/
    Receives batched scan events from the on-site Edge Service outbox.
    Guarantees idempotency: retrying the same event_id produces no duplicate attendance.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        if not _validate_edge_service_request(request):
            return Response({'error': 'Unauthorized: Missing or invalid X-Edge-Api-Key header.'}, status=status.HTTP_401_UNAUTHORIZED)

        alias = _get_db(request)
        from .services_access_control import hash_qr_token
        from .models_attendance import AccessPass
        from .models_org import Branch
        from .models_crm import UserProfile

        branch_id = request.data.get('branch_id')
        branch = Branch.objects.using(alias).filter(id=branch_id).first() if branch_id else None
        if not branch:
            branch = Branch.objects.using(alias).filter(status='ACTIVE').first() or Branch.objects.using(alias).first()

        events_data = request.data.get('events', [])
        if isinstance(request.data, list):
            events_data = request.data

        results = []
        synced_count = 0

        for item in events_data:
            event_id_raw = item.get('event_id')
            event_id = uuid.UUID(str(event_id_raw)) if event_id_raw else uuid.uuid4()
            raw_token = item.get('raw_token') or item.get('token')
            token_hash = item.get('token_hash')
            if not token_hash and raw_token:
                token_hash = hash_qr_token(raw_token)

            direction = item.get('direction', 'ENTRY').upper()
            device_ref = item.get('device_reference') or item.get('device_id', 'TURNSTILE-01')
            scanned_at_str = item.get('scanned_at')
            scanned_at = timezone.now()
            if scanned_at_str:
                try:
                    scanned_at = datetime.fromisoformat(scanned_at_str.replace('Z', '+00:00'))
                except Exception:
                    pass

            reconciliation = item.get('reconciliation_status', 'NORMAL')

            # Lookup AccessPass by token_hash
            access_pass = None
            if token_hash:
                access_pass = AccessPass.objects.using(alias).filter(token_hash=token_hash).select_related('user_profile', 'booking', 'branch').first()

            if access_pass:
                target_user = access_pass.user_profile
                target_booking = access_pass.booking
                target_branch = access_pass.branch or branch

                event = BookingWaitlistAttendanceService.log_access_event(
                    user_profile=target_user,
                    branch=target_branch,
                    event_type=direction,
                    direction=direction,
                    device_reference=device_ref,
                    booking=target_booking,
                    provider_reference='TIMEWATCH_TURNSTILE',
                    event_id=event_id,
                    scanned_at=scanned_at,
                    reconciliation_status=reconciliation,
                    token_hash=token_hash,
                    db_alias=alias,
                )
                results.append({
                    'event_id': str(event.id),
                    'status': 'PROCESSED',
                    'event_type': event.event_type,
                    'reconciliation_status': event.reconciliation_status,
                })
                synced_count += 1
            else:
                fallback_user = UserProfile.objects.using(alias).first()
                if fallback_user and branch:
                    event = BookingWaitlistAttendanceService.log_access_event(
                        user_profile=fallback_user,
                        branch=branch,
                        event_type='DENIED',
                        direction=direction,
                        device_reference=device_ref,
                        provider_reference='TIMEWATCH_TURNSTILE',
                        event_id=event_id,
                        scanned_at=scanned_at,
                        reconciliation_status='UNAUTHORIZED',
                        token_hash=token_hash,
                        db_alias=alias,
                    )
                    results.append({
                        'event_id': str(event.id),
                        'status': 'DENIED_UNAUTHORIZED',
                        'reconciliation_status': 'UNAUTHORIZED',
                    })
                    synced_count += 1
                else:
                    results.append({
                        'event_id': str(event_id),
                        'status': 'FAILED_NO_TARGET',
                    })

        return Response({
            'success': True,
            'synced_count': synced_count,
            'total_received': len(events_data),
            'results': results,
        }, status=status.HTTP_200_OK)


class AccessControlSyncPassesView(APIView):
    """
    GET /api/v1/access-control/sync/
    Provides the on-site Edge Service with today's active pass hashes for the branch.
    Allows Edge Service to make sub-50ms local gate decisions completely offline.
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        if not _validate_edge_service_request(request):
            return Response({'error': 'Unauthorized: Missing or invalid X-Edge-Api-Key header.'}, status=status.HTTP_401_UNAUTHORIZED)

        alias = _get_db(request)
        from .models_attendance import AccessPass
        from .models_org import Branch

        branch_id = request.query_params.get('branch_id')
        now = timezone.now()
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = start_of_day + timedelta(hours=28)  # Today + 4 hours into tomorrow

        qs = AccessPass.objects.using(alias).filter(
            valid_from__gte=start_of_day,
            valid_from__lte=end_of_day,
        ).select_related('booking', 'user_profile')

        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        passes_data = []
        for p in qs:
            passes_data.append({
                'pass_id': str(p.id),
                'token_hash': p.token_hash,
                'pass_type': p.pass_type,
                'valid_from': p.valid_from.isoformat(),
                'valid_until': p.valid_until.isoformat(),
                'status': p.status,
                'booking_id': str(p.booking_id) if p.booking_id else None,
                'booking_number': p.booking.booking_number if p.booking else None,
                'member_id': str(p.user_profile_id),
                'member_name': (
                    f"{getattr(p.user_profile, 'first_name_snapshot', None) or getattr(getattr(p.user_profile, 'user', None), 'first_name', '')} "
                    f"{getattr(p.user_profile, 'last_name_snapshot', None) or getattr(getattr(p.user_profile, 'user', None), 'last_name', '')}"
                ).strip(),
            })

        return Response({
            'server_time_utc': now.isoformat(),
            'branch_id': branch_id,
            'total_passes': len(passes_data),
            'passes': passes_data,
        }, status=status.HTTP_200_OK)


class TurnstileVerifyPunchView(APIView):
    """
    POST /api/v1/tenant/access/turnstile/verify-punch/
    POST /api/v1/access/turnstile/verify-punch/
    POST /api/v1/access-control/turnstile/verify-punch/

    Real-time webhook receiver for TimeWatch / Dahua face and QR turnstile access terminals.
    Hardware synchronously calls this endpoint when a member presents their QR pass.

    Protocol:
    - Input: JSON or form payload containing card_no, CardNo, qr_code, token, szCardNo, etc.
    - Output:
      {
        "allow_access": true,   # Triggers turnstile relay unlock for 5-7 seconds
        "status": "GRANTED",
        "user_name": "Faiz Khan",
        "member_number": "MEM-97BE4D",
        "class_name": "Sweat Total",
        "message": "Welcome Faiz Khan! Access Granted."
      }
      or
      {
        "allow_access": false,  # Keeps gate locked, beeps error
        "status": "DENIED",
        "reason": "REPLAY_ATTEMPT" | "EXPIRED" | "INVALID_PASS" | "EARLY",
        "message": "..."
      }
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request, *args, **kwargs):
        """Health check endpoint for device setup verification."""
        return Response({
            "service": "SWEAT Turnstile Verify Punch Gateway",
            "status": "ONLINE",
            "endpoint": "/api/v1/tenant/access/turnstile/verify-punch/",
            "timestamp": timezone.now().isoformat(),
        }, status=status.HTTP_200_OK)

    def post(self, request, *args, **kwargs):
        alias = self._resolve_db_alias(request)
        raw_token = self._extract_token(request)
        if not raw_token:
            return Response({
                "allow_access": False,
                "status": "DENIED",
                "reason": "MISSING_TOKEN",
                "message": "No card or QR token found in request payload.",
            }, status=status.HTTP_200_OK)

        device_ref = self._extract_device_ref(request)
        direction = str(
            request.data.get('direction', 'ENTRY')
            if hasattr(request, 'data') and isinstance(request.data, dict)
            else 'ENTRY'
        ).upper()
        if direction not in ('ENTRY', 'EXIT'):
            direction = 'ENTRY'

        now = timezone.now()

        from .services_access_control import hash_qr_token
        from .models_attendance import AccessPass, AttendanceRecord, AccessEvent
        from .models_org import Branch
        from .models_crm import UserProfile
        from .services_bookings import BookingWaitlistAttendanceService

        token_str = str(raw_token).strip()
        # Compute SHA-256 hash if raw token; if already a 64-char hex hash, use directly
        if len(token_str) == 64 and all(c in '0123456789abcdefABCDEF' for c in token_str):
            token_hash = token_str.lower()
        else:
            token_hash = hash_qr_token(token_str)

        # Lookup AccessPass
        access_pass = (
            AccessPass.objects.using(alias)
            .filter(token_hash=token_hash)
            .select_related('user_profile__user', 'booking__occurrence__class_template', 'branch')
            .first()
        )

        # If not found by token_hash, check if token_str matches booking_number
        if not access_pass:
            access_pass = (
                AccessPass.objects.using(alias)
                .filter(booking__booking_number=token_str)
                .select_related('user_profile__user', 'booking__occurrence__class_template', 'branch')
                .first()
            )

        # Default branch resolution
        branch = None
        if access_pass and access_pass.branch:
            branch = access_pass.branch
        else:
            branch_id = (request.data.get('branch_id') if isinstance(request.data, dict) else None) or request.query_params.get('branch_id')
            if branch_id:
                branch = Branch.objects.using(alias).filter(id=branch_id).first()
            if not branch:
                branch = Branch.objects.using(alias).filter(status='ACTIVE').first() or Branch.objects.using(alias).first()

        # 1. Unknown / Invalid Pass
        if not access_pass:
            fallback_user = UserProfile.objects.using(alias).first()
            if fallback_user and branch:
                BookingWaitlistAttendanceService.log_access_event(
                    user_profile=fallback_user,
                    branch=branch,
                    event_type='DENIED',
                    direction=direction,
                    device_reference=device_ref,
                    provider_reference='TIMEWATCH_TURNSTILE',
                    reconciliation_status='UNAUTHORIZED',
                    token_hash=token_hash,
                    db_alias=alias,
                )
            return Response({
                "allow_access": False,
                "status": "DENIED",
                "reason": "INVALID_PASS",
                "message": "Invalid or unrecognized access pass.",
            }, status=status.HTTP_200_OK)

        target_user = access_pass.user_profile
        target_booking = access_pass.booking
        user_name = (
            f"{getattr(target_user, 'first_name_snapshot', '')} {getattr(target_user, 'last_name_snapshot', '')}"
        ).strip() or "Valued Member"
        member_num = getattr(target_user, 'member_number', '') or ''
        class_name = (
            target_booking.occurrence.class_template.name
            if target_booking and target_booking.occurrence and target_booking.occurrence.class_template
            else "SWEAT Session"
        )

        # 2. Check if pass has already completed both ENTRY and EXIT
        if access_pass.status == 'CONSUMED_EXITED':
            BookingWaitlistAttendanceService.log_access_event(
                user_profile=target_user,
                branch=branch,
                booking=target_booking,
                event_type='DENIED',
                direction=direction,
                device_reference=device_ref,
                provider_reference='TIMEWATCH_TURNSTILE',
                reconciliation_status='REPLAY_ATTEMPT',
                token_hash=token_hash,
                db_alias=alias,
            )
            return Response({
                "allow_access": False,
                "status": "DENIED",
                "reason": "REPLAY_ATTEMPT",
                "user_name": user_name,
                "member_number": member_num,
                "message": "Access pass has already been consumed for entry and exit. Re-entry denied.",
            }, status=status.HTTP_200_OK)

        # Determine direction:
        # Check explicit request payload/query param, or device name containing 'exit',
        # or state machine: if pass is already CONSUMED_ENTERED, next valid punch is EXIT.
        explicit_dir = None
        if hasattr(request, 'data') and isinstance(request.data, dict) and request.data.get('direction'):
            explicit_dir = str(request.data.get('direction')).upper()
        elif request.query_params.get('direction'):
            explicit_dir = str(request.query_params.get('direction')).upper()

        if explicit_dir in ('ENTRY', 'EXIT'):
            direction = explicit_dir
        elif 'exit' in device_ref.lower():
            direction = 'EXIT'
        elif access_pass.status == 'CONSUMED_ENTERED':
            direction = 'EXIT'
        else:
            direction = 'ENTRY'

        # If duplicate ENTRY attempted when member is already inside:
        if direction == 'ENTRY' and access_pass.status == 'CONSUMED_ENTERED':
            BookingWaitlistAttendanceService.log_access_event(
                user_profile=target_user,
                branch=branch,
                booking=target_booking,
                event_type='DENIED',
                direction='ENTRY',
                device_reference=device_ref,
                provider_reference='TIMEWATCH_TURNSTILE',
                reconciliation_status='REPLAY_ATTEMPT',
                token_hash=token_hash,
                db_alias=alias,
            )
            return Response({
                "allow_access": False,
                "status": "DENIED",
                "reason": "REPLAY_ATTEMPT",
                "user_name": user_name,
                "member_number": member_num,
                "message": "Member is already inside the studio. Duplicate entry denied.",
            }, status=status.HTTP_200_OK)

        # 3. Expired or Cancelled
        if access_pass.status in ('CANCELLED', 'EXPIRED') or (access_pass.valid_until and now > access_pass.valid_until):
            if access_pass.status != 'EXPIRED':
                access_pass.status = 'EXPIRED'
                access_pass.save(using=alias, update_fields=['status'])

            BookingWaitlistAttendanceService.log_access_event(
                user_profile=target_user,
                branch=branch,
                booking=target_booking,
                event_type='DENIED',
                direction=direction,
                device_reference=device_ref,
                provider_reference='TIMEWATCH_TURNSTILE',
                reconciliation_status='EXPIRED',
                token_hash=token_hash,
                db_alias=alias,
            )
            return Response({
                "allow_access": False,
                "status": "DENIED",
                "reason": "EXPIRED",
                "user_name": user_name,
                "member_number": member_num,
                "message": "Access pass has expired. Entry denied.",
            }, status=status.HTTP_200_OK)

        # 4. Early scan (before activation window) - only applies to initial ENTRY
        if direction == 'ENTRY' and access_pass.valid_from and now < access_pass.valid_from:
            BookingWaitlistAttendanceService.log_access_event(
                user_profile=target_user,
                branch=branch,
                booking=target_booking,
                event_type='DENIED',
                direction=direction,
                device_reference=device_ref,
                provider_reference='TIMEWATCH_TURNSTILE',
                reconciliation_status='EARLY',
                token_hash=token_hash,
                db_alias=alias,
            )
            return Response({
                "allow_access": False,
                "status": "DENIED",
                "reason": "EARLY",
                "user_name": user_name,
                "member_number": member_num,
                "message": "Access window not yet open. Please arrive during your scheduled class window.",
            }, status=status.HTTP_200_OK)

        # 5. GRANTED ACCESS (ENTRY or EXIT)
        with transaction.atomic(using=alias):
            BookingWaitlistAttendanceService.log_access_event(
                user_profile=target_user,
                branch=branch,
                booking=target_booking,
                event_type=direction,
                direction=direction,
                device_reference=device_ref,
                provider_reference='TIMEWATCH_TURNSTILE',
                reconciliation_status='NORMAL',
                token_hash=token_hash,
                db_alias=alias,
            )
            if direction == 'ENTRY':
                access_pass.status = 'CONSUMED_ENTERED'
                access_pass.consumed_at = now
            else:
                access_pass.status = 'CONSUMED_EXITED'
            access_pass.save(using=alias, update_fields=['status', 'consumed_at', 'updated_at'])

        if direction == 'ENTRY':
            welcome_msg = f"Welcome {user_name}! Access Granted."
        else:
            welcome_msg = f"Goodbye {user_name}! Workout completed. Have a great day!"

        return Response({
            "allow_access": True,
            "status": "GRANTED",
            "direction": direction,
            "user_name": user_name,
            "member_number": member_num,
            "class_name": class_name,
            "message": welcome_msg,
        }, status=status.HTTP_200_OK)

    def _resolve_db_alias(self, request):
        from apps.master.models import Tenant
        from apps.authentication.views import _register_and_resolve_tenant
        from apps.tenant_core.context import set_tenant_db_alias, get_tenant_db_alias

        alias = get_tenant_db_alias()
        if alias and alias != 'default':
            return alias

        explicit_slug = (
            (request.headers.get('X-Tenant-Slug') if hasattr(request, 'headers') else None)
            or request.META.get('HTTP_X_TENANT_SLUG')
            or (getattr(request, 'query_params', None) or getattr(request, 'GET', {})).get('tenant')
            or (request.data.get('tenant_slug') if hasattr(request, 'data') and isinstance(request.data, dict) else None)
        )
        if explicit_slug:
            slug = str(explicit_slug).strip().lower()
            if slug in ('sweat-demo', 'sweat_demo', 'sweat-staging', 'sweat-dev'):
                slug = 'sweat'
            tenant = Tenant.objects.using('default').filter(slug=slug, status='ACTIVE').first()
        else:
            tenant = Tenant.objects.using('default').filter(slug='sweat', status='ACTIVE').first()
            if not tenant:
                tenant = Tenant.objects.using('default').filter(status='ACTIVE').first()

        if tenant:
            alias = _register_and_resolve_tenant(tenant)
            set_tenant_db_alias(alias)
            return alias
        return 'default'

    def _extract_token(self, request):
        data = request.data if hasattr(request, 'data') and isinstance(request.data, dict) else {}
        params = getattr(request, 'query_params', None) or getattr(request, 'GET', {})
        for key in ('card_no', 'CardNo', 'cardNo', 'token', 'qr_code', 'qrcode', 'code', 'szCardNo', 'user_id', 'userId', 'UserNo'):
            val = data.get(key) or params.get(key)
            if val:
                return str(val).strip()
        if hasattr(request, 'data') and isinstance(request.data, (str, int)):
            return str(request.data).strip()
        return None

    def _extract_device_ref(self, request):
        data = request.data if hasattr(request, 'data') and isinstance(request.data, dict) else {}
        params = getattr(request, 'query_params', None) or getattr(request, 'GET', {})
        for key in ('device_reference', 'device_id', 'sn', 'device_sn', 'mac', 'ip', 'terminal_id'):
            val = data.get(key) or params.get(key)
            if val:
                return str(val).strip()
        return 'TIMEWATCH-GATE-01'


class BookingPolicyViewSet(viewsets.ViewSet):
    """
    API endpoint for retrieving, updating, and triggering configurable booking policies.
    Supports tenant-wide defaults and optional branch overrides.
    Captures explicit choice: new bookings only vs existing eligible bookings.
    """
    permission_classes = [permissions.IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        user = request.user
        if not user or not user.is_authenticated:
            return

        # Explicitly block consumer members/leads from managing studio policies
        user_type = str(getattr(user, 'user_type', '')).upper()
        user_role = str(getattr(user, 'role', '')).upper()
        if user_type in ('MEMBER', 'LEAD') or user_role in ('MEMBER', 'LEAD'):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Members are not authorized to access or modify organization booking policies.")

    def list(self, request):
        alias = _get_db(request)
        from .models_org import Organization
        from .services_booking_policy import BookingPolicyService
        from .serializers_bookings import BookingPolicyConfigSerializer
        from .models_bookings import BookingPolicyConfig

        org = Organization.objects.using(alias).filter(status='ACTIVE').first()
        if not org:
            return Response({'detail': 'No active organization found.'}, status=status.HTTP_404_NOT_FOUND)

        branch_id = request.query_params.get('branch_id') or request.query_params.get('branch')
        branch = None
        if branch_id:
            branch = Branch.objects.using(alias).filter(id=branch_id).first()

        effective = BookingPolicyService.get_effective_policy(org, branch=branch, db_alias=alias)
        config_obj = BookingPolicyConfig.objects.using(alias).filter(organization=org, branch=branch).first()

        return Response({
            'organization_id': str(org.id),
            'branch_id': str(branch.id) if branch else None,
            'is_branch_override': branch is not None and config_obj is not None,
            'effective_policy': effective,
            'saved_config': BookingPolicyConfigSerializer(config_obj).data if config_obj else None,
        }, status=status.HTTP_200_OK)

    def create(self, request):
        alias = _get_db(request)
        from .models_org import Organization
        from .services_booking_policy import BookingPolicyService
        from .serializers_bookings import BookingPolicyConfigSerializer

        org = Organization.objects.using(alias).filter(status='ACTIVE').first()
        if not org:
            return Response({'detail': 'No active organization found.'}, status=status.HTTP_404_NOT_FOUND)

        branch_id = request.data.get('branch_id') or request.data.get('branch')
        branch = None
        if branch_id:
            branch = Branch.objects.using(alias).filter(id=branch_id).first()
            if not branch:
                return Response({'detail': f"Branch '{branch_id}' not found."}, status=status.HTTP_404_NOT_FOUND)

        apply_to_existing = request.data.get('apply_to_existing_bookings', False)
        if isinstance(apply_to_existing, str):
            apply_to_existing = apply_to_existing.lower() in ('true', '1', 'yes')

        saved_cfg = BookingPolicyService.save_policy(
            organization=org,
            policy_data=request.data,
            branch=branch,
            apply_to_existing=bool(apply_to_existing),
            actor_user=request.user,
            db_alias=alias,
        )

        effective = BookingPolicyService.get_effective_policy(org, branch=branch, db_alias=alias)

        return Response({
            'detail': 'Booking policy saved successfully.',
            'is_branch_override': branch is not None,
            'apply_to_existing_bookings': saved_cfg.apply_to_existing_bookings,
            'policy_effective_at': saved_cfg.policy_effective_at.isoformat(),
            'effective_policy': effective,
            'saved_config': BookingPolicyConfigSerializer(saved_cfg).data,
        }, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='run-auto-cancellation')
    def run_auto_cancellation(self, request):
        alias = _get_db(request)
        from .models_org import Organization
        from .services_booking_policy import BookingPolicyService

        org = Organization.objects.using(alias).filter(status='ACTIVE').first()
        if not org:
            return Response({'detail': 'No active organization found.'}, status=status.HTTP_404_NOT_FOUND)

        branch_id = request.data.get('branch_id')
        branch = Branch.objects.using(alias).filter(id=branch_id).first() if branch_id else None
        dry_run = request.data.get('dry_run', False)

        results = BookingPolicyService.process_auto_cancellations(
            organization=org,
            branch=branch,
            dry_run=bool(dry_run),
            db_alias=alias,
        )
        cancelled_unpaid = results.get('cancelled_unpaid_count', 0)
        cancelled_unconfirmed = results.get('cancelled_unconfirmed_count', 0)
        total_cancelled = results.get('auto_cancelled_count', cancelled_unpaid + cancelled_unconfirmed)
        promoted = results.get('promoted_from_waitlist_count', 0)
        detail = (
            f"Auto-cancellation complete: {total_cancelled} cancelled "
            f"({cancelled_unpaid} unpaid, {cancelled_unconfirmed} unconfirmed), "
            f"{promoted} promoted from waitlist."
        )
        return Response({
            'detail': detail,
            'auto_cancelled_count': total_cancelled,
            'cancelled_unpaid_count': cancelled_unpaid,
            'cancelled_unconfirmed_count': cancelled_unconfirmed,
            'promoted_from_waitlist_count': promoted,
            'evaluated_count': results.get('evaluated_count', 0),
            'results': results,
        }, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='reset-branch-override')
    def reset_branch_override(self, request):
        alias = _get_db(request)
        from .models_org import Organization
        from .services_booking_policy import BookingPolicyService
        from .models_bookings import BookingPolicyConfig
        from apps.tenant_core.services_reliability import record_business_audit

        org = Organization.objects.using(alias).filter(status='ACTIVE').first()
        if not org:
            return Response({'detail': 'No active organization found.'}, status=status.HTTP_404_NOT_FOUND)

        branch_id = request.data.get('branch_id')
        if not branch_id:
            return Response({'detail': 'branch_id is required to reset branch override.'}, status=status.HTTP_400_BAD_REQUEST)

        branch = Branch.objects.using(alias).filter(id=branch_id).first()
        if not branch:
            return Response({'detail': f"Branch '{branch_id}' not found."}, status=status.HTTP_404_NOT_FOUND)

        cfg = BookingPolicyConfig.objects.using(alias).filter(organization=org, branch=branch).first()
        if cfg:
            cfg.delete(using=alias)

        from .models_govern import BranchSettings
        b_settings = BranchSettings.objects.using(alias).filter(branch=branch).first()
        if b_settings:
            b_settings.booking_config = None
            b_settings.booking_cancellation_window_hours = None
            b_settings.late_cancellation_fee = None
            b_settings.save(using=alias, update_fields=['booking_config', 'booking_cancellation_window_hours', 'late_cancellation_fee', 'updated_at'])

        record_business_audit(
            organization=org,
            module='BOOKINGS',
            action_code='BOOKING_POLICY_BRANCH_OVERRIDE_REMOVED',
            entity_type='BookingPolicyConfig',
            entity_id=None,
            branch=branch,
            actor_user=request.user,
            metadata={'branch_name': branch.name},
            db_alias=alias,
        )

        effective = BookingPolicyService.get_effective_policy(org, branch=branch, db_alias=alias)
        return Response({
            'detail': f"Branch override for '{branch.name}' removed. Reverted to organization defaults.",
            'is_branch_override': False,
            'effective_policy': effective,
        }, status=status.HTTP_200_OK)

