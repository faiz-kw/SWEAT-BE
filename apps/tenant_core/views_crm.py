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
from decimal import Decimal
from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.response import Response
import re
from typing import Optional, Set
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.shortcuts import get_object_or_404
from django.core.exceptions import ValidationError
from rest_framework.exceptions import PermissionDenied
from config.routers import get_tenant_db_alias

from .models_org import Organization, Branch
from .models_users import TenantUser
from .models_workforce import TrainerProfile
from .models_govern import InAppNotification
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
    CRMStageSlaPolicy,
    CRMTrialReminderPolicy,
    CRMAgentAssignmentConfig,
    LeadAttribution,
)
from .serializers_crm import (
    LeadSourceSerializer,
    LeadSerializer,
    LeadAttributionSerializer,
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
    CRMStageSlaPolicySerializer,
    CRMTrialReminderPolicySerializer,
    CRMAttentionPolicySerializer,
    CRMAgentAssignmentConfigSerializer,
    InAppNotificationSerializer,
)
from .models_attention import CRMAttentionPolicy
from .models_communication import CommunicationMessage, CommunicationStatusEvent
from .serializers_communication import (
    CommunicationMessageSerializer,
    CommunicationSendInputSerializer,
    CommunicationChannelStatusSerializer,
)
from .communication.service import CommunicationService, ConsentViolationError
from .communication.registry import CommunicationProviderRegistry
from .services_crm import CRMLeadService
from .services_reliability import record_business_audit
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
        return Organization.objects.using(alias).filter(status='ACTIVE').order_by('-created_at').first()
    return org


def get_user_effective_branch_ids(user, db_alias: str = 'default') -> Optional[Set[str]]:
    """
    Derives the effective permitted branch IDs for the user based on active RoleAssignments,
    role scopes, and UserBranch records.
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

    if not assignments:
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


class LeadSourceViewSet(viewsets.ModelViewSet):
    serializer_class = LeadSourceSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'crm'
    required_submodule = 'leads'
    required_permission = 'crm.leads.view'
    permission_action_map = {
        'create': 'crm.settings.edit',
        'update': 'crm.settings.edit',
        'partial_update': 'crm.settings.edit',
        'destroy': 'crm.settings.edit',
        'activate': 'crm.settings.edit',
        'deactivate': 'crm.settings.edit',
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
            if not qs.exists():
                default_sources = [
                    ('WALK_IN', 'Walk In', 'WALK_IN'),
                    ('WEBSITE', 'Website', 'WEBSITE'),
                    ('META', 'Instagram / Meta', 'META'),
                    ('GOOGLE', 'Google Search', 'GOOGLE'),
                    ('WHATSAPP', 'WhatsApp Inquiry', 'WHATSAPP'),
                    ('PHONE', 'Phone Call', 'PHONE'),
                    ('REFERRAL', 'Member Referral', 'REFERRAL'),
                    ('CORPORATE', 'Corporate Tie-up', 'OTHER'),
                    ('CAMPAIGN', 'Marketing Campaign', 'CAMPAIGN'),
                    ('OTHER', 'Other / Unknown', 'OTHER'),
                ]
                for code, name, stype in default_sources:
                    LeadSource.objects.using(alias).get_or_create(
                        organization=org,
                        code=code,
                        defaults={'name': name, 'source_type': stype, 'status': 'ACTIVE'},
                    )
                qs = LeadSource.objects.using(alias).filter(organization=org)
        status_val = self.request.query_params.get('status')
        if status_val:
            qs = qs.filter(status=status_val)
        active_only = self.request.query_params.get('active_only')
        if active_only and str(active_only).lower() in ('true', '1'):
            qs = qs.filter(status='ACTIVE')
        return qs

    def perform_create(self, serializer):
        org = _get_org(self.request)
        alias = _get_db(self.request)
        if not serializer.validated_data.get('source_type'):
            serializer.validated_data['source_type'] = 'OTHER'
        code = serializer.validated_data.get('code')
        name = serializer.validated_data.get('name', '').strip()
        if not code:
            base_code = re.sub(r'[^A-Z0-9]+', '_', name.upper()).strip('_') or 'SOURCE'
            unique_code = base_code
            counter = 1
            while LeadSource.objects.using(alias).filter(organization=org, code=unique_code).exists():
                unique_code = f"{base_code}_{counter}"
                counter += 1
            serializer.validated_data['code'] = unique_code
        instance = serializer.save(organization=org)
        record_business_audit(
            organization=org,
            module='crm',
            action_code='CRM_LEAD_SOURCE_CREATED',
            entity_type='LeadSource',
            entity_id=instance.id,
            actor_user=getattr(self.request, 'user', None),
            metadata={'code': instance.code, 'name': instance.name, 'source_type': instance.source_type},
            db_alias=alias,
        )

    def perform_update(self, serializer):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        old_status = serializer.instance.status
        instance = serializer.save()
        action_code = 'CRM_LEAD_SOURCE_UPDATED'
        if old_status != instance.status:
            action_code = 'CRM_LEAD_SOURCE_ACTIVATED' if instance.status == 'ACTIVE' else 'CRM_LEAD_SOURCE_DEACTIVATED'
        record_business_audit(
            organization=org,
            module='crm',
            action_code=action_code,
            entity_type='LeadSource',
            entity_id=instance.id,
            actor_user=getattr(self.request, 'user', None),
            metadata={'code': instance.code, 'name': instance.name, 'status': instance.status},
            db_alias=alias,
        )

    def destroy(self, request, *args, **kwargs):
        alias = _get_db(request)
        instance = self.get_object()
        # Reject deletion with 400 if historical lead references exist
        if Lead.objects.using(alias).filter(lead_source=instance).exists():
            return Response(
                {'error': 'Permanent deletion of lead sources is not permitted as historical references exist. Please deactivate the lead source instead.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Soft-deactivate to preserve audit trail and references
        instance.status = 'INACTIVE'
        instance.save(using=alias, update_fields=['status', 'updated_at'])
        org = _get_org(request)
        record_business_audit(
            organization=org,
            module='crm',
            action_code='CRM_LEAD_SOURCE_DEACTIVATED',
            entity_type='LeadSource',
            entity_id=instance.id,
            actor_user=getattr(request, 'user', None),
            metadata={'code': instance.code, 'name': instance.name, 'status': 'INACTIVE'},
            db_alias=alias,
        )
        return Response({'status': 'INACTIVE', 'message': 'Lead source deactivated successfully.'}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='activate')
    def activate(self, request, pk=None):
        instance = self.get_object()
        instance.status = 'ACTIVE'
        instance.save(using=_get_db(request), update_fields=['status', 'updated_at'])
        alias = _get_db(request)
        org = _get_org(request)
        record_business_audit(
            organization=org,
            module='crm',
            action_code='CRM_LEAD_SOURCE_ACTIVATED',
            entity_type='LeadSource',
            entity_id=instance.id,
            actor_user=getattr(request, 'user', None),
            metadata={'code': instance.code, 'name': instance.name, 'status': 'ACTIVE'},
            db_alias=alias,
        )
        return Response({'status': 'ACTIVE', 'message': 'Lead source activated successfully.'}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='deactivate')
    def deactivate(self, request, pk=None):
        return self.destroy(request, pk=pk)


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
        'metadata': 'crm.leads.view',
        'eligible_agents': 'crm.leads.view',
        'eligible_assignees': 'crm.leads.view',
        'search_referrers': 'crm.leads.view',
        'check_duplicates': 'crm.leads.view',
        'timeline': 'crm.leads.view',
        'attributions': 'crm.leads.view',
        'activities': 'crm.leads.view',
        'followups': 'crm.leads.view',
        'attention': 'crm.leads.view',
        'attention_metrics': 'crm.leads.view',
        'next_action': 'crm.leads.view',
        'scan_sla_breaches': 'crm.leads.edit',
        'conversion_eligibility': 'crm.leads.convert',
        'conversion_quote': 'crm.leads.convert',
        'convert': 'crm.leads.convert',
    }
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['first_name', 'last_name', 'phone_normalized', 'email_normalized', 'company_name']
    ordering_fields = ['created_at', 'first_name', 'current_status']
    ordering = ['-created_at']

    def get_queryset(self):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        qs = Lead.objects.using(alias).select_related(
            'organization', 'branch', 'lead_source', 'assigned_sales_user', 'assigned_trainer_user',
            'interested_program', 'referred_by_user', 'commercial_profile'
        ).prefetch_related('attributions')
        if org:
            qs = qs.filter(organization=org)

        # Enforce server-side branch scope
        permitted_branches = get_user_effective_branch_ids(self.request.user, alias)
        if permitted_branches is not None:
            qs = qs.filter(branch_id__in=permitted_branches)

        status_val = self.request.query_params.get('current_status')
        if status_val:
            qs = qs.filter(current_status=status_val)

        branch_id = self.request.query_params.get('branch_id') or self.request.query_params.get('branch')
        if branch_id:
            if permitted_branches is not None and str(branch_id) not in permitted_branches:
                return qs.none()
            qs = qs.filter(branch_id=branch_id)

        assigned_to_me = self.request.query_params.get('assigned_to_me')
        if assigned_to_me and str(assigned_to_me).lower() in ('true', '1'):
            qs = qs.filter(assigned_sales_user=self.request.user)

        unassigned = self.request.query_params.get('unassigned')
        if unassigned and str(unassigned).lower() in ('true', '1'):
            qs = qs.filter(assigned_sales_user__isnull=True)

        assigned_user = self.request.query_params.get('assigned_sales_user_id')
        if assigned_user:
            qs = qs.filter(assigned_sales_user_id=assigned_user)

        lead_source_id = self.request.query_params.get('lead_source_id') or self.request.query_params.get('lead_source')
        if lead_source_id:
            qs = qs.filter(lead_source_id=lead_source_id)

        campaign = self.request.query_params.get('campaign')
        if campaign:
            qs = qs.filter(Q(campaign_reference__icontains=campaign) | Q(attributions__campaign_name__icontains=campaign)).distinct()

        platform = self.request.query_params.get('platform')
        if platform:
            qs = qs.filter(Q(first_touch_source__icontains=platform) | Q(latest_touch_source__icontains=platform) | Q(attributions__platform__iexact=platform)).distinct()

        program_id = self.request.query_params.get('program_id') or self.request.query_params.get('interested_program_id')
        if program_id:
            qs = qs.filter(interested_program_id=program_id)

        sla_param = self.request.query_params.get('sla_status')
        if sla_param:
            sla_upper = sla_param.upper()
            matching_ids = []
            for item in qs:
                sla_data = CRMLeadService.calculate_lead_sla(item, db_alias=alias)
                if sla_data.get('sla_status') == sla_upper:
                    matching_ids.append(item.id)
            qs = qs.filter(id__in=matching_ids)

        return qs

    def perform_create(self, serializer):
        org = _get_org(self.request)
        alias = _get_db(self.request)
        user = self.request.user
        permitted_branches = get_user_effective_branch_ids(user, alias)

        branch = serializer.validated_data.get('branch')
        if branch and permitted_branches is not None and str(branch.id) not in permitted_branches:
            raise PermissionDenied("You do not have permission to create leads for this branch.")

        if not branch and permitted_branches is not None:
            if len(permitted_branches) == 1:
                branch_obj = Branch.objects.using(alias).filter(id=list(permitted_branches)[0], organization=org).first()
                if branch_obj:
                    branch = branch_obj
            else:
                raise PermissionDenied("A branch selection is required for branch-scoped staff.")

        assignment_mode = str(self.request.data.get('assignment_mode') or 'MANUAL').upper()
        if assignment_mode not in ('MANUAL', 'AUTO'):
            assignment_mode = 'MANUAL'

        assigned_sales_user = serializer.validated_data.get('assigned_sales_user')
        if assigned_sales_user and assignment_mode == 'MANUAL':
            from .services_lead_assignment import LeadAssignmentEligibilityService
            is_valid, validation_err = LeadAssignmentEligibilityService.validate_assignee_eligibility(
                organization=org,
                user=assigned_sales_user,
                branch=branch,
                db_alias=alias,
            )
            if not is_valid:
                from rest_framework.exceptions import ValidationError as DRFValidationError
                raise DRFValidationError({"assigned_sales_user": f"Selected representative is not eligible: {validation_err}"})

        extra_fields = {
            k: v for k, v in serializer.validated_data.items()
            if k not in ('first_name', 'last_name', 'phone_normalized', 'email_normalized', 'branch', 'lead_source', 'assigned_sales_user', 'attribution')
        }
        extra_fields['assignment_mode'] = assignment_mode

        lead = CRMLeadService.create_lead(
            organization=org,
            first_name=serializer.validated_data.get('first_name'),
            last_name=serializer.validated_data.get('last_name'),
            phone=serializer.validated_data.get('phone_normalized'),
            email=serializer.validated_data.get('email_normalized'),
            branch=branch,
            lead_source=serializer.validated_data.get('lead_source'),
            assigned_sales_user=assigned_sales_user if assignment_mode == 'MANUAL' else None,
            actor_user=getattr(self.request, 'user', None),
            extra_fields=extra_fields,
            attribution_data=serializer.validated_data.get('attribution'),
            db_alias=alias,
        )
        serializer.instance = lead

    def perform_update(self, serializer):
        lead = self.get_object()
        alias = _get_db(self.request)
        actor = getattr(self.request, 'user', None)
        permitted_branches = get_user_effective_branch_ids(actor, alias)

        new_branch = serializer.validated_data.get('branch')
        if new_branch and permitted_branches is not None and str(new_branch.id) not in permitted_branches:
            raise PermissionDenied("You do not have permission to move leads to this branch.")

        updated_lead = CRMLeadService.update_lead(
            lead=lead,
            data=serializer.validated_data,
            actor_user=actor,
            db_alias=alias,
        )
        serializer.instance = updated_lead

    def destroy(self, request, *args, **kwargs):
        # Soft-lifecycle preservation: hard deletion forbidden in tenant CRM
        return Response(
            {'error': 'Permanent deletion of leads is not permitted. Transition status to LOST or NOT_INTERESTED instead.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    @action(detail=False, methods=['get'], url_path='metadata')
    def metadata(self, request):
        return Response({
            'statuses': [{'value': code, 'label': label} for code, label in Lead.STATUSES],
            'initial_status': 'NEW_LEAD',
            'genders': ['Male', 'Female', 'Other', 'Prefer not to say'],
            'countries': ['India', 'United States', 'United Kingdom', 'United Arab Emirates', 'Singapore', 'Australia', 'Canada'],
            'goal_suggestions': [
                'Weight loss',
                'Build muscle',
                'Improve mobility',
                'General fitness',
                'Athletic conditioning',
                'Post-rehab / Recovery',
            ],
        })

    @action(detail=False, methods=['get'], url_path='eligible-agents')
    def eligible_agents(self, request):
        return self._get_eligible_assignees_response(request, default_include_unavailable=False)

    @action(detail=False, methods=['get'], url_path='eligible-assignees')
    def eligible_assignees(self, request):
        return self._get_eligible_assignees_response(request, default_include_unavailable=True)

    def _get_eligible_assignees_response(self, request, default_include_unavailable=False):
        alias = _get_db(request)
        org = _get_org(request)
        if not org:
            return Response([])

        query_params = getattr(request, 'query_params', getattr(request, 'GET', {}))
        branch_id = query_params.get('branch_id')
        inc_param = query_params.get('include_unavailable')
        if inc_param is not None:
            include_unavailable = str(inc_param).lower() in ('true', '1', 'yes')
        else:
            include_unavailable = default_include_unavailable

        branch_obj = None
        if branch_id:
            branch_obj = Branch.objects.using(alias).filter(id=branch_id, organization=org).first()

        from .services_lead_assignment import LeadAssignmentEligibilityService
        reps = LeadAssignmentEligibilityService.get_eligible_representatives(
            organization=org,
            branch=branch_obj,
            db_alias=alias,
            include_unavailable=include_unavailable,
        )
        return Response(reps)

    @action(detail=False, methods=['get'], url_path='search-referrers')
    def search_referrers(self, request):
        alias = _get_db(request)
        org = _get_org(request)
        q = (request.query_params.get('query') or '').strip()
        if not q or len(q) < 2:
            return Response([])

        qs = TenantUser.objects.using(alias).filter(organization=org).select_related('profile')
        filter_q = (
            Q(first_name__icontains=q) |
            Q(last_name__icontains=q) |
            Q(email__icontains=q) |
            Q(phone__icontains=q) |
            Q(profile__member_number__icontains=q)
        )
        results = [
            {
                'id': str(u.id),
                'name': f"{u.first_name} {u.last_name}".strip() or u.email,
                'email': u.email,
                'phone': u.phone,
                'member_number': getattr(getattr(u, 'profile', None), 'member_number', None),
            }
            for u in qs.filter(filter_q)[:20]
        ]
        return Response(results)

    @action(detail=False, methods=['post'], url_path='check-duplicates')
    def check_duplicates(self, request):
        alias = _get_db(request)
        org = _get_org(request)
        phone = (request.data.get('phone') or request.data.get('phone_normalized') or '').strip()
        email = (request.data.get('email') or request.data.get('email_normalized') or '').strip().lower()
        exclude_id = request.data.get('exclude_id')

        if not phone and not email:
            return Response({'has_duplicate': False, 'matches': []})

        q = Q()
        if phone:
            from .services_crm import validate_lead_phone
            try:
                norm_phone = validate_lead_phone(phone, required=False)
                if norm_phone:
                    q |= Q(phone_normalized=norm_phone)
            except Exception:
                pass
            q |= Q(phone_normalized=phone)
            clean_digits = re.sub(r'\D', '', phone)
            if len(clean_digits) == 10:
                q |= Q(phone_normalized=f"+91{clean_digits}") | Q(phone_normalized=clean_digits)
        if email:
            q |= Q(email_normalized__iexact=email)

        qs = Lead.objects.using(alias).filter(organization=org).filter(q)
        if exclude_id:
            qs = qs.exclude(id=exclude_id)

        matches = [
            {
                'id': str(lead.id),
                'full_name': f"{lead.first_name} {lead.last_name}".strip(),
                'phone': lead.phone_normalized,
                'email': lead.email_normalized,
                'current_status': lead.current_status,
                'created_at': lead.created_at.isoformat() if lead.created_at else None,
            }
            for lead in qs[:5]
        ]
        return Response({
            'has_duplicate': len(matches) > 0,
            'matches': matches,
        })

    @action(detail=True, methods=['post'], url_path='transition-status')
    def transition_status(self, request, pk=None):
        lead = self.get_object()
        new_status = request.data.get('new_status')
        reason_code = request.data.get('reason_code')
        reason_text = request.data.get('reason_text')

        if not new_status:
            return Response({'error': 'new_status is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            updated_lead = CRMLeadService.transition_lead_status(
                lead=lead,
                new_status=new_status,
                reason_code=reason_code,
                reason_text=reason_text,
                actor_user=request.user,
                db_alias=_get_db(request),
            )
            return Response(LeadSerializer(updated_lead).data)
        except (ValueError, ValidationError) as exc:
            msg = exc.message if hasattr(exc, 'message') else str(exc)
            return Response({'error': msg}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='assign')
    def assign(self, request, pk=None):
        alias = _get_db(request)
        org = _get_org(request)
        lead = self.get_object()
        user_id = request.data.get('assigned_to_user_id')
        assignment_type = request.data.get('assignment_type', 'SALES')
        notes = request.data.get('notes', '')

        # Explicit unassign
        if not user_id or str(user_id).lower() in ('unassign', 'none', 'null', ''):
            updated_lead = CRMLeadService.unassign_lead(
                lead=lead,
                assignment_type=assignment_type,
                actor_user=request.user,
                reason=notes or 'Unassigned via CRM',
                db_alias=alias,
            )
            return Response(LeadSerializer(updated_lead).data)

        try:
            target_user = TenantUser.objects.using(alias).get(id=user_id, organization=org)
        except TenantUser.DoesNotExist:
            return Response({'error': 'Target user not found or not in organization'}, status=status.HTTP_404_NOT_FOUND)

        if target_user.status != 'ACTIVE' or not target_user.is_login_allowed:
            return Response({'error': 'Assigned agent is inactive or not allowed to log in'}, status=status.HTTP_400_BAD_REQUEST)

        # Branch eligibility
        if lead.branch_id:
            agent_permitted = get_user_effective_branch_ids(target_user, alias)
            if agent_permitted is not None and str(lead.branch_id) not in agent_permitted:
                return Response(
                    {'error': f"Agent is not authorized for branch '{lead.branch.name if lead.branch else lead.branch_id}'"},
                    status=status.HTTP_400_BAD_REQUEST
                )

        try:
            assignment = CRMLeadService.assign_lead(
                lead=lead,
                assigned_to_user=target_user,
                assignment_type=assignment_type,
                notes=notes,
                actor_user=request.user,
                db_alias=alias,
            )
            return Response(LeadAssignmentSerializer(assignment).data)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='book-trial')
    def book_trial(self, request, pk=None):
        alias = _get_db(request)
        lead = self.get_object()

        branch_id = request.data.get('branch_id') or getattr(lead, 'branch_id', None)
        if not branch_id:
            return Response({'error': 'branch_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        permitted_branches = get_user_effective_branch_ids(request.user, alias)
        if permitted_branches is not None and str(branch_id) not in permitted_branches:
            return Response({'error': 'You do not have permission to book trials at this branch.'}, status=status.HTTP_403_FORBIDDEN)

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
                class_occurrence_id=request.data.get('class_occurrence_id'),
                assigned_trainer=trainer,
                trial_type=request.data.get('trial_type', 'GROUP_CLASS'),
                booking_source=request.data.get('booking_source', 'WEB'),
                notes=request.data.get('notes'),
                actor_user=request.user,
                db_alias=alias,
            )
            return Response(TrialBookingSerializer(trial).data, status=status.HTTP_201_CREATED)
        except (ValueError, ValidationError) as exc:
            msg = exc.message if hasattr(exc, 'message') else str(exc)
            return Response({'error': msg}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['get'], url_path='timeline')
    def timeline(self, request, pk=None):
        lead = self.get_object()
        alias = _get_db(request)
        limit = int(request.query_params.get('limit', 50))
        events = CRMLeadService.get_lead_timeline(lead, limit=limit, db_alias=alias)
        return Response(events)

    @action(detail=True, methods=['get', 'post'], url_path='attributions')
    def attributions(self, request, pk=None):
        lead = self.get_object()
        alias = _get_db(request)
        if request.method == 'POST':
            from .rbac_engine import RBACAuthorizationEngine
            allowed, reason, check_code = RBACAuthorizationEngine.evaluate(
                user=request.user,
                required_module='crm',
                required_submodule='leads',
                required_permission='crm.leads.edit',
                branch_id=str(lead.branch_id) if lead.branch_id else None,
                request=request,
            )
            if not allowed:
                raise PermissionDenied(f"Permission denied: crm.leads.edit required ({reason})")
            data = request.data.copy() if hasattr(request.data, 'copy') else dict(request.data)
            if 'lead' not in data:
                data['lead'] = str(lead.id)
            serializer = LeadAttributionSerializer(data=data)
            serializer.is_valid(raise_exception=True)
            attr = CRMLeadService.record_lead_attribution(
                lead=lead,
                attribution_data=serializer.validated_data,
                actor_user=request.user,
                db_alias=alias,
            )
            return Response(LeadAttributionSerializer(attr).data, status=status.HTTP_201_CREATED)
        qs = LeadAttribution.objects.using(alias).filter(lead=lead).select_related('lead_source').order_by('captured_at', 'created_at')
        return Response(LeadAttributionSerializer(qs, many=True).data)

    @action(detail=True, methods=['get', 'post'], url_path='activities')
    def activities(self, request, pk=None):
        lead = self.get_object()
        alias = _get_db(request)
        if request.method == 'POST':
            from .rbac_engine import RBACAuthorizationEngine
            allowed, reason, check_code = RBACAuthorizationEngine.evaluate(
                user=request.user,
                required_module='crm',
                required_submodule='leads',
                required_permission='crm.leads.edit',
                branch_id=str(lead.branch_id) if lead.branch_id else None,
                request=request,
            )
            if not allowed:
                raise PermissionDenied(f"Permission denied: crm.leads.edit required ({reason})")
            data = request.data.copy() if hasattr(request.data, 'copy') else dict(request.data)
            if 'lead' not in data:
                data['lead'] = str(lead.id)
            serializer = LeadActivitySerializer(data=data)
            serializer.is_valid(raise_exception=True)
            act = CRMLeadService.record_lead_activity(
                lead=lead,
                activity_type=serializer.validated_data.get('activity_type'),
                outcome=serializer.validated_data.get('outcome'),
                notes=serializer.validated_data.get('notes'),
                performed_by_user=serializer.validated_data.get('performed_by_user') or request.user,
                activity_at=serializer.validated_data.get('activity_at') or timezone.now(),
                external_reference=serializer.validated_data.get('external_reference'),
                actor_user=request.user,
                db_alias=alias,
            )
            return Response(LeadActivitySerializer(act).data, status=status.HTTP_201_CREATED)
        qs = LeadActivity.objects.using(alias).filter(lead=lead).select_related('performed_by_user').order_by('-activity_at')
        return Response(LeadActivitySerializer(qs, many=True).data)

    @action(detail=True, methods=['get', 'post'], url_path='followups')
    def followups(self, request, pk=None):
        lead = self.get_object()
        alias = _get_db(request)
        if request.method == 'POST':
            from .rbac_engine import RBACAuthorizationEngine
            allowed, reason, check_code = RBACAuthorizationEngine.evaluate(
                user=request.user,
                required_module='crm',
                required_submodule='leads',
                required_permission='crm.leads.edit',
                branch_id=str(lead.branch_id) if lead.branch_id else None,
                request=request,
            )
            if not allowed:
                raise PermissionDenied(f"Permission denied: crm.leads.edit required ({reason})")
            assigned_user_id = request.data.get('assigned_to_user') or request.data.get('assigned_to_user_id')
            assigned_to = None
            if assigned_user_id:
                try:
                    assigned_to = TenantUser.objects.using(alias).get(id=assigned_user_id)
                except TenantUser.DoesNotExist:
                    return Response({'error': 'Assigned user not found'}, status=status.HTTP_404_NOT_FOUND)
            else:
                assigned_to = request.user

            due_at_str = request.data.get('due_at')
            if not due_at_str:
                return Response({'error': 'due_at is required'}, status=status.HTTP_400_BAD_REQUEST)
            due_at = parse_datetime(due_at_str)
            if not due_at:
                return Response({'error': 'Invalid datetime format for due_at'}, status=status.HTTP_400_BAD_REQUEST)

            task = CRMLeadService.create_followup_task(
                lead=lead,
                assigned_to_user=assigned_to,
                task_type=request.data.get('task_type', 'CALL'),
                due_at=due_at,
                priority=request.data.get('priority', 'NORMAL'),
                outcome=request.data.get('outcome'),
                created_by_user=request.user,
                db_alias=alias,
            )
            return Response(SalesFollowupTaskSerializer(task).data, status=status.HTTP_201_CREATED)
        qs = SalesFollowupTask.objects.using(alias).filter(lead=lead).select_related('assigned_to_user', 'created_by_user').order_by('due_at')
        return Response(SalesFollowupTaskSerializer(qs, many=True).data)

    @action(detail=True, methods=['get'], url_path='trials')
    def trials(self, request, pk=None):
        lead = self.get_object()
        alias = _get_db(request)
        qs = TrialBooking.objects.using(alias).filter(lead=lead).select_related(
            'branch', 'assigned_trainer_profile'
        ).order_by('-scheduled_start')
        return Response(TrialBookingSerializer(qs, many=True).data)

    @action(detail=True, methods=['get'], url_path='offers')
    def offers(self, request, pk=None):
        """
        Lead 360 commercial tab: returns eligible promotional campaigns,
        available coupons, redemption history, and conversion offer details.
        """
        lead = self.get_object()
        alias = _get_db(request)
        org = lead.organization
        now = timezone.now()

        from .models_discounts import DiscountCampaign, DiscountCode, DiscountRedemption, DiscountEligibilityRule
        from .serializers_discounts import (
            DiscountCampaignSerializer,
            DiscountCodeSerializer,
            DiscountRedemptionSerializer,
            DiscountEligibilityRuleSerializer,
        )

        # 1. Active promotional discount campaigns
        campaigns_qs = DiscountCampaign.objects.using(alias).filter(
            organization=org,
            status='ACTIVE',
        ).filter(
            Q(valid_from__isnull=True) | Q(valid_from__lte=now)
        ).filter(
            Q(valid_until__isnull=True) | Q(valid_until__gte=now)
        ).prefetch_related('codes', 'redemptions').order_by('-created_at')

        # 2. Available coupons for lead's branch / package
        codes_qs = DiscountCode.objects.using(alias).filter(
            campaign__organization=org,
            status='ACTIVE',
            campaign__status='ACTIVE',
        ).filter(
            Q(branch__isnull=True) | Q(branch=lead.branch)
        )
        if lead.interested_program_id:
            codes_qs = codes_qs.filter(
                Q(package__isnull=True) | Q(package__program_id=lead.interested_program_id)
            )
        codes_qs = codes_qs.select_related('campaign', 'branch', 'package').order_by('code')

        # 3. Redemption history for this lead
        redemptions_qs = DiscountRedemption.objects.using(alias).filter(
            Q(order__lead=lead) | Q(user_profile__lead_conversions__lead=lead)
        ).select_related('discount_code', 'campaign', 'order').distinct().order_by('-redeemed_at')

        # 4. Conversion offer details (if converted)
        conversion_offer = None
        if lead.current_status == 'CONVERTED':
            conv = LeadConversion.objects.using(alias).filter(lead=lead).order_by('-converted_at').first()
            if conv:
                redemption = None
                if conv.order_id:
                    redemption = DiscountRedemption.objects.using(alias).filter(order_id=conv.order_id).select_related('discount_code', 'campaign').first()
                conversion_offer = {
                    'conversion_id': str(conv.id),
                    'converted_at': conv.converted_at.isoformat(),
                    'order_id': str(conv.order_id) if conv.order_id else None,
                    'has_discount': redemption is not None,
                    'coupon_code': redemption.discount_code.code if (redemption and redemption.discount_code) else None,
                    'campaign_name': redemption.campaign.name if (redemption and redemption.campaign) else None,
                    'discount_amount': str(redemption.discount_amount) if redemption else '0.00',
                }

        # 5. Dynamic eligibility rules
        rules_qs = DiscountEligibilityRule.objects.using(alias).filter(
            organization=org,
            status='ACTIVE',
        ).filter(
            Q(branch__isnull=True) | Q(branch=lead.branch)
        ).prefetch_related('conditions', 'actions').order_by('priority')

        return Response({
            'lead_id': str(lead.id),
            'lead_name': f"{lead.first_name} {lead.last_name}".strip(),
            'current_status': lead.current_status,
            'branch_id': str(lead.branch_id) if lead.branch_id else None,
            'branch_name': lead.branch.name if lead.branch else None,
            'campaigns': DiscountCampaignSerializer(campaigns_qs, many=True).data,
            'available_coupons': DiscountCodeSerializer(codes_qs, many=True).data,
            'redemptions': DiscountRedemptionSerializer(redemptions_qs, many=True).data,
            'conversion_offer': conversion_offer,
            'rules': DiscountEligibilityRuleSerializer(rules_qs, many=True).data,
        })

    @action(detail=False, methods=['get'], url_path='attention')
    def attention(self, request):
        """
        Paginated Attention Queue endpoint.
        Returns active leads requiring operational attention with full explanation,
        plus authoritative tenant-level summary metrics.
        """
        alias = _get_db(request)
        org = _get_org(request)
        from .services_attention import LeadAttentionService, ACTIVE_PIPELINE_STAGES

        qs = self.get_queryset().filter(current_status__in=ACTIVE_PIPELINE_STAGES)

        # Filters
        branch = request.query_params.get('branch') or request.query_params.get('branch_id')
        if branch and branch != 'ALL':
            qs = qs.filter(branch_id=branch)

        assigned_to = request.query_params.get('assigned_to') or request.query_params.get('assigned_sales_user_id')
        if assigned_to and assigned_to != 'ALL':
            if assigned_to == 'UNASSIGNED':
                qs = qs.filter(assigned_sales_user__isnull=True)
            else:
                qs = qs.filter(assigned_sales_user_id=assigned_to)

        stage = request.query_params.get('stage') or request.query_params.get('current_status')
        if stage and stage != 'ALL':
            qs = qs.filter(current_status=stage)

        source = request.query_params.get('source') or request.query_params.get('lead_source')
        if source and source != 'ALL':
            qs = qs.filter(lead_source_id=source)

        search_q = (request.query_params.get('search') or '').strip()
        if search_q:
            qs = qs.filter(
                Q(first_name__icontains=search_q) |
                Q(last_name__icontains=search_q) |
                Q(phone_normalized__icontains=search_q) |
                Q(email_normalized__icontains=search_q)
            )

        policy = LeadAttentionService.get_attention_policy(org, db_alias=alias)
        reason_filter = request.query_params.get('reason') or request.query_params.get('primary_reason')
        severity_filter = request.query_params.get('severity')

        all_leads = list(qs)
        evaluated_leads = []
        for lead in all_leads:
            eval_res = LeadAttentionService.evaluate_lead(lead, db_alias=alias, policy=policy)
            if eval_res['is_stuck']:
                if reason_filter and reason_filter != 'ALL' and eval_res['primary_reason'] != reason_filter:
                    continue
                if severity_filter and severity_filter != 'ALL' and eval_res['severity'] != severity_filter:
                    continue
                lead._attention_summary = eval_res
                lead._full_attention = eval_res
                evaluated_leads.append(lead)

        # Sort attention queue by overdue urgency descending
        evaluated_leads.sort(
            key=lambda l: getattr(l, '_full_attention', {}).get('overdue_by_seconds', 0),
            reverse=True
        )

        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 20))
        total_count = len(evaluated_leads)
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        page_slice = evaluated_leads[start_idx:end_idx]

        serialized_data = []
        for l in page_slice:
            s_data = LeadSerializer(l).data
            s_data['attention'] = getattr(l, '_full_attention', None)
            serialized_data.append(s_data)

        metrics = LeadAttentionService.get_attention_metrics(
            organization=org,
            branch_id=branch,
            db_alias=alias,
        )

        return Response({
            'count': total_count,
            'page': page,
            'page_size': page_size,
            'total_pages': max(1, (total_count + page_size - 1) // page_size),
            'results': serialized_data,
            'metrics': metrics,
        })

    @action(detail=False, methods=['get'], url_path='attention/metrics')
    def attention_metrics(self, request):
        """
        Authoritative aggregate counts for Attention Queue KPI headers.
        """
        alias = _get_db(request)
        org = _get_org(request)
        from .services_attention import LeadAttentionService

        branch = request.query_params.get('branch') or request.query_params.get('branch_id')
        metrics = LeadAttentionService.get_attention_metrics(
            organization=org,
            branch_id=branch,
            db_alias=alias,
        )
        return Response(metrics)

    @action(detail=True, methods=['get'], url_path='next-action')
    def next_action(self, request, pk=None):
        """
        Returns the complete explainable Next Best Action payload for Lead 360.
        """
        lead = self.get_object()
        alias = _get_db(request)
        from .services_attention import LeadAttentionService
        attention_payload = LeadAttentionService.evaluate_lead(lead, db_alias=alias)
        return Response(attention_payload)

    @action(detail=False, methods=['post'], url_path='scan-sla-breaches')
    def scan_sla_breaches(self, request):
        """
        Admin/worker endpoint to scan active leads and enqueue idempotent DomainOutboxEvents for SLA breaches.
        """
        alias = _get_db(request)
        org = _get_org(request)
        from .services_attention import LeadAttentionService
        count = LeadAttentionService.scan_and_emit_sla_breaches(org, db_alias=alias)
        return Response({
            'status': 'SUCCESS',
            'emitted_events_count': count,
        })

    # ------------------------------------------------------------------
    # CRM PHASE 8 — CONVERSION ENDPOINTS
    # ------------------------------------------------------------------

    @action(detail=True, methods=['get'], url_path='conversion-eligibility')
    def conversion_eligibility(self, request, pk=None):
        """
        GET /api/v1/tenant/leads/{id}/conversion-eligibility/

        Returns backend-authoritative eligibility status for the conversion wizard.
        Frontend must not decide eligibility solely from lead.current_status.
        """
        from .services_crm import LeadConversionService
        lead = self.get_object()
        alias = _get_db(request)
        result = LeadConversionService.get_conversion_eligibility(lead=lead, db_alias=alias)
        return Response(result)

    @action(detail=True, methods=['post'], url_path='conversion-quote')
    def conversion_quote(self, request, pk=None):
        """
        POST /api/v1/tenant/leads/{id}/conversion-quote/

        Read-only pricing preview. No DB writes. No coupon redemption.
        Required body: { package_version_id, branch_id }
        Optional body: { coupon_code }

        Returns: catalog snapshot, priced breakdown, entitlements.
        """
        from .services_crm import LeadConversionService
        lead = self.get_object()
        alias = _get_db(request)

        package_version_id = request.data.get('package_version_id')
        branch_id = request.data.get('branch_id')
        coupon_code = request.data.get('coupon_code')

        if not package_version_id:
            return Response(
                {'error': 'package_version_id is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not branch_id:
            return Response(
                {'error': 'branch_id is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        permitted_branches = get_user_effective_branch_ids(request.user, alias)
        if permitted_branches is not None and str(branch_id) not in permitted_branches:
            return Response(
                {'error': 'You do not have permission to access pricing for this branch.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            quote = LeadConversionService.get_conversion_quote(
                lead=lead,
                package_version_id=str(package_version_id),
                branch_id=str(branch_id),
                coupon_code=coupon_code,
                db_alias=alias,
            )
            return Response(quote)
        except ValidationError as exc:
            msg = exc.message if hasattr(exc, 'message') else str(exc)
            return Response({'error': msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Error in conversion_quote: %s", exc)
            return Response({'error': str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='convert')
    def convert(self, request, pk=None):
        """
        POST /api/v1/tenant/leads/{id}/convert/

        Execute the full Lead → Member conversion.

        Required body:
          - package_version_id: UUID
          - branch_id: UUID
          - payment_provider: CASH | RAZORPAY | ICICI_POS | BANK_TRANSFER | STRIPE | OTHER
          - payment_amount: Decimal string (must equal or exceed quoted total)

        Optional body:
          - payment_method: str (defaults to payment_provider)
          - coupon_code: str
          - start_date: ISO date string (YYYY-MM-DD)
          - idempotency_key: str (for retry safety)

        Returns: conversion_id, order, membership, invoice details.
        """
        from .services_crm import (
            LeadConversionService, IdentityConflictError, LeadAlreadyConvertedError,
            check_payment_recording_permission
        )
        from .services_reliability import IdempotencyConflictError
        from rest_framework.exceptions import PermissionDenied
        lead = self.get_object()
        alias = _get_db(request)

        # Validate required fields
        package_version_id = request.data.get('package_version_id')
        branch_id = request.data.get('branch_id')
        payment_provider = request.data.get('payment_provider')
        payment_amount = request.data.get('payment_amount')

        missing = [f for f in ('package_version_id', 'branch_id', 'payment_provider', 'payment_amount')
                   if not request.data.get(f)]
        if missing:
            return Response(
                {'error': f"Required fields missing: {', '.join(missing)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Branch scope check
        permitted_branches = get_user_effective_branch_ids(request.user, alias)
        if permitted_branches is not None and str(branch_id) not in permitted_branches:
            return Response(
                {'error': 'You do not have permission to convert leads for this branch.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Payment recording authority check (strictly separate from crm.leads.convert)
        if payment_provider and payment_amount:
            has_pay_perm, pay_reason = check_payment_recording_permission(
                request.user, branch_id=str(branch_id), request=request, db_alias=alias
            )
            if not has_pay_perm:
                return Response(
                    {
                        'error': 'Permission denied: payment confirmation permission (finance.payments.create) required to record payment.',
                        'code': 'PAYMENT_AUTHORITY_REQUIRED',
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

        try:
            result = LeadConversionService.execute_conversion(
                lead=lead,
                package_version_id=str(package_version_id),
                branch_id=str(branch_id),
                payment_provider=str(payment_provider),
                payment_amount=payment_amount,
                payment_method=request.data.get('payment_method'),
                coupon_code=request.data.get('coupon_code'),
                start_date=request.data.get('start_date'),
                actor_user=request.user,
                idempotency_key=request.data.get('idempotency_key'),
                db_alias=alias,
            )
            return Response(result, status=status.HTTP_201_CREATED)

        except IdempotencyConflictError as exc:
            return Response(
                {'error': str(exc), 'code': 'IDEMPOTENCY_CONFLICT'},
                status=status.HTTP_409_CONFLICT,
            )
        except PermissionDenied as exc:
            return Response(
                {'error': str(exc), 'code': 'PAYMENT_AUTHORITY_REQUIRED'},
                status=status.HTTP_403_FORBIDDEN,
            )
        except LeadAlreadyConvertedError as exc:
            return Response(
                {'error': str(exc), 'code': 'ALREADY_CONVERTED'},
                status=status.HTTP_409_CONFLICT,
            )
        except IdentityConflictError as exc:
            return Response(
                {'error': str(exc), 'code': 'IDENTITY_CONFLICT'},
                status=status.HTTP_409_CONFLICT,
            )
        except ValidationError as exc:
            msg = exc.message if hasattr(exc, 'message') else str(exc)
            return Response({'error': msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Error in lead convert: %s", exc)
            return Response({'error': str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class LeadAttributionViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = LeadAttributionSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'crm'
    required_submodule = 'leads'
    required_permission = 'crm.leads.view'
    ordering = ['-captured_at', '-created_at']

    def get_queryset(self):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        qs = LeadAttribution.objects.using(alias).select_related('lead', 'lead_source')
        if org:
            qs = qs.filter(organization=org)
        permitted_branches = get_user_effective_branch_ids(self.request.user, alias)
        if permitted_branches is not None:
            qs = qs.filter(lead__branch_id__in=permitted_branches)
        lead_id = self.request.query_params.get('lead_id')
        if lead_id:
            qs = qs.filter(lead_id=lead_id)
        return qs


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
        permitted_branches = get_user_effective_branch_ids(self.request.user, alias)
        if permitted_branches is not None:
            qs = qs.filter(lead__branch_id__in=permitted_branches)
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
        permitted_branches = get_user_effective_branch_ids(self.request.user, alias)
        if permitted_branches is not None:
            qs = qs.filter(lead__branch_id__in=permitted_branches)
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
        permitted_branches = get_user_effective_branch_ids(self.request.user, alias)
        if permitted_branches is not None:
            qs = qs.filter(lead__branch_id__in=permitted_branches)
        lead_id = self.request.query_params.get('lead_id')
        if lead_id:
            qs = qs.filter(lead_id=lead_id)
        return qs

    def perform_create(self, serializer):
        alias = _get_db(self.request)
        lead = serializer.validated_data.get('lead')
        if lead:
            permitted_branches = get_user_effective_branch_ids(self.request.user, alias)
            if permitted_branches is not None and lead.branch_id and str(lead.branch_id) not in permitted_branches:
                raise PermissionDenied("You do not have permission to add notes for this lead's branch.")
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
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['activity_type', 'outcome', 'notes', 'lead__first_name', 'lead__last_name']
    ordering_fields = ['activity_at', 'created_at']
    ordering = ['-activity_at']

    def get_queryset(self):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        qs = LeadActivity.objects.using(alias).select_related('lead', 'lead__branch', 'performed_by_user')
        if org:
            qs = qs.filter(lead__organization=org)
        permitted_branches = get_user_effective_branch_ids(self.request.user, alias)
        if permitted_branches is not None:
            qs = qs.filter(lead__branch_id__in=permitted_branches)
        lead_id = self.request.query_params.get('lead_id')
        if lead_id:
            qs = qs.filter(lead_id=lead_id)
        branch_id = self.request.query_params.get('branch_id') or self.request.query_params.get('branch')
        if branch_id:
            if permitted_branches is not None and str(branch_id) not in permitted_branches:
                return qs.none()
            qs = qs.filter(lead__branch_id=branch_id)
        activity_type = self.request.query_params.get('activity_type')
        if activity_type:
            qs = qs.filter(activity_type=activity_type)
        performed_by = self.request.query_params.get('performed_by_user_id')
        if performed_by:
            qs = qs.filter(performed_by_user_id=performed_by)
        start_date = self.request.query_params.get('start_date')
        if start_date:
            qs = qs.filter(activity_at__date__gte=start_date)
        end_date = self.request.query_params.get('end_date')
        if end_date:
            qs = qs.filter(activity_at__date__lte=end_date)
        q = self.request.query_params.get('search') or self.request.query_params.get('q')
        if q:
            qs = qs.filter(
                Q(lead__first_name__icontains=q) |
                Q(lead__last_name__icontains=q) |
                Q(notes__icontains=q) |
                Q(outcome__icontains=q)
            )
        return qs

    def perform_create(self, serializer):
        alias = _get_db(self.request)
        lead = serializer.validated_data.get('lead')
        if not lead:
            raise serializers.ValidationError({"lead": "Lead is required."})
        org = _get_org(self.request)
        if org and lead.organization_id != org.id:
            raise PermissionDenied("Cannot record activities for a lead belonging to another organization.")
        permitted_branches = get_user_effective_branch_ids(self.request.user, alias)
        if permitted_branches is not None and lead.branch_id and str(lead.branch_id) not in permitted_branches:
            raise PermissionDenied("You do not have permission to record activities for this lead's branch.")
        activity = CRMLeadService.record_lead_activity(
            lead=lead,
            activity_type=serializer.validated_data.get('activity_type'),
            outcome=serializer.validated_data.get('outcome'),
            notes=serializer.validated_data.get('notes'),
            performed_by_user=serializer.validated_data.get('performed_by_user') or self.request.user,
            activity_at=serializer.validated_data.get('activity_at') or timezone.now(),
            external_reference=serializer.validated_data.get('external_reference'),
            actor_user=self.request.user,
            db_alias=alias,
        )
        serializer.instance = activity


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
        'confirm': 'crm.leads.edit',
        'request_reschedule': 'crm.leads.edit',
        'reschedule': 'crm.leads.edit',
        'cancel': 'crm.leads.edit',
        'mark_attended': 'crm.leads.edit',
        'mark_no_show': 'crm.leads.edit',
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

        permitted_branches = get_user_effective_branch_ids(self.request.user, alias)
        if permitted_branches is not None:
            qs = qs.filter(branch_id__in=permitted_branches)

        status_val = self.request.query_params.get('status')
        if status_val:
            qs = qs.filter(status=status_val)

        conf_status = self.request.query_params.get('confirmation_status')
        if conf_status:
            qs = qs.filter(confirmation_status=conf_status)

        branch_id = self.request.query_params.get('branch_id')
        if branch_id:
            if permitted_branches is not None and str(branch_id) not in permitted_branches:
                return qs.none()
            qs = qs.filter(branch_id=branch_id)

        lead_id = self.request.query_params.get('lead') or self.request.query_params.get('lead_id')
        if lead_id:
            qs = qs.filter(lead_id=lead_id)

        date_val = self.request.query_params.get('date')
        if date_val:
            qs = qs.filter(scheduled_start__date=date_val)

        date_from = self.request.query_params.get('date_from')
        if date_from:
            qs = qs.filter(scheduled_start__date__gte=date_from)

        date_to = self.request.query_params.get('date_to')
        if date_to:
            qs = qs.filter(scheduled_start__date__lte=date_to)

        return qs

    def create(self, request, *args, **kwargs):
        alias = _get_db(request)
        lead_id = request.data.get('lead_id') or request.data.get('lead')
        branch_id = request.data.get('branch_id') or request.data.get('branch')
        class_occurrence_id = request.data.get('class_occurrence_id')

        if not lead_id or (not branch_id and not class_occurrence_id):
            return super().create(request, *args, **kwargs)

        if class_occurrence_id and not branch_id:
            from .models_classes import ClassOccurrence
            occ = get_object_or_404(ClassOccurrence.objects.using(alias), id=class_occurrence_id)
            branch_id = occ.branch_id

        permitted_branches = get_user_effective_branch_ids(request.user, alias)
        if permitted_branches is not None and str(branch_id) not in permitted_branches:
            raise PermissionDenied("You do not have permission to book trials at this branch.")

        org = _get_org(request)
        lead = get_object_or_404(Lead.objects.using(alias).filter(organization=org) if org else Lead.objects.using(alias), id=lead_id)
        branch = get_object_or_404(Branch.objects.using(alias).filter(organization=org) if org else Branch.objects.using(alias), id=branch_id)

        try:
            start_str = request.data.get('scheduled_start')
            end_str = request.data.get('scheduled_end')
            scheduled_start = parse_datetime(start_str) if start_str else None
            scheduled_end = parse_datetime(end_str) if end_str else None

            trial = CRMLeadService.book_trial(
                lead=lead,
                branch=branch,
                scheduled_start=scheduled_start,
                scheduled_end=scheduled_end,
                class_occurrence_id=class_occurrence_id,
                trial_type=request.data.get('trial_type', 'GROUP_CLASS'),
                booking_source=request.data.get('booking_source', 'FRONT_DESK'),
                notes=request.data.get('notes'),
                actor_user=request.user,
                db_alias=alias,
            )
            return Response(TrialBookingSerializer(trial).data, status=status.HTTP_201_CREATED)
        except (ValueError, ValidationError) as exc:
            msg = exc.message if hasattr(exc, 'message') else str(exc)
            return Response({'error': msg}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['get'], url_path='available_slots')
    def available_slots(self, request):
        alias = _get_db(request)
        branch_id = request.query_params.get('branch_id')
        if not branch_id:
            return Response({'error': 'branch_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        permitted_branches = get_user_effective_branch_ids(request.user, alias)
        if permitted_branches is not None and str(branch_id) not in permitted_branches:
            raise PermissionDenied("You do not have permission to view slots for this branch.")

        lead_id = request.query_params.get('lead_id')
        date_from = request.query_params.get('date_from') or request.query_params.get('date')
        date_to = request.query_params.get('date_to') or request.query_params.get('date')
        class_template_id = request.query_params.get('class_template_id')
        program_id = request.query_params.get('program_id')

        slots = CRMLeadService.get_available_trial_slots(
            branch_id=branch_id,
            lead_id=lead_id,
            date_from=date_from,
            date_to=date_to,
            class_template_id=class_template_id,
            program_id=program_id,
            db_alias=alias,
        )
        return Response({'slots': slots, 'results': slots})

    @action(detail=False, methods=['get'], url_path='available-slots')
    def available_slots_hyphen(self, request):
        return self.available_slots(request)

    @action(detail=False, methods=['get'], url_path='summary_counts')
    def summary_counts(self, request):
        alias = _get_db(request)
        org = _get_org(request)
        qs = TrialBooking.objects.using(alias)
        if org:
            qs = qs.filter(lead__organization=org)
        permitted_branches = get_user_effective_branch_ids(request.user, alias)
        if permitted_branches is not None:
            qs = qs.filter(branch_id__in=permitted_branches)
        branch_id = request.query_params.get('branch_id')
        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        now = timezone.now()
        today = now.date()

        counts = {
            'total': qs.count(),
            'upcoming': qs.filter(status='BOOKED', scheduled_start__gte=now).count(),
            'booked': qs.filter(status='BOOKED').count(),
            'confirmed': qs.filter(Q(confirmation_status='CONFIRMED') | Q(status='CONFIRMED')).count(),
            'awaiting_confirmation': qs.filter(confirmation_status='PENDING', status='BOOKED').count(),
            'today': qs.filter(scheduled_start__date=today).count(),
            'attended': qs.filter(status='ATTENDED').count(),
            'no_show': qs.filter(status='NO_SHOW').count(),
            'rescheduled': qs.filter(Q(status='RESCHEDULED')).count(),
            'reschedule_requested': qs.filter(confirmation_status='RESCHEDULE_REQUESTED').count(),
            'cancelled': qs.filter(status='CANCELLED').count(),
        }
        return Response({'counts': counts, **counts})

    @action(detail=False, methods=['get'], url_path='summary-counts')
    def summary_counts_hyphen(self, request):
        return self.summary_counts(request)

    @action(detail=True, methods=['post'], url_path='confirm')
    def confirm(self, request, pk=None):
        trial = self.get_object()
        channel = request.data.get('channel', 'MANUAL')
        notes = request.data.get('notes')
        confirmed_trial = CRMLeadService.confirm_trial(
            trial=trial,
            channel=channel,
            notes=notes,
            actor_user=request.user,
            db_alias=_get_db(request),
        )
        return Response(TrialBookingSerializer(confirmed_trial).data)

    @action(detail=True, methods=['post'], url_path='request-reschedule')
    def request_reschedule(self, request, pk=None):
        trial = self.get_object()
        reason = request.data.get('cancellation_reason') or request.data.get('reason') or request.data.get('notes')
        updated_trial = CRMLeadService.request_reschedule_trial(
            trial=trial,
            reason=reason,
            actor_user=request.user,
            db_alias=_get_db(request),
        )
        return Response(TrialBookingSerializer(updated_trial).data)

    @action(detail=True, methods=['post'], url_path='request_reschedule')
    def request_reschedule_underscore(self, request, pk=None):
        return self.request_reschedule(request, pk=pk)

    @action(detail=True, methods=['post'], url_path='reschedule')
    def reschedule(self, request, pk=None):
        trial = self.get_object()
        start_str = request.data.get('new_scheduled_start') or request.data.get('scheduled_start')
        end_str = request.data.get('new_scheduled_end') or request.data.get('scheduled_end')
        new_start = parse_datetime(start_str) if start_str else None
        new_end = parse_datetime(end_str) if end_str else None
        occ_id = request.data.get('new_class_occurrence_id') or request.data.get('class_occurrence_id')
        reason = request.data.get('cancellation_reason') or request.data.get('reason') or request.data.get('notes')

        try:
            new_trial = CRMLeadService.reschedule_trial(
                trial=trial,
                new_scheduled_start=new_start,
                new_scheduled_end=new_end,
                new_class_occurrence_id=occ_id,
                reason=reason,
                actor_user=request.user,
                db_alias=_get_db(request),
            )
            return Response({'new_trial': TrialBookingSerializer(new_trial).data, **TrialBookingSerializer(new_trial).data}, status=status.HTTP_200_OK)
        except (ValueError, ValidationError) as exc:
            msg = exc.message if hasattr(exc, 'message') else str(exc)
            return Response({'error': msg}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='cancel')
    def cancel(self, request, pk=None):
        trial = self.get_object()
        reason = request.data.get('cancellation_reason') or request.data.get('reason') or request.data.get('notes')
        try:
            cancelled_trial = CRMLeadService.cancel_trial(
                trial=trial,
                reason=reason,
                actor_user=request.user,
                db_alias=_get_db(request),
            )
            return Response(TrialBookingSerializer(cancelled_trial).data)
        except (ValueError, ValidationError) as exc:
            msg = exc.message if hasattr(exc, 'message') else str(exc)
            return Response({'error': msg}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='mark-attended')
    def mark_attended(self, request, pk=None):
        trial = self.get_object()
        notes = request.data.get('notes')
        try:
            attended_trial = CRMLeadService.mark_trial_attended(
                trial=trial,
                notes=notes,
                actor_user=request.user,
                db_alias=_get_db(request),
            )
            return Response(TrialBookingSerializer(attended_trial).data)
        except (ValueError, ValidationError) as exc:
            msg = exc.message if hasattr(exc, 'message') else str(exc)
            return Response({'error': msg}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='mark_attended')
    def mark_attended_underscore(self, request, pk=None):
        return self.mark_attended(request, pk=pk)

    @action(detail=True, methods=['post'], url_path='mark-no-show')
    def mark_no_show(self, request, pk=None):
        trial = self.get_object()
        notes = request.data.get('notes')
        try:
            noshow_trial = CRMLeadService.mark_trial_no_show(
                trial=trial,
                notes=notes,
                actor_user=request.user,
                db_alias=_get_db(request),
            )
            return Response(TrialBookingSerializer(noshow_trial).data)
        except (ValueError, ValidationError) as exc:
            msg = exc.message if hasattr(exc, 'message') else str(exc)
            return Response({'error': msg}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='mark_no_show')
    def mark_no_show_underscore(self, request, pk=None):
        return self.mark_no_show(request, pk=pk)

    @action(detail=True, methods=['get'], url_path='reminder-schedule')
    def reminder_schedule(self, request, pk=None):
        trial = self.get_object()
        schedule = CRMLeadService.calculate_trial_reminder_schedule(
            trial=trial,
            db_alias=_get_db(request),
        )
        return Response({'reminder_schedule': schedule, 'points': schedule})

    @action(detail=True, methods=['get'], url_path='reminder_schedule')
    def reminder_schedule_underscore(self, request, pk=None):
        return self.reminder_schedule(request, pk=pk)

    @action(detail=True, methods=['post'], url_path='transition-status')
    def transition_status(self, request, pk=None):
        trial = self.get_object()
        new_status = request.data.get('new_status')
        reason_code = request.data.get('reason_code')

        if not new_status:
            return Response({'error': 'new_status is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            updated_trial = CRMLeadService.transition_trial_status(
                trial=trial,
                new_status=new_status,
                reason_code=reason_code,
                actor_user=request.user,
                db_alias=_get_db(request),
            )
            return Response(TrialBookingSerializer(updated_trial).data)
        except (ValueError, ValidationError) as exc:
            msg = exc.message if hasattr(exc, 'message') else str(exc)
            return Response({'error': msg}, status=status.HTTP_400_BAD_REQUEST)


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
        permitted_branches = get_user_effective_branch_ids(self.request.user, alias)
        if permitted_branches is not None:
            qs = qs.filter(trial_booking__branch_id__in=permitted_branches)
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
        permitted_branches = get_user_effective_branch_ids(self.request.user, alias)
        if permitted_branches is not None:
            qs = qs.filter(lead__branch_id__in=permitted_branches)
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
        'complete': 'crm.leads.edit',
        'reschedule': 'crm.leads.edit',
        'cancel': 'crm.leads.edit',
        'work_queue': 'crm.leads.view',
    }
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['task_type', 'priority', 'outcome', 'lead__first_name', 'lead__last_name']
    ordering_fields = ['due_at', 'created_at', 'priority']
    ordering = ['due_at']

    def get_queryset(self):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        qs = SalesFollowupTask.objects.using(alias).select_related(
            'lead', 'lead__branch', 'assigned_to_user', 'created_by_user'
        )
        if org:
            qs = qs.filter(lead__organization=org)
        permitted_branches = get_user_effective_branch_ids(self.request.user, alias)
        if permitted_branches is not None:
            qs = qs.filter(lead__branch_id__in=permitted_branches)
        lead_id = self.request.query_params.get('lead_id')
        if lead_id:
            qs = qs.filter(lead_id=lead_id)
        assigned_to = self.request.query_params.get('assigned_to_user_id') or self.request.query_params.get('assigned_to')
        if assigned_to:
            qs = qs.filter(assigned_to_user_id=assigned_to)
        status_val = self.request.query_params.get('status')
        if status_val:
            qs = qs.filter(status=status_val)
        priority = self.request.query_params.get('priority')
        if priority:
            qs = qs.filter(priority=priority)
        task_type = self.request.query_params.get('task_type')
        if task_type:
            qs = qs.filter(task_type=task_type)
        branch_id = self.request.query_params.get('branch_id') or self.request.query_params.get('branch')
        if branch_id:
            if permitted_branches is not None and str(branch_id) not in permitted_branches:
                return qs.none()
            qs = qs.filter(lead__branch_id=branch_id)

        now = timezone.now()
        today = now.date()

        is_overdue = self.request.query_params.get('is_overdue')
        if is_overdue and str(is_overdue).lower() in ('true', '1'):
            qs = qs.filter(status__in=['PENDING', 'IN_PROGRESS'], due_at__lt=now)

        due_today = self.request.query_params.get('due_today')
        if due_today and str(due_today).lower() in ('true', '1'):
            qs = qs.filter(status__in=['PENDING', 'IN_PROGRESS'], due_at__date=today)

        due_filter = self.request.query_params.get('due_filter')
        if due_filter == 'overdue':
            qs = qs.filter(status__in=['PENDING', 'IN_PROGRESS'], due_at__lt=now)
        elif due_filter == 'today':
            qs = qs.filter(status__in=['PENDING', 'IN_PROGRESS'], due_at__date=today)
        elif due_filter == 'upcoming':
            qs = qs.filter(status__in=['PENDING', 'IN_PROGRESS'], due_at__date__gt=today)
        elif due_filter == 'high_priority':
            qs = qs.filter(status__in=['PENDING', 'IN_PROGRESS'], priority__in=['HIGH', 'URGENT'])
        elif due_filter == 'completed':
            qs = qs.filter(status='COMPLETED')

        q = self.request.query_params.get('search') or self.request.query_params.get('q')
        if q:
            qs = qs.filter(
                Q(lead__first_name__icontains=q) |
                Q(lead__last_name__icontains=q) |
                Q(task_type__icontains=q) |
                Q(outcome__icontains=q)
            )
        return qs

    def perform_create(self, serializer):
        alias = _get_db(self.request)
        lead = serializer.validated_data.get('lead')
        if not lead:
            raise serializers.ValidationError({"lead": "Lead is required."})
        org = _get_org(self.request)
        if org and lead.organization_id != org.id:
            raise PermissionDenied("Cannot create follow-up task for a lead belonging to another organization.")
        permitted_branches = get_user_effective_branch_ids(self.request.user, alias)
        if permitted_branches is not None and lead.branch_id and str(lead.branch_id) not in permitted_branches:
            raise PermissionDenied("You do not have permission to create tasks for this lead's branch.")

        assigned_to = serializer.validated_data.get('assigned_to_user') or self.request.user
        task = CRMLeadService.create_followup_task(
            lead=lead,
            assigned_to_user=assigned_to,
            task_type=serializer.validated_data.get('task_type', 'CALL'),
            due_at=serializer.validated_data.get('due_at'),
            priority=serializer.validated_data.get('priority', 'NORMAL'),
            outcome=serializer.validated_data.get('outcome'),
            created_by_user=self.request.user,
            db_alias=alias,
        )
        serializer.instance = task

    @action(detail=False, methods=['get'], url_path='work-queue')
    def work_queue(self, request):
        alias = _get_db(request)
        org = _get_org(request)
        qs = SalesFollowupTask.objects.using(alias).select_related('lead')
        if org:
            qs = qs.filter(lead__organization=org)
        permitted_branches = get_user_effective_branch_ids(request.user, alias)
        if permitted_branches is not None:
            qs = qs.filter(lead__branch_id__in=permitted_branches)

        assigned_to = request.query_params.get('assigned_to_user_id') or request.query_params.get('assigned_to')
        if assigned_to:
            qs = qs.filter(assigned_to_user_id=assigned_to)

        branch_id = request.query_params.get('branch_id') or request.query_params.get('branch')
        if branch_id:
            if permitted_branches is not None and str(branch_id) not in permitted_branches:
                return Response({
                    'counts': {
                        'overdue': 0,
                        'due_today': 0,
                        'due_later': 0,
                        'high_priority': 0,
                        'completed_today': 0,
                        'total_pending': 0,
                    }
                })
            qs = qs.filter(lead__branch_id=branch_id)

        now = timezone.now()
        today = now.date()

        overdue_count = qs.filter(status__in=['PENDING', 'IN_PROGRESS'], due_at__lt=now).count()
        due_today_count = qs.filter(status__in=['PENDING', 'IN_PROGRESS'], due_at__date=today).count()
        due_later_count = qs.filter(status__in=['PENDING', 'IN_PROGRESS'], due_at__date__gt=today).count()
        high_priority_count = qs.filter(status__in=['PENDING', 'IN_PROGRESS'], priority__in=['HIGH', 'URGENT']).count()
        completed_today_count = qs.filter(status='COMPLETED', updated_at__date=today).count()
        total_pending = qs.filter(status__in=['PENDING', 'IN_PROGRESS']).count()

        return Response({
            'counts': {
                'overdue': overdue_count,
                'due_today': due_today_count,
                'due_later': due_later_count,
                'high_priority': high_priority_count,
                'completed_today': completed_today_count,
                'total_pending': total_pending,
            }
        })

    @action(detail=True, methods=['post'], url_path='complete')
    def complete(self, request, pk=None):
        task = self.get_object()
        alias = _get_db(request)
        outcome = request.data.get('outcome', '').strip()
        next_followup_at = request.data.get('next_followup_at')
        next_dt = parse_datetime(next_followup_at) if next_followup_at else None
        log_activity = bool(request.data.get('log_activity', False))

        updated_task = CRMLeadService.complete_followup_task(
            task=task,
            outcome=outcome,
            next_followup_at=next_dt,
            log_activity=log_activity,
            actor_user=request.user,
            db_alias=alias,
        )
        return Response(SalesFollowupTaskSerializer(updated_task).data)

    @action(detail=True, methods=['post'], url_path='reschedule')
    def reschedule(self, request, pk=None):
        task = self.get_object()
        alias = _get_db(request)
        due_at_str = request.data.get('due_at')
        if not due_at_str:
            return Response({'error': 'due_at is required'}, status=status.HTTP_400_BAD_REQUEST)
        new_due = parse_datetime(due_at_str)
        if not new_due:
            return Response({'error': 'Invalid datetime format for due_at'}, status=status.HTTP_400_BAD_REQUEST)
        reason = request.data.get('reason', '')

        updated_task = CRMLeadService.reschedule_followup_task(
            task=task,
            new_due_at=new_due,
            reason=reason,
            actor_user=request.user,
            db_alias=alias,
        )
        return Response(SalesFollowupTaskSerializer(updated_task).data)

    @action(detail=True, methods=['post'], url_path='cancel')
    def cancel(self, request, pk=None):
        task = self.get_object()
        alias = _get_db(request)
        reason = request.data.get('reason', '')
        updated_task = CRMLeadService.cancel_followup_task(
            task=task,
            reason=reason,
            actor_user=request.user,
            db_alias=alias,
        )
        return Response(SalesFollowupTaskSerializer(updated_task).data)


class CRMStageSlaPolicyViewSet(viewsets.ModelViewSet):
    serializer_class = CRMStageSlaPolicySerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'crm'
    required_submodule = 'leads'
    required_permission = 'crm.settings.view'
    permission_action_map = {
        'create': 'crm.settings.edit',
        'update': 'crm.settings.edit',
        'partial_update': 'crm.settings.edit',
        'destroy': 'crm.settings.edit',
    }
    ordering = ['display_order', 'created_at']

    def get_queryset(self):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        qs = CRMStageSlaPolicy.objects.using(alias).all()
        if org:
            qs = qs.filter(organization=org)
            if not qs.exists():
                defaults = [
                    ('NEW_LEAD', 'New Lead', 15, 'MINUTES', True, False, None, None, 1),
                    ('TRIAL_BOOKED', 'Trial Booked', 2, 'HOURS', True, False, None, None, 2),
                    ('TRIAL_ATTENDED', 'Trial Attended', 2, 'HOURS', True, True, 24, 'HOURS', 3),
                    ('FOLLOW_UP_PENDING', 'Follow-up Pending', 4, 'HOURS', True, True, 24, 'HOURS', 4),
                    ('INTERESTED', 'Interested', 4, 'HOURS', True, False, None, None, 5),
                    ('HOT_LEAD', 'Hot Lead', 1, 'HOURS', True, True, 6, 'HOURS', 6),
                    ('PAYMENT_PENDING', 'Payment Pending', 24, 'HOURS', True, True, 48, 'HOURS', 7),
                ]
                for stage, label, val, unit, en, esc_en, esc_v, esc_u, order in defaults:
                    CRMStageSlaPolicy.objects.using(alias).get_or_create(
                        organization=org,
                        canonical_stage=stage,
                        defaults={
                            'display_label': label,
                            'response_target_value': val,
                            'response_target_unit': unit,
                            'is_enabled': en,
                            'escalation_enabled': esc_en,
                            'escalation_after_value': esc_v,
                            'escalation_after_unit': esc_u,
                            'display_order': order,
                        },
                    )
                qs = CRMStageSlaPolicy.objects.using(alias).filter(organization=org)
        return qs

    def perform_create(self, serializer):
        org = _get_org(self.request)
        alias = _get_db(self.request)
        instance = serializer.save(organization=org)
        record_business_audit(
            organization=org,
            module='crm',
            action_code='CRM_SLA_POLICY_CREATED',
            entity_type='CRMStageSlaPolicy',
            entity_id=instance.id,
            actor_user=getattr(self.request, 'user', None),
            metadata={'stage': instance.canonical_stage, 'target': f"{instance.response_target_value} {instance.response_target_unit}"},
            db_alias=alias,
        )

    def perform_update(self, serializer):
        org = _get_org(self.request)
        alias = _get_db(self.request)
        instance = serializer.save()
        record_business_audit(
            organization=org,
            module='crm',
            action_code='CRM_SLA_POLICY_UPDATED',
            entity_type='CRMStageSlaPolicy',
            entity_id=instance.id,
            actor_user=getattr(self.request, 'user', None),
            metadata={'stage': instance.canonical_stage, 'target': f"{instance.response_target_value} {instance.response_target_unit}"},
            db_alias=alias,
        )


class CRMTrialReminderPolicyViewSet(viewsets.ViewSet):
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'crm'
    required_submodule = 'leads'
    required_permission = 'crm.settings.view'
    permission_action_map = {
        'list': 'crm.settings.view',
        'create': 'crm.settings.edit',
        'update': 'crm.settings.edit',
        'partial_update': 'crm.settings.edit',
    }

    def list(self, request):
        alias = _get_db(request)
        org = _get_org(request)
        if not org:
            return Response({'error': 'Organization context missing'}, status=status.HTTP_400_BAD_REQUEST)

        policy, _ = CRMTrialReminderPolicy.objects.using(alias).get_or_create(
            organization=org,
            defaults={
                'immediate_whatsapp': True,
                'immediate_email': True,
                'immediate_sms': False,
                'reminder_offsets': [1440, 120, 30],
                'ask_attendance_confirmation': True,
                'confirmation_wait_duration_value': 2,
                'confirmation_wait_duration_unit': 'HOURS',
                'no_response_action': 'CREATE_FOLLOWUP',
                'ai_calling_enabled': False,
            }
        )
        return Response(CRMTrialReminderPolicySerializer(policy).data)

    def create(self, request):
        return self._save(request)

    def update(self, request, pk=None):
        return self._save(request)

    def partial_update(self, request, pk=None):
        return self._save(request)

    def _save(self, request):
        alias = _get_db(request)
        org = _get_org(request)
        if not org:
            return Response({'error': 'Organization context missing'}, status=status.HTTP_400_BAD_REQUEST)

        policy, _ = CRMTrialReminderPolicy.objects.using(alias).get_or_create(
            organization=org,
            defaults={
                'immediate_whatsapp': True,
                'immediate_email': True,
                'immediate_sms': False,
                'reminder_offsets': [1440, 120, 30],
            }
        )
        serializer = CRMTrialReminderPolicySerializer(policy, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        updated_policy = serializer.save()

        record_business_audit(
            organization=org,
            module='crm',
            action_code='CRM_TRIAL_REMINDER_POLICY_UPDATED',
            entity_type='CRMTrialReminderPolicy',
            entity_id=updated_policy.id,
            actor_user=getattr(request, 'user', None),
            metadata={'whatsapp': updated_policy.immediate_whatsapp, 'email': updated_policy.immediate_email, 'offsets': updated_policy.reminder_offsets},
            db_alias=alias,
        )
        return Response(CRMTrialReminderPolicySerializer(updated_policy).data)


class CRMCommunicationChannelsViewSet(viewsets.ViewSet):
    """
    Returns safe status overview of communication channel integrations.
    NO secrets or private credentials are ever returned to the frontend.
    Inspects tenant Integrations and ProviderRegistry.
    """
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'crm'
    required_submodule = 'leads'
    required_permission = 'crm.communications.view'

    def list(self, request):
        alias = _get_db(request)
        org = _get_org(request)

        from .models_infra import Integration

        # Active integrations in tenant database
        integrations = {
            i.provider.upper(): i
            for i in Integration.objects.using(alias).filter(status='ACTIVE')
        }

        # Resolve WhatsApp status
        wa_adapter, wa_integ = CommunicationProviderRegistry.resolve_for_tenant('WHATSAPP')
        wa_connected = bool(wa_adapter and wa_adapter.is_configured())
        wa_sender = 'Not set'
        if wa_integ and wa_integ.configuration:
            raw_s = wa_integ.configuration.get('sender_id') or wa_integ.configuration.get('phone_number_id') or ''
            wa_sender = f"+91******{raw_s[-4:]}" if len(raw_s) >= 4 else (raw_s or 'Not set')

        # Resolve Email status
        email_adapter, email_integ = CommunicationProviderRegistry.resolve_for_tenant('EMAIL')
        email_connected = bool(email_adapter and email_adapter.is_configured())
        email_sender = 'welcome@sweat.fit'
        if email_integ and email_integ.configuration:
            email_sender = email_integ.configuration.get('from_email') or 'welcome@sweat.fit'

        # Resolve SMS status
        sms_adapter, sms_integ = CommunicationProviderRegistry.resolve_for_tenant('SMS')
        sms_connected = bool(sms_adapter and sms_adapter.is_configured())
        sms_sender = 'SWTFIT'
        if sms_integ and sms_integ.configuration:
            sms_sender = sms_integ.configuration.get('sender_id') or 'SWTFIT'

        channels = [
            {
                'id': 'whatsapp',
                'name': 'WhatsApp Business API',
                'channel_type': 'WHATSAPP',
                'provider': wa_adapter.provider_name if wa_adapter else 'Unconfigured',
                'status': 'CONNECTED' if wa_connected else 'NOT_CONFIGURED',
                'is_configured': wa_connected,
                'sender_identity': wa_sender,
                'description': 'Automated booking confirmations, trial reminders, and lead follow-ups via WhatsApp.',
                'capabilities': wa_adapter.capabilities if wa_adapter else ['OUTBOUND', 'INBOUND_WEBHOOK', 'TEMPLATES', 'DELIVERY_RECEIPTS'],
                'implemented_adapters': ['MetaWhatsAppAdapter', 'GupshupWhatsAppAdapter'],
                'last_sync_at': wa_integ.last_sync_at if wa_integ else None,
            },
            {
                'id': 'email',
                'name': 'Transactional Email (SMTP/SES)',
                'channel_type': 'EMAIL',
                'provider': email_adapter.provider_name if email_adapter else 'SMTP',
                'status': 'CONNECTED' if email_connected else 'NOT_CONFIGURED',
                'is_configured': email_connected,
                'sender_identity': email_sender,
                'description': 'Welcome emails, rich schedule reminders, and invoice notifications.',
                'capabilities': email_adapter.capabilities if email_adapter else ['OUTBOUND', 'DELIVERY_RECEIPTS'],
                'implemented_adapters': ['SMTPEmailAdapter', 'SESEmailAdapter'],
                'last_sync_at': email_integ.last_sync_at if email_integ else None,
            },
            {
                'id': 'sms',
                'name': 'SMS Gateway (DLT / Telecom)',
                'channel_type': 'SMS',
                'provider': sms_adapter.provider_name if sms_adapter else 'Unconfigured',
                'status': 'CONNECTED' if sms_connected else 'NOT_CONFIGURED',
                'is_configured': sms_connected,
                'sender_identity': sms_sender,
                'description': 'High-priority OTP delivery and critical trial check-in reminders.',
                'capabilities': sms_adapter.capabilities if sms_adapter else ['OUTBOUND', 'DELIVERY_RECEIPTS'],
                'implemented_adapters': ['TwilioSMSAdapter', 'MSG91SMSAdapter', 'GupshupSMSAdapter'],
                'last_sync_at': sms_integ.last_sync_at if sms_integ else None,
            },
            {
                'id': 'ai_calling',
                'name': 'Sarvam AI Voice Agent',
                'channel_type': 'VOICE_AI',
                'provider': 'Sarvam',
                'status': 'COMING_SOON',
                'is_configured': False,
                'sender_identity': 'Virtual Receptionist',
                'description': 'Autonomous conversational AI agent for lead qualification and trial reminders.',
                'capabilities': ['OUTBOUND', 'INBOUND_WEBHOOK'],
                'implemented_adapters': [],
                'last_sync_at': None,
            },
        ]
        return Response(channels)


class CommunicationMessageViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for listing and retrieving customer communication records.
    Provides action /send/ to dispatch outbound messages with consent enforcement.
    """
    serializer_class = CommunicationMessageSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'crm'
    required_submodule = 'leads'
    required_permission = 'crm.communications.view'
    permission_action_map = {
        'send': 'crm.communications.send',
    }
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['recipient', 'sender', 'subject', 'body_snapshot', 'sender_identifier']
    ordering = ['-created_at']

    def get_queryset(self):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        if not org:
            return CommunicationMessage.objects.none()

        qs = CommunicationMessage.objects.using(alias).filter(
            organization=org,
        ).select_related(
            'lead', 'created_by_user', 'template', 'related_trial', 'related_followup', 'in_reply_to',
        ).prefetch_related('status_events', 'replies')

        # Filter by lead_id
        lead_id = self.request.query_params.get('lead_id')
        if lead_id:
            qs = qs.filter(lead_id=lead_id)

        # Filter by channel
        channel = self.request.query_params.get('channel')
        if channel:
            qs = qs.filter(channel=channel.upper())

        # Filter by direction
        direction = self.request.query_params.get('direction')
        if direction:
            qs = qs.filter(direction=direction.upper())

        # Filter by status
        status_val = self.request.query_params.get('status')
        if status_val:
            qs = qs.filter(status=status_val.upper())

        # Filter by resolution_status
        res_status = self.request.query_params.get('resolution_status')
        if res_status:
            qs = qs.filter(resolution_status=res_status.upper())

        # Branch scoping: enforce branch isolation on lead communications
        permitted_branches = get_user_effective_branch_ids(self.request.user, alias)
        if permitted_branches is not None:
            qs = qs.filter(
                Q(lead__branch_id__in=permitted_branches) |
                Q(related_trial__branch_id__in=permitted_branches) |
                Q(lead__isnull=True)
            )

        return qs

    @action(detail=False, methods=['post'], url_path='send')
    def send(self, request):
        alias = _get_db(request)
        org = _get_org(request)
        if not org:
            return Response({'error': 'Organization context missing'}, status=status.HTTP_400_BAD_REQUEST)

        serializer = CommunicationSendInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        lead = None
        if data.get('lead_id'):
            try:
                lead = Lead.objects.using(alias).get(id=data['lead_id'], organization=org)
            except Lead.DoesNotExist:
                return Response({'error': 'Lead not found'}, status=status.HTTP_404_NOT_FOUND)

            # Check branch access
            permitted_branches = get_user_effective_branch_ids(request.user, alias)
            if permitted_branches is not None and lead.branch_id and str(lead.branch_id) not in permitted_branches:
                raise PermissionDenied("You do not have permission to send communications to leads in this branch.")

        template = None
        if data.get('template_id'):
            from .models_govern import NotificationTemplate
            try:
                template = NotificationTemplate.objects.using(alias).get(id=data['template_id'], organization=org)
            except NotificationTemplate.DoesNotExist:
                return Response({'error': 'NotificationTemplate not found'}, status=status.HTTP_404_NOT_FOUND)

        trial = None
        if data.get('related_trial_id'):
            trial = TrialBooking.objects.using(alias).filter(id=data['related_trial_id'], organization=org).first()

        followup = None
        if data.get('related_followup_id'):
            followup = SalesFollowupTask.objects.using(alias).filter(id=data['related_followup_id'], organization=org).first()

        try:
            msg = CommunicationService.send_communication(
                organization=org,
                channel=data['channel'],
                recipient=data['recipient'],
                lead=lead,
                template=template,
                subject=data.get('subject', ''),
                body=data.get('body', ''),
                purpose=data.get('purpose', 'TRANSACTIONAL'),
                idempotency_key=data.get('idempotency_key'),
                trigger_type=data.get('trigger_type', 'MANUAL'),
                related_trial=trial,
                related_followup=followup,
                created_by_user=request.user,
                provider_preference=data.get('provider_preference'),
            )
            return Response(CommunicationMessageSerializer(msg).data, status=status.HTTP_201_CREATED)
        except ConsentViolationError as cve:
            return Response({'error': str(cve), 'code': 'CONSENT_DENIED'}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.error("Error in CommunicationMessageViewSet.send: %s", exc)
            return Response({'error': str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class CRMAttentionPolicyViewSet(viewsets.ModelViewSet):
    """
    ViewSet for tenant-configurable CRM Attention & Next Action rules.
    """
    serializer_class = CRMAttentionPolicySerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'crm'
    required_submodule = 'settings'
    required_permission = 'crm.settings.view'
    permission_action_map = {
        'create': 'crm.settings.edit',
        'update': 'crm.settings.edit',
        'partial_update': 'crm.settings.edit',
        'destroy': 'crm.settings.edit',
    }

    def get_queryset(self):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        qs = CRMAttentionPolicy.objects.using(alias).all()
        if org:
            qs = qs.filter(organization=org)
            if not qs.exists():
                CRMAttentionPolicy.objects.using(alias).get_or_create(organization=org)
                qs = CRMAttentionPolicy.objects.using(alias).filter(organization=org)
        return qs

    def perform_create(self, serializer):
        org = _get_org(self.request)
        alias = _get_db(self.request)
        instance = serializer.save(organization=org)
        record_business_audit(
            organization=org,
            module='crm',
            action_code='CRM_ATTENTION_POLICY_CREATED',
            entity_type='CRMAttentionPolicy',
            entity_id=instance.id,
            actor_user=getattr(self.request, 'user', None),
            db_alias=alias,
        )

    def perform_update(self, serializer):
        org = _get_org(self.request)
        alias = _get_db(self.request)
        instance = serializer.save()
        record_business_audit(
            organization=org,
            module='crm',
            action_code='CRM_ATTENTION_POLICY_UPDATED',
            entity_type='CRMAttentionPolicy',
            entity_id=instance.id,
            actor_user=getattr(self.request, 'user', None),
            db_alias=alias,
        )


class CRMAgentAssignmentConfigViewSet(viewsets.ViewSet):
    """
    ViewSet for managing tenant-configurable sales roles and agent eligibility.
    """
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'crm'
    required_submodule = 'settings'
    required_permission = 'crm.settings.view'
    permission_action_map = {
        'list': 'crm.settings.view',
        'create': 'crm.settings.edit',
        'update': 'crm.settings.edit',
        'partial_update': 'crm.settings.edit',
    }

    def list(self, request):
        alias = _get_db(request)
        org = _get_org(request)
        if not org:
            return Response({'error': 'Organization context missing'}, status=status.HTTP_400_BAD_REQUEST)

        config, created = CRMAgentAssignmentConfig.objects.using(alias).get_or_create(
            organization=org,
            defaults={
                'allowed_role_codes': ['SALES_REP', 'BRANCH_MANAGER', 'FRONT_DESK', 'ORG_ADMIN'],
                'require_branch_match': True,
                'allow_all_staff_fallback': True,
            }
        )
        if not config.allowed_role_codes:
            config.allowed_role_codes = ['SALES_REP', 'BRANCH_MANAGER', 'FRONT_DESK', 'ORG_ADMIN']
            config.save(using=alias)

        from .models_rbac import Role, RoleAssignment
        roles_qs = Role.objects.using(alias).filter(organization=org, is_active=True).order_by('name')

        role_data = []
        for r in roles_qs:
            user_count = RoleAssignment.objects.using(alias).filter(
                role=r, status='ACTIVE', is_active=True
            ).values('user_id').distinct().count()
            role_data.append({
                'id': str(r.id),
                'code': r.code,
                'name': r.name,
                'scope': r.scope,
                'description': r.description or '',
                'user_count': user_count,
                'is_system': r.is_system or r.is_system_role,
            })

        users_qs = TenantUser.objects.using(alias).filter(
            organization=org, status='ACTIVE', is_login_allowed=True
        ).prefetch_related('role_assignments__role', 'home_branch').order_by('first_name', 'last_name')

        users_data = []
        for u in users_qs:
            user_roles = [
                {
                    'id': str(ra.role.id),
                    'code': ra.role.code,
                    'name': ra.role.name,
                    'scope_type': ra.scope_type,
                }
                for ra in u.role_assignments.all()
                if ra.status == 'ACTIVE' and ra.is_active
            ]
            users_data.append({
                'id': str(u.id),
                'name': f"{u.first_name} {u.last_name}".strip() or u.email,
                'email': u.email,
                'user_type': u.user_type,
                'home_branch_id': str(u.home_branch_id) if u.home_branch_id else None,
                'home_branch_name': u.home_branch.name if u.home_branch else None,
                'roles': user_roles,
            })

        res_data = CRMAgentAssignmentConfigSerializer(config).data
        res_data['available_roles'] = role_data
        res_data['available_users'] = users_data
        return Response(res_data)

    def create(self, request):
        return self._save(request)

    def update(self, request, pk=None):
        return self._save(request)

    def partial_update(self, request, pk=None):
        return self._save(request)

    def _save(self, request):
        alias = _get_db(request)
        org = _get_org(request)
        if not org:
            return Response({'error': 'Organization context missing'}, status=status.HTTP_400_BAD_REQUEST)

        config, _ = CRMAgentAssignmentConfig.objects.using(alias).get_or_create(
            organization=org,
            defaults={
                'allowed_role_codes': ['SALES_REP', 'BRANCH_MANAGER', 'FRONT_DESK', 'ORG_ADMIN'],
                'require_branch_match': True,
            }
        )

        serializer = CRMAgentAssignmentConfigSerializer(config, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        updated_config = serializer.save()

        record_business_audit(
            organization=org,
            module='crm',
            action_code='CRM_AGENT_ASSIGNMENT_CONFIG_UPDATED',
            entity_type='CRMAgentAssignmentConfig',
            entity_id=updated_config.id,
            actor_user=getattr(request, 'user', None),
            metadata={
                'allowed_role_codes': updated_config.allowed_role_codes,
                'excluded_user_ids': updated_config.excluded_user_ids,
                'require_branch_match': updated_config.require_branch_match,
                'allow_all_staff_fallback': updated_config.allow_all_staff_fallback,
                'assignment_mode_allowed': updated_config.assignment_mode_allowed,
                'default_assignment_mode': updated_config.default_assignment_mode,
                'auto_assignment_strategy': updated_config.auto_assignment_strategy,
                'allow_unassigned_fallback': updated_config.allow_unassigned_fallback,
                'consider_leave_availability': updated_config.consider_leave_availability,
                'notify_manager_on_unassigned': updated_config.notify_manager_on_unassigned,
            },
            db_alias=alias,
        )

        return self.list(request)


class CRMCampaignPerformanceViewSet(viewsets.ViewSet):
    """
    Marketing acquisition campaign performance analytics workspace.
    Groups LeadAttribution by (campaign_name, platform) and computes:
    - Leads generated
    - Trials booked
    - Converted members
    - Conversion rate
    - Paid revenue (strictly from paid Orders / successful transactions)
    Plus drill-down endpoints for explainability.
    """
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'crm'
    required_submodule = 'leads'
    required_permission = 'crm.leads.view'
    permission_action_map = {
        'list': 'crm.leads.view',
        'leads': 'crm.leads.view',
        'conversions': 'crm.leads.view',
        'revenue': 'crm.leads.view',
        'redemptions': 'crm.leads.view',
    }

    def list(self, request):
        alias = _get_db(request)
        org = _get_org(request)
        if not org:
            return Response({'summary': {}, 'campaigns': []})

        from .models_crm import LeadAttribution, Lead, TrialBooking, LeadConversion
        from .models_commerce import Order

        qs = LeadAttribution.objects.using(alias).filter(organization=org).select_related('lead')

        # Filters
        branch_id = request.query_params.get('branch_id') or request.query_params.get('branch')
        if branch_id and branch_id != 'ALL':
            qs = qs.filter(lead__branch_id=branch_id)

        platform = request.query_params.get('platform')
        if platform and platform != 'ALL':
            qs = qs.filter(platform__iexact=platform)

        source = request.query_params.get('source')
        if source and source != 'ALL':
            qs = qs.filter(Q(lead_source_id=source) | Q(utm_source__iexact=source))

        start_date = request.query_params.get('start_date')
        if start_date:
            qs = qs.filter(captured_at__date__gte=start_date)

        end_date = request.query_params.get('end_date')
        if end_date:
            qs = qs.filter(captured_at__date__lte=end_date)

        search = request.query_params.get('search')
        if search and search.strip():
            s = search.strip()
            qs = qs.filter(
                Q(campaign_name__icontains=s) |
                Q(platform__icontains=s) |
                Q(utm_campaign__icontains=s)
            )

        campaign_map = {}
        for attr in qs:
            camp_name = (attr.campaign_name or attr.utm_campaign or 'Direct / Unattributed').strip()
            plat = (attr.platform or attr.utm_source or 'Direct').strip()
            key = (camp_name, plat)
            if key not in campaign_map:
                campaign_map[key] = {
                    'campaign_name': camp_name,
                    'platform': plat,
                    'lead_ids': set(),
                }
            campaign_map[key]['lead_ids'].add(attr.lead_id)

        all_lead_ids = set()
        for data in campaign_map.values():
            all_lead_ids.update(data['lead_ids'])

        trials_per_lead = {}
        if all_lead_ids:
            for row in TrialBooking.objects.using(alias).filter(lead_id__in=all_lead_ids).values('lead_id'):
                lid = row['lead_id']
                trials_per_lead[lid] = trials_per_lead.get(lid, 0) + 1

        conversions = list(
            LeadConversion.objects.using(alias).filter(lead_id__in=all_lead_ids).values('id', 'lead_id', 'order_id')
        ) if all_lead_ids else []

        conversions_per_lead = {}
        order_ids = set()
        for conv in conversions:
            lid = conv['lead_id']
            conversions_per_lead[lid] = conversions_per_lead.get(lid, 0) + 1
            if conv['order_id']:
                order_ids.add(conv['order_id'])

        paid_orders_map = {}
        if order_ids:
            for ord_obj in Order.objects.using(alias).filter(id__in=order_ids, status='PAID').values('id', 'total_amount'):
                paid_orders_map[ord_obj['id']] = ord_obj['total_amount']

        results = []
        tot_leads = 0
        tot_trials = 0
        tot_conversions = 0
        tot_revenue = Decimal('0.00')

        for (camp_name, plat), data in campaign_map.items():
            l_ids = data['lead_ids']
            leads_count = len(l_ids)
            trials_count = sum(trials_per_lead.get(lid, 0) for lid in l_ids)
            conv_count = sum(conversions_per_lead.get(lid, 0) for lid in l_ids)
            c_rate = round((conv_count / leads_count * 100), 1) if leads_count > 0 else 0.0

            camp_order_ids = {conv['order_id'] for conv in conversions if conv['lead_id'] in l_ids and conv['order_id']}
            camp_revenue = sum((paid_orders_map.get(oid, Decimal('0.00')) for oid in camp_order_ids), Decimal('0.00'))

            tot_leads += leads_count
            tot_trials += trials_count
            tot_conversions += conv_count
            tot_revenue += camp_revenue

            results.append({
                'id': f"{camp_name}::{plat}",
                'campaign_name': camp_name,
                'platform': plat,
                'leads_count': leads_count,
                'trials_count': trials_count,
                'conversions_count': conv_count,
                'conversion_rate': c_rate,
                'paid_revenue': str(camp_revenue),
            })

        results.sort(key=lambda x: (-x['leads_count'], -Decimal(x['paid_revenue'])))
        overall_conv_rate = round((tot_conversions / tot_leads * 100), 1) if tot_leads > 0 else 0.0

        return Response({
            'summary': {
                'total_campaigns': len(results),
                'total_leads': tot_leads,
                'total_trials': tot_trials,
                'total_conversions': tot_conversions,
                'overall_conversion_rate': overall_conv_rate,
                'total_paid_revenue': str(tot_revenue),
            },
            'campaigns': results,
        })

    @action(detail=False, methods=['get'], url_path='leads')
    def leads(self, request):
        alias = _get_db(request)
        org = _get_org(request)
        campaign_name = request.query_params.get('campaign_name')
        platform = request.query_params.get('platform')

        from .models_crm import LeadAttribution, Lead
        from .serializers_crm import LeadSerializer

        qs = LeadAttribution.objects.using(alias).filter(organization=org)
        if campaign_name:
            if campaign_name == 'Direct / Unattributed':
                qs = qs.filter(Q(campaign_name__isnull=True) | Q(campaign_name=''))
            else:
                qs = qs.filter(campaign_name=campaign_name)
        if platform:
            qs = qs.filter(platform=platform)

        lead_ids = qs.values_list('lead_id', flat=True).distinct()
        leads_qs = Lead.objects.using(alias).filter(id__in=lead_ids).select_related('branch', 'assigned_sales_user').order_by('-created_at')
        return Response(LeadSerializer(leads_qs, many=True).data)

    @action(detail=False, methods=['get'], url_path='conversions')
    def conversions(self, request):
        alias = _get_db(request)
        org = _get_org(request)
        campaign_name = request.query_params.get('campaign_name')
        platform = request.query_params.get('platform')

        from .models_crm import LeadAttribution, LeadConversion
        from .models_commerce import Order

        qs = LeadAttribution.objects.using(alias).filter(organization=org)
        if campaign_name:
            if campaign_name == 'Direct / Unattributed':
                qs = qs.filter(Q(campaign_name__isnull=True) | Q(campaign_name=''))
            else:
                qs = qs.filter(campaign_name=campaign_name)
        if platform:
            qs = qs.filter(platform=platform)

        lead_ids = qs.values_list('lead_id', flat=True).distinct()
        conversions = LeadConversion.objects.using(alias).filter(lead_id__in=lead_ids).select_related('lead', 'user_profile').order_by('-converted_at')

        results = []
        for conv in conversions:
            order = None
            if conv.order_id:
                order = Order.objects.using(alias).filter(id=conv.order_id).first()
            results.append({
                'id': str(conv.id),
                'lead_id': str(conv.lead_id),
                'lead_name': f"{conv.lead.first_name} {conv.lead.last_name}".strip(),
                'member_name': f"{conv.user_profile.first_name_snapshot} {conv.user_profile.last_name_snapshot}".strip(),
                'converted_at': conv.converted_at.isoformat(),
                'order_id': str(conv.order_id) if conv.order_id else None,
                'order_number': order.order_number if order else None,
                'order_total': str(order.total_amount) if order else '0.00',
                'order_status': order.status if order else None,
            })
        return Response(results)

    @action(detail=False, methods=['get'], url_path='revenue')
    def revenue(self, request):
        alias = _get_db(request)
        org = _get_org(request)
        campaign_name = request.query_params.get('campaign_name')
        platform = request.query_params.get('platform')

        from .models_crm import LeadAttribution, LeadConversion
        from .models_commerce import Order, PaymentTransaction

        qs = LeadAttribution.objects.using(alias).filter(organization=org)
        if campaign_name:
            if campaign_name == 'Direct / Unattributed':
                qs = qs.filter(Q(campaign_name__isnull=True) | Q(campaign_name=''))
            else:
                qs = qs.filter(campaign_name=campaign_name)
        if platform:
            qs = qs.filter(platform=platform)

        lead_ids = qs.values_list('lead_id', flat=True).distinct()
        order_ids = LeadConversion.objects.using(alias).filter(lead_id__in=lead_ids, order_id__isnull=False).values_list('order_id', flat=True).distinct()

        paid_orders = Order.objects.using(alias).filter(id__in=order_ids, status='PAID').select_related('lead', 'user_profile').order_by('-created_at')
        results = []
        for ord_obj in paid_orders:
            ptxn = PaymentTransaction.objects.using(alias).filter(order=ord_obj, status='SUCCESS').first()
            results.append({
                'order_id': str(ord_obj.id),
                'order_number': ord_obj.order_number,
                'lead_name': f"{ord_obj.lead.first_name} {ord_obj.lead.last_name}".strip() if ord_obj.lead else None,
                'amount': str(ord_obj.total_amount),
                'status': ord_obj.status,
                'payment_provider': ptxn.provider if ptxn else 'OFFLINE',
                'created_at': ord_obj.created_at.isoformat(),
            })
        return Response(results)

    @action(detail=False, methods=['get'], url_path='redemptions')
    def redemptions(self, request):
        alias = _get_db(request)
        org = _get_org(request)
        campaign_name = request.query_params.get('campaign_name')

        from .models_discounts import DiscountRedemption
        from .serializers_discounts import DiscountRedemptionSerializer

        qs = DiscountRedemption.objects.using(alias).filter(campaign__organization=org).select_related('discount_code', 'campaign', 'user_profile', 'order')
        if campaign_name:
            qs = qs.filter(campaign__name=campaign_name)
        return Response(DiscountRedemptionSerializer(qs.order_by('-redeemed_at')[:100], many=True).data)


class CRMDashboardViewSet(viewsets.ViewSet):
    """
    CRMDashboardViewSet — Layer 2 Module B Phase 10: CRM Dashboard & Sales Analytics
    Authoritative backend aggregation of leads, funnel, attribution, trials,
    follow-ups, attention, agent/branch metrics, and verified commercial revenue.
    """
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'crm'
    required_submodule = 'leads'
    required_permission = 'crm.dashboard.view'
    permission_action_map = {
        'list': 'crm.dashboard.view',
    }

    def list(self, request):
        alias = _get_db(request)
        org = _get_org(request)
        if not org:
            return Response({'error': 'Organization not found'}, status=400)

        from .services_crm_dashboard import CRMDashboardService

        try:
            data = CRMDashboardService.get_dashboard_data(
                organization=org,
                user=request.user,
                filters=request.query_params,
                db_alias=alias,
            )
            return Response(data)
        except PermissionDenied as e:
            return Response({'error': str(e)}, status=403)
        except ValidationError as e:
            return Response({'error': str(e)}, status=400)
        except Exception as e:
            logger.exception("CRM Dashboard aggregation failed")
            return Response({'error': 'Failed to load CRM dashboard metrics'}, status=500)


class InAppNotificationViewSet(viewsets.ModelViewSet):
    serializer_class = InAppNotificationSerializer
    permission_classes = [RequireActiveTenantAndOrg]
    filter_backends = [filters.OrderingFilter]
    ordering = ['-created_at']

    def get_queryset(self):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        user = self.request.user
        qs = InAppNotification.objects.using(alias).filter(user=user)
        if org:
            qs = qs.filter(organization=org)

        unread_only = self.request.query_params.get('unread')
        if unread_only and str(unread_only).lower() in ('true', '1'):
            qs = qs.filter(is_read=False)
        return qs

    @action(detail=False, methods=['get'], url_path='unread-count')
    def unread_count(self, request):
        alias = _get_db(request)
        org = _get_org(request)
        qs = InAppNotification.objects.using(alias).filter(user=request.user, is_read=False)
        if org:
            qs = qs.filter(organization=org)
        return Response({'unread_count': qs.count()})

    @action(detail=True, methods=['post'], url_path='mark-read')
    def mark_read(self, request, pk=None):
        alias = _get_db(request)
        notification = self.get_object()
        if not notification.is_read:
            notification.is_read = True
            notification.read_at = timezone.now()
            notification.save(using=alias, update_fields=['is_read', 'read_at'])
        return Response(InAppNotificationSerializer(notification).data)

    @action(detail=False, methods=['post'], url_path='mark-all-read')
    def mark_all_read(self, request):
        alias = _get_db(request)
        org = _get_org(request)
        now = timezone.now()
        qs = InAppNotification.objects.using(alias).filter(user=request.user, is_read=False)
        if org:
            qs = qs.filter(organization=org)
        count = qs.update(is_read=True, read_at=now)
        return Response({'marked_read': count})

