"""
apps/tenant_core/views_crm.py — ViewSets for Layer 2 Module B: CRM, Leads, Trials & Sales

Endpoints:
- LeadSourceViewSet: /api/v1/tenant/lead-sources/
- LeadViewSet: /api/v1/tenant/leads/
  + transition-status (detail=True, POST)
  + assign (detail=True, POST)
  + book-trial (detail=True, POST)
- LeadStatusHistoryViewSet: /api/v1/tenant/lead-status-history/
- LeadAssignmentViewSet: /api/v1/tenant/lead-assignments/
- LeadNoteViewSet: /api/v1/tenant/lead-notes/
- LeadActivityViewSet: /api/v1/tenant/lead-activities/
- IntakeFormViewSet: /api/v1/tenant/intake-forms/
  + submit (detail=True, POST)
- IntakeQuestionViewSet: /api/v1/tenant/intake-questions/
- IntakeQuestionOptionViewSet: /api/v1/tenant/intake-question-options/
- IntakeSubmissionViewSet: /api/v1/tenant/intake-submissions/
- TrialBookingViewSet: /api/v1/tenant/trial-bookings/
  + transition-status (detail=True, POST)
- TrialStatusHistoryViewSet: /api/v1/tenant/trial-status-history/
- LeadConversionViewSet: /api/v1/tenant/lead-conversions/
- SalesFollowupTaskViewSet: /api/v1/tenant/sales-followup-tasks/
"""

import logging
from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db.models import Q
from django.utils.dateparse import parse_datetime
from config.routers import get_tenant_db_alias

from .models_org import Organization, Branch
from .models_users import TenantUser
from .models_workforce import TrainerProfile
from .models_crm import (
    LeadSource,
    Lead,
    LeadStatusHistory,
    LeadAssignment,
    LeadNote,
    LeadActivity,
    IntakeForm,
    IntakeQuestion,
    IntakeQuestionOption,
    IntakeSubmission,
    IntakeAnswer,
    TrialBooking,
    TrialStatusHistory,
    LeadConversion,
    SalesFollowupTask,
)
from .serializers_crm import (
    LeadSourceSerializer,
    LeadSerializer,
    LeadStatusHistorySerializer,
    LeadAssignmentSerializer,
    LeadNoteSerializer,
    LeadActivitySerializer,
    IntakeFormSerializer,
    IntakeQuestionSerializer,
    IntakeQuestionOptionSerializer,
    IntakeSubmissionSerializer,
    IntakeAnswerSerializer,
    TrialBookingSerializer,
    TrialStatusHistorySerializer,
    LeadConversionSerializer,
    SalesFollowupTaskSerializer,
)
from .services_crm import CRMLeadService
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
        if user and getattr(user, 'organization_id', None):
            return Organization.objects.using(alias).filter(id=user.organization_id).first()
        return Organization.objects.using(alias).filter(status='ACTIVE').first()
    return org


class LeadSourceViewSet(viewsets.ModelViewSet):
    serializer_class = LeadSourceSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'crm'
    required_submodule = 'leads'
    required_permission = 'crm.leads.view'
    permission_action_map = {
        'create': 'crm.leads.create',
        'update': 'crm.leads.edit',
        'partial_update': 'crm.leads.edit',
        'destroy': 'crm.leads.delete',
    }
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['code', 'name', 'source_type']
    ordering = ['name']

    def get_queryset(self):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        qs = LeadSource.objects.using(alias).all()
        if org:
            qs = qs.filter(organization=org)
        status_val = self.request.query_params.get('status')
        if status_val:
            qs = qs.filter(status=status_val)
        return qs

    def perform_create(self, serializer):
        org = _get_org(self.request)
        serializer.save(organization=org)


class LeadViewSet(viewsets.ModelViewSet):
    serializer_class = LeadSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'crm'
    required_submodule = 'leads'
    required_permission = 'crm.leads.view'
    permission_action_map = {
        'create': 'crm.leads.create',
        'update': 'crm.leads.edit',
        'partial_update': 'crm.leads.edit',
        'destroy': 'crm.leads.delete',
        'transition_status': 'crm.leads.edit',
        'assign': 'crm.leads.edit',
        'book_trial': 'crm.leads.edit',
    }
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['first_name', 'last_name', 'phone_normalized', 'email_normalized', 'company_name']
    ordering_fields = ['created_at', 'first_name', 'current_status']
    ordering = ['-created_at']

    def get_queryset(self):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        qs = Lead.objects.using(alias).select_related(
            'organization', 'branch', 'lead_source', 'assigned_sales_user', 'assigned_trainer_user'
        )
        if org:
            qs = qs.filter(organization=org)
        status_val = self.request.query_params.get('current_status')
        if status_val:
            qs = qs.filter(current_status=status_val)
        branch_id = self.request.query_params.get('branch_id')
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        assigned_user = self.request.query_params.get('assigned_sales_user_id')
        if assigned_user:
            qs = qs.filter(assigned_sales_user_id=assigned_user)
        return qs

    def perform_create(self, serializer):
        org = _get_org(self.request)
        lead = CRMLeadService.create_lead(
            organization=org,
            first_name=serializer.validated_data.get('first_name'),
            last_name=serializer.validated_data.get('last_name'),
            phone=serializer.validated_data.get('phone_normalized'),
            email=serializer.validated_data.get('email_normalized'),
            branch=serializer.validated_data.get('branch'),
            lead_source=serializer.validated_data.get('lead_source'),
            assigned_sales_user=serializer.validated_data.get('assigned_sales_user'),
            actor_user=getattr(self.request, 'user', None),
            extra_fields={
                k: v for k, v in serializer.validated_data.items()
                if k not in ('first_name', 'last_name', 'phone_normalized', 'email_normalized', 'branch', 'lead_source', 'assigned_sales_user')
            },
            db_alias=_get_db(self.request),
        )
        serializer.instance = lead

    @action(detail=True, methods=['post'], url_path='transition-status')
    def transition_status(self, request, pk=None):
        lead = self.get_object()
        new_status = request.data.get('new_status')
        reason_code = request.data.get('reason_code')
        reason_text = request.data.get('reason_text')

        if not new_status:
            return Response({'error': 'new_status is required'}, status=status.HTTP_400_BAD_REQUEST)

        updated_lead = CRMLeadService.transition_lead_status(
            lead=lead,
            new_status=new_status,
            reason_code=reason_code,
            reason_text=reason_text,
            actor_user=request.user,
            db_alias=_get_db(request),
        )
        return Response(LeadSerializer(updated_lead).data)

    @action(detail=True, methods=['post'], url_path='assign')
    def assign(self, request, pk=None):
        alias = _get_db(request)
        lead = self.get_object()
        user_id = request.data.get('assigned_to_user_id')
        assignment_type = request.data.get('assignment_type', 'SALES')

        if not user_id:
            return Response({'error': 'assigned_to_user_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            target_user = TenantUser.objects.using(alias).get(id=user_id)
        except TenantUser.DoesNotExist:
            return Response({'error': 'Target user not found'}, status=status.HTTP_404_NOT_FOUND)

        assignment = CRMLeadService.assign_lead(
            lead=lead,
            assigned_to_user=target_user,
            assignment_type=assignment_type,
            actor_user=request.user,
            db_alias=alias,
        )
        return Response(LeadAssignmentSerializer(assignment).data)

    @action(detail=True, methods=['post'], url_path='book-trial')
    def book_trial(self, request, pk=None):
        alias = _get_db(request)
        lead = self.get_object()

        branch_id = request.data.get('branch_id') or getattr(lead, 'branch_id', None)
        if not branch_id:
            return Response({'error': 'branch_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            branch = Branch.objects.using(alias).get(id=branch_id)
        except Branch.DoesNotExist:
            return Response({'error': 'Branch not found'}, status=status.HTTP_404_NOT_FOUND)

        start_str = request.data.get('scheduled_start')
        end_str = request.data.get('scheduled_end')
        if not start_str or not end_str:
            return Response({'error': 'scheduled_start and scheduled_end are required'}, status=status.HTTP_400_BAD_REQUEST)

        start_dt = parse_datetime(start_str)
        end_dt = parse_datetime(end_str)

        trainer_id = request.data.get('assigned_trainer_profile_id')
        trainer = None
        if trainer_id:
            try:
                trainer = TrainerProfile.objects.using(alias).get(id=trainer_id)
            except TrainerProfile.DoesNotExist:
                return Response({'error': 'Trainer profile not found'}, status=status.HTTP_404_NOT_FOUND)

        try:
            trial = CRMLeadService.book_trial(
                lead=lead,
                branch=branch,
                scheduled_start=start_dt,
                scheduled_end=end_dt,
                assigned_trainer=trainer,
                trial_type=request.data.get('trial_type', 'GROUP_CLASS'),
                booking_source=request.data.get('booking_source', 'WEB'),
                actor_user=request.user,
                db_alias=alias,
            )
            return Response(TrialBookingSerializer(trial).data, status=status.HTTP_201_CREATED)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class LeadStatusHistoryViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = LeadStatusHistorySerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'crm'
    required_submodule = 'leads'
    required_permission = 'crm.leads.view'
    ordering = ['-changed_at']

    def get_queryset(self):
        alias = _get_db(self.request)
        qs = LeadStatusHistory.objects.using(alias).select_related('lead', 'changed_by_user')
        lead_id = self.request.query_params.get('lead_id')
        if lead_id:
            qs = qs.filter(lead_id=lead_id)
        return qs


class LeadAssignmentViewSet(viewsets.ModelViewSet):
    serializer_class = LeadAssignmentSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'crm'
    required_submodule = 'leads'
    required_permission = 'crm.leads.view'
    permission_action_map = {
        'create': 'crm.leads.create',
        'update': 'crm.leads.edit',
        'partial_update': 'crm.leads.edit',
        'destroy': 'crm.leads.delete',
    }
    ordering = ['-assigned_at']

    def get_queryset(self):
        alias = _get_db(self.request)
        qs = LeadAssignment.objects.using(alias).select_related('lead', 'assigned_to_user', 'assigned_by_user')
        lead_id = self.request.query_params.get('lead_id')
        if lead_id:
            qs = qs.filter(lead_id=lead_id)
        return qs


class LeadNoteViewSet(viewsets.ModelViewSet):
    serializer_class = LeadNoteSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'crm'
    required_submodule = 'leads'
    required_permission = 'crm.leads.view'
    permission_action_map = {
        'create': 'crm.leads.create',
        'update': 'crm.leads.edit',
        'partial_update': 'crm.leads.edit',
        'destroy': 'crm.leads.delete',
    }
    ordering = ['-created_at']

    def get_queryset(self):
        alias = _get_db(self.request)
        qs = LeadNote.objects.using(alias).select_related('lead', 'created_by_user')
        lead_id = self.request.query_params.get('lead_id')
        if lead_id:
            qs = qs.filter(lead_id=lead_id)
        return qs

    def perform_create(self, serializer):
        serializer.save(created_by_user=self.request.user)


class LeadActivityViewSet(viewsets.ModelViewSet):
    serializer_class = LeadActivitySerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'crm'
    required_submodule = 'leads'
    required_permission = 'crm.leads.view'
    permission_action_map = {
        'create': 'crm.leads.create',
        'update': 'crm.leads.edit',
        'partial_update': 'crm.leads.edit',
        'destroy': 'crm.leads.delete',
    }
    ordering = ['-activity_at']

    def get_queryset(self):
        alias = _get_db(self.request)
        qs = LeadActivity.objects.using(alias).select_related('lead', 'performed_by_user')
        lead_id = self.request.query_params.get('lead_id')
        if lead_id:
            qs = qs.filter(lead_id=lead_id)
        return qs

    def perform_create(self, serializer):
        serializer.save(performed_by_user=self.request.user)


class IntakeFormViewSet(viewsets.ModelViewSet):
    serializer_class = IntakeFormSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'crm'
    required_submodule = 'leads'
    required_permission = 'crm.leads.view'
    permission_action_map = {
        'create': 'crm.leads.create',
        'update': 'crm.leads.edit',
        'partial_update': 'crm.leads.edit',
        'destroy': 'crm.leads.delete',
        'submit': 'crm.leads.create',
    }
    ordering = ['-version_number']

    def get_queryset(self):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        qs = IntakeForm.objects.using(alias).prefetch_related('questions', 'questions__options')
        if org:
            qs = qs.filter(organization=org)
        status_val = self.request.query_params.get('status')
        if status_val:
            qs = qs.filter(status=status_val)
        return qs

    def perform_create(self, serializer):
        org = _get_org(self.request)
        serializer.save(organization=org)

    @action(detail=True, methods=['post'], url_path='submit')
    def submit(self, request, pk=None):
        alias = _get_db(request)
        form = self.get_object()
        answers = request.data.get('answers', [])
        lead_id = request.data.get('lead_id')
        user_profile_id = request.data.get('user_profile_id')

        lead = None
        if lead_id:
            lead = Lead.objects.using(alias).filter(id=lead_id).first()

        submission = CRMLeadService.submit_intake_form(
            intake_form=form,
            answers=answers,
            lead=lead,
            submitted_by_user=request.user,
            db_alias=alias,
        )
        return Response(IntakeSubmissionSerializer(submission).data, status=status.HTTP_201_CREATED)


class IntakeQuestionViewSet(viewsets.ModelViewSet):
    serializer_class = IntakeQuestionSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'crm'
    required_submodule = 'leads'
    required_permission = 'crm.leads.view'
    permission_action_map = {
        'create': 'crm.leads.create',
        'update': 'crm.leads.edit',
        'partial_update': 'crm.leads.edit',
        'destroy': 'crm.leads.delete',
    }
    ordering = ['display_order']

    def get_queryset(self):
        alias = _get_db(self.request)
        qs = IntakeQuestion.objects.using(alias).prefetch_related('options')
        form_id = self.request.query_params.get('intake_form_id')
        if form_id:
            qs = qs.filter(intake_form_id=form_id)
        return qs


class IntakeQuestionOptionViewSet(viewsets.ModelViewSet):
    serializer_class = IntakeQuestionOptionSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'crm'
    required_submodule = 'leads'
    required_permission = 'crm.leads.view'
    permission_action_map = {
        'create': 'crm.leads.create',
        'update': 'crm.leads.edit',
        'partial_update': 'crm.leads.edit',
        'destroy': 'crm.leads.delete',
    }
    ordering = ['display_order']

    def get_queryset(self):
        alias = _get_db(self.request)
        qs = IntakeQuestionOption.objects.using(alias).all()
        q_id = self.request.query_params.get('question_id')
        if q_id:
            qs = qs.filter(question_id=q_id)
        return qs


class IntakeSubmissionViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = IntakeSubmissionSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'crm'
    required_submodule = 'leads'
    required_permission = 'crm.leads.view'
    ordering = ['-submitted_at']

    def get_queryset(self):
        alias = _get_db(self.request)
        qs = IntakeSubmission.objects.using(alias).select_related('intake_form', 'lead').prefetch_related('answers', 'answers__question')
        lead_id = self.request.query_params.get('lead_id')
        if lead_id:
            qs = qs.filter(lead_id=lead_id)
        return qs


class TrialBookingViewSet(viewsets.ModelViewSet):
    serializer_class = TrialBookingSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'crm'
    required_submodule = 'leads'
    required_permission = 'crm.leads.view'
    permission_action_map = {
        'create': 'crm.leads.create',
        'update': 'crm.leads.edit',
        'partial_update': 'crm.leads.edit',
        'destroy': 'crm.leads.delete',
        'transition_status': 'crm.leads.edit',
    }
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['lead__first_name', 'lead__last_name', 'trial_type']
    ordering = ['-scheduled_start']

    def get_queryset(self):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        qs = TrialBooking.objects.using(alias).select_related('lead', 'branch', 'assigned_trainer_profile')
        if org:
            qs = qs.filter(lead__organization=org)
        status_val = self.request.query_params.get('status')
        if status_val:
            qs = qs.filter(status=status_val)
        branch_id = self.request.query_params.get('branch_id')
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        return qs

    @action(detail=True, methods=['post'], url_path='transition-status')
    def transition_status(self, request, pk=None):
        trial = self.get_object()
        new_status = request.data.get('new_status')
        reason_code = request.data.get('reason_code')

        if not new_status:
            return Response({'error': 'new_status is required'}, status=status.HTTP_400_BAD_REQUEST)

        updated_trial = CRMLeadService.transition_trial_status(
            trial=trial,
            new_status=new_status,
            reason_code=reason_code,
            actor_user=request.user,
            db_alias=_get_db(request),
        )
        return Response(TrialBookingSerializer(updated_trial).data)


class TrialStatusHistoryViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = TrialStatusHistorySerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'crm'
    required_submodule = 'leads'
    required_permission = 'crm.leads.view'
    ordering = ['-changed_at']

    def get_queryset(self):
        alias = _get_db(self.request)
        qs = TrialStatusHistory.objects.using(alias).select_related('trial_booking')
        trial_id = self.request.query_params.get('trial_booking_id')
        if trial_id:
            qs = qs.filter(trial_booking_id=trial_id)
        return qs


class LeadConversionViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = LeadConversionSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'crm'
    required_submodule = 'leads'
    required_permission = 'crm.leads.view'
    ordering = ['-converted_at']

    def get_queryset(self):
        alias = _get_db(self.request)
        qs = LeadConversion.objects.using(alias).select_related('lead', 'user_profile')
        lead_id = self.request.query_params.get('lead_id')
        if lead_id:
            qs = qs.filter(lead_id=lead_id)
        return qs


class SalesFollowupTaskViewSet(viewsets.ModelViewSet):
    serializer_class = SalesFollowupTaskSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'crm'
    required_submodule = 'leads'
    required_permission = 'crm.leads.view'
    permission_action_map = {
        'create': 'crm.leads.create',
        'update': 'crm.leads.edit',
        'partial_update': 'crm.leads.edit',
        'destroy': 'crm.leads.delete',
    }
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['task_type', 'priority', 'lead__first_name', 'lead__last_name']
    ordering = ['due_at']

    def get_queryset(self):
        alias = _get_db(self.request)
        qs = SalesFollowupTask.objects.using(alias).select_related('lead', 'assigned_to_user')
        lead_id = self.request.query_params.get('lead_id')
        if lead_id:
            qs = qs.filter(lead_id=lead_id)
        assigned_to = self.request.query_params.get('assigned_to_user_id')
        if assigned_to:
            qs = qs.filter(assigned_to_user_id=assigned_to)
        status_val = self.request.query_params.get('status')
        if status_val:
            qs = qs.filter(status=status_val)
        return qs

    def perform_create(self, serializer):
        serializer.save(created_by_user=self.request.user)
