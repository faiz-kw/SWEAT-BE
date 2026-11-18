"""
apps/tenant_core/views_activity_audit.py — Canonical Activity & Audit Log REST ViewSet

Provides:
- GET /api/v1/tenant/activity-logs/ (Server-side paginated, searchable, multi-filter ledger)
- GET /api/v1/tenant/activity-logs/<id>/ (Detailed 9-point contract entry with granular field diffs)
- GET /api/v1/tenant/activity-logs/filters/ (Dynamic filter options & KPI counters — ZERO hardcoding)
- GET /api/v1/tenant/activity-logs/export/ (CSV export streaming for compliance audits)

Strict Security:
- ReadOnlyModelViewSet: 405 Method Not Allowed on POST/PUT/PATCH/DELETE.
- Append-only immutability.
- Authorized via TenantRBACPermission against core.audit.view.
"""

import csv
import logging
from datetime import datetime, time as dt_time
from django.http import HttpResponse
from django.utils import timezone
from django.db.models import Q
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework import serializers

from .models_activity_audit import ActivityAuditLog
from .models_org import Branch
from .models_users import TenantUser
from .views import TenantScopeMixin, TenantDBMixin
from .permissions import TenantRBACPermission

logger = logging.getLogger(__name__)


class ActivityAuditLogPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = 'page_size'
    max_page_size = 200


class ActivityAuditLogSerializer(serializers.ModelSerializer):
    """
    Serializes ActivityAuditLog records fulfilling the complete 9-Point Contract.
    """
    actor_display = serializers.SerializerMethodField()
    occurred_at_ist = serializers.SerializerMethodField()
    field_changes_count = serializers.SerializerMethodField()

    class Meta:
        model = ActivityAuditLog
        fields = [
            'id',
            'occurred_at',
            'occurred_at_ist',
            'organization_id',
            'branch_id',
            'branch_name',
            'actor_type',
            'actor_id',
            'actor_email',
            'actor_name',
            'actor_role',
            'actor_display',
            'action_code',
            'action_category',
            'action_verb',
            'module',
            'submodule',
            'entity_type',
            'entity_id',
            'entity_name',
            'before_state',
            'after_state',
            'field_changes',
            'field_changes_count',
            'event_description',
            'reason',
            'remarks',
            'result',
            'error_code',
            'error_message',
            'metadata',
            'ip_address',
            'user_agent',
            'request_id',
            'correlation_id',
            'source_channel',
            'created_at',
        ]

    def get_actor_display(self, obj):
        name = obj.actor_name or obj.actor_email or obj.actor_type
        if obj.actor_role and obj.actor_role not in name:
            return f"{name} ({obj.actor_role})"
        return name

    def get_occurred_at_ist(self, obj):
        if not obj.occurred_at:
            return ''
        # Convert UTC to IST (+5:30)
        ist_time = obj.occurred_at.astimezone(timezone.get_fixed_timezone(330))
        return ist_time.strftime('%Y-%m-%d %H:%M:%S IST')

    def get_field_changes_count(self, obj):
        if obj.field_changes and isinstance(obj.field_changes, list):
            return len(obj.field_changes)
        return 0


class ActivityAuditLogViewSet(TenantScopeMixin, TenantDBMixin, viewsets.ReadOnlyModelViewSet):
    """
    Append-only centralized Activity & Audit Logging endpoint.
    Strictly read-only via API (405 Method Not Allowed on mutations).
    """
    permission_classes = [TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'audit'
    permission_prefix = 'core.audit'
    action_permission_map = {
        'list': 'core.audit.view',
        'retrieve': 'core.audit.view',
        'filters': 'core.audit.view',
        'export': 'core.audit.view',
    }
    serializer_class = ActivityAuditLogSerializer
    pagination_class = ActivityAuditLogPagination

    def _get_effective_scope(self):
        """
        Determines (is_org_wide, allowed_branch_ids) honoring Platform SuperAdmins
        and TenantScopeMixin branch restrictions.
        """
        user = getattr(self.request, 'user', None)
        if not user or not getattr(user, 'is_authenticated', False):
            return False, []
        if getattr(user, '_auth_type', None) == 'platform' or getattr(user, 'is_superuser', False):
            return True, []
        return self.get_user_branch_scope()

    def _get_scoped_queryset(self, db: str):
        qs = ActivityAuditLog.objects.using(db).all().order_by('-occurred_at')
        is_org_wide, allowed_branch_ids = self._get_effective_scope()
        if is_org_wide:
            return qs
        if not allowed_branch_ids:
            return qs.none()
        return qs.filter(branch_id__in=allowed_branch_ids)

    @staticmethod
    def _parse_date_bound(raw: str, is_end: bool = False):
        """
        Parses either YYYY-MM-DD or full ISO-8601 timestamps.
        Date-only bounds are normalized in IST (+05:30) to start/end of day so
        selecting a single calendar date covers the full 24-hour window.
        """
        raw = raw.strip()
        ist = timezone.get_fixed_timezone(330)
        if len(raw) == 10 and raw[4] == '-' and raw[7] == '-':
            d = datetime.strptime(raw, '%Y-%m-%d').date()
            t = dt_time(23, 59, 59, 999999) if is_end else dt_time(0, 0, 0, 0)
            return datetime.combine(d, t, tzinfo=ist)
        dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if timezone.is_naive(dt):
            dt = dt.replace(tzinfo=ist)
        return dt

    def get_queryset(self):
        db = self.get_db()
        qs = self._get_scoped_queryset(db)

        # Request parameters
        params = self.request.query_params

        # 1. Date Range Filtering
        start_date = params.get('start_date') or params.get('from_date')
        if start_date:
            try:
                dt = self._parse_date_bound(start_date, is_end=False)
                qs = qs.filter(occurred_at__gte=dt)
            except ValueError:
                pass

        end_date = params.get('end_date') or params.get('to_date')
        if end_date:
            try:
                dt = self._parse_date_bound(end_date, is_end=True)
                qs = qs.filter(occurred_at__lte=dt)
            except ValueError:
                pass

        # 2. Module & Submodule
        module = params.get('module')
        if module and module.upper() != 'ALL':
            qs = qs.filter(module__iexact=module)

        submodule = params.get('submodule')
        if submodule:
            qs = qs.filter(submodule__iexact=submodule)

        # 3. Action Category & Code
        category = params.get('category') or params.get('action_category')
        if category and category.upper() != 'ALL':
            qs = qs.filter(action_category__iexact=category)

        action_code = params.get('action_code')
        if action_code:
            qs = qs.filter(action_code__iexact=action_code)

        # 4. Actor Type & Specific Actor
        actor_type = params.get('actor_type')
        if actor_type and actor_type.upper() != 'ALL':
            qs = qs.filter(actor_type__iexact=actor_type)

        actor_id = params.get('actor_id')
        if actor_id and actor_id.upper() != 'ALL':
            qs = qs.filter(actor_id=actor_id)

        actor_email = params.get('actor_email')
        if actor_email and actor_email.upper() != 'ALL':
            qs = qs.filter(actor_email__icontains=actor_email)

        # 5. Result Status (SUCCESS, FAILURE, WARNING)
        result = params.get('result')
        if result and result.upper() != 'ALL':
            qs = qs.filter(result__iexact=result)

        # 6. Branch Filter (within already RBAC-scoped queryset)
        branch_id = params.get('branch_id') or params.get('branch')
        if branch_id and branch_id.upper() != 'ALL':
            qs = qs.filter(branch_id=branch_id)

        # 7. Source Channel
        source_channel = params.get('source_channel')
        if source_channel and source_channel.upper() != 'ALL':
            qs = qs.filter(source_channel__iexact=source_channel)

        # 8. Entity Type & Entity ID
        entity_type = params.get('entity_type')
        if entity_type:
            qs = qs.filter(entity_type__iexact=entity_type)

        entity_id = params.get('entity_id')
        if entity_id:
            qs = qs.filter(entity_id=entity_id)

        # 9. Free-text Search
        search = params.get('search')
        if search:
            search = search.strip()
            qs = qs.filter(
                Q(event_description__icontains=search) |
                Q(entity_name__icontains=search) |
                Q(actor_name__icontains=search) |
                Q(actor_email__icontains=search) |
                Q(action_code__icontains=search) |
                Q(reason__icontains=search) |
                Q(remarks__icontains=search) |
                Q(entity_id__icontains=search)
            )

        return qs

    @action(detail=False, methods=['get'])
    def filters(self, request):
        """
        GET /api/v1/tenant/activity-logs/filters/
        Returns dynamic distinct filter values and KPI summary counters strictly within user's RBAC scope.
        Zero hardcoding: all dropdowns and metrics are populated live from the tenant DB.
        """
        db = self.get_db()
        is_org_wide, allowed_branch_ids = self._get_effective_scope()
        qs = self._get_scoped_queryset(db)

        # Dynamic distinct modules (case-normalized; get_queryset filters via iexact)
        distinct_modules = sorted(list(set(
            v.strip().upper()
            for v in qs.order_by().exclude(module='').values_list('module', flat=True)
            if v and v.strip()
        )))

        # Dynamic distinct action categories
        distinct_categories = sorted(list(set(
            v.strip().upper()
            for v in qs.order_by().exclude(action_category='').values_list('action_category', flat=True)
            if v and v.strip()
        )))

        # Dynamic distinct actor types
        distinct_actor_types = sorted(list(set(
            v.strip().upper()
            for v in qs.order_by().exclude(actor_type='').values_list('actor_type', flat=True)
            if v and v.strip()
        )))

        # Dynamic distinct source channels
        distinct_channels = sorted(list(set(
            v.strip().upper()
            for v in qs.order_by().exclude(source_channel='').values_list('source_channel', flat=True)
            if v and v.strip()
        )))

        # Dynamic distinct results
        distinct_results = sorted(list(set(
            v.strip().upper()
            for v in qs.order_by().exclude(result='').values_list('result', flat=True)
            if v and v.strip()
        )))

        # Active branches strictly within user's RBAC scope
        branch_qs = Branch.objects.using(db).all()
        if not is_org_wide:
            branch_qs = branch_qs.filter(id__in=allowed_branch_ids) if allowed_branch_ids else branch_qs.none()
        branches = list(
            branch_qs.values('id', 'name', 'code', 'status').order_by('name')
        )

        # Dynamic active actors (order_by() clears default ordering so PostgreSQL DISTINCT deduplicates accurately)
        actors_raw = (
            qs.order_by()
            .exclude(actor_name='')
            .values('actor_id', 'actor_name', 'actor_email', 'actor_role', 'actor_type')
            .distinct()[:100]
        )
        actors = []
        seen_keys = set()
        for a in actors_raw:
            dedup_key = (a.get('actor_id') or a.get('actor_email') or a.get('actor_name') or '').lower()
            if dedup_key and dedup_key not in seen_keys:
                seen_keys.add(dedup_key)
                actors.append(a)
        actors.sort(key=lambda x: (x.get('actor_name') or '').lower())

        # KPI Summary Counters (strictly within user's RBAC scope)
        total_activities = qs.count()
        security_events = qs.filter(
            Q(action_category__in=['AUTHENTICATION', 'SECURITY']) |
            Q(action_code__startswith='AUTH') |
            Q(action_code__startswith='SECURITY')
        ).count()
        commercial_mutations = qs.filter(
            Q(action_category__in=['COMMERCE', 'COMMERCIAL']) |
            Q(action_code__startswith='COMMERCE') |
            Q(action_code__startswith='ORDER') |
            Q(action_code__startswith='RAZORPAY')
        ).count()
        failed_operations = qs.filter(result='FAILURE').count()

        return Response({
            'modules': distinct_modules,
            'categories': distinct_categories,
            'actor_types': distinct_actor_types,
            'source_channels': distinct_channels,
            'results': distinct_results,
            'branches': branches,
            'actors': actors,
            'kpis': {
                'total_activities': total_activities,
                'security_events': security_events,
                'commercial_mutations': commercial_mutations,
                'failed_operations': failed_operations,
            }
        }, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'])
    def export(self, request):
        """
        GET /api/v1/tenant/activity-logs/export/
        Streams a structured CSV export of filtered activity logs for auditing and compliance.
        """
        qs = self.get_queryset()[:5000]  # Cap export at 5,000 recent records for streaming stability

        response = HttpResponse(content_type='text/csv')
        filename = f"activity_audit_logs_{timezone.now().strftime('%Y%m%d_%H%M%S')}.csv"
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        writer.writerow([
            'Timestamp (UTC)',
            'Timestamp (IST)',
            'Result',
            'Actor Type',
            'Actor Name',
            'Actor Email',
            'Actor Role',
            'Action Code',
            'Category',
            'Module',
            'Submodule',
            'Entity Type',
            'Entity ID',
            'Entity Name',
            'Description',
            'Reason',
            'Remarks',
            'Branch Name',
            'Source Channel',
            'IP Address',
            'Error Code',
            'Error Message',
        ])

        for log in qs:
            ist_str = ''
            if log.occurred_at:
                ist_time = log.occurred_at.astimezone(timezone.get_fixed_timezone(330))
                ist_str = ist_time.strftime('%Y-%m-%d %H:%M:%S IST')

            writer.writerow([
                log.occurred_at.strftime('%Y-%m-%d %H:%M:%S UTC') if log.occurred_at else '',
                ist_str,
                log.result,
                log.actor_type,
                log.actor_name,
                log.actor_email,
                log.actor_role,
                log.action_code,
                log.action_category,
                log.module,
                log.submodule,
                log.entity_type,
                log.entity_id or '',
                log.entity_name,
                log.event_description,
                log.reason,
                log.remarks,
                log.branch_name,
                log.source_channel,
                log.ip_address or '',
                log.error_code,
                log.error_message,
            ])

        return response
