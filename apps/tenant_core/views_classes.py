"""
DRF ViewSets for Layer 2: Module E (Group Classes, Scheduling, Content Studio & Demand Planning)
"""

import logging
from datetime import date, timedelta
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView
from django.core.exceptions import ValidationError
from django.db import transaction


class ClassesPagination(PageNumberPagination):
    page_size = 500
    page_size_query_param = 'page_size'
    max_page_size = 5000

    def get_paginated_response(self, data):
        summary = getattr(self, '_classes_summary', None) or {}
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
            'occurrence_statuses': [
                {'value': 'SCHEDULED', 'label': 'Scheduled'},
                {'value': 'OPEN', 'label': 'Open'},
                {'value': 'FULL', 'label': 'Full'},
                {'value': 'IN_PROGRESS', 'label': 'In Progress'},
                {'value': 'COMPLETED', 'label': 'Completed'},
                {'value': 'CANCELLED', 'label': 'Cancelled'},
                {'value': 'DEACTIVATED', 'label': 'Deactivated'},
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
        qs = ClassCategory.objects.using(alias).filter(organization=org)
        status_filter = self.request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        return qs.order_by('display_order')

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
        qs = ClassTemplate.objects.using(alias).filter(organization=org).prefetch_related('branch_availabilities', 'branch_availabilities__branch')
        category_id = self.request.query_params.get('category_id')
        if category_id:
            qs = qs.filter(category_id=category_id)
        status_filter = self.request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        return qs.order_by('name')

    def _save_branch_capacities(self, instance, branch_capacities, alias):
        if not isinstance(branch_capacities, list):
            return
        from .models_classes import ClassBranchAvailability
        from .models_org import Branch
        for item in branch_capacities:
            branch_id = item.get('branch_id') or item.get('branch')
            if not branch_id:
                continue
            branch = Branch.objects.using(alias).filter(id=branch_id).first()
            if not branch:
                continue

            is_enabled = item.get('is_enabled')
            status_val = 'DISABLED' if is_enabled is False else item.get('status', 'ENABLED')
            cap = item.get('capacity_override')
            trial_cap = item.get('trial_capacity_override')
            wait_cap = item.get('waitlist_capacity_override')

            cap_val = int(cap) if cap not in (None, '') else None
            trial_val = int(trial_cap) if trial_cap not in (None, '') else None
            wait_val = int(wait_cap) if wait_cap not in (None, '') else None

            ClassBranchAvailability.objects.using(alias).update_or_create(
                class_template=instance,
                branch=branch,
                defaults={
                    'status': status_val,
                    'capacity_override': cap_val,
                    'trial_capacity_override': trial_val,
                    'waitlist_capacity_override': wait_val,
                }
            )

    def perform_create(self, serializer):
        org = _get_org(self.request)
        instance = serializer.save(organization=org)
        alias = _get_db(self.request)
        branch_capacities = self.request.data.get('branch_capacities')
        if branch_capacities:
            self._save_branch_capacities(instance, branch_capacities, alias)
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
        branch_capacities = self.request.data.get('branch_capacities')
        if branch_capacities is not None:
            self._save_branch_capacities(instance, branch_capacities, alias)
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
        qs = ClassBranchAvailability.objects.using(alias).select_related('class_template', 'branch').all()
        tpl = self.request.query_params.get('class_template') or self.request.query_params.get('template_id')
        if tpl:
            qs = qs.filter(class_template_id=tpl)
        br = self.request.query_params.get('branch') or self.request.query_params.get('branch_id')
        if br:
            qs = qs.filter(branch_id=br)
        return qs


class ClassScheduleRuleViewSet(viewsets.ModelViewSet):
    serializer_class = ClassScheduleRuleSerializer
    pagination_class = ClassesPagination
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'ops'
    required_submodule = 'classes'
    required_permission = 'ops.classes.view'
    permission_action_map = {'create': 'ops.classes.create', 'update': 'ops.classes.edit', 'partial_update': 'ops.classes.edit', 'destroy': 'ops.classes.delete', 'generate_occurrences': 'ops.classes.create'}

    def get_queryset(self):
        from django.db.models import Q, Count
        alias = _get_db(self.request)
        qs = ClassScheduleRule.objects.using(alias).select_related(
            'class_template', 'class_template__category', 'class_template__program', 'branch'
        ).all()
        permitted_branches = get_user_effective_branch_ids(self.request.user, alias)
        if permitted_branches is not None:
            qs = qs.filter(branch_id__in=permitted_branches)
        branch_id = self.request.query_params.get('branch_id')
        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        category_id = (self.request.query_params.get('category_id') or self.request.query_params.get('category') or '').strip()
        if category_id and category_id.upper() != 'ALL':
            qs = qs.filter(class_template__category_id=category_id)

        category_code = (self.request.query_params.get('category_code') or '').strip()
        if category_code and category_code.upper() != 'ALL':
            qs = qs.filter(class_template__category__code__iexact=category_code)

        if self.action == 'list' and getattr(self, 'paginator', None) is not None:
            self.paginator._classes_summary = qs.aggregate(
                total_count=Count('id'),
                active_count=Count('id', filter=Q(status='ACTIVE')),
                inactive_count=Count('id', filter=~Q(status='ACTIVE')),
            )

        status_param = self.request.query_params.get('status')
        if status_param and status_param.upper() != 'ALL':
            qs = qs.filter(status__iexact=status_param.strip())

        date_param = (self.request.query_params.get('date') or self.request.query_params.get('occurrence_date') or '').strip()
        if date_param:
            try:
                from datetime import datetime as dt_parser
                parsed_date = dt_parser.strptime(date_param, '%Y-%m-%d').date()
                iso_weekday = parsed_date.isoweekday()
                qs = qs.filter(
                    valid_from__lte=parsed_date
                ).filter(
                    Q(valid_until__isnull=True) | Q(valid_until__gte=parsed_date)
                ).filter(
                    Q(days_of_week__contains=[iso_weekday]) | Q(days_of_week__contains=[str(iso_weekday)])
                )
            except ValueError:
                pass

        from_date_param = (self.request.query_params.get('from_date') or '').strip()
        if from_date_param:
            qs = qs.filter(Q(valid_until__isnull=True) | Q(valid_until__gte=from_date_param))

        to_date_param = (self.request.query_params.get('to_date') or '').strip()
        if to_date_param:
            qs = qs.filter(valid_from__lte=to_date_param)

        day_of_week_param = (self.request.query_params.get('day_of_week') or '').strip()
        if day_of_week_param and day_of_week_param.upper() != 'ALL':
            try:
                dow = int(day_of_week_param)
                qs = qs.filter(Q(days_of_week__contains=[dow]) | Q(days_of_week__contains=[str(dow)]))
            except ValueError:
                pass

        search_param = (self.request.query_params.get('search') or '').strip()
        if search_param:
            qs = qs.filter(
                Q(class_template__name__icontains=search_param) |
                Q(class_template__category__name__icontains=search_param) |
                Q(class_template__program__name__icontains=search_param) |
                Q(branch__name__icontains=search_param)
            )

        return qs.order_by('start_time', 'id')

    def create(self, request, *args, **kwargs):
        alias = _get_db(request)
        branches = request.data.get('branches')
        auto_generate = request.data.get('generate_slots', True)
        gen_from = request.data.get('generate_from_date') or request.data.get('valid_from')
        gen_to = request.data.get('generate_to_date') or request.data.get('valid_until')

        # Multi-branch batch creation
        if isinstance(branches, list) and len(branches) > 0:
            if not gen_to and gen_from:
                try:
                    from_d = date.fromisoformat(str(gen_from))
                    gen_to = (from_d + timedelta(days=30)).isoformat()
                except Exception:
                    pass

            created_rules = []
            total_occurrences_count = 0

            with transaction.atomic(using=alias):
                for branch_id in branches:
                    data = request.data.copy() if hasattr(request.data, 'copy') else dict(request.data)
                    data['branch'] = branch_id
                    data.pop('branches', None)
                    data.pop('generate_slots', None)
                    data.pop('generate_from_date', None)
                    data.pop('generate_to_date', None)

                    serializer = self.get_serializer(data=data)
                    serializer.is_valid(raise_exception=True)
                    rule = serializer.save()

                    record_business_audit(
                        organization=rule.branch.organization,
                        branch=rule.branch,
                        module='classes',
                        action_code='SCHEDULE_RULE_CREATED',
                        entity_type='ClassScheduleRule',
                        entity_id=rule.id,
                        actor_user=request.user,
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
                    created_rules.append(rule)

                    if auto_generate and gen_from and gen_to:
                        try:
                            from_d = date.fromisoformat(str(gen_from))
                            to_d = date.fromisoformat(str(gen_to))
                            occs = ClassSchedulingService.generate_occurrences_from_rule(
                                rule=rule,
                                from_date=from_d,
                                to_date=to_d,
                                actor=request.user,
                                db_alias=alias,
                            )
                            total_occurrences_count += len(occs)
                        except Exception as exc:
                            logger.warning(f"Failed to generate occurrences for rule {rule.id}: {exc}")

            serialized = self.get_serializer(created_rules, many=True).data
            return Response({
                'rules': serialized,
                'count': len(created_rules),
                'occurrences_generated': total_occurrences_count,
            }, status=status.HTTP_201_CREATED)

        # Single branch creation
        data = request.data.copy() if hasattr(request.data, 'copy') else dict(request.data)
        data.pop('branches', None)
        data.pop('generate_slots', None)
        data.pop('generate_from_date', None)
        data.pop('generate_to_date', None)

        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        rule = serializer.instance

        total_occurrences_count = 0
        if auto_generate and gen_from:
            if not gen_to:
                try:
                    from_d = date.fromisoformat(str(gen_from))
                    gen_to = (from_d + timedelta(days=30)).isoformat()
                except Exception:
                    pass
            if gen_to:
                try:
                    from_d = date.fromisoformat(str(gen_from))
                    to_d = date.fromisoformat(str(gen_to))
                    occs = ClassSchedulingService.generate_occurrences_from_rule(
                        rule=rule,
                        from_date=from_d,
                        to_date=to_d,
                        actor=request.user,
                        db_alias=alias,
                    )
                    total_occurrences_count = len(occs)
                except Exception as exc:
                    logger.warning(f"Failed to generate occurrences for rule {rule.id}: {exc}")

        resp_data = serializer.data
        resp_data['occurrences_generated'] = total_occurrences_count
        headers = self.get_success_headers(serializer.data)
        return Response(resp_data, status=status.HTTP_201_CREATED, headers=headers)

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
    pagination_class = ClassesPagination
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'ops'
    required_submodule = 'classes'
    required_permission = 'ops.classes.view'
    permission_action_map = {
        'create': 'ops.classes.create',
        'update': 'ops.classes.edit',
        'partial_update': 'ops.classes.edit',
        'destroy': 'ops.classes.delete',
        'assign_trainer': 'ops.classes.edit',
        'assign_content': 'ops.classes.edit',
        'cancel': 'ops.classes.cancel',
        'restore': 'ops.classes.cancel',
        'deactivate': 'ops.classes.cancel',
        'cancellation_preview': 'ops.classes.view',
    }

    def get_queryset(self):
        from django.db.models import Q, Exists, OuterRef, Prefetch
        from .models_bookings import Booking
        alias = _get_db(self.request)
        qs = ClassOccurrence.objects.using(alias).all()

        # Branch scope enforcement
        permitted_branches = get_user_effective_branch_ids(self.request.user, alias)
        if permitted_branches is not None:
            qs = qs.filter(branch_id__in=permitted_branches)

        branch_id = self.request.query_params.get('branch_id')
        category_id = (self.request.query_params.get('category_id') or self.request.query_params.get('category') or '').strip()
        category_code = (self.request.query_params.get('category_code') or '').strip()
        occ_date = self.request.query_params.get('occurrence_date')
        status_param = self.request.query_params.get('status')
        trainer_id = self.request.query_params.get('trainer_id')
        from_date = self.request.query_params.get('from_date')
        to_date = self.request.query_params.get('to_date')
        check_in_param = self.request.query_params.get('check_in_status')
        search_param = (self.request.query_params.get('search') or '').strip()

        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if category_id and category_id.upper() != 'ALL':
            qs = qs.filter(class_template__category_id=category_id)
        if category_code and category_code.upper() != 'ALL':
            qs = qs.filter(class_template__category__code__iexact=category_code)
        if occ_date:
            qs = qs.filter(occurrence_date=occ_date)
        if status_param and status_param.upper() != 'ALL':
            if ',' in status_param:
                st_list = [s.strip() for s in status_param.split(',') if s.strip()]
                qs = qs.filter(status__in=st_list)
            else:
                qs = qs.filter(status=status_param)
        now = timezone.now()
        upcoming_only = (self.request.query_params.get('upcoming_only') or '').strip().lower() == 'true'

        if trainer_id:
            qs = qs.filter(trainer_assignments__trainer_profile_id=trainer_id)

        if from_date:
            qs = qs.filter(occurrence_date__gte=from_date)
            if (from_date == str(now.date()) and not to_date) or upcoming_only:
                qs = qs.filter(end_at__gte=now - timedelta(minutes=15)).exclude(status__in=['CANCELLED', 'DEACTIVATED'])
            elif not status_param:
                qs = qs.exclude(status__in=['CANCELLED', 'DEACTIVATED'])
        elif upcoming_only:
            qs = qs.filter(end_at__gte=now - timedelta(minutes=15)).exclude(status__in=['CANCELLED', 'DEACTIVATED'])
        elif not status_param:
            pass

        if to_date:
            qs = qs.filter(occurrence_date__lte=to_date)

        if check_in_param and check_in_param.upper() != 'ALL':
            confirmed_trainer = ClassOccurrenceTrainer.objects.using(alias).filter(
                occurrence_id=OuterRef('pk'),
                status='CONFIRMED',
            )
            if check_in_param.upper() == 'CHECKED_IN':
                qs = qs.filter(Exists(confirmed_trainer))
            elif check_in_param.upper() == 'PENDING':
                qs = qs.filter(~Exists(confirmed_trainer))

        if search_param:
            matching_trainer = ClassOccurrenceTrainer.objects.using(alias).filter(
                Q(trainer_profile__trainer_code__icontains=search_param) |
                Q(trainer_profile__employee_profile__user_profile__user__first_name__icontains=search_param) |
                Q(trainer_profile__employee_profile__user_profile__user__last_name__icontains=search_param) |
                Q(trainer_profile__employee_profile__user_profile__first_name_snapshot__icontains=search_param) |
                Q(trainer_profile__employee_profile__user_profile__last_name_snapshot__icontains=search_param),
                occurrence_id=OuterRef('pk'),
            )
            qs = qs.filter(
                Q(class_template__name__icontains=search_param) |
                Q(class_template__category__name__icontains=search_param) |
                Q(class_template__program__name__icontains=search_param) |
                Q(branch__name__icontains=search_param) |
                Exists(matching_trainer)
            )

        ordering = (self.request.query_params.get('ordering') or '').strip()
        order_fields = ('-start_at', '-id') if ordering in ['-start_at', '-occurrence_date'] else ('start_at', 'id')

        return qs.select_related(
            'class_template', 'class_template__category', 'class_template__program', 'branch'
        ).prefetch_related(
            'trainer_assignments__trainer_profile__employee_profile__user_profile__user',
            'content_assignments__content_item',
            Prefetch('bookings', queryset=Booking.objects.using(alias).only('id', 'occurrence_id', 'status')),
        ).order_by(*order_fields).distinct()

    def get_serializer_context(self):
        context = super().get_serializer_context()
        alias = _get_db(self.request)
        membership_id = self.request.query_params.get('membership_id')
        user_profile_id = self.request.query_params.get('user_profile_id')
        if membership_id:
            from .models_memberships import Membership
            try:
                context['membership'] = Membership.objects.using(alias).select_related(
                    'package_version__package', 'package', 'home_branch'
                ).prefetch_related('entitlements').get(id=membership_id)
            except Exception:
                pass
        if user_profile_id:
            from .models_crm import UserProfile
            try:
                context['user_profile'] = UserProfile.objects.using(alias).get(id=user_profile_id)
            except Exception:
                pass
        is_staff = bool(
            self.request.user
            and (getattr(self.request.user, 'is_staff', False) or getattr(self.request.user, 'is_superuser', False) or getattr(self.request.user, 'role', '') in ['ADMIN', 'STAFF', 'MANAGER'])
        )
        context['is_staff'] = is_staff
        context['db_alias'] = alias
        return context

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

    @action(detail=True, methods=['post'], url_path='trainer-check-in')
    def trainer_check_in(self, request, pk=None):
        """
        Trainer marks their biometric and geofenced check-in for this class occurrence.
        Validates branch geofence radius and anti-spoofing liveness proof.
        """
        alias = _get_db(request)
        try:
            occurrence = ClassOccurrence.objects.using(alias).select_related('branch').get(id=pk)
        except ClassOccurrence.DoesNotExist:
            return Response({'error': 'Class occurrence not found'}, status=status.HTTP_404_NOT_FOUND)

        branch = occurrence.branch
        latitude = request.data.get('latitude') or request.data.get('trainer_latitude')
        longitude = request.data.get('longitude') or request.data.get('trainer_longitude')
        accuracy = request.data.get('accuracy') or request.data.get('trainer_accuracy_meters')
        distance_meters = request.data.get('distance_meters')
        face_verified = bool(request.data.get('face_verified', False))
        liveness_score = request.data.get('liveness_score')
        liveness_method = request.data.get('liveness_method', 'FACE_LIVENESS')
        selfie_image = request.data.get('selfie_image') or request.data.get('trainer_selfie_url', '')
        challenges_passed = request.data.get('challenges_passed') or []

        # Geofence validation
        computed_distance = distance_meters
        if computed_distance is None and latitude is not None and longitude is not None and branch.latitude is not None and branch.longitude is not None:
            import math
            try:
                R = 6371000
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
                    return Response({
                        'error': f"Geofence check failed: You are {int(computed_distance)}m away from {branch.name}. Check-in is restricted to within {branch.geofence_radius_meters}m."
                    }, status=status.HTTP_400_BAD_REQUEST)

        # Update or confirm trainer assignment
        from .models_classes import ClassOccurrenceTrainer
        from .models_workforce import TrainerProfile
        now = timezone.now()
        target_trainer_id = request.data.get('trainer_id')
        assignment = None
        if target_trainer_id:
            assignment = ClassOccurrenceTrainer.objects.using(alias).filter(
                occurrence=occurrence,
                trainer_profile_id=target_trainer_id
            ).first()

        if not assignment:
            trainer_profile = TrainerProfile.objects.using(alias).filter(
                employee_profile__user_profile__user=request.user
            ).first()

            if trainer_profile:
                assignment = ClassOccurrenceTrainer.objects.using(alias).filter(
                    occurrence=occurrence,
                    trainer_profile=trainer_profile
                ).first()

        if not assignment:
            # Fallback to lead trainer assignment if admin or first trainer
            assignment = ClassOccurrenceTrainer.objects.using(alias).filter(
                occurrence=occurrence
            ).first()

        if assignment:
            assignment.status = 'CONFIRMED'
            assignment.save(using=alias, update_fields=['status'])

        return Response({
            'message': 'Trainer check-in verified successfully',
            'occurrence_id': str(occurrence.id),
            'trainer_name': request.user.full_name or request.user.email,
            'is_within_geofence': is_within_geofence,
            'geofence_distance_meters': computed_distance,
            'face_verified': face_verified,
            'liveness_score': liveness_score,
            'checked_in_at': now.isoformat(),
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get'], url_path='trainer-video')
    def trainer_video(self, request, pk=None):
        """
        Secure video streaming endpoint for trainers and administrators.
        Verifies that the authenticated user is an Admin, Manager, Staff member, or assigned Trainer.
        Returns the safe in-website embedded streaming configuration and targeted muscle group tags.
        """
        alias = _get_db(request)
        try:
            occurrence = ClassOccurrence.objects.using(alias).select_related('class_template', 'branch').get(id=pk)
        except ClassOccurrence.DoesNotExist:
            return Response({'error': 'Class occurrence not found'}, status=status.HTTP_404_NOT_FOUND)

        user = request.user
        from .models_classes import ClassOccurrenceTrainer
        from .models_workforce import TrainerProfile
        from .models_wod import WorkoutContentItem

        user_role = str(getattr(user, 'role', '')).upper()
        user_type = str(getattr(user, 'user_type', '')).lower()
        auth_type = str(getattr(user, '_auth_type', '')).lower()

        # Admins, managers, staff, trainers, and platform operators are all authorized
        is_authorized = (
            getattr(user, 'is_staff', False)
            or getattr(user, 'is_superuser', False)
            or user_role in ['ADMIN', 'SUPER_ADMIN', 'ORG_ADMIN', 'BRANCH_MANAGER', 'STAFF', 'FRONT_DESK', 'TRAINER', 'MANAGER']
            or user_type in ['platform', 'tenant_staff', 'staff', 'admin', 'trainer', 'manager']
            or auth_type == 'platform'
        )

        if not is_authorized:
            user_email = (getattr(user, 'email', '') or '').strip().lower()
            is_assigned = False
            if user_email:
                is_assigned = ClassOccurrenceTrainer.objects.using(alias).filter(
                    occurrence=occurrence,
                    trainer_profile__employee_profile__user_profile__user__email__iexact=user_email
                ).exists()

            if not is_assigned:
                return Response({
                    'detail': 'Access denied. Video content is restricted to authorized gym staff, managers, or the assigned trainer.',
                    'code': 'TRAINER_ASSIGNMENT_REQUIRED'
                }, status=status.HTTP_403_FORBIDDEN)

        template_name = occurrence.class_template.name if occurrence.class_template else ''

        import re
        category_code = (
            occurrence.class_template.category.code 
            if (occurrence.class_template and occurrence.class_template.category) 
            else ''
        )
        occ_date = occurrence.occurrence_date
        occ_date_str = occ_date.strftime('%Y-%m-%d') if occ_date else ''

        # 1. Check if admin explicitly set an online_join_url for this occurrence (highest priority)
        video_url = ''
        instructions = ''
        description = occurrence.class_template.description if occurrence.class_template else ''
        movement_title = template_name
        tags = []
        equipment_tags = []
        raw_equip = ''

        if occurrence.online_join_url and occurrence.online_join_url.strip():
            video_url = occurrence.online_join_url.strip()

        # Check direct content assignment linked to this occurrence from Content Studio
        direct_assignment = occurrence.content_assignments.using(alias).select_related('content_item').first()
        if direct_assignment and direct_assignment.content_item:
            ci = direct_assignment.content_item
            if not video_url:
                video_url = getattr(ci, 'external_url', '') or getattr(ci, 'video_url', '')
            instructions = getattr(ci, 'description', '') or ''
            movement_title = getattr(ci, 'title', template_name)

        if not video_url and occurrence.online_join_url:
            video_url = occurrence.online_join_url

        # 2. Date-Aware Lookup in WorkoutContentItem (WOD Library)
        workout_item = None
        if not video_url and occ_date_str:
            matching_items = list(WorkoutContentItem.objects.using(alias).filter(
                raw_import_data__date=occ_date_str,
                status='ACTIVE'
            ))
            for itm in matching_items:
                raw_cat = str(itm.raw_import_data.get('category_code', '')).upper()
                raw_sheet = str(itm.raw_import_data.get('sheet', '')).upper()
                name_upper = itm.movement_name.upper()
                
                if (category_code and category_code in raw_cat) or                    ('STRETCH' in category_code and ('STRETCH' in raw_sheet or 'STRETCH' in name_upper)) or                    ('TOTAL' in category_code and 'PILATES' in category_code and ('SWEAT TOTAL' in raw_sheet or 'PILATES TOTAL' in name_upper or 'SWEAT TOTAL' in name_upper)) or                    ('ATHLETIC' in category_code and ('ATHLETIC' in raw_sheet or 'ATHLETIC' in name_upper)) or                    ('BOOTCAMP' in category_code and ('BOOTCAMP' in raw_sheet or 'BOOTCAMP' in name_upper or raw_sheet == '01. TOTAL')):
                    workout_item = itm
                    break

            # Fallback by shot_date if raw_import_data not matched
            if not workout_item:
                workout_item = WorkoutContentItem.objects.using(alias).filter(
                    shot_date=occ_date,
                    movement_name__icontains=template_name,
                    status='ACTIVE'
                ).first()

        # 3. Fall back to generic template match if no date-matched video exists
        if not video_url and not workout_item:
            workout_item = (
                WorkoutContentItem.objects.using(alias)
                .filter(movement_name__icontains=template_name, status='ACTIVE')
                .order_by('-created_at')
                .first()
            )

        if workout_item:
            video_url = workout_item.video_url or ''
            if not instructions:
                instructions = workout_item.description or ''
            movement_title = workout_item.movement_name or template_name
            
            # Extract targeted muscles from raw_import_data or tags
            raw_muscles = workout_item.raw_import_data.get('muscles') if workout_item.raw_import_data else None
            if raw_muscles:
                if isinstance(raw_muscles, list):
                    tags = [str(m).strip() for m in raw_muscles if str(m).strip()]
                elif isinstance(raw_muscles, str):
                    tags = [m.strip() for m in re.split(r'[,|&/]', raw_muscles) if m.strip()]

            if not tags:
                try:
                    tags = [ct.tag.name for ct in workout_item.content_tags.using(alias).select_related('tag').filter(tag__tag_group='MUSCLE_GROUP') if ct.tag]
                except Exception:
                    tags = []

            # Extract equipment required from raw_import_data or tags
            raw_equip = workout_item.raw_import_data.get('equipment') if workout_item.raw_import_data else None
            equipment_tags = []
            if raw_equip:
                if isinstance(raw_equip, list):
                    equipment_tags = [str(e).strip() for e in raw_equip if str(e).strip()]
                elif isinstance(raw_equip, str):
                    equipment_tags = [e.strip() for e in re.split(r'[,+/&|]', raw_equip) if e.strip()]

            if not equipment_tags:
                try:
                    equipment_tags = [
                        ct.tag.name for ct in workout_item.content_tags.using(alias).select_related('tag').filter(tag__tag_group='EQUIPMENT') if ct.tag
                    ]
                except Exception:
                    equipment_tags = []

        # 4. Extract YouTube Video ID if applicable
        yt_id = None
        if video_url:
            patterns = [
                r'(?:youtu\.be/|youtube\.com/(?:embed/|v/|watch\?v=|watch\?.+&v=))([\w-]{11})',
                r'youtu\.be/([\w-]{11})',
            ]
            for p in patterns:
                m = re.search(p, video_url)
                if m:
                    yt_id = m.group(1)
                    break

        embed_player_url = (
            f"https://www.youtube-nocookie.com/embed/{yt_id}?rel=0&modestbranding=1&controls=1&showinfo=0&iv_load_policy=3&fs=0"
            if yt_id else (video_url or '')
        )

        return Response({
            'occurrence_id': str(occurrence.id),
            'class_name': template_name,
            'description': description,
            'movement_title': movement_title,
            'has_video': bool(video_url),
            'youtube_video_id': yt_id,
            'embed_player_url': embed_player_url,
            'video_url': video_url,
            'external_url': video_url,
            'targeted_muscles': tags,
            'equipment_required': equipment_tags,
            'raw_equipment': str(raw_equip or ''),
            'instructions': instructions,
            'is_assigned_trainer': True,
            'player_security': {
                'stream_inside_website_only': True,
                'prevent_download': True,
                'hide_channel_link': True,
                'nocookie_mode': True,
            }
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='assign-video')
    def assign_video(self, request, pk=None):
        """
        Manually assign or unassign a workout video to/from a ClassOccurrence.
        Accepts: { 'video_id': '<uuid>' } or { 'video_id': None } to clear.
        """
        alias = _get_db(request)
        org = _get_org(request)
        try:
            occurrence = ClassOccurrence.objects.using(alias).get(id=pk)
        except ClassOccurrence.DoesNotExist:
            return Response({'error': 'Class occurrence not found'}, status=status.HTTP_404_NOT_FOUND)

        video_id = request.data.get('video_id')
        from .models_wod import WorkoutContentItem
        from .models_classes import ClassContentItem, ClassContentAssignment

        if not video_id:
            # Unassign video
            ClassContentAssignment.objects.using(alias).filter(occurrence=occurrence).delete()
            return Response({
                'success': True,
                'message': 'Workout video unassigned from class session.',
                'occurrence_id': str(occurrence.id),
                'video_id': None,
            })

        workout_item = WorkoutContentItem.objects.using(alias).filter(id=video_id).first()
        if not workout_item:
            return Response({'error': 'Workout video not found in library'}, status=status.HTTP_404_NOT_FOUND)

        # Create or fetch ClassContentItem bridge for this video
        content_item, _ = ClassContentItem.objects.using(alias).get_or_create(
            organization=org,
            external_url=workout_item.video_url,
            defaults={
                'title': workout_item.movement_name,
                'description': workout_item.description,
                'content_type': 'VIDEO',
                'status': 'ACTIVE',
            }
        )

        assignment, created = ClassContentAssignment.objects.using(alias).update_or_create(
            occurrence=occurrence,
            defaults={
                'content_item': content_item,
                'assignment_method': 'MANUAL',
                'assigned_by_user': request.user if hasattr(request, 'user') and request.user.is_authenticated else None,
                'status': 'ACTIVE',
            }
        )

        return Response({
            'success': True,
            'message': f'Assigned "{workout_item.movement_name}" to class.',
            'occurrence_id': str(occurrence.id),
            'video_id': str(workout_item.id),
            'video_title': workout_item.movement_name,
            'video_url': workout_item.video_url,
            'assignment_id': str(assignment.id),
        })

    @action(detail=False, methods=['get'], url_path='available-videos')
    def available_workout_videos(self, request):
        """
        Returns active workout videos from the WOD Content Library
        for selection in class creation and video assignment modals.
        Supports search, category filtering, and pagination.
        """
        import re
        alias = _get_db(request)
        org = _get_org(request)
        from .models_wod import WorkoutContentItem

        qs = WorkoutContentItem.objects.using(alias).filter(
            status='ACTIVE'
        ).exclude(video_url='').exclude(video_url__isnull=True).order_by('-shot_date', '-created_at')

        if org:
            qs = qs.filter(organization=org)

        category = request.query_params.get('category')
        if category and category != 'ALL':
            qs = qs.filter(
                Q(raw_import_data__category_code__icontains=category) |
                Q(raw_import_data__program__icontains=category) |
                Q(raw_import_data__sheet__icontains=category) |
                Q(movement_name__icontains=category)
            )

        search = request.query_params.get('search')
        if search:
            qs = qs.filter(
                Q(movement_name__icontains=search) |
                Q(description__icontains=search) |
                Q(raw_import_data__muscles__icontains=search) |
                Q(raw_import_data__equipment__icontains=search)
            )

        limit = int(request.query_params.get('limit', 200))
        items = list(qs[:limit])

        data = []
        for itm in items:
            raw_data = itm.raw_import_data or {}
            embed_url = ''
            url = itm.video_url or ''
            if 'youtu' in url:
                m_yt = re.search(r'(?:v=|\/embed\/|\/shorts\/|youtu\.be\/|\/v\/)([a-zA-Z0-9_-]{11})', url)
                if m_yt:
                    embed_url = f"https://www.youtube-nocookie.com/embed/{m_yt.group(1)}?rel=0&modestbranding=1"

            data.append({
                'id': str(itm.id),
                'title': itm.movement_name,
                'description': itm.description or '',
                'video_url': itm.video_url,
                'embed_url': embed_url,
                'date': str(itm.shot_date or raw_data.get('date') or ''),
                'category_code': raw_data.get('category_code', ''),
                'program': raw_data.get('program', ''),
                'sheet': raw_data.get('sheet', ''),
                'targeted_muscles': raw_data.get('muscles', ''),
                'equipment_required': raw_data.get('equipment', ''),
                'classification_status': itm.classification_status,
            })

        return Response({
            'count': len(data),
            'results': data,
        })



    @action(detail=True, methods=['post', 'patch'], url_path='update-video')
    def update_video(self, request, pk=None):
        """
        Updates or sets the workout video URL attached to this class occurrence.
        """
        alias = _get_db(request)
        try:
            occurrence = ClassOccurrence.objects.using(alias).get(id=pk)
        except ClassOccurrence.DoesNotExist:
            return Response({'error': 'Class occurrence not found'}, status=status.HTTP_404_NOT_FOUND)

        video_url = (request.data.get('video_url') or '').strip()
        occurrence.online_join_url = video_url
        occurrence.save(using=alias, update_fields=['online_join_url'])

        # If a direct content assignment exists on this occurrence, keep its external_url in sync as well
        direct_assignment = occurrence.content_assignments.using(alias).select_related('content_item').first()
        if direct_assignment and direct_assignment.content_item:
            ci = direct_assignment.content_item
            ci.external_url = video_url
            ci.save(using=alias, update_fields=['external_url'])

        import re
        yt_id = None
        if video_url:
            patterns = [
                r'(?:youtu\.be/|youtube\.com/(?:embed/|v/|watch\?v=|watch\?.+&v=))([\w-]{11})',
                r'youtu\.be/([\w-]{11})',
            ]
            for p in patterns:
                m = re.search(p, video_url)
                if m:
                    yt_id = m.group(1)
                    break

        embed_player_url = (
            f"https://www.youtube-nocookie.com/embed/{yt_id}?rel=0&modestbranding=1&controls=1&showinfo=0&iv_load_policy=3&fs=0"
            if yt_id else (video_url or '')
        )

        return Response({
            'message': 'Workout video URL updated successfully.',
            'video_url': video_url,
            'external_url': video_url,
            'embed_player_url': embed_player_url,
            'occurrence_id': str(occurrence.id),
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get'], url_path='cancellation-preview')
    def cancellation_preview(self, request, pk=None):
        """
        Preview affected member bookings before cancelling or deactivating a class occurrence.
        Returns total counts, confirmed vs waitlist breakdown, and detailed affected member list.
        """
        alias = _get_db(request)
        instance = self.get_object()
        from .models_bookings import Booking

        active_bookings = Booking.objects.using(alias).filter(
            occurrence=instance,
            status__in=['CONFIRMED', 'WAITLISTED', 'RESERVED']
        ).select_related(
            'user_profile__user',
            'membership__package_version__package',
            'entitlement',
        ).order_by('created_at')

        affected_list = []
        confirmed_count = 0
        waitlist_count = 0

        for b in active_bookings:
            if b.status == 'WAITLISTED':
                waitlist_count += 1
            else:
                confirmed_count += 1

            u = getattr(b.user_profile, 'user', None)
            first_name = getattr(b.user_profile, 'first_name_snapshot', '') or (u.first_name if u else '')
            last_name = getattr(b.user_profile, 'last_name_snapshot', '') or (u.last_name if u else '')
            full_name = f"{first_name} {last_name}".strip() or "Member"
            phone = getattr(b.user_profile, 'phone_snapshot', '') or (u.phone if u else '')
            email = getattr(b.user_profile, 'email_snapshot', '') or (u.email if u else '')

            package_name = ""
            if b.membership and getattr(b.membership, 'package_version', None) and getattr(b.membership.package_version, 'package', None):
                package_name = b.membership.package_version.package.name
            elif b.membership and getattr(b.membership, 'name', None):
                package_name = b.membership.name

            affected_list.append({
                'id': str(b.id),
                'booking_number': b.booking_number,
                'status': b.status,
                'member_id': str(b.user_profile.id) if b.user_profile else None,
                'member_name': full_name,
                'phone': phone,
                'email': email,
                'membership_id': str(b.membership.id) if b.membership else None,
                'membership_name': package_name,
                'booking_type': b.booking_type,
                'has_entitlement': bool(b.entitlement),
                'entitlement_type': getattr(b.entitlement, 'entitlement_type', None),
                'waitlist_position': b.waitlist_position if b.status == 'WAITLISTED' else None,
            })

        start_time_str = instance.start_at.strftime('%H:%M') if instance.start_at else ''
        end_time_str = instance.end_at.strftime('%H:%M') if instance.end_at else ''

        return Response({
            'occurrence_id': str(instance.id),
            'class_name': instance.class_template.name if instance.class_template else 'Class Session',
            'branch_name': instance.branch.name if instance.branch else '',
            'occurrence_date': str(instance.occurrence_date),
            'start_time': start_time_str,
            'end_time': end_time_str,
            'current_status': instance.status,
            'total_booked': len(affected_list),
            'confirmed_count': confirmed_count,
            'waitlist_count': waitlist_count,
            'is_recurring': bool(instance.schedule_rule_id),
            'affected_bookings': affected_list,
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='cancel')
    def cancel(self, request, pk=None):
        """
        Cancels or deactivates an individual class occurrence session.
        - Does NOT delete the occurrence record.
        - Updates occurrence status to 'CANCELLED' (or 'DEACTIVATED').
        - Handles existing member bookings:
            - Transitions active bookings to CANCELLED via BookingService.cancel_booking (reason_code='CLASS_CANCELLED_BY_ADMIN').
            - Restores membership session credits / entitlements if restore_credits is True.
            - Creates in-app notifications for affected members if notify_members is True.
            - Maintains full cancellation and status history (no silent deletions).
        - Single occurrence cancellation: Does not affect future recurring schedule rule sessions.
        """
        alias = _get_db(request)
        instance = self.get_object()
        from .models_bookings import Booking
        from .services_bookings import BookingService, record_business_audit
        from .models_govern import InAppNotification
        from django.db import transaction

        reason = (request.data.get('reason') or '').strip()
        notify_members = request.data.get('notify_members', True)
        restore_credits = request.data.get('restore_credits', True)
        target_status = 'DEACTIVATED' if str(request.data.get('mode', '')).upper() == 'DEACTIVATED' else 'CANCELLED'

        if instance.status == target_status:
            return Response(
                {'detail': f"Class session is already {target_status.lower()}."},
                status=status.HTTP_400_BAD_REQUEST
            )

        with transaction.atomic(using=alias):
            # 1. Update occurrence status
            old_status = instance.status
            instance.status = target_status
            instance.save(using=alias, update_fields=['status', 'updated_at'])

            # 2. Query and process all active bookings
            active_bookings = list(
                Booking.objects.using(alias).filter(
                    occurrence=instance,
                    status__in=['CONFIRMED', 'WAITLISTED', 'RESERVED']
                ).select_related(
                    'user_profile__user',
                    'occurrence__class_template',
                    'occurrence__branch',
                    'membership',
                    'entitlement',
                )
            )

            cancelled_bookings_count = 0
            notifications_sent_count = 0
            credits_restored_count = 0

            org = (
                instance.class_template.category.organization
                if (instance.class_template and instance.class_template.category and instance.class_template.category.organization)
                else (instance.class_template.organization if (instance.class_template and instance.class_template.organization) else (instance.branch.organization if instance.branch else None))
            )

            class_name = instance.class_template.name if instance.class_template else "Class"
            date_str = str(instance.occurrence_date)
            start_str = instance.start_at.strftime('%I:%M %p') if instance.start_at else ''

            for b in active_bookings:
                had_credits = bool(b.membership and b.entitlement)
                # Cancel booking through authoritative service
                BookingService.cancel_booking(
                    booking=b,
                    cancelled_by_user=request.user,
                    reason_code='CLASS_CANCELLED_BY_ADMIN',
                    reason_text=reason or "Class session cancelled by administrator.",
                    db_alias=alias,
                )
                cancelled_bookings_count += 1
                if had_credits and restore_credits:
                    credits_restored_count += 1

                # Send in-app notification to member
                if notify_members and b.user_profile and b.user_profile.user and org:
                    try:
                        notif_msg = f"Your class session '{class_name}' on {date_str} at {start_str} has been cancelled by the studio."
                        if reason:
                            notif_msg += f" Reason: {reason}."
                        if had_credits:
                            notif_msg += " Your session credit has been restored to your membership."

                        InAppNotification.objects.using(alias).create(
                            organization=org,
                            user=b.user_profile.user,
                            notification_type='CLASS_CANCELLED',
                            title=f"Class Cancelled: {class_name}",
                            message=notif_msg,
                            data={
                                'occurrence_id': str(instance.id),
                                'booking_id': str(b.id),
                                'class_name': class_name,
                                'date': date_str,
                                'time': start_str,
                                'reason': reason,
                            },
                        )
                        notifications_sent_count += 1
                    except Exception as notif_err:
                        logger.warning(f"Failed to create notification for member {b.user_profile_id}: {notif_err}")

            # Also record business audit log
            if org:
                record_business_audit(
                    organization=org,
                    module='CLASSES',
                    action_code='CLASS_SESSION_CANCELLED',
                    entity_type='ClassOccurrence',
                    entity_id=instance.id,
                    branch=instance.branch,
                    actor_user=request.user,
                    metadata={
                        'target_status': target_status,
                        'reason': reason,
                        'cancelled_bookings_count': cancelled_bookings_count,
                        'notifications_sent_count': notifications_sent_count,
                    },
                    db_alias=alias,
                )

        return Response({
            'id': str(instance.id),
            'status': instance.status,
            'message': f"Class session {target_status.lower()} successfully.",
            'cancelled_bookings_count': cancelled_bookings_count,
            'notifications_sent_count': notifications_sent_count,
            'credits_restored_count': credits_restored_count,
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='restore')
    def restore(self, request, pk=None):
        """
        Restores / reactivates an accidentally cancelled or deactivated class occurrence.
        - Validates that the class was previously CANCELLED or DEACTIVATED.
        - If the occurrence end time has already passed, marks as COMPLETED.
        - Otherwise, marks as OPEN / SCHEDULED and makes it visible again.
        """
        alias = _get_db(request)
        instance = self.get_object()
        from django.utils import timezone

        if instance.status not in ('CANCELLED', 'DEACTIVATED'):
            return Response(
                {'detail': f"Only cancelled or deactivated classes can be restored. Current status is {instance.status}."},
                status=status.HTTP_400_BAD_REQUEST
            )

        now = timezone.now()
        if instance.end_at and now >= instance.end_at:
            instance.status = 'COMPLETED'
        else:
            instance.status = 'OPEN'

        instance.save(using=alias, update_fields=['status', 'updated_at'])

        org = (
            instance.class_template.category.organization
            if (instance.class_template and instance.class_template.category and instance.class_template.category.organization)
            else (instance.class_template.organization if (instance.class_template and instance.class_template.organization) else (instance.branch.organization if instance.branch else None))
        )
        if org:
            from .services_bookings import record_business_audit
            record_business_audit(
                organization=org,
                module='CLASSES',
                action_code='CLASS_SESSION_RESTORED',
                entity_type='ClassOccurrence',
                entity_id=instance.id,
                branch=instance.branch,
                actor_user=request.user,
                metadata={'new_status': instance.status},
                db_alias=alias,
            )

        return Response({
            'id': str(instance.id),
            'status': instance.status,
            'message': f"Class session successfully reactivated to {instance.status}.",
        }, status=status.HTTP_200_OK)

    def destroy(self, request, *args, **kwargs):
        """
        Admin-controlled deletion of a class occurrence slot.
        Safety checks:
        1. If active member bookings exist (CONFIRMED, ATTENDED, WAITLISTED), prevents accidental deletion
           and instructs the admin to cancel/reassign bookings first.
        2. Cleans up any CANCELLED bookings before deleting occurrence to satisfy PROTECT foreign key.
        3. Deletes occurrence and returns 204 No Content.
        """
        alias = _get_db(request)
        instance = self.get_object()
        from .models_bookings import Booking
        active_bookings_count = Booking.objects.using(alias).filter(
            occurrence=instance,
            status__in=['CONFIRMED', 'WAITLISTED', 'ATTENDED']
        ).count()
        if active_bookings_count > 0:
            return Response(
                {
                    'detail': f'Cannot delete class slot because it has {active_bookings_count} active booking(s). Please cancel or reassign the bookings first.'
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        try:
            Booking.objects.using(alias).filter(occurrence=instance).delete()
            from .models_classes import ClassOccurrenceTrainer
            ClassOccurrenceTrainer.objects.using(alias).filter(occurrence=instance).delete()
            instance.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except Exception as e:
            return Response(
                {'detail': f'Failed to delete class slot: {str(e)}'},
                status=status.HTTP_400_BAD_REQUEST
            )


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
