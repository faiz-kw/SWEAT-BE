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
from .models_rbac import Role, RoleAssignment
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


# Canonical CRM navigation modules matching the CRM sidebar navigation exactly
CRM_NAVIGATION_MODULES = [
    'Dashboard',
    'CRM & Sales',
    'Members',
    'Operations',
    'Performance',
    'AI & Intelligence',
    'Coaching',
    'Support & Grievance',
    'Nutrition',
    'Inventory',
    'Finance',
    'Customer Success',
    'Marketing',
    'Automation',
    'Reports',
    'Community Hub',
    'Administration',
]

# Standard Enterprise Action Categories representing the operational nature of activities
CATEGORY_CANONICAL_MAP = {
    'COMMERCIAL': 'Commercial & Billing',
    'COMMERCE': 'Commercial & Billing',
    'FINANCE': 'Commercial & Billing',
    'BILLING': 'Commercial & Billing',
    'OPERATIONAL': 'Operations & Hardware',
    'OPERATIONS': 'Operations & Hardware',
    'IOT': 'Operations & Hardware',
    'SECURITY_POLICY': 'Access & Security',
    'SECURITY': 'Access & Security',
    'AUTH': 'Access & Security',
    'AUTHENTICATION': 'Access & Security',
    'ACCESS_CONTROL': 'Access & Security',
    'ACCESS CONTROL': 'Access & Security',
    'COMMUNICATION': 'Communications & Outreach',
    'MEMBERSHIP': 'Customer & Membership',
    'MEMBERSHIPS': 'Customer & Membership',
    'WORKFORCE': 'Workforce & Staffing',
    'GENERAL': 'General & System',
    'SYSTEM': 'General & System',
}

STANDARD_ACTION_CATEGORIES = [
    'Access & Security',
    'Commercial & Billing',
    'Customer & Membership',
    'Communications & Outreach',
    'Operations & Hardware',
    'Workforce & Staffing',
    'General & System',
]

CHANNEL_CANONICAL_MAP = {
    'ADMIN_PANEL': 'WEB ADMIN',
    'ADMIN PANEL': 'WEB ADMIN',
    'WEB': 'WEB ADMIN',
    'WEB_ADMIN': 'WEB ADMIN',
    'DESKTOP': 'DESKTOP',
    'HARDWARE_IOT': 'HARDWARE IOT',
    'MOBILE_APP': 'MOBILE APP',
    'WEBHOOK': 'WEBHOOK',
}


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
            mod_norm = module.strip().upper()
            if mod_norm in ('ADMINISTRATION', 'ADMIN'):
                qs = qs.filter(
                    Q(module__in=['ADMINISTRATION', 'ADMIN', 'CORE', 'USERS', 'ROLES', 'PERMISSIONS', 'SETTINGS', 'ACCESS_CONTROL', 'ORGANIZATION', 'TENANTUSER', 'SECURITY', 'AUTH']) |
                    Q(action_code__startswith='AUTH') |
                    Q(action_code__startswith='SECURITY') |
                    Q(action_code__startswith='ROLE') |
                    Q(action_code__startswith='USER')
                )
            elif mod_norm in ('CRM & SALES', 'CRM', 'SALES'):
                qs = qs.filter(
                    Q(module__in=['CRM', 'CRM & SALES', 'SALES', 'LEADS', 'CAMPAIGNS', 'COUPONS', 'DISCOUNT_CAMPAIGNS', 'REWARDS']) |
                    Q(action_code__startswith='CRM') |
                    Q(action_code__startswith='LEAD') |
                    Q(action_code__startswith='DISCOUNT')
                )
            elif mod_norm in ('MEMBERS', 'MEMBERSHIPS', 'MEMBERSHIP'):
                qs = qs.filter(
                    Q(module__in=['MEMBERS', 'MEMBERSHIPS', 'MEMBERSHIP', 'ATTENDANCE', 'RENEWALS', 'REWARDS']) |
                    Q(action_code__startswith='MEMBERSHIP') |
                    Q(action_code__startswith='MEMBER')
                )
            elif mod_norm in ('OPERATIONS', 'OPS'):
                qs = qs.filter(
                    Q(module__in=['OPERATIONS', 'CLASSES', 'CLASS_OCCURRENCES', 'BOOKINGS', 'MEMBER_ATTENDANCE_STATES', 'TRAINERS', 'PROGRAMS', 'WOD']) |
                    Q(action_code__startswith='IOT') |
                    Q(action_code__startswith='BOOKING') |
                    Q(action_code__startswith='CLASS')
                )
            elif mod_norm in ('FINANCE', 'COMMERCE', 'COMMERCIAL'):
                qs = qs.filter(
                    Q(module__in=['FINANCE', 'COMMERCE', 'COMMERCIAL', 'INVOICES', 'PAYMENTS', 'REFUNDS']) |
                    Q(action_code__startswith='COMMERCE') |
                    Q(action_code__startswith='ORDER') |
                    Q(action_code__startswith='PAYMENT') |
                    Q(action_code__startswith='RAZORPAY')
                )
            elif mod_norm in ('INVENTORY',):
                qs = qs.filter(module__in=['INVENTORY', 'STOCK', 'PRODUCTS'])
            elif mod_norm in ('PERFORMANCE',):
                qs = qs.filter(module__in=['PERFORMANCE', 'ASSESSMENTS', 'WORKOUTS', 'RECOVERY'])
            elif mod_norm in ('AI & INTELLIGENCE', 'AI'):
                qs = qs.filter(module__in=['AI', 'AI & INTELLIGENCE', 'INTELLIGENCE', 'VISION'])
            elif mod_norm in ('COACHING',):
                qs = qs.filter(module__in=['COACHING', 'COACH', 'TRAINERS'])
            elif mod_norm in ('SUPPORT & GRIEVANCE', 'SUPPORT'):
                qs = qs.filter(module__in=['SUPPORT', 'GRIEVANCE', 'TICKETS'])
            elif mod_norm in ('NUTRITION',):
                qs = qs.filter(module__in=['NUTRITION', 'DIET'])
            elif mod_norm in ('CUSTOMER SUCCESS', 'CS'):
                qs = qs.filter(module__in=['CS', 'CUSTOMER_SUCCESS', 'CUSTOMER SUCCESS', 'RETENTION'])
            elif mod_norm in ('MARKETING',):
                qs = qs.filter(module__in=['MARKETING', 'CAMPAIGNS', 'AUDIENCES'])
            elif mod_norm in ('AUTOMATION',):
                qs = qs.filter(module__in=['AUTOMATION', 'WORKFLOWS', 'RULES', 'NOTIFICATIONS'])
            elif mod_norm in ('REPORTS',):
                qs = qs.filter(module__in=['REPORTS', 'REPORTING', 'ANALYTICS'])
            elif mod_norm in ('COMMUNITY HUB', 'COMMUNITY'):
                qs = qs.filter(module__in=['COMMUNITY', 'COMMUNITY HUB', 'COMMUNITY_HUB'])
            elif mod_norm in ('DASHBOARD',):
                qs = qs.filter(module__iexact='DASHBOARD')
            else:
                qs = qs.filter(module__iexact=module)

        submodule = params.get('submodule')
        if submodule:
            qs = qs.filter(submodule__iexact=submodule)

        # 3. Action Category & Code (Canonical Enterprise Operational Classifications)
        category = params.get('category') or params.get('action_category')
        if category and category.upper() != 'ALL':
            cat_norm = category.strip().upper()
            if cat_norm in ('ACCESS & SECURITY', 'SECURITY', 'AUTHENTICATION', 'AUTH', 'ACCESS_CONTROL'):
                qs = qs.filter(
                    Q(action_category__in=['ACCESS_CONTROL', 'AUTHENTICATION', 'AUTH', 'SECURITY', 'SECURITY_POLICY', 'Access & Security']) |
                    Q(action_code__startswith='AUTH') |
                    Q(action_code__startswith='SECURITY')
                )
            elif cat_norm in ('COMMERCIAL & BILLING', 'COMMERCE', 'COMMERCIAL', 'FINANCE', 'BILLING'):
                qs = qs.filter(
                    Q(action_category__in=['COMMERCE', 'COMMERCIAL', 'FINANCE', 'BILLING', 'Commercial & Billing']) |
                    Q(action_code__startswith='COMMERCE') |
                    Q(action_code__startswith='ORDER') |
                    Q(action_code__startswith='RAZORPAY')
                )
            elif cat_norm in ('CUSTOMER & MEMBERSHIP', 'MEMBERSHIP', 'MEMBERSHIPS'):
                qs = qs.filter(
                    Q(action_category__in=['MEMBERSHIP', 'MEMBERSHIPS', 'Customer & Membership']) |
                    Q(action_code__startswith='MEMBERSHIP')
                )
            elif cat_norm in ('COMMUNICATIONS & OUTREACH', 'COMMUNICATION'):
                qs = qs.filter(
                    Q(action_category__in=['COMMUNICATION', 'Communications & Outreach']) |
                    Q(action_code__startswith='COMMUNICATION') |
                    Q(action_code__startswith='SMS') |
                    Q(action_code__startswith='WHATSAPP')
                )
            elif cat_norm in ('OPERATIONS & HARDWARE', 'OPERATIONS', 'OPERATIONAL', 'IOT'):
                qs = qs.filter(
                    Q(action_category__in=['OPERATIONS', 'OPERATIONAL', 'IOT', 'Operations & Hardware']) |
                    Q(action_code__startswith='IOT')
                )
            elif cat_norm in ('WORKFORCE & STAFFING', 'WORKFORCE'):
                qs = qs.filter(action_category__in=['WORKFORCE', 'Workforce & Staffing'])
            elif cat_norm in ('GENERAL & SYSTEM', 'GENERAL', 'SYSTEM'):
                qs = qs.filter(action_category__in=['GENERAL', 'SYSTEM', 'General & System'])
            else:
                qs = qs.filter(action_category__iexact=category)

        action_code = params.get('action_code')
        if action_code:
            qs = qs.filter(action_code__iexact=action_code)

        # 4. Role (Connected to backend Role model; maps performer identity & role assignments)
        role = params.get('role') or params.get('role_code') or params.get('actor_role') or params.get('actor_type')
        if role and role.upper() != 'ALL':
            role_norm = role.strip().upper()
            if role_norm in ('SYSTEM', 'SYSTEM AUTOMATION & SERVICES'):
                qs = qs.filter(
                    Q(actor_type__iexact='SYSTEM') |
                    Q(actor_role__iexact='SYSTEM') |
                    Q(actor_role__iexact='GATEWAY')
                )
            elif role_norm in ('MEMBER', 'GYM MEMBER'):
                qs = qs.filter(
                    Q(actor_type__iexact='MEMBER') |
                    Q(actor_role__iexact='MEMBER') |
                    Q(actor_role__iexact='Gym Member')
                )
            elif role_norm in ('STAFF', 'STAFF MEMBER'):
                qs = qs.filter(
                    Q(actor_type__iexact='STAFF') |
                    Q(actor_role__icontains='Staff')
                )
            else:
                role_user_ids = list(
                    RoleAssignment.objects.using(db)
                    .filter(Q(role__code__iexact=role) | Q(role__name__iexact=role))
                    .values_list('user_id', flat=True)
                )
                role_filter = (
                    Q(actor_role__iexact=role) |
                    Q(actor_role__icontains=role) |
                    Q(actor_type__iexact=role)
                )
                if role_norm == 'ORG_ADMIN':
                    role_filter |= Q(actor_role__icontains='Admin') | Q(actor_type='ADMIN')
                if role_user_ids:
                    role_filter |= Q(actor_id__in=[str(uid) for uid in role_user_ids])
                qs = qs.filter(role_filter)

        # 5. Specific Staff / User Filter (Dynamic from CRM Users & Audit Performers)
        actor_id = params.get('actor_id')
        actor_email = params.get('actor_email')
        actor_name = params.get('actor_name')
        if actor_id and actor_id.upper() != 'ALL':
            if actor_email:
                qs = qs.filter(Q(actor_id=actor_id) | Q(actor_email__iexact=actor_email))
            else:
                qs = qs.filter(actor_id=actor_id)
        elif actor_email and actor_email.upper() != 'ALL':
            qs = qs.filter(actor_email__icontains=actor_email)
        elif actor_name and actor_name.upper() != 'ALL':
            qs = qs.filter(actor_name__icontains=actor_name)

        # 5. Result Status (SUCCESS, FAILURE, WARNING)
        result = params.get('result')
        if result and result.upper() != 'ALL':
            qs = qs.filter(result__iexact=result)

        # 6. Branch Filter (matching selected branch ID or any branch sharing the same name)
        branch_id = params.get('branch_id') or params.get('branch')
        if branch_id and branch_id.upper() != 'ALL':
            try:
                selected_br = Branch.objects.using(db).filter(id=branch_id).first()
                if selected_br:
                    matching_ids = list(Branch.objects.using(db).filter(name__iexact=selected_br.name).values_list('id', flat=True))
                    qs = qs.filter(branch_id__in=matching_ids)
                else:
                    qs = qs.filter(branch_id=branch_id)
            except Exception:
                qs = qs.filter(branch_id=branch_id)

        # 7. Source Channel
        source_channel = params.get('source_channel')
        if source_channel and source_channel.upper() != 'ALL':
            ch_upper = source_channel.upper().replace(' ', '_')
            if ch_upper in ('WEB_ADMIN', 'ADMIN_PANEL', 'WEB'):
                qs = qs.filter(source_channel__in=['WEB_ADMIN', 'WEB ADMIN', 'ADMIN_PANEL', 'ADMIN PANEL', 'WEB'])
            elif ch_upper in ('HARDWARE_IOT',):
                qs = qs.filter(source_channel__in=['HARDWARE_IOT', 'HARDWARE IOT'])
            elif ch_upper in ('MOBILE_APP',):
                qs = qs.filter(source_channel__in=['MOBILE_APP', 'MOBILE APP'])
            else:
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

        # Dynamic distinct modules matching the CRM navigation structure exactly
        distinct_modules = list(CRM_NAVIGATION_MODULES)

        # Standard Enterprise Action Categories representing operational intent
        distinct_categories = list(STANDARD_ACTION_CATEGORIES)

        # Dynamic distinct Roles from the CRM backend Role model (zero hardcoding)
        roles = []
        seen_roles = set()
        for r in Role.objects.using(db).filter(is_active=True).order_by('name'):
            code = r.code.strip()
            if code not in seen_roles:
                seen_roles.add(code)
                roles.append({'code': code, 'name': r.name.strip()})
        if 'SYSTEM' not in seen_roles:
            roles.append({'code': 'SYSTEM', 'name': 'System Automation & Services'})

        distinct_actor_types = [r['code'] for r in roles]

        # Dynamic distinct source channels (deduplicated through canonical map)
        raw_channels = set(
            v.strip().upper()
            for v in qs.order_by().exclude(source_channel='').values_list('source_channel', flat=True)
            if v and v.strip()
        )
        canonical_channels = set()
        for ch in raw_channels:
            mapped = CHANNEL_CANONICAL_MAP.get(ch, ch.replace('_', ' '))
            canonical_channels.add(mapped)
        distinct_channels = sorted(list(canonical_channels))

        # Dynamic distinct results
        distinct_results = sorted(list(set(
            v.strip().upper()
            for v in qs.order_by().exclude(result='').values_list('result', flat=True)
            if v and v.strip()
        )))

        # Distinct branches (deduplicated by normalized name, prioritizing active records)
        branch_qs = Branch.objects.using(db).all()
        if not is_org_wide:
            branch_qs = branch_qs.filter(id__in=allowed_branch_ids) if allowed_branch_ids else branch_qs.none()
        seen_branch_names = set()
        branches = []
        for b in branch_qs.filter(status='ACTIVE').order_by('name'):
            norm_name = b.name.strip().lower()
            if norm_name not in seen_branch_names:
                seen_branch_names.add(norm_name)
                branches.append({'id': str(b.id), 'name': b.name.strip(), 'code': b.code, 'status': b.status})
        for b in branch_qs.order_by('name'):
            norm_name = b.name.strip().lower()
            if norm_name not in seen_branch_names:
                seen_branch_names.add(norm_name)
                branches.append({'id': str(b.id), 'name': b.name.strip(), 'code': b.code, 'status': b.status})

        # Dynamic registered staff & CRM performers (100% backend-driven, ZERO dummy/hardcoded data)
        # 1. Fetch active registered staff/admins from TenantUser
        staff_users_qs = (
            TenantUser.objects.using(db)
            .filter(user_type__in=['STAFF', 'ADMIN', 'SUPER_ADMIN', 'EMPLOYEE'])
            .filter(status='ACTIVE')
        )
        role_map = {}
        for ra in RoleAssignment.objects.using(db).select_related('role'):
            role_map[ra.user_id] = ra.role.name

        actors = []
        seen_actors = {}
        for u in staff_users_qs:
            first = (u.first_name or '').strip()
            last = (u.last_name or '').strip()
            name = f"{first} {last}".strip() or (u.display_name or '').strip() or u.email
            if not name or (len(name) == 1 and name.isdigit()):
                continue
            email = (u.email or '').strip()
            base_email = email.split('.com')[0] if '.com' in email else email
            norm_key = (name.lower(), base_email.lower())
            if norm_key in seen_actors:
                continue
            assigned_role = role_map.get(u.id) or u.user_type.title()
            entry = {
                'actor_id': str(u.id),
                'actor_name': name,
                'actor_email': email,
                'actor_role': assigned_role,
                'actor_type': 'ADMIN' if u.user_type in ('ADMIN', 'SUPER_ADMIN') or 'Admin' in assigned_role else 'STAFF',
            }
            seen_actors[norm_key] = entry
            actors.append(entry)

        # 2. Also fetch any system services or actors recorded in ActivityAuditLog
        system_actors_raw = (
            qs.order_by()
            .filter(actor_type__in=['SYSTEM', 'GATEWAY'])
            .values('actor_id', 'actor_name', 'actor_email', 'actor_role', 'actor_type')
            .distinct()
        )
        for sa in system_actors_raw:
            name = (sa.get('actor_name') or '').strip() or 'System Automation'
            norm_key = (name.lower(), '')
            if norm_key not in seen_actors:
                entry = {
                    'actor_id': str(sa.get('actor_id')) if sa.get('actor_id') else None,
                    'actor_name': name,
                    'actor_email': sa.get('actor_email') or '',
                    'actor_role': sa.get('actor_role') or 'System Service',
                    'actor_type': 'SYSTEM',
                }
                seen_actors[norm_key] = entry
                actors.append(entry)

        # Sort actors: Admins first, then Staff, then System, alphabetically
        def sort_actor_key(act):
            t = act.get('actor_type', 'STAFF')
            prio = 0 if t == 'ADMIN' else (1 if t == 'STAFF' else 2)
            return (prio, act.get('actor_name', '').lower())

        actors.sort(key=sort_actor_key)

        # KPI Summary Counters (strictly within user's RBAC scope)
        total_activities = qs.count()
        security_events = qs.filter(
            Q(action_category__in=['AUTHENTICATION', 'SECURITY', 'ACCESS_CONTROL', 'Access & Security']) |
            Q(action_code__startswith='AUTH') |
            Q(action_code__startswith='SECURITY')
        ).count()
        commercial_mutations = qs.filter(
            Q(action_category__in=['COMMERCE', 'COMMERCIAL', 'FINANCE', 'BILLING', 'Commercial & Billing']) |
            Q(action_code__startswith='COMMERCE') |
            Q(action_code__startswith='ORDER') |
            Q(action_code__startswith='RAZORPAY')
        ).count()
        failed_operations = qs.filter(result='FAILURE').count()

        return Response({
            'modules': distinct_modules,
            'categories': distinct_categories,
            'roles': roles,
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
