"""
apps/tenant_core/views_appointments.py — ViewSets for Layer 2 Module F: Individual Appointments
"""

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.core.exceptions import ValidationError

from apps.tenant_core.permissions import RequireActiveTenantAndOrg, TenantRBACPermission
from apps.tenant_core.context import get_tenant_db_alias

from .models_appointments import (
    AppointmentType,
    AppointmentTypeSpecialtyRequirement,
    Appointment,
    AppointmentTrainer,
)
from .serializers_appointments import (
    AppointmentTypeSerializer,
    AppointmentTypeSpecialtyRequirementSerializer,
    AppointmentSerializer,
    AppointmentTrainerSerializer,
)
from .services_appointments import AppointmentSchedulingService


def _get_db(request):
    return get_tenant_db_alias() or 'default'


class AppointmentTypeViewSet(viewsets.ModelViewSet):
    serializer_class = AppointmentTypeSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'
    permission_action_map = {
        'create': 'core.settings.edit',
        'update': 'core.settings.edit',
        'destroy': 'core.settings.edit',
    }

    def get_queryset(self):
        alias = _get_db(self.request)
        return AppointmentType.objects.using(alias).prefetch_related('specialty_requirements').all().order_by('name')

    def perform_create(self, serializer):
        alias = _get_db(self.request)
        org = self.request.user.organization
        serializer.save(organization=org)


class AppointmentTypeSpecialtyRequirementViewSet(viewsets.ModelViewSet):
    serializer_class = AppointmentTypeSpecialtyRequirementSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'
    permission_action_map = {
        'create': 'core.settings.edit',
        'update': 'core.settings.edit',
        'destroy': 'core.settings.edit',
    }

    def get_queryset(self):
        alias = _get_db(self.request)
        qs = AppointmentTypeSpecialtyRequirement.objects.using(alias).all()
        appt_type_id = self.request.query_params.get('appointment_type_id')
        if appt_type_id:
            qs = qs.filter(appointment_type_id=appt_type_id)
        return qs.order_by('created_at')


class AppointmentViewSet(viewsets.ModelViewSet):
    serializer_class = AppointmentSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'
    permission_action_map = {
        'create': 'core.settings.edit',
        'update': 'core.settings.edit',
        'destroy': 'core.settings.edit',
        'assign_trainer': 'core.settings.edit',
        'cancel': 'core.settings.edit',
        'complete': 'core.settings.edit',
    }

    def get_queryset(self):
        alias = _get_db(self.request)
        qs = Appointment.objects.using(alias).all()
        branch_id = self.request.query_params.get('branch_id')
        user_profile_id = self.request.query_params.get('user_profile_id')
        date_param = self.request.query_params.get('date')
        status_param = self.request.query_params.get('status')

        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if user_profile_id:
            qs = qs.filter(user_profile_id=user_profile_id)
        if date_param:
            qs = qs.filter(start_at__date=date_param)
        if status_param:
            qs = qs.filter(status=status_param)

        return qs.select_related('appointment_type', 'branch', 'user_profile').prefetch_related(
            'assigned_trainers__trainer_profile__employee_profile__user_profile'
        ).order_by('start_at')

    @action(detail=True, methods=['post'], url_path='assign-trainer')
    def assign_trainer(self, request, pk=None):
        alias = _get_db(request)
        trainer_profile_id = request.data.get('trainer_profile_id')
        role = request.data.get('role', 'LEAD')
        if not trainer_profile_id:
            return Response({'error': 'trainer_profile_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            assignment = AppointmentSchedulingService.assign_trainer_to_appointment(
                appointment_id=pk,
                trainer_profile_id=trainer_profile_id,
                role=role,
                actor=request.user,
                db_alias=alias,
            )
            return Response(AppointmentTrainerSerializer(assignment).data, status=status.HTTP_201_CREATED)
        except (ValidationError, Exception) as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='cancel')
    def cancel(self, request, pk=None):
        alias = _get_db(request)
        reason = request.data.get('reason')
        try:
            appt = AppointmentSchedulingService.cancel_appointment(
                appointment_id=pk,
                reason=reason,
                actor=request.user,
                db_alias=alias,
            )
            return Response(AppointmentSerializer(appt).data, status=status.HTTP_200_OK)
        except (ValidationError, Exception) as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='complete')
    def complete(self, request, pk=None):
        alias = _get_db(request)
        try:
            appt = AppointmentSchedulingService.complete_appointment(
                appointment_id=pk,
                actor=request.user,
                db_alias=alias,
            )
            return Response(AppointmentSerializer(appt).data, status=status.HTTP_200_OK)
        except (ValidationError, Exception) as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


class AppointmentTrainerViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = AppointmentTrainerSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'settings'
    required_permission = 'core.settings.view'

    def get_queryset(self):
        alias = _get_db(self.request)
        return AppointmentTrainer.objects.using(alias).select_related(
            'trainer_profile__employee_profile__user_profile', 'appointment'
        ).all().order_by('-assigned_at')
