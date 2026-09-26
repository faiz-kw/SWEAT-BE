"""
backend/apps/tenant_core/services_lead_assignment.py — Canonical Lead Assignment Engine

Authoritative, backend-driven lead assignment services supporting:
1. Dynamic Eligible Representative Determination (Workforce & Leave Integrated)
2. Manual Assignment Validation
3. Auto Assignment with Configurable Strategies:
   - ROUND_ROBIN (concurrency-safe with row-locking)
   - LEAST_OPEN_LEADS (active/open non-terminal leads count)
   - Fallback to UNASSIGNED without dropping leads
4. Complete Assignment History tracking with source, strategy, branch, and previous assignment
5. Notification & Outbox Dispatch
"""

import logging
from typing import List, Dict, Any, Optional, Tuple
from datetime import date, datetime
from django.db import models, transaction
from django.db.models import Q, Count
from django.utils import timezone
from django.core.exceptions import ValidationError

from .context import get_tenant_db_alias
from .models_org import Organization, Branch
from .models_users import TenantUser
from .models_workforce import EmployeeScheduleException
from .models_rbac import RoleAssignment, RolePermissionSetItem, Permission, Role
from .models_crm import Lead, LeadAssignment, CRMAgentAssignmentConfig
from .services_reliability import enqueue_outbox_event, record_business_audit

logger = logging.getLogger(__name__)

TERMINAL_LEAD_STATUSES = ('CONVERTED', 'NOT_INTERESTED', 'LOST')


class LeadAssignmentEligibilityService:
    """
    Evaluates representative eligibility for lead handling based on:
    - Organization & User Active status
    - Active Employee Profile (where applicable)
    - Role Assignments & Lead Handling Permissions
    - Branch Scope Matching
    - Workforce Schedule Exceptions (Approved Leave / Time-off)
    """

    CANONICAL_LEAD_PERMISSION = 'crm.leads.edit'

    @classmethod
    def get_eligible_representatives(
        cls,
        organization: Organization,
        branch: Optional[Branch] = None,
        target_date: Optional[date] = None,
        db_alias: Optional[str] = None,
        include_unavailable: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Returns all eligible representative candidates for the given org/branch,
        annotating each with active vs available status and reasoning.
        Authorization is authoritatively governed by effective CRM permission ('crm.leads.edit'),
        valid branch/scope access, and workforce availability. Role codes/labels are returned
        strictly for display purposes.
        """
        alias = db_alias or get_tenant_db_alias() or 'default'
        eval_date = target_date or timezone.localdate()

        # 1. Tenant active check (master DB if resolvable)
        try:
            from apps.master.models import TenantDataSource
            ds = TenantDataSource.objects.using('default').filter(db_name=alias, status='ACTIVE').select_related('tenant').first()
            if ds and ds.tenant and ds.tenant.status != 'ACTIVE':
                return []
        except Exception:
            pass

        # 2. Organization active check
        if getattr(organization, 'status', 'ACTIVE') != 'ACTIVE':
            return []
        if branch and getattr(branch, 'status', 'ACTIVE') != 'ACTIVE':
            return []

        config = CRMAgentAssignmentConfig.objects.using(alias).filter(organization=organization).first()
        allowed_roles = set(config.allowed_role_codes) if config and config.allowed_role_codes else set()
        excluded_users = set(config.excluded_user_ids if config and config.excluded_user_ids else [])
        require_branch = config.require_branch_match if config is not None else True
        consider_leave = config.consider_leave_availability if config is not None else True
        allow_all_staff_fallback = config.allow_all_staff_fallback if config is not None else True

        # Base candidate query: all users in org
        qs = TenantUser.objects.using(alias).filter(
            organization=organization,
        ).prefetch_related(
            'role_assignments__role__permission_sets__items__permission',
            'branch_assignments',
            'home_branch',
            'profile__employee_profile__schedule_exceptions',
        ).select_related('home_branch')

        if excluded_users:
            qs = qs.exclude(id__in=list(excluded_users))

        results: List[Dict[str, Any]] = []

        for user in qs.order_by('first_name', 'last_name', 'email'):
            is_active_account = (
                user.status == 'ACTIVE' and
                user.is_login_allowed and
                getattr(user, 'is_active', True)
            )

            # Check employee profile if exists
            is_employee_active = True
            employee_inactivity_reason = None
            if hasattr(user, 'profile') and hasattr(user.profile, 'employee_profile'):
                emp_status = getattr(user.profile.employee_profile, 'employment_status', 'ACTIVE')
                if emp_status not in ('ACTIVE', 'NOTICE_PERIOD'):
                    is_employee_active = False
                    employee_inactivity_reason = f"Employment status is {emp_status}"

            # Check active role assignments
            active_ras = [
                ra for ra in user.role_assignments.all()
                if ra.status == 'ACTIVE' and ra.is_active and ra.role and ra.role.status == 'ACTIVE' and ra.role.is_active
            ]
            has_active_role = bool(active_ras) or getattr(user, 'is_superuser', False)

            # Check branch scope matching on role assignments & user branch assignments
            has_branch_match = True
            branch_matching_ras = list(active_ras)
            if branch and require_branch:
                branch_id = branch.id
                home_match = (user.home_branch_id == branch_id)
                branch_assign_match = any(
                    ba.status == 'ACTIVE' and ba.is_active and (ba.branch_id == branch_id or ba.scope_type == 'ALL')
                    for ba in user.branch_assignments.all()
                )

                # Filter role assignments valid for this branch
                scoped_ras = []
                for ra in active_ras:
                    is_org_scope = (
                        ra.scope_type in ('ORGANIZATION', 'ALL') or
                        getattr(ra.role, 'scope', '') == 'ORG'
                    )
                    is_branch_scope = (ra.branch_id == branch_id)
                    is_unscoped_branch_role = (ra.branch_id is None and (home_match or branch_assign_match))
                    if is_org_scope or is_branch_scope or is_unscoped_branch_role or getattr(user, 'is_superuser', False):
                        scoped_ras.append(ra)

                branch_matching_ras = scoped_ras
                org_wide_fallback = (
                    user.home_branch_id is None and
                    not any(ba.status == 'ACTIVE' and ba.is_active for ba in user.branch_assignments.all()) and
                    any(getattr(ra.role, 'scope', '') == 'ORG' or ra.scope_type in ('ORGANIZATION', 'ALL') for ra in active_ras)
                )
                has_branch_match = (
                    home_match or branch_assign_match or bool(scoped_ras) or
                    org_wide_fallback or getattr(user, 'is_superuser', False)
                )

            # Authoritative RBAC check: does user have effective permission 'crm.leads.edit'?
            has_lead_permission = False
            perm_granting_role = None

            if getattr(user, 'is_superuser', False):
                has_lead_permission = True
            else:
                candidate_roles = [ra.role for ra in branch_matching_ras]
                # Direct check via RolePermissionSetItem for canonical permission
                if candidate_roles:
                    perm_items = RolePermissionSetItem.objects.using(alias).filter(
                        permission_set__role__in=candidate_roles,
                        permission_set__is_active=True,
                        granted=True,
                    ).filter(
                        Q(permission__permission_code__iexact=cls.CANONICAL_LEAD_PERMISSION) |
                        Q(permission__code__iexact=cls.CANONICAL_LEAD_PERMISSION)
                    ).select_related('permission_set__role')

                    first_perm = perm_items.first()
                    if first_perm:
                        has_lead_permission = True
                        perm_granting_role = first_perm.permission_set.role
                    else:
                        for r in candidate_roles:
                            if getattr(r, 'code', '') == 'ORG_ADMIN':
                                has_lead_permission = True
                                perm_granting_role = r
                                break

            # Optional secondary business restriction (CRMAgentAssignmentConfig.allowed_role_codes)
            matches_tenant_role_restriction = True
            if allowed_roles and not allow_all_staff_fallback and not getattr(user, 'is_superuser', False):
                matches_tenant_role_restriction = any(ra.role.code in allowed_roles for ra in branch_matching_ras)

            # Determine primary role for display purposes (role_label / role_code)
            display_ra = None
            if perm_granting_role:
                display_ra = next((ra for ra in branch_matching_ras if ra.role_id == perm_granting_role.id), None)
            if not display_ra and branch_matching_ras:
                display_ra = branch_matching_ras[0]
            elif not display_ra and active_ras:
                display_ra = active_ras[0]

            role_code = display_ra.role.code if display_ra else None
            role_name = display_ra.role.name if display_ra else (user.user_type or 'Representative')
            role_label = display_ra.role.name if display_ra else (user.user_type or 'Representative')

            # Check leave / schedule exceptions for target date
            is_on_leave = False
            leave_reason = None
            if consider_leave and hasattr(user, 'profile') and hasattr(user.profile, 'employee_profile'):
                emp = user.profile.employee_profile
                exceptions = EmployeeScheduleException.objects.using(alias).filter(
                    employee_profile=emp,
                    exception_date=eval_date,
                    status='ACTIVE',
                    is_available=False,
                )
                if branch:
                    exceptions = exceptions.filter(Q(branch__isnull=True) | Q(branch=branch))
                exc = exceptions.first()
                if exc:
                    is_on_leave = True
                    exc_type_display = exc.get_exception_type_display() if hasattr(exc, 'get_exception_type_display') else exc.exception_type
                    leave_reason = f"On {exc_type_display}" + (f": {exc.reason}" if exc.reason else "")

            # Determine availability status
            if not is_active_account:
                avail_status = 'INACTIVE'
                avail_reason = 'User account is inactive or login disabled'
                is_available = False
            elif not is_employee_active:
                avail_status = 'INACTIVE'
                avail_reason = employee_inactivity_reason or 'Employee profile is inactive'
                is_available = False
            elif not has_active_role:
                avail_status = 'NO_PERMISSION'
                avail_reason = 'User has no active role assignments in organization'
                is_available = False
            elif not has_branch_match:
                avail_status = 'OUTSIDE_BRANCH'
                avail_reason = f"User is not assigned to branch '{branch.name if branch else branch_id}'"
                is_available = False
            elif not has_lead_permission:
                avail_status = 'NO_PERMISSION'
                avail_reason = f"User lacks required effective permission '{cls.CANONICAL_LEAD_PERMISSION}'"
                is_available = False
            elif not matches_tenant_role_restriction:
                avail_status = 'ROLE_RESTRICTED'
                avail_reason = 'Role not included in tenant allowed assignment roles'
                is_available = False
            elif is_on_leave:
                avail_status = 'ON_LEAVE'
                avail_reason = leave_reason or 'User is on approved leave today'
                is_available = False
            else:
                avail_status = 'AVAILABLE'
                avail_reason = None
                is_available = True

            rep_item = {
                'id': str(user.id),
                'user_id': str(user.id),
                'name': f"{user.first_name} {user.last_name}".strip() or user.email,
                'display_name': f"{user.first_name} {user.last_name}".strip() or user.email,
                'email': user.email,
                'phone': user.phone,
                'user_type': user.user_type,
                'role_code': role_code,
                'role_name': role_name,
                'role_label': role_label,
                'home_branch_id': str(user.home_branch_id) if user.home_branch_id else None,
                'home_branch_name': user.home_branch.name if user.home_branch else None,
                'branch': user.home_branch.name if user.home_branch else None,
                'eligible': is_available,
                'is_available': is_available,
                'availability_status': avail_status,
                'availability_reason': avail_reason,
            }

            if is_available or include_unavailable:
                results.append(rep_item)

        return results

    @classmethod
    def validate_assignee_eligibility(
        cls,
        organization: Organization,
        user: TenantUser,
        branch: Optional[Branch] = None,
        target_date: Optional[date] = None,
        db_alias: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Validates whether a specific selected user is eligible and available to receive a lead.
        """
        alias = db_alias or get_tenant_db_alias() or 'default'
        eval_date = target_date or timezone.localdate()

        if user.organization_id != organization.id:
            return False, "Target user does not belong to this organization."

        candidates = cls.get_eligible_representatives(
            organization=organization,
            branch=branch,
            target_date=eval_date,
            db_alias=alias,
            include_unavailable=True,
        )

        match = next((c for c in candidates if c['user_id'] == str(user.id)), None)
        if not match:
            return False, "User is not in the eligible representative pool."
        if not match['is_available']:
            return False, f"User is currently unavailable: {match['availability_reason'] or match['availability_status']}"

        return True, None


class LeadAutoAssignmentService:
    """
    Executes auto-assignment of a lead to an eligible available representative.
    Supports:
    - ROUND_ROBIN with concurrency-safe pointer persistence
    - LEAST_OPEN_LEADS with terminal state exclusion & deterministic tie-breaking
    - Safe fallback to UNASSIGNED without lead dropping
    """

    @classmethod
    def resolve_assignee(
        cls,
        organization: Organization,
        branch: Optional[Branch] = None,
        db_alias: Optional[str] = None,
    ) -> Tuple[Optional[TenantUser], str, Optional[str]]:
        """
        Determines the assigned representative according to tenant CRM assignment config.
        Returns: (assigned_user, strategy_name, reason)
        """
        alias = db_alias or get_tenant_db_alias() or 'default'

        with transaction.atomic(using=alias):
            # Select and lock the config row to guarantee concurrency safety across workers
            config, _ = CRMAgentAssignmentConfig.objects.using(alias).select_for_update().get_or_create(
                organization=organization,
                defaults={
                    'allowed_role_codes': ['SALES_REP', 'BRANCH_MANAGER', 'FRONT_DESK', 'ORG_ADMIN'],
                    'auto_assignment_strategy': 'ROUND_ROBIN',
                    'allow_unassigned_fallback': True,
                    'consider_leave_availability': True,
                }
            )

            strategy = config.auto_assignment_strategy or 'ROUND_ROBIN'
            if strategy == 'MANUAL_ONLY':
                if config.allow_unassigned_fallback:
                    return None, 'MANUAL_ONLY', 'Auto assignment disabled by strategy (MANUAL_ONLY)'
                raise ValidationError("Auto assignment is disabled by policy (Manual only).")

            # Get eligible representatives who are currently AVAILABLE
            candidates = LeadAssignmentEligibilityService.get_eligible_representatives(
                organization=organization,
                branch=branch,
                db_alias=alias,
                include_unavailable=False,
            )

            available_candidates = [c for c in candidates if c['is_available']]

            if not available_candidates:
                if config.allow_unassigned_fallback:
                    logger.warning(
                        "No available representatives for auto-assignment in org %s branch %s. Falling back to UNASSIGNED.",
                        organization.id, branch.id if branch else None,
                    )
                    return None, strategy, "NO_AVAILABLE_REPRESENTATIVE"
                raise ValidationError("No eligible representatives are currently available for auto-assignment.")

            available_user_ids = [c['user_id'] for c in available_candidates]
            available_users = list(
                TenantUser.objects.using(alias).filter(id__in=available_user_ids)
            )

            if strategy == 'ROUND_ROBIN':
                selected_user = cls._execute_round_robin(config, branch, available_users, alias)
                return selected_user, 'ROUND_ROBIN', 'Round robin rotation'

            elif strategy == 'LEAST_OPEN_LEADS':
                selected_user = cls._execute_least_open_leads(organization, available_users, alias)
                return selected_user, 'LEAST_OPEN_LEADS', 'Least open leads workload'

            else:
                # Default fallback strategy to round robin
                selected_user = cls._execute_round_robin(config, branch, available_users, alias)
                return selected_user, 'ROUND_ROBIN', 'Default round robin fallback'

    @classmethod
    def _execute_round_robin(
        cls,
        config: CRMAgentAssignmentConfig,
        branch: Optional[Branch],
        available_users: List[TenantUser],
        alias: str,
    ) -> TenantUser:
        """
        Concurrency-safe round robin rotation using locked config state.
        """
        branch_key = str(branch.id) if branch else 'org_wide'
        state = dict(config.round_robin_state or {})
        last_assigned_id = state.get(branch_key)

        # Deterministic sorting of available users
        sorted_users = sorted(available_users, key=lambda u: str(u.id))
        user_ids = [str(u.id) for u in sorted_users]

        if last_assigned_id and last_assigned_id in user_ids:
            last_index = user_ids.index(last_assigned_id)
            next_index = (last_index + 1) % len(sorted_users)
        else:
            next_index = 0

        selected_user = sorted_users[next_index]
        state[branch_key] = str(selected_user.id)
        config.round_robin_state = state
        config.save(using=alias, update_fields=['round_robin_state', 'updated_at'])

        return selected_user

    @classmethod
    def _execute_least_open_leads(
        cls,
        organization: Organization,
        available_users: List[TenantUser],
        alias: str,
    ) -> TenantUser:
        """
        Selects representative with lowest count of active (non-terminal) leads.
        Tie-breaker: oldest last assignment or deterministic user ID order.
        """
        user_workloads = []
        for user in available_users:
            open_count = Lead.objects.using(alias).filter(
                organization=organization,
                assigned_sales_user=user,
            ).exclude(
                current_status__in=TERMINAL_LEAD_STATUSES
            ).count()

            latest_assign = LeadAssignment.objects.using(alias).filter(
                assigned_to_user=user,
                status='ACTIVE',
            ).order_by('-assigned_at').first()

            last_assign_time = latest_assign.assigned_at if latest_assign else timezone.make_aware(datetime.min)
            user_workloads.append((open_count, last_assign_time, str(user.id), user))

        # Sort primarily by lowest open_count, then earliest last_assign_time, then user ID string
        user_workloads.sort(key=lambda item: (item[0], item[1], item[2]))
        return user_workloads[0][3]


class LeadAssignmentExecutionService:
    """
    Coordinates creation, reassignment, notifications, and auditing for LeadAssignments.
    """

    @classmethod
    def execute_assignment(
        cls,
        lead: Lead,
        assigned_to_user: Optional[TenantUser],
        assignment_source: str = 'MANUAL',
        assignment_strategy: str = '',
        reason: str = '',
        actor_user: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> Optional[LeadAssignment]:
        """
        Executes assignment or leaves unassigned with audit, outbox, and notifications.
        """
        alias = db_alias or get_tenant_db_alias() or 'default'

        with transaction.atomic(using=alias):
            # If no user assigned (Unassigned Lead)
            if not assigned_to_user:
                lead.assigned_sales_user = None
                lead.save(using=alias, update_fields=['assigned_sales_user', 'updated_at'])

                # Notify managers if unassigned alert is enabled
                config = CRMAgentAssignmentConfig.objects.using(alias).filter(organization=lead.organization).first()
                if config and config.notify_manager_on_unassigned:
                    cls._notify_managers_unassigned(lead, alias)

                record_business_audit(
                    organization=lead.organization,
                    branch=lead.branch,
                    actor_user=actor_user,
                    module='crm',
                    action_code='CRM_LEAD_UNASSIGNED',
                    entity_type='Lead',
                    entity_id=lead.id,
                    event_description=f"Lead {lead.first_name} {lead.last_name} created as unassigned ({reason or 'No available agent'})",
                    after_data={'lead_id': str(lead.id), 'branch_id': str(lead.branch_id) if lead.branch else None},
                    db_alias=alias,
                )
                return None

            # Reassignment / Existing check
            existing_active = LeadAssignment.objects.using(alias).filter(
                lead=lead,
                assignment_type='SALES',
                status='ACTIVE',
            ).first()

            if existing_active and existing_active.assigned_to_user_id == assigned_to_user.id:
                # Idempotent replay protection
                return existing_active

            prev_user = existing_active.assigned_to_user if existing_active else None

            # End previous assignment
            if existing_active:
                existing_active.status = 'INACTIVE'
                existing_active.unassigned_at = timezone.now()
                existing_active.save(using=alias, update_fields=['status', 'unassigned_at', 'updated_at'])

            new_assign = LeadAssignment.objects.using(alias).create(
                lead=lead,
                assigned_to_user=assigned_to_user,
                assigned_by_user=actor_user,
                assignment_type='SALES',
                assignment_source=assignment_source,
                assignment_strategy=assignment_strategy or '',
                reason=reason or '',
                branch=lead.branch,
                previous_assignment=existing_active,
                status='ACTIVE',
            )

            lead.assigned_sales_user = assigned_to_user
            lead.save(using=alias, update_fields=['assigned_sales_user', 'updated_at'])

            # Audit & Outbox
            action_code = 'CRM_LEAD_REASSIGNED' if prev_user else 'CRM_LEAD_ASSIGNED'
            record_business_audit(
                organization=lead.organization,
                branch=lead.branch,
                actor_user=actor_user,
                module='crm',
                action_code=action_code,
                entity_type='LeadAssignment',
                entity_id=new_assign.id,
                event_description=(
                    f"Assigned lead {lead.first_name} {lead.last_name} to {assigned_to_user.email} [{assignment_source}]"
                    + (f" (reassigned from {prev_user.email})" if prev_user else "")
                ),
                before_data={'assigned_to_user_id': str(prev_user.id) if prev_user else None},
                after_data={
                    'assigned_to_user_id': str(assigned_to_user.id),
                    'assignment_source': assignment_source,
                    'assignment_strategy': assignment_strategy,
                    'branch_id': str(lead.branch_id) if lead.branch else None,
                },
                db_alias=alias,
            )

            enqueue_outbox_event(
                organization=lead.organization,
                event_type='crm.lead.assigned',
                aggregate_type='Lead',
                aggregate_id=lead.id,
                payload={
                    'lead_id': str(lead.id),
                    'assigned_user_id': str(assigned_to_user.id),
                    'previous_user_id': str(prev_user.id) if prev_user else None,
                    'branch_id': str(lead.branch_id) if lead.branch else None,
                    'assigned_by': str(actor_user.id) if actor_user else None,
                    'assignment_source': assignment_source,
                    'assignment_strategy': assignment_strategy,
                    'created_at': timezone.now().isoformat(),
                },
                db_alias=alias,
            )

            # In-App Notification to assigned representative
            from .services_crm import send_in_app_notification
            send_in_app_notification(
                organization=lead.organization,
                user=assigned_to_user,
                notification_type='LEAD_ASSIGNED',
                title='New Lead Assigned',
                message=f"A new lead has been assigned to you: {lead.first_name} {lead.last_name} — {lead.branch.name if lead.branch else 'General'}",
                data={
                    'lead_id': str(lead.id),
                    'lead_name': f"{lead.first_name} {lead.last_name}",
                    'branch_name': lead.branch.name if lead.branch else 'General',
                    'branch_id': str(lead.branch_id) if lead.branch else None,
                    'previous_user_id': str(prev_user.id) if prev_user else None,
                    'assignment_source': assignment_source,
                    'assigned_by': actor_user.email if actor_user else 'System',
                },
                deep_link=f"/crm/leads?lead_id={lead.id}",
                idempotency_key=f"lead_assigned:{lead.id}:{assigned_to_user.id}:{new_assign.id}",
                db_alias=alias,
            )

            return new_assign

    @classmethod
    def _notify_managers_unassigned(cls, lead: Lead, alias: str):
        """
        Sends notifications to branch/org managers when a lead is created as UNASSIGNED.
        """
        from .services_crm import send_in_app_notification
        from .models_rbac import RoleAssignment
        manager_ras = RoleAssignment.objects.using(alias).filter(
            organization=lead.organization,
            role__code__in=['BRANCH_MANAGER', 'ORG_ADMIN'],
            status='ACTIVE',
            is_active=True,
        ).select_related('user')

        notified_user_ids = set()
        for ra in manager_ras:
            user = ra.user
            if user and user.status == 'ACTIVE' and user.id not in notified_user_ids:
                notified_user_ids.add(user.id)
                send_in_app_notification(
                    organization=lead.organization,
                    user=user,
                    notification_type='LEAD_UNASSIGNED',
                    title='Unassigned Lead Requires Attention',
                    message=f"Lead {lead.first_name} {lead.last_name} was created as UNASSIGNED (no available representative).",
                    data={
                        'lead_id': str(lead.id),
                        'lead_name': f"{lead.first_name} {lead.last_name}",
                        'branch_name': lead.branch.name if lead.branch else 'General',
                        'branch_id': str(lead.branch_id) if lead.branch else None,
                    },
                    deep_link=f"/crm/leads?lead_id={lead.id}",
                    idempotency_key=f"lead_unassigned:{lead.id}:{user.id}",
                    db_alias=alias,
                )
