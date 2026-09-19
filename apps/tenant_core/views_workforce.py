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

    def get_queryset(self):
        alias = _get_db(self.request)
        org = getattr(self.request, 'organization', None)
        qs = TrainerProfile.objects.using(alias).select_related(
            'employee_profile',
            'employee_profile__user_profile',
            'employee_profile__user_profile__user',
        ).prefetch_related('specialty_assignments', 'specialty_assignments__trainer_specialty')
        if org:
            qs = qs.filter(employee_profile__organization=org)
        status_filter = self.request.query_params.get('trainer_status')
        if status_filter:
            qs = qs.filter(trainer_status=status_filter)
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
        return qs


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
