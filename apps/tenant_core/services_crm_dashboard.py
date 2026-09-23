"""
apps/tenant_core/services_crm_dashboard.py — Layer 2 Module B Phase 10: CRM Dashboard & Sales Analytics Service

Authoritative, backend-driven aggregation for executive CRM management:
- Strict tenant DB isolation and tenant/org scoping
- Server-enforced user branch scoping (intersection with effective branch permissions)
- Canonical status verification (Lead, TrialBooking, SalesFollowupTask, LeadConversion)
- Reuses Phase 9 Campaign Attribution & Revenue semantics
- Consumes Phase 7 Attention Engine authoritative metrics
- Verified Commercial Revenue (Order status='PAID' linked via LeadConversion minus successful Refunds)
- Zero mock business data, zero Math.random(), zero N+1 query loops
"""

import logging
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Dict, Any, List, Optional, Set

from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Sum, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from config.routers import get_tenant_db_alias
from .models_org import Organization, Branch
from .models_users import TenantUser
from .models_crm import (
    Lead,
    LeadSource,
    LeadAttribution,
    LeadConversion,
    LeadActivity,
    TrialBooking,
    SalesFollowupTask,
)
from .models_commerce import Order, PaymentTransaction, Refund
from .services_attention import LeadAttentionService

logger = logging.getLogger(__name__)


class CRMDashboardService:
    """
    Consolidated CRM sales analytics and operational reporting engine.
    """

    @classmethod
    def get_dashboard_data(
        cls,
        organization: Organization,
        user: Optional[TenantUser],
        filters: Dict[str, Any],
        db_alias: Optional[str] = None,
    ) -> Dict[str, Any]:
        alias = db_alias or get_tenant_db_alias() or 'default'

        # 1. Date resolution
        start_dt, end_dt, preset_applied = cls._resolve_date_range(filters)

        # 2. Branch scope resolution
        permitted_branches, branch_filter_id, effective_branch_ids = cls._resolve_branch_scope(
            user=user,
            requested_branch=filters.get('branch_id') or filters.get('branch'),
            organization=organization,
            db_alias=alias,
        )

        # 3. Base Querysets with scope and date filters applied
        agent_id = filters.get('agent_id') or filters.get('agent')
        source_id = filters.get('lead_source_id') or filters.get('source_id') or filters.get('source')
        campaign_name = filters.get('campaign_name') or filters.get('campaign')
        program_id = filters.get('program_id') or filters.get('program')
        platform = filters.get('platform')

        # Clean 'ALL' values
        agent_id = None if agent_id in ('ALL', '', None) else agent_id
        source_id = None if source_id in ('ALL', '', None) else source_id
        campaign_name = None if campaign_name in ('ALL', '', None) else campaign_name
        program_id = None if program_id in ('ALL', '', None) else program_id
        platform = None if platform in ('ALL', '', None) else platform

        # Base Lead QS
        lead_qs = Lead.objects.using(alias).filter(
            organization=organization,
            created_at__gte=start_dt,
            created_at__lte=end_dt,
        )
        if effective_branch_ids is not None:
            lead_qs = lead_qs.filter(branch_id__in=effective_branch_ids)
        if agent_id:
            lead_qs = lead_qs.filter(assigned_sales_user_id=agent_id)
        if source_id:
            lead_qs = lead_qs.filter(lead_source_id=source_id)
        if campaign_name:
            lead_qs = lead_qs.filter(
                Q(campaign_reference__icontains=campaign_name) |
                Q(attributions__campaign_name__icontains=campaign_name) |
                Q(attributions__utm_campaign__icontains=campaign_name)
            ).distinct()
        if program_id:
            lead_qs = lead_qs.filter(interested_program_id=program_id)
        if platform:
            lead_qs = lead_qs.filter(
                Q(attributions__platform__iexact=platform) |
                Q(attributions__utm_source__iexact=platform)
            ).distinct()

        # Base Trial QS
        trial_qs = TrialBooking.objects.using(alias).filter(
            lead__organization=organization,
            created_at__gte=start_dt,
            created_at__lte=end_dt,
        )
        if effective_branch_ids is not None:
            trial_qs = trial_qs.filter(branch_id__in=effective_branch_ids)
        if agent_id:
            trial_qs = trial_qs.filter(lead__assigned_sales_user_id=agent_id)
        if source_id:
            trial_qs = trial_qs.filter(lead__lead_source_id=source_id)
        if program_id:
            trial_qs = trial_qs.filter(lead__interested_program_id=program_id)

        # Base Conversion QS
        conversion_qs = LeadConversion.objects.using(alias).filter(
            lead__organization=organization,
            converted_at__gte=start_dt,
            converted_at__lte=end_dt,
        )
        if effective_branch_ids is not None:
            conversion_qs = conversion_qs.filter(lead__branch_id__in=effective_branch_ids)
        if agent_id:
            conversion_qs = conversion_qs.filter(lead__assigned_sales_user_id=agent_id)
        if source_id:
            conversion_qs = conversion_qs.filter(lead__lead_source_id=source_id)
        if program_id:
            conversion_qs = conversion_qs.filter(lead__interested_program_id=program_id)

        # Base Follow-up QS
        followup_qs = SalesFollowupTask.objects.using(alias).filter(
            lead__organization=organization,
        )
        if effective_branch_ids is not None:
            followup_qs = followup_qs.filter(lead__branch_id__in=effective_branch_ids)
        if agent_id:
            followup_qs = followup_qs.filter(assigned_to_user_id=agent_id)

        # 4. Aggregations
        summary = cls._get_summary_kpis(
            organization=organization,
            lead_qs=lead_qs,
            trial_qs=trial_qs,
            conversion_qs=conversion_qs,
            followup_qs=followup_qs,
            branch_filter_id=branch_filter_id,
            db_alias=alias,
        )

        funnel = cls._get_funnel(lead_qs=lead_qs)

        sources = cls._get_source_performance(
            organization=organization,
            lead_qs=lead_qs,
            trial_qs=trial_qs,
            conversion_qs=conversion_qs,
            db_alias=alias,
        )

        campaigns = cls._get_campaign_performance(
            organization=organization,
            start_dt=start_dt,
            end_dt=end_dt,
            effective_branch_ids=effective_branch_ids,
            platform=platform,
            db_alias=alias,
        )

        trials = cls._get_trial_performance(trial_qs=trial_qs)

        followups = cls._get_followup_performance(followup_qs=followup_qs, start_dt=start_dt, end_dt=end_dt)

        attention = cls._get_attention_performance(
            organization=organization,
            branch_filter_id=branch_filter_id,
            db_alias=alias,
        )

        agents = cls._get_agent_performance(
            organization=organization,
            lead_qs=lead_qs,
            trial_qs=trial_qs,
            conversion_qs=conversion_qs,
            followup_qs=followup_qs,
            start_dt=start_dt,
            end_dt=end_dt,
            db_alias=alias,
        )

        branches = cls._get_branch_performance(
            organization=organization,
            permitted_branches=permitted_branches,
            lead_qs=lead_qs,
            trial_qs=trial_qs,
            conversion_qs=conversion_qs,
            db_alias=alias,
        )

        trends = cls._get_trends(
            start_dt=start_dt,
            end_dt=end_dt,
            lead_qs=lead_qs,
            trial_qs=trial_qs,
            conversion_qs=conversion_qs,
            db_alias=alias,
        )

        return {
            'filters': {
                'date_from': start_dt.date().isoformat(),
                'date_to': end_dt.date().isoformat(),
                'preset': preset_applied,
                'branch_id': branch_filter_id or 'ALL',
                'agent_id': agent_id or 'ALL',
                'lead_source_id': source_id or 'ALL',
                'campaign_name': campaign_name or 'ALL',
                'program_id': program_id or 'ALL',
                'platform': platform or 'ALL',
            },
            'summary': summary,
            'funnel': funnel,
            'sources': sources,
            'campaigns': campaigns,
            'trials': trials,
            'followups': followups,
            'attention': attention,
            'agents': agents,
            'branches': branches,
            'trends': trends,
        }

    # -------------------------------------------------------------------------
    # Helper: Date Range Resolution (Timezone-Aware)
    # -------------------------------------------------------------------------
    @classmethod
    def _resolve_date_range(cls, filters: Dict[str, Any]):
        preset = (filters.get('preset') or 'LAST_30_DAYS').upper()
        now = timezone.now()
        today = now.date()

        date_from_str = filters.get('date_from') or filters.get('start_date')
        date_to_str = filters.get('date_to') or filters.get('end_date')

        if preset == 'CUSTOM' or (date_from_str and date_to_str and preset != 'CUSTOM'):
            try:
                start_date = datetime.strptime(str(date_from_str).split('T')[0], '%Y-%m-%d').date()
                end_date = datetime.strptime(str(date_to_str).split('T')[0], '%Y-%m-%d').date()
                if start_date > end_date:
                    start_date, end_date = end_date, start_date
                start_dt = timezone.make_aware(datetime.combine(start_date, time.min))
                end_dt = timezone.make_aware(datetime.combine(end_date, time.max))
                return start_dt, end_dt, 'CUSTOM'
            except (ValueError, TypeError):
                preset = 'LAST_30_DAYS'

        if preset == 'TODAY':
            start_dt = timezone.make_aware(datetime.combine(today, time.min))
            end_dt = timezone.make_aware(datetime.combine(today, time.max))
        elif preset == 'YESTERDAY':
            y = today - timedelta(days=1)
            start_dt = timezone.make_aware(datetime.combine(y, time.min))
            end_dt = timezone.make_aware(datetime.combine(y, time.max))
        elif preset == 'LAST_7_DAYS':
            start_dt = timezone.make_aware(datetime.combine(today - timedelta(days=6), time.min))
            end_dt = timezone.make_aware(datetime.combine(today, time.max))
        elif preset == 'THIS_MONTH':
            first_day = today.replace(day=1)
            start_dt = timezone.make_aware(datetime.combine(first_day, time.min))
            end_dt = timezone.make_aware(datetime.combine(today, time.max))
        elif preset == 'LAST_MONTH':
            first_this_month = today.replace(day=1)
            last_day_prev = first_this_month - timedelta(days=1)
            first_day_prev = last_day_prev.replace(day=1)
            start_dt = timezone.make_aware(datetime.combine(first_day_prev, time.min))
            end_dt = timezone.make_aware(datetime.combine(last_day_prev, time.max))
        else:  # Default: LAST_30_DAYS
            preset = 'LAST_30_DAYS'
            start_dt = timezone.make_aware(datetime.combine(today - timedelta(days=29), time.min))
            end_dt = timezone.make_aware(datetime.combine(today, time.max))

        return start_dt, end_dt, preset

    # -------------------------------------------------------------------------
    # Helper: Branch Scope Enforcement
    # -------------------------------------------------------------------------
    @classmethod
    def _resolve_branch_scope(
        cls,
        user: Optional[TenantUser],
        requested_branch: Optional[str],
        organization: Organization,
        db_alias: str,
    ):
        from .views_crm import get_user_effective_branch_ids

        all_branches = list(
            Branch.objects.using(db_alias)
            .filter(organization=organization, status='ACTIVE')
            .order_by('name')
        )

        effective_branch_ids_user = get_user_effective_branch_ids(user, db_alias=db_alias) if user else None

        if effective_branch_ids_user is None:
            permitted_branches = all_branches
            permitted_ids = {str(b.id) for b in all_branches}
        else:
            permitted_branches = [b for b in all_branches if str(b.id) in effective_branch_ids_user]
            permitted_ids = {str(b.id) for b in permitted_branches}

        # If user requested a specific branch
        if requested_branch and requested_branch != 'ALL':
            req_str = str(requested_branch).strip()
            if req_str not in permitted_ids:
                # Unauthorized branch request! Refuse to leak any data
                raise PermissionDenied("You do not have access to the requested branch metrics.")
            return permitted_branches, req_str, {req_str}

        return permitted_branches, None, (permitted_ids if effective_branch_ids_user is not None else None)

    # -------------------------------------------------------------------------
    # PART C & D: Summary KPIs
    # -------------------------------------------------------------------------
    @classmethod
    def _get_summary_kpis(
        cls,
        organization: Organization,
        lead_qs,
        trial_qs,
        conversion_qs,
        followup_qs,
        branch_filter_id: Optional[str],
        db_alias: str,
    ) -> Dict[str, Any]:
        total_leads = lead_qs.count()
        new_leads = lead_qs.filter(current_status='NEW_LEAD').count()

        open_stages = [
            'NEW_LEAD',
            'TRIAL_BOOKED',
            'TRIAL_CONFIRMED',
            'TRIAL_ATTENDED',
            'NO_SHOW',
            'FOLLOW_UP_PENDING',
            'INTERESTED',
            'HOT_LEAD',
            'PAYMENT_PENDING',
        ]
        open_leads = lead_qs.filter(current_status__in=open_stages).count()

        converted_members = conversion_qs.values('lead_id').distinct().count()
        conversion_rate = round((converted_members / total_leads * 100), 1) if total_leads > 0 else 0.0

        trials_booked = trial_qs.count()
        trials_attended = trial_qs.filter(status='ATTENDED').count()
        trial_no_shows = trial_qs.filter(status='NO_SHOW').count()

        now = timezone.now()
        overdue_followups = followup_qs.filter(
            status__in=['PENDING', 'IN_PROGRESS'],
            due_at__lt=now,
        ).count()

        # Attention metrics from Phase 7 engine
        attn_metrics = LeadAttentionService.get_attention_metrics(
            organization=organization,
            branch_id=branch_filter_id,
            db_alias=db_alias,
        )
        stuck_leads = attn_metrics.get('stuck_leads', 0)

        # Revenue: Canonical commerce data
        order_ids = list(
            conversion_qs.filter(order_id__isnull=False).values_list('order_id', flat=True).distinct()
        )
        gross_rev = Decimal('0.00')
        refund_amt = Decimal('0.00')

        if order_ids:
            gross_res = Order.objects.using(db_alias).filter(
                id__in=order_ids,
                status__in=['PAID', 'PARTIALLY_REFUNDED', 'REFUNDED'],
            ).aggregate(total=Sum('total_amount'))
            gross_rev = gross_res['total'] or Decimal('0.00')

            refund_res = Refund.objects.using(db_alias).filter(
                order_id__in=order_ids,
                status='SUCCESS',
            ).aggregate(total=Sum('amount'))
            refund_amt = refund_res['total'] or Decimal('0.00')

        net_rev = max(Decimal('0.00'), gross_rev - refund_amt)

        return {
            'total_leads': total_leads,
            'new_leads': new_leads,
            'open_leads': open_leads,
            'converted_members': converted_members,
            'conversion_rate': conversion_rate,
            'trials_booked': trials_booked,
            'trials_attended': trials_attended,
            'trial_no_shows': trial_no_shows,
            'overdue_followups': overdue_followups,
            'leads_requiring_attention': stuck_leads,
            'paid_revenue': str(net_rev),
            'gross_revenue': str(gross_rev),
            'refund_amount': str(refund_amt),
        }

    # -------------------------------------------------------------------------
    # PART E: Lead Funnel
    # -------------------------------------------------------------------------
    @classmethod
    def _get_funnel(cls, lead_qs) -> List[Dict[str, Any]]:
        total_leads = lead_qs.count()
        counts_by_status = dict(
            lead_qs.values('current_status')
            .annotate(cnt=Count('id'))
            .values_list('current_status', 'cnt')
        )

        funnel = []
        prev_count = None
        for code, label in Lead.STATUSES:
            c = counts_by_status.get(code, 0)
            pct = round((c / total_leads * 100), 1) if total_leads > 0 else 0.0
            conv_prev = None
            if prev_count is not None and prev_count > 0:
                conv_prev = round((c / prev_count * 100), 1)
            prev_count = c if c > 0 else prev_count

            funnel.append({
                'status': code,
                'display_label': label,
                'count': c,
                'percentage_of_total': pct,
                'conversion_from_previous_stage': conv_prev,
            })
        return funnel

    # -------------------------------------------------------------------------
    # PART F: Source Performance
    # -------------------------------------------------------------------------
    @classmethod
    def _get_source_performance(
        cls,
        organization: Organization,
        lead_qs,
        trial_qs,
        conversion_qs,
        db_alias: str,
    ) -> List[Dict[str, Any]]:
        sources = list(
            LeadSource.objects.using(db_alias)
            .filter(organization=organization)
            .order_by('name')
        )
        source_map = {str(s.id): s for s in sources}

        lead_counts = dict(
            lead_qs.values('lead_source_id')
            .annotate(cnt=Count('id'))
            .values_list('lead_source_id', 'cnt')
        )
        trial_counts = dict(
            trial_qs.values('lead__lead_source_id')
            .annotate(cnt=Count('id'))
            .values_list('lead__lead_source_id', 'cnt')
        )

        conv_rows = list(
            conversion_qs.values('lead__lead_source_id', 'order_id')
        )
        conv_counts = {}
        order_ids_by_source = {}
        for r in conv_rows:
            sid = str(r['lead__lead_source_id']) if r['lead__lead_source_id'] else None
            conv_counts[sid] = conv_counts.get(sid, 0) + 1
            if r['order_id']:
                order_ids_by_source.setdefault(sid, set()).add(r['order_id'])

        all_order_ids = set()
        for oids in order_ids_by_source.values():
            all_order_ids.update(oids)

        paid_orders_map = {}
        if all_order_ids:
            for ord_row in Order.objects.using(db_alias).filter(id__in=all_order_ids, status='PAID').values('id', 'total_amount'):
                paid_orders_map[ord_row['id']] = ord_row['total_amount']

        results = []
        for sid_uuid, s in source_map.items():
            l_cnt = lead_counts.get(s.id, 0)
            t_cnt = trial_counts.get(s.id, 0)
            c_cnt = conv_counts.get(sid_uuid, 0)
            c_rate = round((c_cnt / l_cnt * 100), 1) if l_cnt > 0 else 0.0

            s_oids = order_ids_by_source.get(sid_uuid, set())
            rev = sum((paid_orders_map.get(oid, Decimal('0.00')) for oid in s_oids), Decimal('0.00'))

            results.append({
                'source_id': sid_uuid,
                'source_name': s.name,
                'source_type': s.source_type,
                'leads': l_cnt,
                'trials': t_cnt,
                'conversions': c_cnt,
                'conversion_rate': c_rate,
                'paid_revenue': str(rev),
            })

        # Include direct / unassigned if present
        direct_leads = lead_counts.get(None, 0)
        if direct_leads > 0:
            direct_trials = trial_counts.get(None, 0)
            direct_convs = conv_counts.get(None, 0)
            direct_rate = round((direct_convs / direct_leads * 100), 1)
            direct_oids = order_ids_by_source.get(None, set())
            direct_rev = sum((paid_orders_map.get(oid, Decimal('0.00')) for oid in direct_oids), Decimal('0.00'))
            results.append({
                'source_id': None,
                'source_name': 'Direct / Unassigned',
                'source_type': 'DIRECT',
                'leads': direct_leads,
                'trials': direct_trials,
                'conversions': direct_convs,
                'conversion_rate': direct_rate,
                'paid_revenue': str(direct_rev),
            })

        results.sort(key=lambda x: (-x['leads'], -Decimal(x['paid_revenue'])))
        return results

    # -------------------------------------------------------------------------
    # PART G: Campaign Performance (Reusing Phase 9 Semantics)
    # -------------------------------------------------------------------------
    @classmethod
    def _get_campaign_performance(
        cls,
        organization: Organization,
        start_dt,
        end_dt,
        effective_branch_ids: Optional[Set[str]],
        platform: Optional[str],
        db_alias: str,
    ) -> List[Dict[str, Any]]:
        qs = LeadAttribution.objects.using(db_alias).filter(
            organization=organization,
            captured_at__gte=start_dt,
            captured_at__lte=end_dt,
        ).select_related('lead')

        if effective_branch_ids is not None:
            qs = qs.filter(lead__branch_id__in=effective_branch_ids)
        if platform:
            qs = qs.filter(Q(platform__iexact=platform) | Q(utm_source__iexact=platform))

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
            for row in TrialBooking.objects.using(db_alias).filter(lead_id__in=all_lead_ids).values('lead_id'):
                lid = row['lead_id']
                trials_per_lead[lid] = trials_per_lead.get(lid, 0) + 1

        conversions = list(
            LeadConversion.objects.using(db_alias).filter(lead_id__in=all_lead_ids).values('id', 'lead_id', 'order_id')
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
            for ord_obj in Order.objects.using(db_alias).filter(id__in=order_ids, status='PAID').values('id', 'total_amount'):
                paid_orders_map[ord_obj['id']] = ord_obj['total_amount']

        results = []
        for (camp_name, plat), data in campaign_map.items():
            l_ids = data['lead_ids']
            leads_count = len(l_ids)
            trials_count = sum(trials_per_lead.get(lid, 0) for lid in l_ids)
            conv_count = sum(conversions_per_lead.get(lid, 0) for lid in l_ids)
            c_rate = round((conv_count / leads_count * 100), 1) if leads_count > 0 else 0.0

            camp_order_ids = {conv['order_id'] for conv in conversions if conv['lead_id'] in l_ids and conv['order_id']}
            camp_revenue = sum((paid_orders_map.get(oid, Decimal('0.00')) for oid in camp_order_ids), Decimal('0.00'))

            results.append({
                'id': f"{camp_name}::{plat}",
                'campaign_name': camp_name,
                'platform': plat,
                'leads': leads_count,
                'trials': trials_count,
                'conversions': conv_count,
                'conversion_rate': c_rate,
                'paid_revenue': str(camp_revenue),
            })

        results.sort(key=lambda x: (-x['leads'], -Decimal(x['paid_revenue'])))
        return results

    # -------------------------------------------------------------------------
    # PART H: Trial Performance
    # -------------------------------------------------------------------------
    @classmethod
    def _get_trial_performance(cls, trial_qs) -> Dict[str, Any]:
        booked = trial_qs.count()
        status_counts = dict(
            trial_qs.values('status')
            .annotate(cnt=Count('id'))
            .values_list('status', 'cnt')
        )

        attended = status_counts.get('ATTENDED', 0)
        confirmed = status_counts.get('CONFIRMED', 0)
        no_show = status_counts.get('NO_SHOW', 0)
        cancelled = status_counts.get('CANCELLED', 0)

        # Converted after trial: trial attended AND lead is converted
        converted_after_trial = trial_qs.filter(status='ATTENDED', lead__current_status='CONVERTED').count()
        attendance_rate = round((attended / (attended + no_show) * 100), 1) if (attended + no_show) > 0 else 0.0

        # Program breakdown
        by_program = list(
            trial_qs.filter(lead__interested_program__isnull=False)
            .values('lead__interested_program_id', 'lead__interested_program__name')
            .annotate(count=Count('id'))
            .order_by('-count')[:5]
        )
        programs_list = [
            {'program_id': str(p['lead__interested_program_id']), 'program_name': p['lead__interested_program__name'], 'count': p['count']}
            for p in by_program
        ]

        return {
            'booked': booked,
            'confirmed': confirmed,
            'attended': attended,
            'no_show': no_show,
            'cancelled': cancelled,
            'converted_after_trial': converted_after_trial,
            'attendance_rate': attendance_rate,
            'top_programs': programs_list,
        }

    # -------------------------------------------------------------------------
    # PART I: Follow-up Performance
    # -------------------------------------------------------------------------
    @classmethod
    def _get_followup_performance(cls, followup_qs, start_dt, end_dt) -> Dict[str, Any]:
        now = timezone.now()
        today = now.date()
        today_start = timezone.make_aware(datetime.combine(today, time.min))
        today_end = timezone.make_aware(datetime.combine(today, time.max))

        due_today = followup_qs.filter(
            status__in=['PENDING', 'IN_PROGRESS'],
            due_at__range=(today_start, today_end),
        ).count()

        overdue = followup_qs.filter(
            status__in=['PENDING', 'IN_PROGRESS'],
            due_at__lt=now,
        ).count()

        upcoming = followup_qs.filter(
            status__in=['PENDING', 'IN_PROGRESS'],
            due_at__gt=today_end,
        ).count()

        completed = followup_qs.filter(
            status='COMPLETED',
            created_at__gte=start_dt,
            created_at__lte=end_dt,
        ).count()

        total_pending = due_today + overdue + upcoming
        denom = completed + overdue
        completion_rate = round((completed / denom * 100), 1) if denom > 0 else 100.0

        return {
            'due_today': due_today,
            'overdue': overdue,
            'upcoming': upcoming,
            'completed': completed,
            'total_pending': total_pending,
            'completion_rate': completion_rate,
        }

    # -------------------------------------------------------------------------
    # PART J: Attention Performance
    # -------------------------------------------------------------------------
    @classmethod
    def _get_attention_performance(
        cls,
        organization: Organization,
        branch_filter_id: Optional[str],
        db_alias: str,
    ) -> Dict[str, Any]:
        metrics = LeadAttentionService.get_attention_metrics(
            organization=organization,
            branch_id=branch_filter_id,
            db_alias=db_alias,
        )
        return {
            'stuck_leads': metrics.get('stuck_leads', 0),
            'sla_breached': metrics.get('sla_breached', 0),
            'overdue_tasks': metrics.get('overdue_tasks', 0),
            'awaiting_response': metrics.get('awaiting_response', 0),
            'total_active_leads': metrics.get('total_active_leads', 0),
        }

    # -------------------------------------------------------------------------
    # PART K: Agent Performance
    # -------------------------------------------------------------------------
    @classmethod
    def _get_agent_performance(
        cls,
        organization: Organization,
        lead_qs,
        trial_qs,
        conversion_qs,
        followup_qs,
        start_dt,
        end_dt,
        db_alias: str,
    ) -> List[Dict[str, Any]]:
        # Find all agents who have leads assigned or are active staff
        agent_lead_counts = dict(
            lead_qs.filter(assigned_sales_user__isnull=False)
            .values('assigned_sales_user_id')
            .annotate(cnt=Count('id'))
            .values_list('assigned_sales_user_id', 'cnt')
        )

        agent_ids = set(agent_lead_counts.keys())

        # Also get agents with activities in period
        act_counts = dict(
            LeadActivity.objects.using(db_alias)
            .filter(lead__organization=organization, created_at__gte=start_dt, created_at__lte=end_dt)
            .values('performed_by_user_id')
            .annotate(cnt=Count('id'))
            .values_list('performed_by_user_id', 'cnt')
        )
        agent_ids.update(act_counts.keys())

        # Filter out None
        agent_ids = {aid for aid in agent_ids if aid is not None}
        if not agent_ids:
            return []

        users = list(
            TenantUser.objects.using(db_alias)
            .filter(id__in=agent_ids)
            .select_related('profile')
        )
        user_map = {u.id: u for u in users}

        trial_counts = dict(
            trial_qs.filter(lead__assigned_sales_user__isnull=False)
            .values('lead__assigned_sales_user_id')
            .annotate(cnt=Count('id'))
            .values_list('lead__assigned_sales_user_id', 'cnt')
        )

        conv_rows = list(
            conversion_qs.filter(lead__assigned_sales_user__isnull=False)
            .values('lead__assigned_sales_user_id', 'order_id')
        )
        conv_counts = {}
        order_ids_by_agent = {}
        for r in conv_rows:
            uid = r['lead__assigned_sales_user_id']
            conv_counts[uid] = conv_counts.get(uid, 0) + 1
            if r['order_id']:
                order_ids_by_agent.setdefault(uid, set()).add(r['order_id'])

        completed_tasks = dict(
            followup_qs.filter(status='COMPLETED', updated_at__gte=start_dt, updated_at__lte=end_dt)
            .values('assigned_to_user_id')
            .annotate(cnt=Count('id'))
            .values_list('assigned_to_user_id', 'cnt')
        )

        all_oids = set()
        for oids in order_ids_by_agent.values():
            all_oids.update(oids)

        paid_orders_map = {}
        if all_oids:
            for ord_row in Order.objects.using(db_alias).filter(id__in=all_oids, status='PAID').values('id', 'total_amount'):
                paid_orders_map[ord_row['id']] = ord_row['total_amount']

        results = []
        for uid in agent_ids:
            u = user_map.get(uid)
            name = u.get_full_name() if u and hasattr(u, 'get_full_name') and u.get_full_name().strip() else (u.email if u else 'Sales Agent')
            l_cnt = agent_lead_counts.get(uid, 0)
            t_cnt = trial_counts.get(uid, 0)
            c_cnt = conv_counts.get(uid, 0)
            act_cnt = act_counts.get(uid, 0)
            f_cnt = completed_tasks.get(uid, 0)

            a_oids = order_ids_by_agent.get(uid, set())
            rev = sum((paid_orders_map.get(oid, Decimal('0.00')) for oid in a_oids), Decimal('0.00'))

            results.append({
                'agent_id': str(uid),
                'agent_name': name,
                'email': u.email if u else '',
                'assigned_leads': l_cnt,
                'activities': act_cnt,
                'completed_followups': f_cnt,
                'trials_booked': t_cnt,
                'conversions': c_cnt,
                'paid_revenue': str(rev),
            })

        results.sort(key=lambda x: (-x['assigned_leads'], -x['conversions'], -Decimal(x['paid_revenue'])))
        return results

    # -------------------------------------------------------------------------
    # PART L: Branch Performance
    # -------------------------------------------------------------------------
    @classmethod
    def _get_branch_performance(
        cls,
        organization: Organization,
        permitted_branches: List[Branch],
        lead_qs,
        trial_qs,
        conversion_qs,
        db_alias: str,
    ) -> List[Dict[str, Any]]:
        lead_counts = dict(
            lead_qs.values('branch_id')
            .annotate(cnt=Count('id'))
            .values_list('branch_id', 'cnt')
        )
        trial_counts = dict(
            trial_qs.values('branch_id')
            .annotate(cnt=Count('id'))
            .values_list('branch_id', 'cnt')
        )

        conv_rows = list(
            conversion_qs.values('lead__branch_id', 'order_id')
        )
        conv_counts = {}
        order_ids_by_branch = {}
        for r in conv_rows:
            bid = r['lead__branch_id']
            conv_counts[bid] = conv_counts.get(bid, 0) + 1
            if r['order_id']:
                order_ids_by_branch.setdefault(bid, set()).add(r['order_id'])

        all_oids = set()
        for oids in order_ids_by_branch.values():
            all_oids.update(oids)

        paid_orders_map = {}
        if all_oids:
            for ord_row in Order.objects.using(db_alias).filter(id__in=all_oids, status='PAID').values('id', 'total_amount'):
                paid_orders_map[ord_row['id']] = ord_row['total_amount']

        results = []
        for b in permitted_branches:
            l_cnt = lead_counts.get(b.id, 0)
            t_cnt = trial_counts.get(b.id, 0)
            c_cnt = conv_counts.get(b.id, 0)
            c_rate = round((c_cnt / l_cnt * 100), 1) if l_cnt > 0 else 0.0

            b_oids = order_ids_by_branch.get(b.id, set())
            rev = sum((paid_orders_map.get(oid, Decimal('0.00')) for oid in b_oids), Decimal('0.00'))

            results.append({
                'branch_id': str(b.id),
                'branch_name': b.name,
                'code': b.code,
                'leads': l_cnt,
                'trials': t_cnt,
                'conversions': c_cnt,
                'conversion_rate': c_rate,
                'paid_revenue': str(rev),
            })

        results.sort(key=lambda x: (-x['leads'], -Decimal(x['paid_revenue'])))
        return results

    # -------------------------------------------------------------------------
    # PART P: Trend Analytics (Continuous Time Series Buckets)
    # -------------------------------------------------------------------------
    @classmethod
    def _get_trends(
        cls,
        start_dt,
        end_dt,
        lead_qs,
        trial_qs,
        conversion_qs,
        db_alias: str,
    ) -> List[Dict[str, Any]]:
        # TruncDate aggregation across date bounds
        lead_trend = dict(
            lead_qs.annotate(d=TruncDate('created_at'))
            .values('d')
            .annotate(cnt=Count('id'))
            .values_list('d', 'cnt')
        )

        trial_trend = dict(
            trial_qs.annotate(d=TruncDate('created_at'))
            .values('d')
            .annotate(cnt=Count('id'))
            .values_list('d', 'cnt')
        )

        conv_rows = list(
            conversion_qs.annotate(d=TruncDate('converted_at'))
            .values('d', 'order_id')
        )
        conv_trend = {}
        order_ids_by_date = {}
        for r in conv_rows:
            d = r['d']
            conv_trend[d] = conv_trend.get(d, 0) + 1
            if r['order_id']:
                order_ids_by_date.setdefault(d, set()).add(r['order_id'])

        all_oids = set()
        for oids in order_ids_by_date.values():
            all_oids.update(oids)

        paid_orders_map = {}
        if all_oids:
            for ord_row in Order.objects.using(db_alias).filter(id__in=all_oids, status='PAID').values('id', 'total_amount'):
                paid_orders_map[ord_row['id']] = ord_row['total_amount']

        # Generate complete continuous daily array
        trends = []
        cur_date = start_dt.date()
        end_date = end_dt.date()
        while cur_date <= end_date:
            d_oids = order_ids_by_date.get(cur_date, set())
            d_rev = sum((paid_orders_map.get(oid, Decimal('0.00')) for oid in d_oids), Decimal('0.00'))

            trends.append({
                'date': cur_date.isoformat(),
                'display_date': cur_date.strftime('%b %d'),
                'leads': lead_trend.get(cur_date, 0),
                'trials': trial_trend.get(cur_date, 0),
                'conversions': conv_trend.get(cur_date, 0),
                'paid_revenue': float(d_rev),
            })
            cur_date += timedelta(days=1)

        return trends
