"""
DRF ViewSets for Layer 2: Module E (Group Classes, Scheduling, Content Studio & Demand Planning)
"""

import logging
from datetime import date
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from django.core.exceptions import ValidationError

from .context import get_tenant_db_alias
from .models_classes import (
    ClassCategory, ClassTemplate, ClassPrice, ClassBranchAvailability,
    ClassScheduleRule, ClassOccurrence, ClassOccurrenceTrainer,
    PackageClassAccessRule, ClassSpecialtyRequirement,
    ClassScheduleImportBatch, ClassScheduleImportRow,
    ClassContentItem, ClassContentMapping, ClassContentAssignment,
    ClassDemandEvent, ClassDemandPlanningRun, ClassScheduleRecommendation,
)
from .serializers_classes import (
    ClassCategorySerializer, ClassTemplateSerializer, ClassPriceSerializer,
    ClassBranchAvailabilitySerializer, ClassScheduleRuleSerializer, ClassOccurrenceSerializer,
    ClassOccurrenceTrainerSerializer, PackageClassAccessRuleSerializer, ClassSpecialtyRequirementSerializer,
    ClassScheduleImportBatchSerializer, ClassScheduleImportRowSerializer,
    ClassContentItemSerializer, ClassContentMappingSerializer, ClassContentAssignmentSerializer,
    ClassDemandEventSerializer, ClassDemandPlanningRunSerializer, ClassScheduleRecommendationSerializer,
)
from .services_classes import ClassSchedulingService, ContentStudioService
from .permissions import RequireActiveTenantAndOrg, TenantRBACPermission
from .services_reliability import record_business_audit
from django.utils import timezone

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


def get_user_effective_branch_ids(user, db_alias: str):
    """
    Derives the effective permitted branch IDs for a user based on active RoleAssignments
    and UserBranch records.
    Returns None if user has ORG scope (meaning all branches permitted).
    Returns a set of branch ID strings if branch-scoped.
    """
    if getattr(user, 'is_superuser', False):
        return None

    from .models_rbac import RoleAssignment
    from .models_users import UserBranch

    assignments = list(
        RoleAssignment.objects.using(db_alias)
        .filter(user=user, is_active=True, status='ACTIVE')
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


class ClassesMetadataView(APIView):
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'ops'
    required_submodule = 'classes'
    required_permission = 'ops.classes.view'
    action = 'view'

    def get(self, request):
        return Response({
            'statuses': [
                {'value': 'ACTIVE', 'label': 'Active'},
                {'value': 'INACTIVE', 'label': 'Inactive'},
                {'value': 'DRAFT', 'label': 'Draft'},
                {'value': 'ARCHIVED', 'label': 'Archived'},
            ],
            'delivery_modes': [
                {'value': 'OFFLINE', 'label': 'Offline / Studio'},
                {'value': 'ONLINE', 'label': 'Online Live Stream'},
                {'value': 'HYBRID', 'label': 'Hybrid'},
            ],
            'recurrence_types': [
                {'value': 'WEEKLY', 'label': 'Weekly'},
            ],
            'weekdays': [
                {'value': 1, 'label': 'Mon', 'full_name': 'Monday'},
                {'value': 2, 'label': 'Tue', 'full_name': 'Tuesday'},
                {'value': 3, 'label': 'Wed', 'full_name': 'Wednesday'},
                {'value': 4, 'label': 'Thu', 'full_name': 'Thursday'},
                {'value': 5, 'label': 'Fri', 'full_name': 'Friday'},
                {'value': 6, 'label': 'Sat', 'full_name': 'Saturday'},
                {'value': 7, 'label': 'Sun', 'full_name': 'Sunday'},
            ],
            'trainer_roles': [
                {'value': 'LEAD', 'label': 'Lead Trainer'},
                {'value': 'ASSISTANT', 'label': 'Assistant Trainer'},
                {'value': 'SUBSTITUTE', 'label': 'Substitute Trainer'},
            ],
            'content_types': [
                {'value': 'VIDEO', 'label': 'Video'},
                {'value': 'VIDEO_LINK', 'label': 'Video Stream Link'},
                {'value': 'DOCUMENT', 'label': 'Document'},
                {'value': 'IMAGE', 'label': 'Image'},
                {'value': 'OTHER', 'label': 'Other'},
            ],
            'branch_avail_statuses': [
                {'value': 'ENABLED', 'label': 'Enabled'},
                {'value': 'DISABLED', 'label': 'Disabled'},
            ],
        })


class ClassCategoryViewSet(viewsets.ModelViewSet):
    serializer_class = ClassCategorySerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'ops'
    required_submodule = 'classes'
    required_permission = 'ops.classes.view'
    permission_action_map = {'create': 'ops.classes.create', 'update': 'ops.classes.edit', 'partial_update': 'ops.classes.edit', 'destroy': 'ops.classes.delete'}

    def get_queryset(self):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        return ClassCategory.objects.using(alias).filter(organization=org).order_by('display_order')

    def perform_create(self, serializer):
        org = _get_org(self.request)
        instance = serializer.save(organization=org)
        alias = _get_db(self.request)
        record_business_audit(
            organization=org,
            module='classes',
            action_code='CLASS_CATEGORY_CREATED',
            entity_type='ClassCategory',
            entity_id=instance.id,
            actor_user=self.request.user,
            event_description=f"Created class category '{instance.name}'",
            after_data={'code': instance.code, 'name': instance.name, 'status': instance.status},
            db_alias=alias,
        )

    def perform_update(self, serializer):
        instance = serializer.save()
        alias = _get_db(self.request)
        org = _get_org(self.request)
        record_business_audit(
            organization=org,
            module='classes',
            action_code='CLASS_CATEGORY_UPDATED',
            entity_type='ClassCategory',
            entity_id=instance.id,
            actor_user=self.request.user,
            event_description=f"Updated class category '{instance.name}'",
            after_data={'code': instance.code, 'name': instance.name, 'status': instance.status},
            db_alias=alias,
        )


class ClassTemplateViewSet(viewsets.ModelViewSet):
    serializer_class = ClassTemplateSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'ops'
    required_submodule = 'classes'
    required_permission = 'ops.classes.view'
    permission_action_map = {'create': 'ops.classes.create', 'update': 'ops.classes.edit', 'partial_update': 'ops.classes.edit', 'destroy': 'ops.classes.delete'}

    def get_queryset(self):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        qs = ClassTemplate.objects.using(alias).filter(organization=org)
        category_id = self.request.query_params.get('category_id')
        if category_id:
            qs = qs.filter(category_id=category_id)
        return qs.order_by('name')

    def perform_create(self, serializer):
        org = _get_org(self.request)
        instance = serializer.save(organization=org)
        alias = _get_db(self.request)
        record_business_audit(
            organization=org,
            module='classes',
            action_code='CLASS_TEMPLATE_CREATED',
            entity_type='ClassTemplate',
            entity_id=instance.id,
            actor_user=self.request.user,
            event_description=f"Created class template '{instance.name}'",
            after_data={'code': instance.code, 'name': instance.name, 'status': instance.status},
            db_alias=alias,
        )

    def perform_update(self, serializer):
        instance = serializer.save()
        alias = _get_db(self.request)
        org = _get_org(self.request)
        record_business_audit(
            organization=org,
            module='classes',
            action_code='CLASS_TEMPLATE_UPDATED',
            entity_type='ClassTemplate',
            entity_id=instance.id,
            actor_user=self.request.user,
            event_description=f"Updated class template '{instance.name}'",
            after_data={'code': instance.code, 'name': instance.name, 'status': instance.status},
            db_alias=alias,
        )


class ClassPriceViewSet(viewsets.ModelViewSet):
    serializer_class = ClassPriceSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'ops'
    required_submodule = 'classes'
    required_permission = 'ops.classes.view'
    permission_action_map = {'create': 'ops.classes.create', 'update': 'ops.classes.edit', 'partial_update': 'ops.classes.edit', 'destroy': 'ops.classes.delete'}

    def get_queryset(self):
        alias = _get_db(self.request)
        return ClassPrice.objects.using(alias).all().order_by('-effective_from')

    def perform_create(self, serializer):
        alias = _get_db(self.request)
        tpl = serializer.validated_data.get('class_template')
        existing_count = ClassPrice.objects.using(alias).filter(class_template=tpl).count()
        ver = serializer.validated_data.get('version_number') or (existing_count + 1)
        eff_from = serializer.validated_data.get('effective_from') or timezone.now().date()
        serializer.save(created_by_user=self.request.user, version_number=ver, effective_from=eff_from)


class ClassBranchAvailabilityViewSet(viewsets.ModelViewSet):
    serializer_class = ClassBranchAvailabilitySerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'ops'
    required_submodule = 'classes'
    required_permission = 'ops.classes.view'
    permission_action_map = {'create': 'ops.classes.create', 'update': 'ops.classes.edit', 'partial_update': 'ops.classes.edit', 'destroy': 'ops.classes.delete'}

    def get_queryset(self):
        alias = _get_db(self.request)
        return ClassBranchAvailability.objects.using(alias).all()


class ClassScheduleRuleViewSet(viewsets.ModelViewSet):
    serializer_class = ClassScheduleRuleSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'ops'
    required_submodule = 'classes'
    required_permission = 'ops.classes.view'
    permission_action_map = {'create': 'ops.classes.create', 'update': 'ops.classes.edit', 'partial_update': 'ops.classes.edit', 'destroy': 'ops.classes.delete', 'generate_occurrences': 'ops.classes.create'}

    def get_queryset(self):
        alias = _get_db(self.request)
        qs = ClassScheduleRule.objects.using(alias).all()
        permitted_branches = get_user_effective_branch_ids(self.request.user, alias)
        if permitted_branches is not None:
            qs = qs.filter(branch_id__in=permitted_branches)
        branch_id = self.request.query_params.get('branch_id')
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        return qs.order_by('start_time')

    def perform_create(self, serializer):
        rule = serializer.save()
        alias = _get_db(self.request)
        record_business_audit(
            organization=rule.branch.organization,
            branch=rule.branch,
            module='classes',
            action_code='SCHEDULE_RULE_CREATED',
            entity_type='ClassScheduleRule',
            entity_id=rule.id,
            actor_user=self.request.user,
            event_description=f"Created recurring schedule rule for {rule.class_template.name} at {rule.branch.name}",
            after_data={
                'template_name': rule.class_template.name,
                'branch_name': rule.branch.name,
                'days': rule.days_of_week,
                'time_window': f"{rule.start_time.strftime('%H:%M')} – {rule.end_time.strftime('%H:%M')}",
                'status': rule.status,
            },
            db_alias=alias,
        )

    def perform_update(self, serializer):
        rule = serializer.save()
        alias = _get_db(self.request)
        record_business_audit(
            organization=rule.branch.organization,
            branch=rule.branch,
            module='classes',
            action_code='SCHEDULE_RULE_UPDATED',
            entity_type='ClassScheduleRule',
            entity_id=rule.id,
            actor_user=self.request.user,
            event_description=f"Updated recurring schedule rule for {rule.class_template.name} at {rule.branch.name}",
            after_data={
                'template_name': rule.class_template.name,
                'branch_name': rule.branch.name,
                'days': rule.days_of_week,
                'time_window': f"{rule.start_time.strftime('%H:%M')} – {rule.end_time.strftime('%H:%M')}",
                'status': rule.status,
            },
            db_alias=alias,
        )

    @action(detail=True, methods=['post'], url_path='generate-occurrences')
    def generate_occurrences(self, request, pk=None):
        alias = _get_db(request)
        rule = self.get_object()
        from_str = request.data.get('from_date')
        to_str = request.data.get('to_date')
        if not from_str or not to_str:
            return Response({'error': 'from_date and to_date (YYYY-MM-DD) are required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            from_d = date.fromisoformat(from_str)
            to_d = date.fromisoformat(to_str)
            occurrences = ClassSchedulingService.generate_occurrences_from_rule(
                rule=rule,
                from_date=from_d,
                to_date=to_d,
                actor=request.user,
                db_alias=alias,
            )
            return Response(ClassOccurrenceSerializer(occurrences, many=True).data, status=status.HTTP_201_CREATED)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


class ClassOccurrenceViewSet(viewsets.ModelViewSet):
    serializer_class = ClassOccurrenceSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'ops'
    required_submodule = 'classes'
    required_permission = 'ops.classes.view'
    permission_action_map = {'create': 'ops.classes.create', 'update': 'ops.classes.edit', 'partial_update': 'ops.classes.edit', 'destroy': 'ops.classes.delete', 'assign_trainer': 'ops.classes.edit', 'assign_content': 'ops.classes.edit'}

    def get_queryset(self):
        alias = _get_db(self.request)
        qs = ClassOccurrence.objects.using(alias).all()

        # Branch scope enforcement
        permitted_branches = get_user_effective_branch_ids(self.request.user, alias)
        if permitted_branches is not None:
            qs = qs.filter(branch_id__in=permitted_branches)

        branch_id = self.request.query_params.get('branch_id')
        occ_date = self.request.query_params.get('occurrence_date')
        status_param = self.request.query_params.get('status')
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if occ_date:
            qs = qs.filter(occurrence_date=occ_date)
        if status_param:
            qs = qs.filter(status=status_param)
        return qs.select_related('class_template', 'branch').prefetch_related('trainer_assignments__trainer_profile').order_by('start_at')

    def perform_create(self, serializer):
        occ = serializer.save(is_manual=True)
        alias = _get_db(self.request)
        record_business_audit(
            organization=occ.branch.organization,
            branch=occ.branch,
            module='classes',
            action_code='OCCURRENCE_CREATED',
            entity_type='ClassOccurrence',
            entity_id=occ.id,
            actor_user=self.request.user,
            event_description=f"Created one-off class occurrence for {occ.class_template.name} at {occ.branch.name} on {occ.occurrence_date}",
            after_data={'template_id': str(occ.class_template_id), 'branch_id': str(occ.branch_id), 'date': str(occ.occurrence_date)},
            db_alias=alias,
        )

    @action(detail=True, methods=['post'], url_path='assign-trainer')
    def assign_trainer(self, request, pk=None):
        alias = _get_db(request)
        trainer_profile_id = request.data.get('trainer_profile_id')
        trainer_role = request.data.get('trainer_role', 'LEAD')
        if not trainer_profile_id:
            return Response({'error': 'trainer_profile_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            assignment = ClassSchedulingService.assign_trainer_to_occurrence(
                occurrence_id=pk,
                trainer_profile_id=trainer_profile_id,
                trainer_role=trainer_role,
                actor=request.user,
                db_alias=alias,
            )
            return Response(ClassOccurrenceTrainerSerializer(assignment).data, status=status.HTTP_201_CREATED)
        except ValidationError as e:
            msg = e.messages[0] if hasattr(e, 'messages') and e.messages else str(e)
            return Response({'error': msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='assign-content')
    def assign_content(self, request, pk=None):
        alias = _get_db(request)
        try:
            assignment = ContentStudioService.rotate_and_assign_content_to_occurrence(
                occurrence_id=pk,
                actor=request.user,
                db_alias=alias,
            )
            if not assignment:
                return Response({'error': 'No eligible content items found in mapping pool'}, status=status.HTTP_404_NOT_FOUND)
            return Response(ClassContentAssignmentSerializer(assignment).data, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


class ClassOccurrenceTrainerViewSet(viewsets.ModelViewSet):
    serializer_class = ClassOccurrenceTrainerSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'ops'
    required_submodule = 'classes'
    required_permission = 'ops.classes.view'
    permission_action_map = {'create': 'ops.classes.create', 'update': 'ops.classes.edit', 'partial_update': 'ops.classes.edit', 'destroy': 'ops.classes.delete'}

    def get_queryset(self):
        alias = _get_db(self.request)
        return ClassOccurrenceTrainer.objects.using(alias).all().order_by('-assigned_at')


class PackageClassAccessRuleViewSet(viewsets.ModelViewSet):
    serializer_class = PackageClassAccessRuleSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'ops'
    required_submodule = 'classes'
    required_permission = 'ops.classes.view'
    permission_action_map = {'create': 'ops.classes.create', 'update': 'ops.classes.edit', 'partial_update': 'ops.classes.edit', 'destroy': 'ops.classes.delete'}

    def get_queryset(self):
        alias = _get_db(self.request)
        return PackageClassAccessRule.objects.using(alias).all()


class ClassSpecialtyRequirementViewSet(viewsets.ModelViewSet):
    serializer_class = ClassSpecialtyRequirementSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'ops'
    required_submodule = 'classes'
    required_permission = 'ops.classes.view'
    permission_action_map = {'create': 'ops.classes.create', 'update': 'ops.classes.edit', 'partial_update': 'ops.classes.edit', 'destroy': 'ops.classes.delete'}

    def get_queryset(self):
        alias = _get_db(self.request)
        return ClassSpecialtyRequirement.objects.using(alias).all()


class ClassScheduleImportBatchViewSet(viewsets.ModelViewSet):
    serializer_class = ClassScheduleImportBatchSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'ops'
    required_submodule = 'classes'
    required_permission = 'ops.classes.view'
    permission_action_map = {'create': 'ops.classes.create', 'update': 'ops.classes.edit', 'partial_update': 'ops.classes.edit', 'destroy': 'ops.classes.delete'}

    def get_queryset(self):
        alias = _get_db(self.request)
        return ClassScheduleImportBatch.objects.using(alias).all().order_by('-uploaded_at')


class ClassScheduleImportRowViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ClassScheduleImportRowSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'ops'
    required_submodule = 'classes'
    required_permission = 'ops.classes.view'

    def get_queryset(self):
        alias = _get_db(self.request)
        return ClassScheduleImportRow.objects.using(alias).all().order_by('row_number')


class ClassContentItemViewSet(viewsets.ModelViewSet):
    serializer_class = ClassContentItemSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'ops'
    required_submodule = 'classes'
    required_permission = 'ops.classes.view'
    permission_action_map = {'create': 'ops.classes.create', 'update': 'ops.classes.edit', 'partial_update': 'ops.classes.edit', 'destroy': 'ops.classes.delete'}

    def get_queryset(self):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        return ClassContentItem.objects.using(alias).filter(organization=org).order_by('display_order', 'title')

    def perform_create(self, serializer):
        serializer.save(organization=_get_org(self.request))


class ClassContentMappingViewSet(viewsets.ModelViewSet):
    serializer_class = ClassContentMappingSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'ops'
    required_submodule = 'classes'
    required_permission = 'ops.classes.view'
    permission_action_map = {'create': 'ops.classes.create', 'update': 'ops.classes.edit', 'partial_update': 'ops.classes.edit', 'destroy': 'ops.classes.delete'}

    def get_queryset(self):
        alias = _get_db(self.request)
        return ClassContentMapping.objects.using(alias).all()


class ClassContentAssignmentViewSet(viewsets.ModelViewSet):
    serializer_class = ClassContentAssignmentSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'ops'
    required_submodule = 'classes'
    required_permission = 'ops.classes.view'
    permission_action_map = {'create': 'ops.classes.create', 'update': 'ops.classes.edit', 'partial_update': 'ops.classes.edit', 'destroy': 'ops.classes.delete'}

    def get_queryset(self):
        alias = _get_db(self.request)
        return ClassContentAssignment.objects.using(alias).all().order_by('-assigned_at')


class ClassDemandEventViewSet(viewsets.ModelViewSet):
    serializer_class = ClassDemandEventSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'ops'
    required_submodule = 'classes'
    required_permission = 'ops.classes.view'
    permission_action_map = {'create': 'ops.classes.create'}

    def get_queryset(self):
        alias = _get_db(self.request)
        return ClassDemandEvent.objects.using(alias).all().order_by('-event_at')


class ClassDemandPlanningRunViewSet(viewsets.ModelViewSet):
    serializer_class = ClassDemandPlanningRunSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'ops'
    required_submodule = 'classes'
    required_permission = 'ops.classes.view'
    permission_action_map = {'create': 'ops.classes.create', 'update': 'ops.classes.edit', 'partial_update': 'ops.classes.edit', 'destroy': 'ops.classes.delete'}

    def get_queryset(self):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        return ClassDemandPlanningRun.objects.using(alias).filter(organization=org).order_by('-created_at')

    def perform_create(self, serializer):
        serializer.save(organization=_get_org(self.request))


class ClassScheduleRecommendationViewSet(viewsets.ModelViewSet):
    serializer_class = ClassScheduleRecommendationSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'ops'
    required_submodule = 'classes'
    required_permission = 'ops.classes.view'
    permission_action_map = {'create': 'ops.classes.create', 'update': 'ops.classes.edit', 'partial_update': 'ops.classes.edit', 'destroy': 'ops.classes.delete'}

    def get_queryset(self):
        alias = _get_db(self.request)
        return ClassScheduleRecommendation.objects.using(alias).all().order_by('-created_at')
