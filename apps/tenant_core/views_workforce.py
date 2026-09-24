"""
apps/tenant_core/views_workforce.py — ViewSets for Layer 2 Module A: Workforce & Trainers

Endpoints:
- UserProfileViewSet: /api/v1/tenant/user-profiles/
- EmployeeProfileViewSet: /api/v1/tenant/employee-profiles/
- TrainerProfileViewSet: /api/v1/tenant/trainer-profiles/
  + check-availability (detail=True)
  + find-eligible (detail=False)
- SalesProfileViewSet: /api/v1/tenant/sales-profiles/
- EmployeeWorkScheduleViewSet: /api/v1/tenant/work-schedules/
- EmployeeScheduleExceptionViewSet: /api/v1/tenant/schedule-exceptions/
- TrainerSpecialtyViewSet: /api/v1/tenant/trainer-specialties/
- TrainerSpecialtyAssignmentViewSet: /api/v1/tenant/trainer-specialty-assignments/
"""

import logging
import uuid
from datetime import datetime
from django.db import transaction
from django.utils import timezone
from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db.models import Q
from django.utils.dateparse import parse_datetime
from config.routers import get_tenant_db_alias

from .models_org import Branch
from .models_users import TenantUser
from .models_workforce import (
    UserProfile,
    EmployeeProfile,
    TrainerProfile,
    SalesProfile,
    EmployeeWorkSchedule,
    EmployeeScheduleException,
    TrainerSpecialty,
    TrainerSpecialtyAssignment,
)
from .serializers_workforce import (
    UserProfileSerializer,
    EmployeeProfileSerializer,
    TrainerProfileSerializer,
    SalesProfileSerializer,
    EmployeeWorkScheduleSerializer,
    EmployeeScheduleExceptionSerializer,
    TrainerSpecialtySerializer,
    TrainerSpecialtyAssignmentSerializer,
)
from .services_workforce import TrainerAvailabilityService
from .permissions import RequireActiveTenantAndOrg, TenantRBACPermission

logger = logging.getLogger(__name__)


def _get_db(request):
    return (
        get_tenant_db_alias()
        or getattr(getattr(request, 'user', None), '_db_alias', None)
        or getattr(request, '_tenant_db_alias', None)
        or 'default'
    )


def _get_org(request):
    org = getattr(request, 'organization', None)
    if not org:
        user = getattr(request, 'user', None)
        if user and hasattr(user, 'organization') and user.organization:
            return user.organization
        alias = _get_db(request)
        from .models_org import Organization
        if user and getattr(user, 'organization_id', None):
            return Organization.objects.using(alias).filter(id=user.organization_id).first()
        return Organization.objects.using(alias).filter(status='ACTIVE').first()
    return org


class UserProfileViewSet(viewsets.ModelViewSet):
    serializer_class = UserProfileSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'users'
    required_permission = 'core.users.view'
    permission_action_map = {
        'create': 'core.users.create',
        'update': 'core.users.edit',
        'partial_update': 'core.users.edit',
        'destroy': 'core.users.delete',
    }
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['member_number', 'first_name_snapshot', 'last_name_snapshot', 'user__email', 'user__phone', 'user__full_name']
    ordering = ['-created_at']

    def get_queryset(self):
        alias = _get_db(self.request)
        org = getattr(self.request, 'organization', None)
        qs = UserProfile.objects.using(alias).select_related('user', 'preferred_branch')
        if org:
            qs = qs.filter(user__organization=org)

        bookable_only = self.request.query_params.get('bookable_only')
        if bookable_only in ('true', 'True', '1'):
            qs = qs.filter(member_status='ACTIVE', user__status='ACTIVE')

        status_filter = self.request.query_params.get('member_status')
        if status_filter:
            qs = qs.filter(member_status=status_filter)
        branch_id = self.request.query_params.get('branch_id')
        if branch_id:
            qs = qs.filter(preferred_branch_id=branch_id)
        return qs


class EmployeeProfileViewSet(viewsets.ModelViewSet):
    serializer_class = EmployeeProfileSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'users'
    required_permission = 'core.users.view'
    permission_action_map = {
        'create': 'core.users.create',
        'update': 'core.users.edit',
        'partial_update': 'core.users.edit',
        'destroy': 'core.users.delete',
    }
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['employee_code', 'designation', 'user_profile__user__email']
    ordering = ['employee_code']

    def get_queryset(self):
        alias = _get_db(self.request)
        org = getattr(self.request, 'organization', None)
        qs = EmployeeProfile.objects.using(alias).select_related('user_profile', 'user_profile__user', 'organization')
        if org:
            qs = qs.filter(organization=org)
        status_filter = self.request.query_params.get('employment_status')
        if status_filter:
            qs = qs.filter(employment_status=status_filter)
        return qs

    def perform_create(self, serializer):
        org = getattr(self.request, 'organization', None)
        serializer.save(organization=org)


class TrainerProfileViewSet(viewsets.ModelViewSet):
    serializer_class = TrainerProfileSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'ops'
    required_submodule = 'trainers'
    required_permission = 'ops.trainers.view'
    permission_action_map = {
        'create': 'ops.trainers.create',
        'update': 'ops.trainers.edit',
        'partial_update': 'ops.trainers.edit',
        'destroy': 'ops.trainers.delete',
        'check_availability': 'ops.trainers.view',
        'find_eligible': 'ops.trainers.view',
    }
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['trainer_code', 'bio', 'employee_profile__user_profile__user__email']
    ordering = ['trainer_code']

    @staticmethod
    def _trainer_users(alias, org):
        """Use explicit trainer identity, not staff type or branch membership.

        Employee designations are not authoritative: older auto-sync code wrote
        'Fitness Trainer' onto ordinary staff employee profiles as well.
        """
        from .models_rbac import RoleAssignment

        assignments = RoleAssignment.objects.using(alias).filter(
            status='ACTIVE', is_active=True, role__is_active=True,
        ).filter(
            Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()),
        ).filter(
            Q(role__code__iregex=r'(^|[^a-z])(?:trainer|coach|instructor)([^a-z]|$)') |
            Q(role__name__iregex=r'(^|[^a-z])(?:trainer|coach|instructor)([^a-z]|$)')
        )
        users = TenantUser.objects.using(alias).all()
        if org:
            users = users.filter(organization=org)
            assignments = assignments.filter(organization=org)
        return users.filter(
            Q(id__in=assignments.values('user_id')) | Q(user_type__iexact='TRAINER')
        )

    @classmethod
    def _ensure_trainer_profiles_for_staff(cls, alias, org):
        """
        Auto-syncs users with explicit trainer identity into TrainerProfile
        so organization admins do not have to manually re-register existing staff.
        """
        try:
            trainer_users = cls._trainer_users(alias, org).filter(status='ACTIVE')

            for user in trainer_users:
                # Do not auto-convert superusers who are not staff
                if getattr(user, 'is_superuser', False) and user.user_type not in ('STAFF', 'TRAINER'):
                    continue

                preferred_branch = user.home_branch
                if not preferred_branch:
                    first_branch_ass = user.branch_assignments.using(alias).filter(status='ACTIVE').first()
                    if first_branch_ass:
                        preferred_branch = first_branch_ass.branch

                user_profile, created = UserProfile.objects.using(alias).get_or_create(
                    user=user,
                    defaults={
                        'first_name_snapshot': user.first_name,
                        'last_name_snapshot': user.last_name,
                        'preferred_branch': preferred_branch,
                    }
                )
                if not created and not user_profile.preferred_branch and preferred_branch:
                    user_profile.preferred_branch = preferred_branch
                    user_profile.save(using=alias, update_fields=['preferred_branch'])

                emp = EmployeeProfile.objects.using(alias).filter(user_profile=user_profile).first()
                if not emp:
                    emp = EmployeeProfile.objects.using(alias).create(
                        user_profile=user_profile,
                        organization=user.organization or org,
                        employee_code=f"EMP-{user.id.hex[:6].upper()}",
                        designation='Fitness Trainer',
                        employment_status='ACTIVE',
                    )

                if not TrainerProfile.objects.using(alias).filter(employee_profile=emp).exists():
                    TrainerProfile.objects.using(alias).create(
                        employee_profile=emp,
                        trainer_code=f"TRN-{user.id.hex[:6].upper()}",
                        trainer_status='ACTIVE',
                        can_teach_all_specialties=True,
                        minimum_schedule_buffer_minutes=15,
                    )
        except Exception as e:
            logger.debug(f"Auto trainer sync non-fatal: {e}")

    def get_queryset(self):
        alias = _get_db(self.request)
        org = getattr(self.request, 'organization', None)
        self._ensure_trainer_profiles_for_staff(alias, org)

        qs = TrainerProfile.objects.using(alias).select_related(
            'employee_profile',
            'employee_profile__user_profile',
            'employee_profile__user_profile__user',
            'employee_profile__user_profile__user__home_branch',
        ).prefetch_related(
            'specialty_assignments',
            'specialty_assignments__trainer_specialty',
            'employee_profile__user_profile__user__branch_assignments',
            'employee_profile__user_profile__user__branch_assignments__branch',
        )
        if org:
            qs = qs.filter(employee_profile__organization=org)
        qs = qs.filter(
            employee_profile__user_profile__user_id__in=
            self._trainer_users(alias, org).values('id')
        )
        status_filter = self.request.query_params.get('trainer_status')
        if status_filter:
            qs = qs.filter(trainer_status=status_filter)

        branch_id = self.request.query_params.get('branch_id')
        if branch_id and branch_id not in ('all', 'ALL', '00000000-0000-0000-0000-000000000000', ''):
            qs = qs.filter(
                Q(employee_profile__user_profile__preferred_branch_id=branch_id) |
                Q(employee_profile__user_profile__user__home_branch_id=branch_id) |
                Q(employee_profile__user_profile__user__branch_assignments__branch_id=branch_id) |
                Q(employee_profile__work_schedules__branch_id=branch_id)
            ).distinct()

        return qs

    def create(self, request, *args, **kwargs):
        alias = _get_db(request)
        org = getattr(request, 'organization', None)
        data = request.data.copy()

        user_id = data.get('user_id')
        if user_id and not data.get('employee_profile'):
            user = TenantUser.objects.using(alias).filter(id=user_id).first()
            if not user:
                return Response({'error': 'Selected staff user was not found.'}, status=status.HTTP_404_NOT_FOUND)

            user_profile, _ = UserProfile.objects.using(alias).get_or_create(
                user=user,
                defaults={
                    'first_name_snapshot': user.first_name,
                    'last_name_snapshot': user.last_name,
                }
            )

            emp = EmployeeProfile.objects.using(alias).filter(user_profile=user_profile).first()
            if not emp:
                emp_code = data.get('employee_code') or f"EMP-{uuid.uuid4().hex[:6].upper()}"
                emp = EmployeeProfile.objects.using(alias).create(
                    user_profile=user_profile,
                    organization=org or user.organization,
                    employee_code=emp_code,
                    designation=data.get('designation') or 'Fitness Trainer',
                    employment_status='ACTIVE',
                )

            if TrainerProfile.objects.using(alias).filter(employee_profile=emp).exists():
                return Response(
                    {'error': f"'{user.full_name or user.email}' is already registered as a trainer."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            data['employee_profile'] = str(emp.id)

        if not data.get('trainer_code'):
            data['trainer_code'] = f"TRN-{uuid.uuid4().hex[:6].upper()}"

        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def destroy(self, request, *args, **kwargs):
        alias = _get_db(request)
        instance = self.get_object()
        try:
            instance.specialty_assignments.using(alias).all().delete()
            self.perform_destroy(instance)
            return Response(status=status.HTTP_204_NO_CONTENT)
        except Exception as e:
            return Response(
                {'error': f"Cannot delete trainer: {str(e)}. Try setting their status to INACTIVE instead."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    @action(detail=True, methods=['get'], url_path='check-availability')
    def check_availability(self, request, pk=None):
        """
        Evaluate real-time availability for a single trainer on a specific slot.
        """
        alias = _get_db(request)
        trainer = self.get_object()

        start_str = request.query_params.get('start_datetime')
        if not start_str:
            return Response({'error': 'start_datetime query param is required'}, status=status.HTTP_400_BAD_REQUEST)
        start_dt = parse_datetime(start_str)
        if not start_dt:
            return Response({'error': 'Invalid ISO datetime format for start_datetime'}, status=status.HTTP_400_BAD_REQUEST)

        duration = int(request.query_params.get('duration_minutes', 60))
        branch_id = request.query_params.get('branch_id')
        branch = None
        if branch_id and branch_id not in ('all', '00000000-0000-0000-0000-000000000000'):
            branch = Branch.objects.using(alias).filter(id=branch_id).first()
        if not branch:
            branch = Branch.objects.using(alias).filter(organization=org).first() or Branch.objects.using(alias).first()
        if not branch:
            return Response({'error': 'No active branch found.'}, status=status.HTTP_404_NOT_FOUND)

        delivery_mode = request.query_params.get('delivery_mode', 'GROUP')
        specialty_code = request.query_params.get('specialty_code')
        min_proficiency = request.query_params.get('min_proficiency', 'BASIC')

        available, reason, details = TrainerAvailabilityService.is_trainer_available(
            trainer=trainer,
            branch=branch,
            start_datetime=start_dt,
            duration_minutes=duration,
            delivery_mode=delivery_mode,
            specialty_code=specialty_code,
            min_proficiency=min_proficiency,
            db_alias=alias,
        )
        return Response({
            'is_available': available,
            'reason': reason,
            'details': details,
        })

    @action(detail=False, methods=['get'], url_path='find-eligible')
    def find_eligible(self, request):
        """
        Scan all active trainers and return list of qualified, available trainers.
        """
        alias = _get_db(request)
        org = _get_org(request)
        if not org:
            return Response({'error': 'Organization context is required'}, status=status.HTTP_400_BAD_REQUEST)

        start_str = request.query_params.get('start_datetime')
        if not start_str:
            return Response({'error': 'start_datetime query param is required'}, status=status.HTTP_400_BAD_REQUEST)
        start_dt = parse_datetime(start_str)
        if not start_dt:
            return Response({'error': 'Invalid ISO datetime format for start_datetime'}, status=status.HTTP_400_BAD_REQUEST)

        duration = int(request.query_params.get('duration_minutes', 60))
        branch_id = request.query_params.get('branch_id')
        branch = None
        if branch_id and branch_id not in ('all', '00000000-0000-0000-0000-000000000000'):
            branch = Branch.objects.using(alias).filter(id=branch_id).first()
        if not branch:
            branch = Branch.objects.using(alias).filter(organization=org).first() or Branch.objects.using(alias).first()
        if not branch:
            return Response({'error': 'No active branch found.'}, status=status.HTTP_404_NOT_FOUND)

        delivery_mode = request.query_params.get('delivery_mode', 'GROUP')
        specialty_code = request.query_params.get('specialty_code')
        min_proficiency = request.query_params.get('min_proficiency', 'BASIC')

        eligible = TrainerAvailabilityService.find_eligible_trainers(
            organization=org,
            branch=branch,
            start_datetime=start_dt,
            duration_minutes=duration,
            delivery_mode=delivery_mode,
            specialty_code=specialty_code,
            min_proficiency=min_proficiency,
            db_alias=alias,
        )
        return Response({'count': len(eligible), 'results': eligible})


class SalesProfileViewSet(viewsets.ModelViewSet):
    serializer_class = SalesProfileSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'users'
    required_permission = 'core.users.view'
    permission_action_map = {
        'create': 'core.users.create',
        'update': 'core.users.edit',
        'partial_update': 'core.users.edit',
        'destroy': 'core.users.delete',
    }
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['sales_code', 'sales_type']
    ordering = ['sales_code']

    def get_queryset(self):
        alias = _get_db(self.request)
        org = getattr(self.request, 'organization', None)
        qs = SalesProfile.objects.using(alias).select_related('employee_profile', 'employee_profile__user_profile')
        if org:
            qs = qs.filter(employee_profile__organization=org)
        status_filter = self.request.query_params.get('sales_status')
        if status_filter:
            qs = qs.filter(sales_status=status_filter)
        return qs


class EmployeeWorkScheduleViewSet(viewsets.ModelViewSet):
    serializer_class = EmployeeWorkScheduleSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'ops'
    required_submodule = 'trainers'
    required_permission = 'ops.trainers.view'
    permission_action_map = {
        'create': 'ops.trainers.create',
        'update': 'ops.trainers.edit',
        'partial_update': 'ops.trainers.edit',
        'destroy': 'ops.trainers.delete',
        'bulk_sync': 'ops.trainers.edit',
    }
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    ordering = ['employee_profile', 'day_of_week', 'start_time']

    def get_queryset(self):
        alias = _get_db(self.request)
        org = getattr(self.request, 'organization', None)
        qs = EmployeeWorkSchedule.objects.using(alias).select_related('employee_profile', 'branch')
        if org:
            qs = qs.filter(employee_profile__organization=org)
        emp_id = self.request.query_params.get('employee_profile_id')
        if emp_id:
            qs = qs.filter(employee_profile_id=emp_id)
        branch_id = self.request.query_params.get('branch_id')
        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        # Calendar week / date filtering support
        for_date = self.request.query_params.get('for_date') or self.request.query_params.get('date')
        week_start = self.request.query_params.get('week_start')
        week_end = self.request.query_params.get('week_end') or week_start

        if for_date:
            qs = qs.filter(
                valid_from__lte=for_date
            ).filter(
                Q(valid_until__isnull=True) | Q(valid_until__gte=for_date)
            )
        elif week_start and week_end:
            qs = qs.filter(
                valid_from__lte=week_end
            ).filter(
                Q(valid_until__isnull=True) | Q(valid_until__gte=week_start)
            )

        return qs

    @action(detail=False, methods=['post'], url_path='bulk-sync')
    def bulk_sync(self, request):
        """
        Atomically synchronizes an employee's work schedule for a branch, either for
        a specific week ('SPECIFIC_WEEK') or as an ongoing recurring template from a week
        onwards ('RECURRING_FROM_WEEK').
        Any weekday not present in the payload is omitted/cleared, effectively
        designating it as Week-Off / Normally Unavailable.
        """
        alias = _get_db(request)
        data = request.data
        emp_id = data.get('employee_profile_id')
        branch_id = data.get('branch_id')
        schedules_data = data.get('schedules', [])

        week_start_date = data.get('week_start_date')
        week_end_date = data.get('week_end_date')
        apply_mode = data.get('apply_mode')
        if not apply_mode:
            apply_mode = 'SPECIFIC_WEEK' if week_end_date else 'RECURRING_FROM_WEEK'

        if not emp_id or not branch_id:
            return Response(
                {'error': 'employee_profile_id and branch_id are required.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        emp = EmployeeProfile.objects.using(alias).filter(id=emp_id).first()
        if not emp:
            return Response({'error': 'Employee profile not found.'}, status=status.HTTP_404_NOT_FOUND)

        branch = Branch.objects.using(alias).filter(id=branch_id).first()
        if not branch:
            return Response({'error': 'Branch not found.'}, status=status.HTTP_404_NOT_FOUND)

        with transaction.atomic(using=alias):
            today_str = timezone.now().date().isoformat()

            if week_start_date:
                if apply_mode == 'SPECIFIC_WEEK' and week_end_date:
                    # Clear any existing specific schedules matching this exact week window
                    EmployeeWorkSchedule.objects.using(alias).filter(
                        employee_profile=emp,
                        branch=branch,
                        valid_from=week_start_date,
                        valid_until=week_end_date,
                    ).delete()

                    target_valid_from = week_start_date
                    target_valid_until = week_end_date
                    target_schedule_type = 'TEMPORARY'
                else:
                    # RECURRING_FROM_WEEK
                    # Remove schedules starting on or after week_start_date
                    EmployeeWorkSchedule.objects.using(alias).filter(
                        employee_profile=emp,
                        branch=branch,
                        valid_from__gte=week_start_date,
                    ).delete()

                    # For older ongoing schedules, cap them at week_start_date - 1 day
                    from datetime import timedelta
                    start_dt = datetime.strptime(week_start_date, '%Y-%m-%d').date()
                    day_before = (start_dt - timedelta(days=1)).isoformat()
                    EmployeeWorkSchedule.objects.using(alias).filter(
                        employee_profile=emp,
                        branch=branch,
                        valid_from__lt=week_start_date,
                    ).filter(
                        Q(valid_until__isnull=True) | Q(valid_until__gte=week_start_date)
                    ).update(valid_until=day_before)

                    target_valid_from = week_start_date
                    target_valid_until = None
                    target_schedule_type = 'REGULAR'
            else:
                # Legacy / fallback behavior: remove existing regular shifts
                EmployeeWorkSchedule.objects.using(alias).filter(
                    employee_profile=emp,
                    branch=branch,
                    schedule_type='REGULAR'
                ).delete()
                target_valid_from = today_str
                target_valid_until = None
                target_schedule_type = 'REGULAR'

            created_objects = []
            for item in schedules_data:
                dow = item.get('day_of_week')
                start_t = item.get('start_time')
                end_t = item.get('end_time')
                if not dow or not start_t or not end_t:
                    continue

                item_valid_from = item.get('valid_from') or target_valid_from
                item_valid_until = item.get('valid_until') if 'valid_until' in item else target_valid_until
                item_sched_type = item.get('schedule_type') or target_schedule_type

                obj = EmployeeWorkSchedule.objects.using(alias).create(
                    employee_profile=emp,
                    branch=branch,
                    day_of_week=int(dow),
                    start_time=start_t,
                    end_time=end_t,
                    valid_from=item_valid_from,
                    valid_until=item_valid_until,
                    schedule_type=item_sched_type,
                    status='ACTIVE',
                )
                created_objects.append(obj)

        serializer = self.get_serializer(created_objects, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class EmployeeScheduleExceptionViewSet(viewsets.ModelViewSet):
    serializer_class = EmployeeScheduleExceptionSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'ops'
    required_submodule = 'trainers'
    required_permission = 'ops.trainers.view'
    permission_action_map = {
        'create': 'ops.trainers.create',
        'update': 'ops.trainers.edit',
        'partial_update': 'ops.trainers.edit',
        'destroy': 'ops.trainers.delete',
    }
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    ordering = ['-exception_date']

    def get_queryset(self):
        alias = _get_db(self.request)
        org = getattr(self.request, 'organization', None)
        qs = EmployeeScheduleException.objects.using(alias).select_related('employee_profile', 'branch')
        if org:
            qs = qs.filter(employee_profile__organization=org)
        emp_id = self.request.query_params.get('employee_profile_id')
        if emp_id:
            qs = qs.filter(employee_profile_id=emp_id)
        branch_id = self.request.query_params.get('branch_id')
        if branch_id:
            qs = qs.filter(Q(branch_id=branch_id) | Q(branch__isnull=True))
        date_from = self.request.query_params.get('date_from')
        if date_from:
            qs = qs.filter(exception_date__gte=date_from)
        date_to = self.request.query_params.get('date_to')
        if date_to:
            qs = qs.filter(exception_date__lte=date_to)
        return qs


class TrainerSpecialtyViewSet(viewsets.ModelViewSet):
    serializer_class = TrainerSpecialtySerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'ops'
    required_submodule = 'trainers'
    required_permission = 'ops.trainers.view'
    permission_action_map = {
        'create': 'ops.trainers.create',
        'update': 'ops.trainers.edit',
        'partial_update': 'ops.trainers.edit',
        'destroy': 'ops.trainers.delete',
    }
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['code', 'name', 'category']
    ordering = ['code']

    def get_queryset(self):
        alias = _get_db(self.request)
        org = getattr(self.request, 'organization', None)
        qs = TrainerSpecialty.objects.using(alias).all()
        if org:
            qs = qs.filter(organization=org)
        status_filter = self.request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        return qs

    def perform_create(self, serializer):
        org = getattr(self.request, 'organization', None)
        serializer.save(organization=org)


class TrainerSpecialtyAssignmentViewSet(viewsets.ModelViewSet):
    serializer_class = TrainerSpecialtyAssignmentSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'ops'
    required_submodule = 'trainers'
    required_permission = 'ops.trainers.view'
    permission_action_map = {
        'create': 'ops.trainers.create',
        'update': 'ops.trainers.edit',
        'partial_update': 'ops.trainers.edit',
        'destroy': 'ops.trainers.delete',
    }
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    ordering = ['trainer_profile', 'trainer_specialty']

    def get_queryset(self):
        alias = _get_db(self.request)
        org = getattr(self.request, 'organization', None)
        qs = TrainerSpecialtyAssignment.objects.using(alias).select_related(
            'trainer_profile',
            'trainer_specialty',
            'branch',
        )
        if org:
            qs = qs.filter(trainer_profile__employee_profile__organization=org)
        trainer_id = self.request.query_params.get('trainer_profile_id')
        if trainer_id:
            qs = qs.filter(trainer_profile_id=trainer_id)
        return qs
