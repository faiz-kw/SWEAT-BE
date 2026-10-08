"""
Workout of the Day (WOD) Phase 1 — DRF ViewSets & RBAC Enforcement
Strictly accessible ONLY to Organization Admin (via Layer 1 RBAC + WOD_* permissions).
"""

import json
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Count, Prefetch, Q
from django.http import FileResponse, Http404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .context import get_tenant_db_alias
from .models_org import Organization
from .models_rbac import (
    ModuleCatalog,
    Permission,
    Role,
    RoleModuleAccess,
    RolePermissionSet,
    RolePermissionSetItem,
    RoleSubmoduleAccess,
    SubmoduleCatalog,
)
from .models_wod import WorkoutContentItem, WorkoutContentTag, WorkoutTag
from .permissions import RequireActiveTenantAndOrg, TenantRBACPermission
from .serializers_wod import WorkoutContentItemSerializer, WorkoutTagSerializer
from .services_reliability import record_business_audit
from .services_wod import (
    WOD_PERMISSIONS_SPEC,
    WODClassificationService,
    WODImportService,
    WODTagService,
    WODVideoService,
)

logger = logging.getLogger(__name__)


def _get_db(request) -> str:
    return (
        get_tenant_db_alias()
        or getattr(getattr(request, 'user', None), '_db_alias', None)
        or getattr(request, '_tenant_db_alias', None)
        or 'default'
    )


def _get_org(request) -> Optional[Organization]:
    org = getattr(request, 'organization', None)
    if org:
        return org
    user = getattr(request, 'user', None)
    alias = _get_db(request)
    if user and getattr(user, 'organization_id', None):
        found = Organization.objects.using(alias).filter(id=user.organization_id).first()
        if found:
            return found
    return Organization.objects.using(alias).filter(status='ACTIVE').first()


def ensure_wod_rbac_setup(request) -> None:
    """
    Idempotently seeds WOD permissions and submodule under 'ops' in the tenant DB,
    granting them ONLY to the tenant's ORG_ADMIN role by default.
    """
    alias = _get_db(request)
    org = _get_org(request)
    if not org:
        return

    # Request-level memoization so we only check once per request
    if getattr(request, '_wod_rbac_ensured', False):
        return
    request._wod_rbac_ensured = True

    try:
        # Ensure master TenantModule for 'ops' is enabled if tenant context exists
        user = getattr(request, 'user', None)
        tenant_id = getattr(user, '_tenant_id', None)
        if tenant_id:
            try:
                from apps.master.models_saas import ProductModule, TenantModule
                prod_mod = ProductModule.objects.using('default').filter(code__iexact='ops').first()
                if prod_mod:
                    TenantModule.objects.using('default').get_or_create(
                        tenant_id=tenant_id,
                        module=prod_mod,
                        defaults={'is_enabled': True, 'availability_mode': 'ALL_BRANCHES'},
                    )
            except Exception:
                pass

        module = (
            ModuleCatalog.objects.using(alias).filter(module_code__iexact='ops').first()
            or ModuleCatalog.objects.using(alias).filter(code__iexact='ops').first()
        )
        if not module:
            module = ModuleCatalog.objects.using(alias).create(
                code='ops',
                module_code='ops',
                name='Studio Operations & Scheduling',
                is_enabled=True,
                status='ACTIVE',
            )

        submodule = (
            SubmoduleCatalog.objects.using(alias)
            .filter(module=module, submodule_code__iexact='wod_content_library')
            .first()
        )
        if not submodule:
            submodule = SubmoduleCatalog.objects.using(alias).create(
                module=module,
                code='wod_content_library',
                submodule_code='wod_content_library',
                name='WOD Content Library',
                description='Admin-only Workout of the Day Content Library & Tag Master',
                is_enabled=True,
                status='ACTIVE',
            )

        perms_list: List[Permission] = []
        for perm_code, act, label, desc in WOD_PERMISSIONS_SPEC:
            perm = (
                Permission.objects.using(alias).filter(permission_code__iexact=perm_code).first()
                or Permission.objects.using(alias).filter(code__iexact=perm_code).first()
            )
            if not perm:
                perm = Permission(
                    module=module,
                    submodule=submodule,
                    code=perm_code,
                    permission_code=perm_code,
                    action=act,
                    label=label,
                    description=desc,
                    status='ACTIVE',
                    is_active=True,
                )
                perm.save(using=alias)
            perms_list.append(perm)

        # Grant ONLY to ORG_ADMIN (system admin role) if not yet configured
        admin_roles = list(
            Role.objects.using(alias).filter(
                organization=org,
                code__in=['ORG_ADMIN', 'TENANT_ADMIN'],
                is_active=True,
            )
        )
        for admin_role in admin_roles:
            RoleModuleAccess.objects.using(alias).get_or_create(
                role=admin_role,
                module=module,
                defaults={'can_access': True},
            )
            RoleSubmoduleAccess.objects.using(alias).get_or_create(
                role=admin_role,
                submodule=submodule,
                defaults={'can_access': True},
            )
            perm_set = (
                RolePermissionSet.objects.using(alias)
                .filter(role=admin_role, is_active=True)
                .first()
            )
            if not perm_set:
                perm_set = RolePermissionSet.objects.using(alias).create(
                    role=admin_role,
                    organization=org,
                    scope_type='ORGANIZATION',
                    name=f"{admin_role.name} Default Permission Set",
                    status='ACTIVE',
                    is_active=True,
                )
            for perm in perms_list:
                RolePermissionSetItem.objects.using(alias).get_or_create(
                    permission_set=perm_set,
                    permission=perm,
                    defaults={'granted': True},
                )
    except Exception as exc:
        logger.warning("ensure_wod_rbac_setup warning: %s", exc)


class WODRBACPermission(TenantRBACPermission):
    """
    Uses Layer 1 TenantRBACPermission + RBACAuthorizationEngine after ensuring
    WOD permissions exist and are granted to ORG_ADMIN.
    Fails closed (HTTP 403) for all roles that do not hold the required WOD_* permission.
    """

    def has_permission(self, request, view):
        ensure_wod_rbac_setup(request)
        return super().has_permission(request, view)

    def has_object_permission(self, request, view, obj):
        ensure_wod_rbac_setup(request)
        if hasattr(obj, 'organization_id') and obj.organization_id:
            org = _get_org(request)
            if org and str(obj.organization_id) != str(org.id):
                raise PermissionDenied("Cross-organization access is denied.")
        return super().has_object_permission(request, view, obj)


class WODPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 500

    def get_paginated_response(self, data):
        summary = getattr(self, '_wod_summary', None) or {}
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


class WorkoutTagViewSet(viewsets.ModelViewSet):
    """
    CRUD and activation/deactivation for configurable workout tags (`workout_tags`).
    """
    serializer_class = WorkoutTagSerializer
    permission_classes = [RequireActiveTenantAndOrg, WODRBACPermission]
    required_module = 'ops'
    required_submodule = 'wod_content_library'
    required_permission = 'WOD_CONTENT_LIBRARY_VIEW'
    action_permission_map = {
        'list': 'WOD_CONTENT_LIBRARY_VIEW',
        'retrieve': 'WOD_CONTENT_LIBRARY_VIEW',
        'create': 'WOD_TAG_MANAGE',
        'update': 'WOD_TAG_MANAGE',
        'partial_update': 'WOD_TAG_MANAGE',
        'destroy': 'WOD_TAG_MANAGE',
        'activate': 'WOD_TAG_MANAGE',
        'deactivate': 'WOD_TAG_MANAGE',
    }
    action_alternative_permissions = {
        'list': ['WOD_TAG_MANAGE'],
        'retrieve': ['WOD_TAG_MANAGE'],
    }
    pagination_class = None

    def get_queryset(self):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        if not org:
            return WorkoutTag.objects.using(alias).none()

        WODTagService.ensure_initial_tags(org, db_alias=alias)

        qs = (
            WorkoutTag.objects.using(alias)
            .filter(organization=org)
            .annotate(_usage_count=Count('content_tags'))
            .order_by('tag_group', 'display_order', 'name')
        )

        tag_group = self.request.query_params.get('tag_group')
        if tag_group:
            groups = [g.strip().upper() for g in tag_group.split(',') if g.strip()]
            qs = qs.filter(tag_group__in=groups)

        status_param = self.request.query_params.get('status')
        if status_param:
            qs = qs.filter(status__iexact=status_param.strip())

        search = self.request.query_params.get('search')
        if search and search.strip():
            q = search.strip()
            qs = qs.filter(Q(name__icontains=q) | Q(code__icontains=q) | Q(description__icontains=q))

        return qs

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['db_alias'] = _get_db(self.request)
        ctx['organization'] = _get_org(self.request)
        return ctx

    def perform_create(self, serializer):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        instance = WorkoutTag(
            organization=org,
            **serializer.validated_data,
        )
        instance.save(using=alias)
        serializer.instance = instance

    def perform_update(self, serializer):
        alias = _get_db(self.request)
        instance = serializer.instance
        for k, v in serializer.validated_data.items():
            setattr(instance, k, v)
        instance.save(using=alias)

    @action(detail=True, methods=['post'], url_path='activate')
    def activate(self, request, pk=None):
        alias = _get_db(request)
        tag = self.get_object()
        tag.status = 'ACTIVE'
        tag.save(using=alias, update_fields=['status', 'updated_at'])
        return Response(self.get_serializer(tag).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='deactivate')
    def deactivate(self, request, pk=None):
        alias = _get_db(request)
        tag = self.get_object()
        tag.status = 'INACTIVE'
        tag.save(using=alias, update_fields=['status', 'updated_at'])

        # Re-evaluate any content items using this tag in case deactivating it drops a required group
        affected_item_ids = list(
            WorkoutContentTag.objects.using(alias)
            .filter(tag=tag)
            .values_list('content_item_id', flat=True)
        )
        for item in WorkoutContentItem.objects.using(alias).filter(id__in=affected_item_ids):
            WODClassificationService.evaluate_item(item, db_alias=alias, save=True)

        return Response(self.get_serializer(tag).data, status=status.HTTP_200_OK)


class WorkoutContentItemViewSet(viewsets.ModelViewSet):
    """
    CRUD, filtering, video upload, automatic video download from links, WOD eligibility management,
    and Excel import preview/confirm for `workout_content_items`.
    """
    serializer_class = WorkoutContentItemSerializer
    permission_classes = [RequireActiveTenantAndOrg, WODRBACPermission]
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    pagination_class = WODPagination
    required_module = 'ops'
    required_submodule = 'wod_content_library'
    required_permission = 'WOD_CONTENT_LIBRARY_VIEW'
    action_permission_map = {
        'list': 'WOD_CONTENT_LIBRARY_VIEW',
        'retrieve': 'WOD_CONTENT_LIBRARY_VIEW',
        'stream_video': 'WOD_CONTENT_LIBRARY_VIEW',
        'create': 'WOD_CONTENT_LIBRARY_CREATE',
        'update': 'WOD_CONTENT_LIBRARY_EDIT',
        'partial_update': 'WOD_CONTENT_LIBRARY_EDIT',
        'destroy': 'WOD_CONTENT_LIBRARY_EDIT',
        'upload_video': 'WOD_CONTENT_LIBRARY_EDIT',
        'download_video': 'WOD_CONTENT_LIBRARY_EDIT',
        'download_pending_videos': 'WOD_CONTENT_LIBRARY_EDIT',
        'activate': 'WOD_CONTENT_LIBRARY_ACTIVATE',
        'deactivate': 'WOD_CONTENT_LIBRARY_ACTIVATE',
        'set_wod_eligibility': 'WOD_CONTENT_LIBRARY_ACTIVATE',
        'import_preview': 'WOD_CONTENT_LIBRARY_IMPORT',
        'import_confirm': 'WOD_CONTENT_LIBRARY_IMPORT',
    }

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['db_alias'] = _get_db(self.request)
        ctx['organization'] = _get_org(self.request)
        return ctx

    def _apply_tag_filter(self, qs, group: Optional[str], param_val: Optional[str]):
        if not param_val or not param_val.strip():
            return qs
        values = [v.strip() for v in param_val.split(',') if v.strip()]
        if not values:
            return qs

        uuid_vals = []
        text_vals = []
        for v in values:
            try:
                uuid_vals.append(str(uuid.UUID(v)))
            except ValueError:
                text_vals.append(v)

        tag_q = Q()
        if uuid_vals:
            tag_q |= Q(content_tags__tag_id__in=uuid_vals)
        if text_vals:
            text_q = Q()
            for tv in text_vals:
                text_q |= Q(content_tags__tag__code__iexact=tv) | Q(content_tags__tag__name__iexact=tv)
            tag_q |= text_q

        if group:
            if group == 'MUSCLE_GROUP':
                tag_q &= Q(content_tags__tag__tag_group__in=['MUSCLE_GROUP', 'BODY_TARGET'])
            else:
                tag_q &= Q(content_tags__tag__tag_group=group)

        return qs.filter(tag_q)

    def get_queryset(self):
        alias = _get_db(self.request)
        org = _get_org(self.request)
        if not org:
            return WorkoutContentItem.objects.using(alias).none()

        WODTagService.ensure_initial_tags(org, db_alias=alias)

        base_qs = WorkoutContentItem.objects.using(alias).filter(organization=org)

        # Compute summary counts across the organization's library
        if self.paginator is not None:
            total_count = base_qs.count()
            active_count = base_qs.filter(status='ACTIVE').count()
            wod_eligible_count = base_qs.filter(is_wod_eligible=True).count()
            complete_count = base_qs.filter(classification_status='COMPLETE').count()
            incomplete_count = base_qs.filter(classification_status='INCOMPLETE').count()
            needs_review_count = base_qs.filter(classification_status='NEEDS_REVIEW').count()
            setup_instruction_count = base_qs.exclude(content_kind='MOVEMENT').count()
            missing_video_count = base_qs.filter(
                (Q(video_url__isnull=True) | Q(video_url=''))
                & (Q(video_storage_path__isnull=True) | Q(video_storage_path=''))
                & Q(video_file__isnull=True)
            ).count()
            video_downloaded_count = base_qs.filter(video_download_status='COMPLETED').count()
            video_failed_count = base_qs.filter(video_download_status='FAILED').count()

            self.paginator._wod_summary = {
                'total_count': total_count,
                'active_count': active_count,
                'wod_eligible_count': wod_eligible_count,
                'complete_count': complete_count,
                'incomplete_count': incomplete_count,
                'needs_review_count': needs_review_count,
                'setup_instruction_count': setup_instruction_count,
                'missing_video_count': missing_video_count,
                'video_downloaded_count': video_downloaded_count,
                'video_failed_count': video_failed_count,
            }

        qs = base_qs.prefetch_related(
            Prefetch(
                'content_tags',
                queryset=WorkoutContentTag.objects.using(alias).select_related('tag').order_by('tag__tag_group', 'tag__display_order', 'tag__name'),
            )
        )

        params = self.request.query_params

        search = params.get('search')
        if search and search.strip():
            q = search.strip()
            qs = qs.filter(
                Q(movement_name__icontains=q)
                | Q(description__icontains=q)
                | Q(feedback__icontains=q)
                | Q(cue_1__icontains=q)
                | Q(cue_2__icontains=q)
                | Q(cue_3__icontains=q)
                | Q(folder_number__icontains=q)
                | Q(shot_by__icontains=q)
                | Q(editor__icontains=q)
            )

        content_kind = params.get('content_kind')
        if content_kind and content_kind.strip():
            kinds = [k.strip().upper() for k in content_kind.split(',') if k.strip()]
            qs = qs.filter(content_kind__in=kinds)

        status_param = params.get('status')
        if status_param and status_param.strip():
            statuses = [s.strip().upper() for s in status_param.split(',') if s.strip()]
            qs = qs.filter(status__in=statuses)

        classification_status = params.get('classification_status')
        if classification_status and classification_status.strip():
            c_statuses = [c.strip().upper() for c in classification_status.split(',') if c.strip()]
            qs = qs.filter(classification_status__in=c_statuses)

        is_wod_eligible = params.get('is_wod_eligible')
        if is_wod_eligible is not None and is_wod_eligible != '':
            val = str(is_wod_eligible).strip().lower() in ('true', '1', 'yes')
            qs = qs.filter(is_wod_eligible=val)

        missing_video = params.get('missing_video')
        if missing_video is not None and missing_video != '':
            want_missing = str(missing_video).strip().lower() in ('true', '1', 'yes')
            no_video_q = (
                (Q(video_url__isnull=True) | Q(video_url=''))
                & (Q(video_storage_path__isnull=True) | Q(video_storage_path=''))
                & Q(video_file__isnull=True)
            )
            qs = qs.filter(no_video_q if want_missing else ~no_video_q)

        video_download_status = params.get('video_download_status')
        if video_download_status and video_download_status.strip():
            v_statuses = [v.strip().upper() for v in video_download_status.split(',') if v.strip()]
            qs = qs.filter(video_download_status__in=v_statuses)

        # Tag filters by group or generic tag_id
        qs = self._apply_tag_filter(qs, 'PROGRAM', params.get('program') or params.get('program_tag'))
        qs = self._apply_tag_filter(qs, 'SECTION', params.get('section') or params.get('section_tag'))
        qs = self._apply_tag_filter(qs, 'USER_LEVEL', params.get('user_level') or params.get('level_tag'))
        qs = self._apply_tag_filter(qs, 'INTENSITY', params.get('intensity') or params.get('intensity_tag'))
        qs = self._apply_tag_filter(qs, 'MUSCLE_GROUP', params.get('muscle_group') or params.get('muscle_tag'))
        qs = self._apply_tag_filter(qs, 'BODY_TARGET', params.get('body_target'))
        qs = self._apply_tag_filter(qs, 'EQUIPMENT', params.get('equipment') or params.get('equipment_tag'))
        qs = self._apply_tag_filter(qs, 'BREATHING', params.get('breathing') or params.get('breathing_tag'))
        qs = self._apply_tag_filter(qs, 'MOVEMENT_FAMILY', params.get('movement_family'))
        qs = self._apply_tag_filter(qs, None, params.get('tag_id') or params.get('tag'))

        return qs.order_by('movement_name', '-created_at').distinct()

    def _extract_tag_ids_from_request(self, request, validated_data: Dict[str, Any]) -> Optional[List[str]]:
        if 'tag_ids' in validated_data:
            return [str(tid) for tid in validated_data.pop('tag_ids')]
        if 'tag_ids' in request.data:
            raw = request.data.get('tag_ids')
            if isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        return [str(x) for x in parsed]
                except Exception:
                    return [x.strip() for x in raw.split(',') if x.strip()]
            elif isinstance(raw, list):
                return [str(x) for x in raw]
        return None

    def _sync_item_tags(
        self,
        item: WorkoutContentItem,
        tag_ids: Optional[List[str]],
        org: Organization,
        db_alias: str,
    ) -> None:
        if tag_ids is None:
            return
        WorkoutContentTag.objects.using(db_alias).filter(content_item=item).delete()
        if not tag_ids:
            return
        valid_tags = list(
            WorkoutTag.objects.using(db_alias).filter(organization=org, id__in=tag_ids)
        )
        for tag in valid_tags:
            WorkoutContentTag.objects.using(db_alias).get_or_create(
                content_item=item,
                tag=tag,
                defaults={'organization': org},
            )

    def create(self, request, *args, **kwargs):
        alias = _get_db(request)
        org = _get_org(request)
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic(using=alias):
            validated = dict(serializer.validated_data)
            tag_ids = self._extract_tag_ids_from_request(request, validated)
            requested_eligible = bool(validated.get('is_wod_eligible', False))

            movement_name = validated.get('movement_name', '').strip()
            explicit_kind = validated.get('content_kind')
            validated['content_kind'] = WODClassificationService.detect_content_kind(movement_name, explicit_kind)

            if validated['content_kind'] != 'MOVEMENT':
                validated['is_wod_eligible'] = False

            video_url = (validated.get('video_url') or '').strip()
            if video_url and not validated.get('video_provider'):
                validated['video_provider'] = WODVideoService.detect_provider_from_url(video_url)

            item = WorkoutContentItem(organization=org, **validated)
            item.save(using=alias)

            if tag_ids is not None:
                self._sync_item_tags(item, tag_ids, org, alias)

            uploaded_video = request.FILES.get('video_file_upload') or request.FILES.get('video_file')
            skip_download = str(request.data.get('skip_video_download', 'false')).lower() in ('true', '1', 'yes')

            if uploaded_video:
                WODVideoService.save_uploaded_video(item, uploaded_video, actor=request.user, db_alias=alias)
            elif video_url and not skip_download:
                WODVideoService.download_video_from_url(item, url=video_url, actor=request.user, db_alias=alias)

            if requested_eligible:
                try:
                    WODClassificationService.set_wod_eligibility(item, True, db_alias=alias)
                except DjangoValidationError as exc:
                    msg = exc.messages[0] if hasattr(exc, 'messages') and exc.messages else str(exc)
                    return Response({'error': msg, 'detail': msg}, status=status.HTTP_400_BAD_REQUEST)
            else:
                WODClassificationService.evaluate_item(item, db_alias=alias, save=True)

            item_refreshed = self.get_queryset().get(pk=item.pk)
            return Response(self.get_serializer(item_refreshed).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        alias = _get_db(request)
        org = _get_org(request)
        item = self.get_object()
        old_video_url = (item.video_url or '').strip()

        serializer = self.get_serializer(item, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic(using=alias):
            validated = dict(serializer.validated_data)
            tag_ids = self._extract_tag_ids_from_request(request, validated)
            explicit_eligible = validated.get('is_wod_eligible', None)

            for k, v in validated.items():
                setattr(item, k, v)

            if item.content_kind != 'MOVEMENT':
                item.is_wod_eligible = False

            new_video_url = (item.video_url or '').strip()
            if new_video_url and not item.video_provider:
                item.video_provider = WODVideoService.detect_provider_from_url(new_video_url)

            item.save(using=alias)

            if tag_ids is not None:
                self._sync_item_tags(item, tag_ids, org, alias)

            uploaded_video = request.FILES.get('video_file_upload') or request.FILES.get('video_file')
            skip_download = str(request.data.get('skip_video_download', 'false')).lower() in ('true', '1', 'yes')
            force_download = str(request.data.get('force_video_download', 'false')).lower() in ('true', '1', 'yes')

            if uploaded_video:
                WODVideoService.save_uploaded_video(item, uploaded_video, actor=request.user, db_alias=alias)
            elif new_video_url and not skip_download and (
                new_video_url != old_video_url
                or force_download
                or item.video_download_status in ('NONE', 'PENDING', 'FAILED')
            ):
                WODVideoService.download_video_from_url(item, url=new_video_url, actor=request.user, db_alias=alias)

            if explicit_eligible is True:
                try:
                    WODClassificationService.set_wod_eligibility(item, True, db_alias=alias)
                except DjangoValidationError as exc:
                    msg = exc.messages[0] if hasattr(exc, 'messages') and exc.messages else str(exc)
                    return Response({'error': msg, 'detail': msg}, status=status.HTTP_400_BAD_REQUEST)
            else:
                WODClassificationService.evaluate_item(item, db_alias=alias, save=True)

            item_refreshed = self.get_queryset().get(pk=item.pk)
            return Response(self.get_serializer(item_refreshed).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='activate')
    def activate(self, request, pk=None):
        alias = _get_db(request)
        item = self.get_object()
        item.status = 'ACTIVE'
        item.save(using=alias, update_fields=['status', 'updated_at'])
        WODClassificationService.evaluate_item(item, db_alias=alias, save=True)
        return Response(self.get_serializer(item).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='deactivate')
    def deactivate(self, request, pk=None):
        alias = _get_db(request)
        item = self.get_object()
        item.status = 'INACTIVE'
        item.is_wod_eligible = False
        item.save(using=alias, update_fields=['status', 'is_wod_eligible', 'updated_at'])
        return Response(self.get_serializer(item).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='set-wod-eligibility')
    def set_wod_eligibility(self, request, pk=None):
        alias = _get_db(request)
        item = self.get_object()
        raw_eligible = request.data.get('is_wod_eligible', True)
        eligible = str(raw_eligible).strip().lower() in ('true', '1', 'yes') if isinstance(raw_eligible, str) else bool(raw_eligible)

        try:
            WODClassificationService.set_wod_eligibility(item, eligible=eligible, db_alias=alias)
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, 'messages') and exc.messages else str(exc)
            return Response({'error': msg, 'detail': msg}, status=status.HTTP_400_BAD_REQUEST)

        return Response(self.get_serializer(item).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='upload-video')
    def upload_video(self, request, pk=None):
        alias = _get_db(request)
        item = self.get_object()
        uploaded = request.FILES.get('video_file') or request.FILES.get('file') or request.FILES.get('video_file_upload')
        if not uploaded:
            return Response(
                {'error': 'No video file provided. Expected field name "video_file" or "file".'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        WODVideoService.save_uploaded_video(item, uploaded, actor=request.user, db_alias=alias)
        return Response(self.get_serializer(item).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='download-video')
    def download_video(self, request, pk=None):
        alias = _get_db(request)
        item = self.get_object()
        url_override = request.data.get('video_url')
        target_url = (url_override if url_override is not None else item.video_url or '').strip()
        if not target_url:
            return Response(
                {'error': 'No video_url provided on this item to download from.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        WODVideoService.download_video_from_url(item, url=target_url, actor=request.user, db_alias=alias)
        if item.video_download_status == 'FAILED':
            return Response(
                {
                    'error': item.video_download_error or 'Failed to download video from link.',
                    'item': self.get_serializer(item).data,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(self.get_serializer(item).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='download-pending-videos')
    def download_pending_videos(self, request):
        alias = _get_db(request)
        org = _get_org(request)
        pending_items = list(
            WorkoutContentItem.objects.using(alias)
            .filter(
                organization=org,
                video_download_status__in=['PENDING', 'FAILED', 'NONE'],
            )
            .exclude(Q(video_url__isnull=True) | Q(video_url=''))[:50]
        )

        completed = 0
        failed = 0
        for item in pending_items:
            WODVideoService.download_video_from_url(item, actor=request.user, db_alias=alias, timeout=15)
            if item.video_download_status == 'COMPLETED':
                completed += 1
            else:
                failed += 1

        return Response(
            {
                'processed_count': len(pending_items),
                'completed_count': completed,
                'failed_count': failed,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=['get'], url_path='stream-video')
    def stream_video(self, request, pk=None):
        item = self.get_object()
        if not item.video_storage_path:
            raise Http404("No stored video file found for this workout content item.")

        base_media = getattr(settings, 'MEDIA_ROOT', None) or (Path(settings.BASE_DIR) / 'media')
        full_path = Path(base_media) / item.video_storage_path
        if not full_path.exists():
            raise Http404("Stored video file does not exist on disk.")

        content_type = item.video_mime_type or 'video/mp4'
        response = FileResponse(open(full_path, 'rb'), content_type=content_type)
        response['Content-Disposition'] = f'inline; filename="{item.video_file_name or full_path.name}"'
        response['Accept-Ranges'] = 'bytes'
        return response

    @action(detail=False, methods=['post'], url_path='import/preview')
    def import_preview(self, request):
        alias = _get_db(request)
        org = _get_org(request)
        uploaded_file = request.FILES.get('file') or request.FILES.get('excel_file')
        raw_rows = request.data.get('rows')

        if uploaded_file:
            try:
                parsed_rows = WODImportService.parse_uploaded_file(uploaded_file)
            except Exception as exc:
                return Response(
                    {'error': f'Failed to parse uploaded Excel/CSV file: {exc}'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        elif isinstance(raw_rows, list):
            parsed_rows = raw_rows
        elif isinstance(raw_rows, str):
            try:
                parsed_rows = json.loads(raw_rows)
            except Exception as exc:
                return Response({'error': f'Invalid JSON rows payload: {exc}'}, status=status.HTTP_400_BAD_REQUEST)
        else:
            return Response(
                {'error': 'Provide an Excel (.xlsx) / CSV file under "file" or a JSON array under "rows".'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        preview_data = WODImportService.preview_import(org, parsed_rows, db_alias=alias)
        return Response(preview_data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='import/confirm')
    def import_confirm(self, request):
        alias = _get_db(request)
        org = _get_org(request)
        uploaded_file = request.FILES.get('file') or request.FILES.get('excel_file')
        raw_rows = request.data.get('rows')
        duplicate_strategy = request.data.get('duplicate_strategy', 'UPDATE')
        auto_download_raw = request.data.get('auto_download_videos', True)
        auto_download = (
            str(auto_download_raw).strip().lower() in ('true', '1', 'yes')
            if isinstance(auto_download_raw, str)
            else bool(auto_download_raw)
        )

        if uploaded_file:
            try:
                parsed_rows = WODImportService.parse_uploaded_file(uploaded_file)
            except Exception as exc:
                return Response(
                    {'error': f'Failed to parse uploaded Excel/CSV file: {exc}'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        elif isinstance(raw_rows, list):
            parsed_rows = raw_rows
        elif isinstance(raw_rows, str):
            try:
                parsed_rows = json.loads(raw_rows)
            except Exception as exc:
                return Response({'error': f'Invalid JSON rows payload: {exc}'}, status=status.HTTP_400_BAD_REQUEST)
        else:
            return Response(
                {'error': 'Provide confirmed "rows" or an Excel (.xlsx) / CSV "file".'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = WODImportService.confirm_import(
            organization=org,
            rows=parsed_rows,
            actor=request.user,
            duplicate_strategy=duplicate_strategy,
            auto_download_videos=auto_download,
            db_alias=alias,
        )
        return Response(result, status=status.HTTP_201_CREATED)
