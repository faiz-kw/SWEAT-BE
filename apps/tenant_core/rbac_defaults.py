"""
Canonical Default Role Definitions & RBAC Synchronization Engine.
Defines configurable backend default permissions for all standard roles.
All role permissions persist in the Tenant DB:
  Role, RolePermissionSet, RolePermissionSetItem, RoleModuleAccess, RoleSubmoduleAccess

Tenant Organization Admins can customize these role permissions at any time via /admin/permissions.
"""

import logging
from typing import Dict, List, Optional
from django.utils import timezone

logger = logging.getLogger(__name__)

# Canonical System Role Definitions
DEFAULT_ROLES_CONFIG: Dict[str, dict] = {
    'ORG_ADMIN': {
        'name': 'Organization Administrator',
        'scope': 'ORG',
        'description': 'Full administrative and operational authority across all tenant modules and branches.',
        'is_system': True,
        # Wildcard / all permissions granted
        'all_permissions': True,
    },
    'BRANCH_MANAGER': {
        'name': 'Branch Manager',
        'scope': 'BRANCH',
        'description': 'Operational and staff oversight strictly within effective assigned branch scope.',
        'is_system': True,
        'all_permissions': False,
        'permissions': [
            # Core & Locations
            'core.settings.view',
            'core.files.view', 'core.files.create', 'core.users.view', 'core.users.create', 'core.users.edit',
            # Ops (Classes, Bookings, Schedules, Rosters)
            'ops.classes.view', 'ops.classes.create', 'ops.classes.edit', 'ops.classes.delete',
            'ops.bookings.view', 'ops.bookings.create', 'ops.bookings.edit', 'ops.bookings.delete',
            'ops.calendar.view', 'ops.calendar.create', 'ops.calendar.edit',
            'ops.trainers.view', 'ops.trainers.create', 'ops.trainers.edit',
            'ops.personal-training.view', 'ops.personal-training.create', 'ops.personal-training.edit',
            'ops.pilates.view', 'ops.pilates.create', 'ops.pilates.edit',
            # Members & Attendance
            'members.client-360.view', 'members.client-360.create', 'members.client-360.edit',
            'members.memberships.view', 'members.renewals.view',
            'members.attendance.view', 'members.attendance.create', 'members.attendance.edit',
            'members.freeze.view', 'members.freeze.create', 'members.freeze.edit',
            'members.transfers.view', 'members.transfers.create',
            # CRM
            'crm.dashboard.view',
            'crm.leads.view', 'crm.leads.create', 'crm.leads.edit',
            'crm.pipeline.view', 'crm.pipeline.create', 'crm.pipeline.edit',
            'crm.trials.view', 'crm.trials.create', 'crm.trials.edit',
            'crm.follow-ups.view', 'crm.follow-ups.create', 'crm.follow-ups.edit',
            'crm.communications.view', 'crm.communications.send',
            'crm.settings.view',
            'crm.settings.edit',
            'automation.workflows.view',
            'automation.executions.view',
            # Reports & CS
            'reports.business.view', 'reports.sales.view', 'reports.members.view', 'reports.trainers.view',
            'cs.member-health.view', 'cs.at-risk.view', 'cs.feedback.view',
            'cs.grievances.view', 'cs.grievances.create', 'cs.grievances.edit',
        ],
    },
    'FRONT_DESK': {
        'name': 'Front Desk Staff',
        'scope': 'BRANCH',
        'description': 'Operational check-ins, manual front-desk bookings, attendance, and member search at assigned branch.',
        'is_system': True,
        'all_permissions': False,
        'permissions': [
            # Core Read
            'core.settings.view', 'core.files.view', 'core.users.view',
            # Ops (Bookings, Schedule view, Calendar)
            'ops.classes.view',
            'ops.bookings.view', 'ops.bookings.create', 'ops.bookings.edit',
            'ops.calendar.view',
            'ops.trainers.view',
            'ops.personal-training.view', 'ops.personal-training.create', 'ops.personal-training.edit',
            # Members & Attendance
            'members.client-360.view', 'members.client-360.create', 'members.client-360.edit',
            'members.memberships.view',
            'members.attendance.view', 'members.attendance.create', 'members.attendance.edit',
            'members.renewals.view',
            # CRM (Walk-ins, Enquiries, Trials)
            'crm.leads.view', 'crm.leads.create', 'crm.leads.edit',
            'crm.trials.view', 'crm.trials.create', 'crm.trials.edit',
            'crm.follow-ups.view', 'crm.follow-ups.create', 'crm.follow-ups.edit',
            'crm.communications.view', 'crm.communications.send',
        ],
    },
    'TRAINER': {
        'name': 'Fitness Trainer / Coach',
        'scope': 'BRANCH',
        'description': 'View assigned class occurrences, trainer rosters, member progress, and record session attendance.',
        'is_system': True,
        'all_permissions': False,
        'permissions': [
            # Core
            'core.settings.view', 'core.files.view',
            # Ops (Assigned classes, roster, calendar)
            'ops.classes.view',
            'ops.calendar.view',
            'ops.trainers.view',
            'ops.bookings.view',
            'ops.personal-training.view', 'ops.personal-training.edit',
            # Members & Attendance
            'members.client-360.view',
            'members.attendance.view', 'members.attendance.create',
            # Coaching & Performance
            'coaching.trainers.view', 'coaching.program-builder.view', 'coaching.program-builder.create', 'coaching.program-builder.edit',
            'performance.workouts.view', 'performance.workouts.create', 'performance.workouts.edit',
            'performance.analytics.view',
            'performance.pr-tracker.view', 'performance.pr-tracker.create', 'performance.pr-tracker.edit',
            'performance.leaderboards.view',
        ],
    },
    'MEMBER': {
        'name': 'Gym Member',
        'scope': 'ORG',
        'description': 'Member self-service: browse eligible classes, book slots, manage own bookings, and track workouts.',
        'is_system': True,
        'all_permissions': False,
        'permissions': [
            # Ops: View classes, trainers, create own booking, cancel/reschedule own booking
            'ops.classes.view',
            'ops.trainers.view',
            'ops.bookings.view', 'ops.bookings.create', 'ops.bookings.edit',
            'ops.calendar.view',
            # Members: View own profile, membership, attendance
            'members.client-360.view',
            'members.memberships.view',
            'members.attendance.view',
            # Performance: Log own workouts, PRs, leaderboards
            'performance.workouts.view', 'performance.workouts.create', 'performance.workouts.edit',
            'performance.analytics.view',
            'performance.pr-tracker.view', 'performance.pr-tracker.create',
            'performance.leaderboards.view',
        ],
    },
    'SALES_REP': {
        'name': 'Sales & CRM Representative',
        'scope': 'BRANCH',
        'description': 'Lead capture, sales pipeline stages, trial pass booking, and sales conversions.',
        'is_system': True,
        'all_permissions': False,
        'permissions': [
            # Core
            'core.settings.view', 'core.files.view', 'core.users.view',
            # CRM Full
            'crm.dashboard.view',
            'crm.leads.view', 'crm.leads.create', 'crm.leads.edit', 'crm.leads.delete',
            'crm.pipeline.view', 'crm.pipeline.create', 'crm.pipeline.edit',
            'crm.trials.view', 'crm.trials.create', 'crm.trials.edit',
            'crm.campaigns.view',
            'crm.follow-ups.view', 'crm.follow-ups.create', 'crm.follow-ups.edit',
            'crm.communications.view', 'crm.communications.send',
            'crm.settings.view',
            # Members Read
            'members.client-360.view', 'members.client-360.create',
            'members.memberships.view',
            # Ops View
            'ops.classes.view', 'ops.calendar.view',
            # Reports
            'reports.sales.view',
        ],
    },
    'FINANCE_ADMIN': {
        'name': 'Finance & Billing Administrator',
        'scope': 'ORG',
        'description': 'Invoices, payment reconciliation, refunds, dues tracking, and financial analytics.',
        'is_system': True,
        'all_permissions': False,
        'permissions': [
            # Core
            'core.settings.view', 'core.files.view',
            # Finance Full
            'finance.invoices.view', 'finance.invoices.create', 'finance.invoices.edit', 'finance.invoices.delete',
            'finance.payments.view', 'finance.payments.create', 'finance.payments.edit', 'finance.payments.delete',
            'finance.refunds.view', 'finance.refunds.create', 'finance.refunds.edit',
            'finance.outstanding.view', 'finance.outstanding.create', 'finance.outstanding.edit',
            'finance.expenses.view', 'finance.expenses.create', 'finance.expenses.edit',
            'finance.revenue.view',
            # Members & Packages Read
            'members.client-360.view',
            'members.memberships.view', 'members.memberships.create', 'members.memberships.edit',
            # Ops Read for Reconciliation
            'ops.classes.view', 'ops.bookings.view',
            # Reports
            'reports.business.view', 'reports.financial.view',
        ],
    },
}


def sync_default_role_permissions(
    db_alias: str,
    org=None,
    overwrite_custom: bool = False,
    roles_filter: Optional[List[str]] = None,
) -> Dict[str, dict]:
    """
    Idempotently sync canonical system roles and their default permission templates
    into the specified tenant database.

    Args:
        db_alias: Database connection alias for the target tenant.
        org: Optional Organization instance. If None, resolved from DB.
        overwrite_custom: If False, preserves existing custom grants and only
                          backfills missing default grants. If True, resets to default.
        roles_filter: Optional list of role codes to sync (e.g. ['FRONT_DESK', 'MEMBER']).

    Returns:
        Summary dictionary with counts of roles, module grants, and permission grants.
    """
    from apps.tenant_core.models_org import Organization
    from apps.tenant_core.models_rbac import (
        ModuleCatalog, SubmoduleCatalog, Permission, Role,
        RolePermissionSet, RolePermissionSetItem,
        RoleModuleAccess, RoleSubmoduleAccess,
    )
    from django.db import transaction

    if not org:
        org = Organization.objects.using(db_alias).filter(status='ACTIVE').first()
        if not org:
            org = Organization.objects.using(db_alias).first()
    if not org:
        raise ValueError(f"No Organization found in tenant database '{db_alias}'.")

    # Load all available permissions and modules in the tenant DB
    all_perms = {p.permission_code.lower(): p for p in Permission.objects.using(db_alias).select_related('module', 'submodule').all()}
    all_modules = {m.module_code.lower(): m for m in ModuleCatalog.objects.using(db_alias).all()}
    all_submodules = {
        (sm.module.module_code.lower(), sm.submodule_code.lower()): sm
        for sm in SubmoduleCatalog.objects.using(db_alias).select_related('module').all()
    }

    results = {}

    with transaction.atomic(using=db_alias):
        for role_code, config in DEFAULT_ROLES_CONFIG.items():
            if roles_filter and role_code not in roles_filter:
                continue

            # 1. Get or create Role
            role, created = Role.objects.using(db_alias).get_or_create(
                organization=org,
                code=role_code,
                defaults={
                    'name': config['name'],
                    'scope': config['scope'],
                    'description': config['description'],
                    'is_system': config.get('is_system', True),
                    'is_system_role': config.get('is_system', True),
                    'is_active': True,
                    'status': 'ACTIVE',
                },
            )

            # Ensure is_system flag is maintained
            if config.get('is_system') and not role.is_system:
                role.is_system = True
                role.is_system_role = True
                role.save(using=db_alias, update_fields=['is_system', 'is_system_role'])

            # 2. Get or create primary RolePermissionSet
            perm_set, _ = RolePermissionSet.objects.using(db_alias).get_or_create(
                role=role,
                name=f"{role.name} Default Permissions",
                defaults={
                    'organization': org,
                    'description': f"Canonical default permissions for {role.name}",
                    'scope_type': 'ORGANIZATION',
                    'is_active': True,
                    'status': 'ACTIVE',
                },
            )

            # 3. Resolve target permission list
            target_perms = []
            if config.get('all_permissions'):
                target_perms = list(all_perms.values())
            else:
                for p_code in config.get('permissions', []):
                    p_obj = all_perms.get(p_code.lower())
                    if p_obj:
                        target_perms.append(p_obj)
                    else:
                        logger.warning(f"Permission code '{p_code}' not found in tenant DB '{db_alias}'.")

            # 4. Grant permissions and enable module/submodule gates
            granted_count = 0
            enabled_modules = set()
            enabled_submodules = set()

            for p_obj in target_perms:
                # Permission item grant
                item, item_created = RolePermissionSetItem.objects.using(db_alias).get_or_create(
                    permission_set=perm_set,
                    permission=p_obj,
                    defaults={'granted': True, 'is_allowed': True},
                )
                if not item_created and overwrite_custom and not item.granted:
                    item.granted = True
                    item.is_allowed = True
                    item.save(using=db_alias, update_fields=['granted', 'is_allowed'])

                granted_count += 1

                # Track modules and submodules that need access gates opened
                if p_obj.module:
                    enabled_modules.add(p_obj.module)
                if p_obj.submodule:
                    enabled_submodules.add(p_obj.submodule)

            # For ORG_ADMIN, enable all catalog modules & submodules
            if config.get('all_permissions'):
                enabled_modules = set(all_modules.values())
                enabled_submodules = set(all_submodules.values())

            # 5. Open RoleModuleAccess gates
            for mod_obj in enabled_modules:
                RoleModuleAccess.objects.using(db_alias).update_or_create(
                    role=role,
                    module=mod_obj,
                    defaults={
                        'permission_set': perm_set,
                        'can_access': True,
                        'is_visible': True,
                    },
                )

            # 6. Open RoleSubmoduleAccess gates
            for sm_obj in enabled_submodules:
                RoleSubmoduleAccess.objects.using(db_alias).update_or_create(
                    role=role,
                    submodule=sm_obj,
                    defaults={
                        'permission_set': perm_set,
                        'can_access': True,
                        'is_visible': True,
                    },
                )

            results[role_code] = {
                'role_id': str(role.id),
                'role_name': role.name,
                'created': created,
                'granted_permissions': granted_count,
                'modules_enabled': len(enabled_modules),
                'submodules_enabled': len(enabled_submodules),
            }

    return results
