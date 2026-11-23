"""
apps/tenant_core/services_lead_transfer.py — Bulk Lead and Pipeline Transfer Services

Provides production-grade, transaction-safe lead handover and pipeline reassignment:
1. Dynamic Source Representatives Discovery (Active & Deactivated Staff with Pipeline)
2. Real-time Transfer Scope Preview (Counts, Temperatures, Trials, Tasks, Sample Leads)
3. Transactional Transfer Execution (SINGLE, ROUND_ROBIN with weights, LEAST_BUSY)
4. Preservation of Historical Closer Credit (LeadConversion & Order data untouched)
5. Preservation of Trial Booking integrity and Follow-up Task due dates
6. Offboarding Safety (Auto-assignment exclusion & Meta Lead Ad form auto-switching)
7. Audit Trail logging (LeadTransferBatch) & Instant Undo / Rollback capability
8. Aggregated In-App Notifications & Outbox Events
"""

import logging
import uuid
from decimal import Decimal
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
from django.db import models, transaction
from django.db.models import Q, Count
from django.utils import timezone
from django.core.exceptions import ValidationError

from .context import get_tenant_db_alias
from .models_org import Organization, Branch
from .models_users import TenantUser
from .models_rbac import RoleAssignment
from .models_crm import (
    Lead,
    LeadAssignment,
    LeadActivity,
    TrialBooking,
    SalesFollowupTask,
    CRMAgentAssignmentConfig,
    LeadTransferBatch,
)
from .models_meta_leads import MetaLeadMapping
from .models_govern import InAppNotification
from .services_reliability import enqueue_outbox_event, record_business_audit

logger = logging.getLogger(__name__)

TERMINAL_LEAD_STATUSES = ('CONVERTED', 'NOT_INTERESTED', 'LOST')


def get_user_display_name(user: Optional[TenantUser]) -> str:
    """Returns human-friendly display name for a TenantUser."""
    if not user:
        return 'Unassigned'
    full = getattr(user, 'full_name', None)
    if full and str(full).strip():
        return str(full).strip()
    first = getattr(user, 'first_name', '') or ''
    last = getattr(user, 'last_name', '') or ''
    combined = f"{first} {last}".strip()
    return combined or getattr(user, 'email', '') or 'User'


def mask_phone(phone: Optional[str]) -> str:
    """Masks middle/last digits of a phone number for privacy display."""
    if not phone:
        return '—'
    raw = str(phone).strip()
    if len(raw) <= 5:
        return raw
    return f"{raw[:5]}*****{raw[-2:]}" if len(raw) >= 7 else f"{raw[:3]}***"


def get_lead_temperature(lead: Lead) -> str:
    """Computes HOT / WARM / COLD temperature based on lead current_status."""
    status_val = getattr(lead, 'current_status', '') or ''
    if status_val in ('HOT_LEAD', 'PAYMENT_PENDING'):
        return 'HOT'
    if status_val in ('NEW_LEAD', 'NO_SHOW'):
        return 'COLD'
    return 'WARM'


def get_transfer_source_reps(
    organization: Organization,
    db_alias: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Returns all representatives who currently own leads or have pipeline history,
    including both active staff and deactivated/leaving representatives.
    """
    alias = db_alias or get_tenant_db_alias() or 'default'

    user_ids_with_leads = set(
        Lead.objects.using(alias)
        .filter(organization=organization, assigned_sales_user__isnull=False)
        .values_list('assigned_sales_user_id', flat=True)
        .distinct()
    )

    user_ids_with_tasks = set(
        SalesFollowupTask.objects.using(alias)
        .filter(lead__organization=organization, assigned_to_user__isnull=False, status__in=['PENDING', 'IN_PROGRESS'])
        .values_list('assigned_to_user_id', flat=True)
        .distinct()
    )

    staff_user_ids = set(
        TenantUser.objects.using(alias)
        .filter(organization=organization, user_type='STAFF')
        .values_list('id', flat=True)
    )

    all_candidate_ids = user_ids_with_leads | user_ids_with_tasks | staff_user_ids
    if not all_candidate_ids:
        return []

    users = (
        TenantUser.objects.using(alias)
        .filter(organization=organization, id__in=all_candidate_ids)
        .select_related('home_branch')
        .prefetch_related('role_assignments__role')
        .order_by('first_name', 'last_name')
    )

    meta_pinned_user_ids = set(
        MetaLeadMapping.objects.using(alias)
        .filter(organization=organization, assigned_sales_user__isnull=False, is_active=True)
        .values_list('assigned_sales_user_id', flat=True)
        .distinct()
    )

    results: List[Dict[str, Any]] = []

    for user in users:
        is_deactivated = (user.status != 'ACTIVE' or not user.is_login_allowed or not getattr(user, 'is_active', True))

        user_leads = Lead.objects.using(alias).filter(organization=organization, assigned_sales_user=user)
        total_leads = user_leads.count()
        converted_leads = user_leads.filter(current_status='CONVERTED').count()
        lost_leads = user_leads.filter(current_status__in=['NOT_INTERESTED', 'LOST']).count()
        active_leads = total_leads - converted_leads - lost_leads

        hot_leads = user_leads.filter(current_status__in=['HOT_LEAD', 'PAYMENT_PENDING']).count()
        cold_leads = user_leads.filter(current_status__in=['NEW_LEAD', 'NO_SHOW']).count()
        warm_leads = max(0, active_leads - hot_leads - cold_leads)

        trials_count = TrialBooking.objects.using(alias).filter(
            lead__organization=organization,
            lead__assigned_sales_user=user,
        ).count()

        tasks_count = SalesFollowupTask.objects.using(alias).filter(
            lead__organization=organization,
            assigned_to_user=user,
            status__in=['PENDING', 'IN_PROGRESS'],
        ).count()

        role_title = 'Sales & CRM Representative'
        active_roles = [ra.role.name for ra in user.role_assignments.all() if ra.role and ra.status == 'ACTIVE']
        if active_roles:
            role_title = active_roles[0]
        elif user.user_type:
            role_title = user.user_type.replace('_', ' ').title()

        results.append({
            'id': str(user.id),
            'name': get_user_display_name(user),
            'email': user.email,
            'role': role_title,
            'branch_id': str(user.home_branch_id) if user.home_branch_id else None,
            'branch_name': user.home_branch.name if user.home_branch else 'All Branches',
            'is_deactivated': is_deactivated,
            'lead_count': total_leads,
            'active_lead_count': active_leads,
            'converted_lead_count': converted_leads,
            'lost_lead_count': lost_leads,
            'trial_count': trials_count,
            'task_count': tasks_count,
            'hot_count': hot_leads,
            'warm_count': warm_leads,
            'cold_count': cold_leads,
            'has_meta_ads': user.id in meta_pinned_user_ids,
        })

    results.sort(key=lambda r: (-r['lead_count'], not r['is_deactivated'], r['name'].lower()))

    # Include Unassigned Leads Pool at top if there are unassigned leads in the organization
    unassigned_qs = Lead.objects.using(alias).filter(organization=organization, assigned_sales_user__isnull=True)
    unassigned_total = unassigned_qs.count()
    if unassigned_total > 0:
        unassigned_converted = unassigned_qs.filter(current_status='CONVERTED').count()
        unassigned_lost = unassigned_qs.filter(current_status__in=['NOT_INTERESTED', 'LOST']).count()
        unassigned_active = unassigned_total - unassigned_converted - unassigned_lost
        unassigned_hot = unassigned_qs.filter(current_status__in=['HOT_LEAD', 'PAYMENT_PENDING']).count()
        unassigned_cold = unassigned_qs.filter(current_status__in=['NEW_LEAD', 'NO_SHOW']).count()
        unassigned_warm = max(0, unassigned_active - unassigned_hot - unassigned_cold)

        unassigned_trials = TrialBooking.objects.using(alias).filter(
            lead__organization=organization,
            lead__assigned_sales_user__isnull=True,
        ).count()
        unassigned_tasks = SalesFollowupTask.objects.using(alias).filter(
            lead__organization=organization,
            lead__assigned_sales_user__isnull=True,
            status__in=['PENDING', 'IN_PROGRESS'],
        ).count()

        results.insert(0, {
            'id': 'unassigned',
            'name': 'Unassigned Leads Pool',
            'email': '',
            'role': 'Unassigned Pool',
            'branch_id': None,
            'branch_name': 'All Branches',
            'is_deactivated': False,
            'lead_count': unassigned_total,
            'active_lead_count': unassigned_active,
            'converted_lead_count': unassigned_converted,
            'lost_lead_count': unassigned_lost,
            'trial_count': unassigned_trials,
            'task_count': unassigned_tasks,
            'hot_count': unassigned_hot,
            'warm_count': unassigned_warm,
            'cold_count': unassigned_cold,
            'has_meta_ads': False,
        })

    return results


def preview_transfer(
    organization: Organization,
    source_user_id: Optional[str] = None,
    lead_ids: Optional[List[str]] = None,
    is_all_matching: bool = False,
    branch_id: Optional[str] = None,
    stage_filter: Optional[str] = None,
    include_converted: bool = True,
    include_lost: bool = True,
    db_alias: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Computes real-time lead counts, pipeline composition, trial bookings,
    pending follow-ups, and sample leads for the preview drawer.
    """
    alias = db_alias or get_tenant_db_alias() or 'default'

    qs = Lead.objects.using(alias).filter(organization=organization)

    if lead_ids and not is_all_matching:
        qs = qs.filter(id__in=lead_ids)
    elif source_user_id:
        if str(source_user_id).lower() == 'unassigned':
            qs = qs.filter(assigned_sales_user__isnull=True)
        else:
            qs = qs.filter(assigned_sales_user_id=source_user_id)

    if branch_id and branch_id != 'ALL':
        qs = qs.filter(branch_id=branch_id)

    if stage_filter and stage_filter != 'ALL':
        if stage_filter == 'OPEN':
            qs = qs.exclude(current_status__in=TERMINAL_LEAD_STATUSES)
        elif stage_filter == 'CONVERTED':
            qs = qs.filter(current_status='CONVERTED')
        elif stage_filter == 'LOST':
            qs = qs.filter(current_status__in=['NOT_INTERESTED', 'LOST'])
        else:
            qs = qs.filter(current_status=stage_filter)

    if not include_converted:
        qs = qs.exclude(current_status='CONVERTED')

    if not include_lost:
        qs = qs.exclude(current_status__in=['NOT_INTERESTED', 'LOST'])

    total_leads = qs.count()
    converted_leads = qs.filter(current_status='CONVERTED').count()
    lost_leads = qs.filter(current_status__in=['NOT_INTERESTED', 'LOST']).count()
    active_leads = total_leads - converted_leads - lost_leads

    hot_leads = qs.filter(current_status__in=['HOT_LEAD', 'PAYMENT_PENDING']).count()
    cold_leads = qs.filter(current_status__in=['NEW_LEAD', 'NO_SHOW']).count()
    warm_leads = max(0, active_leads - hot_leads - cold_leads)

    trials_count = TrialBooking.objects.using(alias).filter(
        lead__in=qs,
    ).count()

    tasks_count = SalesFollowupTask.objects.using(alias).filter(
        lead__in=qs,
        status__in=['PENDING', 'IN_PROGRESS'],
    ).count()

    has_meta_ads = False
    if source_user_id and str(source_user_id).lower() != 'unassigned':
        has_meta_ads = MetaLeadMapping.objects.using(alias).filter(
            organization=organization,
            assigned_sales_user_id=source_user_id,
            is_active=True,
        ).exists()

    sample_leads_qs = (
        qs.select_related('branch')
        .order_by('-created_at')[:25]
    )

    sample_leads = []
    for lead in sample_leads_qs:
        trial = TrialBooking.objects.using(alias).filter(lead=lead).order_by('-scheduled_start').first()
        trial_status = None
        if trial:
            trial_status = trial.status.title() if trial.status else 'Booked'

        phone_val = getattr(lead, 'phone_normalized', None)
        sample_leads.append({
            'id': str(lead.id),
            'name': f"{lead.first_name or ''} {lead.last_name or ''}".strip() or "Prospect",
            'phone': mask_phone(phone_val),
            'branch': lead.branch.name if lead.branch else 'All Branches',
            'status': lead.current_status,
            'temperature': get_lead_temperature(lead),
            'trialStatus': trial_status,
        })

    return {
        'total_leads': total_leads,
        'active_leads': active_leads,
        'converted_leads': converted_leads,
        'lost_leads': lost_leads,
        'hot_leads': hot_leads,
        'warm_leads': warm_leads,
        'cold_leads': cold_leads,
        'trials_count': trials_count,
        'followups_count': tasks_count,
        'has_meta_ads': has_meta_ads,
        'sample_leads': sample_leads,
    }


def execute_transfer(
    organization: Organization,
    actor_user: TenantUser,
    source_user_id: Optional[str] = None,
    lead_ids: Optional[List[str]] = None,
    is_all_matching: bool = False,
    target_user_ids: Optional[List[str]] = None,
    transfer_mode: str = 'ROUND_ROBIN',
    custom_weights: Optional[Dict[str, float]] = None,
    branch_id: Optional[str] = None,
    stage_filter: Optional[str] = None,
    include_converted: bool = True,
    include_lost: bool = True,
    stop_auto_assign: bool = True,
    auto_switch_meta: bool = True,
    reason: str = '',
    db_alias: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Executes an atomic, concurrency-safe bulk transfer of leads, scheduled trials,
    and pending follow-up tasks from a source rep or selection to target representatives.
    """
    alias = db_alias or get_tenant_db_alias() or 'default'

    if not target_user_ids:
        raise ValidationError({'target_user_ids': 'At least one target recipient must be selected.'})

    target_users = list(
        TenantUser.objects.using(alias)
        .filter(organization=organization, id__in=target_user_ids, status='ACTIVE', is_login_allowed=True)
    )
    if not target_users:
        raise ValidationError({'target_user_ids': 'No active recipient representatives found.'})

    target_users_map = {str(u.id): u for u in target_users}
    ordered_target_ids = [uid for uid in target_user_ids if uid in target_users_map]
    if not ordered_target_ids:
        raise ValidationError({'target_user_ids': 'Selected recipients are not eligible or active.'})

    # Validate eligibility of each recipient against CRM setup policies (allowed_role_codes, excluded_user_ids, availability)
    from .services_lead_assignment import LeadAssignmentEligibilityService
    target_branch = Branch.objects.using(alias).filter(id=branch_id, organization=organization).first() if (branch_id and branch_id != 'ALL') else None
    for target_user in target_users:
        is_valid, validation_err = LeadAssignmentEligibilityService.validate_assignee_eligibility(
            organization=organization,
            user=target_user,
            branch=target_branch,
            db_alias=alias,
        )
        if not is_valid:
            raise ValidationError({
                'target_user_ids': f"Recipient '{get_user_display_name(target_user)}' is not eligible for lead assignment: {validation_err}"
            })

    source_user = None
    source_user_name = ''
    if source_user_id:
        if str(source_user_id).lower() == 'unassigned':
            source_user_name = 'Unassigned Leads Pool'
        else:
            source_user = TenantUser.objects.using(alias).filter(organization=organization, id=source_user_id).first()
            if source_user:
                source_user_name = get_user_display_name(source_user)

    with transaction.atomic(using=alias):
        # 1. Lock and fetch all qualifying leads
        qs = Lead.objects.using(alias).filter(organization=organization)
        if lead_ids and not is_all_matching:
            qs = qs.filter(id__in=lead_ids)
        elif source_user_id:
            if str(source_user_id).lower() == 'unassigned':
                qs = qs.filter(assigned_sales_user__isnull=True)
            else:
                qs = qs.filter(assigned_sales_user_id=source_user_id)

        if branch_id and branch_id != 'ALL':
            qs = qs.filter(branch_id=branch_id)

        if stage_filter and stage_filter != 'ALL':
            if stage_filter == 'OPEN':
                qs = qs.exclude(current_status__in=TERMINAL_LEAD_STATUSES)
            elif stage_filter == 'CONVERTED':
                qs = qs.filter(current_status='CONVERTED')
            elif stage_filter == 'LOST':
                qs = qs.filter(current_status__in=['NOT_INTERESTED', 'LOST'])
            else:
                qs = qs.filter(current_status=stage_filter)

        if not include_converted:
            qs = qs.exclude(current_status='CONVERTED')

        if not include_lost:
            qs = qs.exclude(current_status__in=['NOT_INTERESTED', 'LOST'])

        lead_ids_qualifying = list(qs.values_list('id', flat=True))
        if not lead_ids_qualifying:
            return {
                'success': True,
                'batch_id': None,
                'transferred_leads_count': 0,
                'transferred_trials_count': 0,
                'transferred_followups_count': 0,
                'message': 'No leads matched the transfer criteria.',
                'target_summary': [],
            }

        leads = list(Lead.objects.using(alias).filter(id__in=lead_ids_qualifying).select_for_update())

        # 2. Build distribution schedule
        target_allocation: Dict[str, TenantUser] = {}
        target_counts: Dict[str, int] = {uid: 0 for uid in ordered_target_ids}

        if transfer_mode == 'SINGLE' or len(ordered_target_ids) == 1:
            primary_target = target_users_map[ordered_target_ids[0]]
            for lead in leads:
                target_allocation[str(lead.id)] = primary_target
                target_counts[ordered_target_ids[0]] += 1

        elif transfer_mode == 'LEAST_BUSY':
            current_active_counts = {
                uid: Lead.objects.using(alias)
                .filter(organization=organization, assigned_sales_user_id=uid)
                .exclude(current_status__in=TERMINAL_LEAD_STATUSES)
                .count()
                for uid in ordered_target_ids
            }
            for lead in leads:
                min_rep_id = min(ordered_target_ids, key=lambda uid: current_active_counts[uid] + target_counts[uid])
                target_allocation[str(lead.id)] = target_users_map[min_rep_id]
                target_counts[min_rep_id] += 1

        else:
            if custom_weights and any(custom_weights.get(uid, 0) > 0 for uid in ordered_target_ids):
                total_weight = sum(custom_weights.get(uid, 0) for uid in ordered_target_ids) or 100.0
                target_quotas = {
                    uid: int(round(len(leads) * (custom_weights.get(uid, 0) / total_weight)))
                    for uid in ordered_target_ids
                }
                deficit = len(leads) - sum(target_quotas.values())
                if deficit != 0:
                    target_quotas[ordered_target_ids[0]] += deficit

                rep_idx = 0
                for lead in leads:
                    while rep_idx < len(ordered_target_ids) and target_counts[ordered_target_ids[rep_idx]] >= target_quotas[ordered_target_ids[rep_idx]]:
                        rep_idx += 1
                    if rep_idx >= len(ordered_target_ids):
                        rep_idx = 0
                    chosen_rep_id = ordered_target_ids[rep_idx]
                    target_allocation[str(lead.id)] = target_users_map[chosen_rep_id]
                    target_counts[chosen_rep_id] += 1
            else:
                for idx, lead in enumerate(leads):
                    chosen_rep_id = ordered_target_ids[idx % len(ordered_target_ids)]
                    target_allocation[str(lead.id)] = target_users_map[chosen_rep_id]
                    target_counts[chosen_rep_id] += 1

        # 3. Snapshot previous state for audit & 100% reliable rollback
        previous_assignments_snapshot: Dict[str, Any] = {}
        affected_lead_ids: List[str] = []
        now = timezone.now()

        for lead in leads:
            lid_str = str(lead.id)
            affected_lead_ids.append(lid_str)
            target_user = target_allocation[lid_str]

            previous_assignments_snapshot[lid_str] = {
                'previous_sales_user_id': str(lead.assigned_sales_user_id) if lead.assigned_sales_user_id else None,
                'target_sales_user_id': str(target_user.id),
            }

            # Deactivate prior active assignment record
            prior_assign = LeadAssignment.objects.using(alias).filter(
                lead=lead,
                status='ACTIVE',
            ).first()
            if prior_assign:
                prior_assign.status = 'INACTIVE'
                prior_assign.unassigned_at = now
                prior_assign.save(using=alias, update_fields=['status', 'unassigned_at', 'updated_at'])

            # Create new LeadAssignment record
            LeadAssignment.objects.using(alias).create(
                lead=lead,
                assigned_to_user=target_user,
                assigned_by_user=actor_user,
                assignment_type='SALES',
                assignment_source='MANUAL',
                assignment_strategy=f"HANDOVER_{transfer_mode}",
                branch=lead.branch,
                previous_assignment=prior_assign,
                status='ACTIVE',
                reason=f"Handover transfer from {source_user_name or 'Previous Rep'}. Mode: {transfer_mode}. Reason: {reason or 'RM Leaving'}",
            )

            # Update lead
            lead.assigned_sales_user = target_user
            lead.save(using=alias, update_fields=['assigned_sales_user', 'updated_at'])

            # Log LeadActivity timeline event
            LeadActivity.objects.using(alias).create(
                lead=lead,
                activity_type='OTHER',
                outcome='REASSIGNED',
                notes=f"Pipeline Handover: Reassigned to {get_user_display_name(target_user)} ({target_user.email}) via {transfer_mode}. Reason: {reason or 'RM Leaving'}",
                performed_by_user=actor_user,
            )

        # 4. Reassign Follow-up Tasks (Pending & In Progress)
        tasks_filter = Q(lead_id__in=affected_lead_ids)
        if source_user_id and str(source_user_id).lower() != 'unassigned':
            tasks_filter |= Q(assigned_to_user_id=source_user_id)

        task_ids_to_reassign = list(
            SalesFollowupTask.objects.using(alias)
            .filter(tasks_filter, status__in=['PENDING', 'IN_PROGRESS'])
            .values_list('id', flat=True)
        )

        tasks_qs = list(
            SalesFollowupTask.objects.using(alias)
            .filter(id__in=task_ids_to_reassign)
            .select_for_update()
        )

        followups_transferred_count = 0
        for task in tasks_qs:
            task_lead_id = str(task.lead_id) if task.lead_id else None
            new_assignee = target_allocation.get(task_lead_id) if (task_lead_id and task_lead_id in target_allocation) else target_users_map[ordered_target_ids[0]]
            if new_assignee:
                task.assigned_to_user = new_assignee
                task.save(using=alias, update_fields=['assigned_to_user', 'updated_at'])
                followups_transferred_count += 1

        # 5. Count linked trial bookings transferred
        trials_count = TrialBooking.objects.using(alias).filter(
            lead__in=leads,
        ).count()

        # 6. Offboarding Safety: Exclude leaving RM from future auto-assignments
        if stop_auto_assign and source_user_id and str(source_user_id).lower() != 'unassigned':
            config = CRMAgentAssignmentConfig.objects.using(alias).filter(organization=organization).first()
            if config:
                curr_excluded = list(config.excluded_user_ids or [])
                if source_user_id not in curr_excluded:
                    curr_excluded.append(source_user_id)
                    config.excluded_user_ids = curr_excluded
                    config.save(using=alias, update_fields=['excluded_user_ids', 'updated_at'])

        # 7. Offboarding Safety: Auto-switch pinned Meta Lead Ad forms
        if auto_switch_meta and source_user_id and str(source_user_id).lower() != 'unassigned' and ordered_target_ids:
            primary_target = target_users_map[ordered_target_ids[0]]
            MetaLeadMapping.objects.using(alias).filter(
                organization=organization,
                assigned_sales_user_id=source_user_id,
            ).update(
                assigned_sales_user=primary_target,
                updated_at=now,
            )

        # 8. Create Audit Record: LeadTransferBatch
        target_summary = [
            {
                'user_id': uid,
                'user_name': get_user_display_name(target_users_map[uid]),
                'leads_count': target_counts[uid],
                'percentage': round((target_counts[uid] / len(leads)) * 100, 1) if leads else 0,
            }
            for uid in ordered_target_ids
            if target_counts[uid] > 0
        ]

        batch = LeadTransferBatch.objects.using(alias).create(
            organization=organization,
            source_user=source_user,
            source_user_name=source_user_name or 'Unassigned',
            target_users_summary=target_summary,
            transfer_mode=transfer_mode,
            leads_count=len(leads),
            trials_count=trials_count,
            followups_count=followups_transferred_count,
            filter_criteria={
                'branch_id': branch_id,
                'stage_filter': stage_filter,
                'include_converted': include_converted,
                'include_lost': include_lost,
                'is_all_matching': is_all_matching,
            },
            affected_lead_ids=affected_lead_ids,
            previous_assignments_snapshot=previous_assignments_snapshot,
            reason=reason or 'RM Leaving handover',
            performed_by_user=actor_user,
            status='COMPLETED',
        )

        # 9. Send 1 Consolidated In-App Notification per recipient
        for summary in target_summary:
            uid = summary['user_id']
            cnt = summary['leads_count']
            recipient_user = target_users_map.get(uid)
            if recipient_user:
                InAppNotification.objects.using(alias).create(
                    organization=organization,
                    user=recipient_user,
                    notification_type='LEAD_ASSIGNED',
                    title='Pipeline Handover Received',
                    message=f"You have been assigned {cnt} leads and linked pipeline transferred from {source_user_name or 'CRM Handover'}.",
                    data={
                        'batch_id': str(batch.id),
                        'leads_count': cnt,
                        'source_user_name': source_user_name,
                        'transfer_mode': transfer_mode,
                    },
                    deep_link='/crm/leads',
                )

        # 10. Record Business Audit & Outbox Event
        record_business_audit(
            organization=organization,
            module='crm',
            action_code='LEAD_TRANSFER',
            entity_type='LeadTransferBatch',
            entity_id=batch.id,
            actor_user=actor_user,
            event_description=f"Transferred {len(leads)} leads from {source_user_name or 'Unassigned'} to {len(target_summary)} reps ({transfer_mode}).",
            metadata={
                'batch_id': str(batch.id),
                'leads_count': len(leads),
                'trials_count': trials_count,
                'followups_count': followups_transferred_count,
            },
            db_alias=alias,
        )

        enqueue_outbox_event(
            organization=organization,
            event_type='crm.leads.transferred',
            aggregate_type='LeadTransferBatch',
            aggregate_id=batch.id,
            payload={
                'batch_id': str(batch.id),
                'source_user_id': source_user_id,
                'leads_count': len(leads),
                'target_summary': target_summary,
                'performed_by': str(actor_user.id),
            },
            db_alias=alias,
        )

        return {
            'success': True,
            'batch_id': str(batch.id),
            'transferred_leads_count': len(leads),
            'transferred_trials_count': trials_count,
            'transferred_followups_count': followups_transferred_count,
            'target_summary': target_summary,
            'message': f"Successfully transferred {len(leads)} leads, {trials_count} trials, and {followups_transferred_count} follow-ups.",
        }


def rollback_transfer(
    organization: Organization,
    batch_id: str,
    actor_user: TenantUser,
    db_alias: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Rolls back a previously executed lead transfer batch, restoring previous lead assignments.
    """
    alias = db_alias or get_tenant_db_alias() or 'default'

    with transaction.atomic(using=alias):
        batch = (
            LeadTransferBatch.objects.using(alias)
            .select_for_update()
            .filter(id=batch_id, organization=organization)
            .first()
        )
        if not batch:
            raise ValidationError({'batch_id': 'Transfer batch record not found.'})

        if batch.status == 'ROLLED_BACK':
            return {
                'success': True,
                'batch_id': str(batch.id),
                'message': 'Transfer batch was already rolled back.',
                'status': 'ROLLED_BACK',
            }

        snapshot = batch.previous_assignments_snapshot or {}
        now = timezone.now()
        rolled_back_count = 0

        for lead_id, info in snapshot.items():
            prev_user_id = info.get('previous_sales_user_id')
            lead = Lead.objects.using(alias).filter(id=lead_id, organization=organization).first()
            if lead:
                prev_user = TenantUser.objects.using(alias).filter(id=prev_user_id).first() if prev_user_id else None
                lead.assigned_sales_user = prev_user
                lead.save(using=alias, update_fields=['assigned_sales_user', 'updated_at'])

                LeadActivity.objects.using(alias).create(
                    lead=lead,
                    activity_type='OTHER',
                    outcome='ROLLBACK',
                    notes=f"Pipeline Transfer Rolled Back (Batch {batch.id}). Ownership restored to {get_user_display_name(prev_user)}.",
                    performed_by_user=actor_user,
                )
                rolled_back_count += 1

        batch.status = 'ROLLED_BACK'
        batch.save(using=alias, update_fields=['status', 'updated_at'])

        record_business_audit(
            organization=organization,
            module='crm',
            action_code='LEAD_TRANSFER_ROLLBACK',
            entity_type='LeadTransferBatch',
            entity_id=batch.id,
            actor_user=actor_user,
            event_description=f"Rolled back transfer batch {batch.id} ({rolled_back_count} leads restored).",
            metadata={'batch_id': str(batch.id), 'leads_restored': rolled_back_count},
            db_alias=alias,
        )

        return {
            'success': True,
            'batch_id': str(batch.id),
            'rolled_back_leads_count': rolled_back_count,
            'status': 'ROLLED_BACK',
            'message': f"Rolled back transfer batch. {rolled_back_count} leads restored to previous representatives.",
        }


def get_transfer_history(
    organization: Organization,
    db_alias: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """
    Returns historical lead transfer audit logs for the organization.
    """
    alias = db_alias or get_tenant_db_alias() or 'default'

    batches = (
        LeadTransferBatch.objects.using(alias)
        .filter(organization=organization)
        .select_related('source_user', 'performed_by_user')
        .order_by('-created_at')[:limit]
    )

    history = []
    for b in batches:
        history.append({
            'id': str(b.id),
            'created_at': b.created_at.isoformat(),
            'source_user_id': str(b.source_user_id) if b.source_user_id else None,
            'source_user_name': b.source_user_name or get_user_display_name(b.source_user),
            'target_users_summary': b.target_users_summary,
            'transfer_mode': b.transfer_mode,
            'leads_count': b.leads_count,
            'trials_count': b.trials_count,
            'followups_count': b.followups_count,
            'reason': b.reason,
            'status': b.status,
            'performed_by_user_id': str(b.performed_by_user_id) if b.performed_by_user_id else None,
            'performed_by_user_name': get_user_display_name(b.performed_by_user),
        })

    return history
