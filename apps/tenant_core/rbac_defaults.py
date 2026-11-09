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
            'ops.classes.view', 'ops.classes.create', 'ops.classes.edit', 'ops.classes.delete', 'ops.classes.cancel',
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
            'crm.leads.view', 'crm.leads.create', 'crm.leads.edit', 'crm.leads.convert',
            'crm.pipeline.view', 'crm.pipeline.create', 'crm.pipeline.edit',
            'crm.trials.view', 'crm.trials.create', 'crm.trials.edit',
            'crm.follow-ups.view', 'crm.follow-ups.create', 'crm.follow-ups.edit',
            'crm.communications.view', 'crm.communications.send',
            'crm.telephony.access',
            'crm.settings.view',
            'crm.settings.edit',
            'automation.workflows.view',
            'automation.executions.view',
            # Reports & CS
            'reports.business.view', 'reports.sales.view', 'reports.members.view', 'reports.trainers.view',
            'cs.member-health.view', 'cs.at-risk.view', 'cs.feedback.view',
            'cs.grievances.view', 'cs.grievances.create', 'cs.grievances.edit',
            # CMS (Banners, Media, Visual Categories)
            'cms.banners.view', 'cms.banners.manage',
            'cms.categories.view', 'cms.categories.manage',
            'cms.media.view', 'cms.media.manage',
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
            # CRM (Walk-ins, Enquiries, Trials, Conversions)
            'crm.leads.view', 'crm.leads.create', 'crm.leads.edit', 'crm.leads.convert',
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
            # Ops (Assigned classes, roster, calendar, in-class booking)
            'ops.classes.view',
            'ops.calendar.view',
            'ops.trainers.view',
            'ops.bookings.view',
            'ops.bookings.create',
            'ops.personal-training.view', 'ops.personal-training.edit',
            # Members & Attendance
            'members.client-360.view',
            'members.attendance.view', 'members.attendance.create',
            # CRM Leads & Trial Passes (in-class trial pass booking)
            'crm.leads.view', 'crm.leads.create',
            'crm.trials.view', 'crm.trials.create',
            # Performance
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
            'crm.leads.view', 'crm.leads.create', 'crm.leads.edit', 'crm.leads.delete', 'crm.leads.convert',
            'crm.pipeline.view', 'crm.pipeline.create', 'crm.pipeline.edit',
            'crm.trials.view', 'crm.trials.create', 'crm.trials.edit',
            'crm.campaigns.view',
            'crm.follow-ups.view', 'crm.follow-ups.create', 'crm.follow-ups.edit',
            'crm.communications.view', 'crm.communications.send',
            'crm.telephony.access',
            'crm.settings.view',
            # Members & Memberships (Creation & renewals)
            'members.client-360.view', 'members.client-360.create',
            'members.memberships.view', 'members.memberships.create', 'members.memberships.edit',
            'members.renewals.view', 'members.renewals.create',
            # Payment collection for membership sales & renewals
            'finance.payments.create',
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



CANONICAL_MODULES = {
    'core': {'name': 'Core System & Administration', 'description': 'System governance, branches, roles, and platform users.'},
    'crm': {'name': 'CRM & Sales Automation', 'description': 'Lead capture, sales pipeline, trial bookings, and promotional campaigns.'},
    'members': {'name': 'Member Lifecycle Management', 'description': 'Member 360, membership tiers, renewals, freeze, and check-in attendance.'},
    'ops': {'name': 'Studio Operations & Classes', 'description': 'Class scheduling, trainer roster, room allocation, and personal training.'},
    'finance': {'name': 'Finance, Billing & POS', 'description': 'Tax invoices, POS collections, payment gateways, expenses, and revenue reports.'},
    'performance': {'name': 'Performance & Workouts', 'description': 'Workout logs, personal records, and leaderboard rankings.'},
    'cms': {'name': 'Content Management System', 'description': 'Mobile app banners, visual program category assets, audio tracks, and video clips.'},
    'cs': {'name': 'Customer Success & Retention', 'description': 'Member health scores, at-risk churn detection, and grievances.'},
    'reports': {'name': 'Business & Operational Reports', 'description': 'Executive dashboards, revenue, attendance, and trainer utilization analytics.'},
    'automation': {'name': 'Workflows & Automation', 'description': 'Automated notifications, reminders, and trigger workflows.'},
}

CANONICAL_SUBMODULES = {
    ('core', 'settings'): {'name': 'Settings & Governance'},
    ('core', 'users'): {'name': 'User Management'},
    ('core', 'roles'): {'name': 'Role Management'},
    ('core', 'permissions'): {'name': 'Permissions Management'},
    ('core', 'files'): {'name': 'File & Storage Management'},
    ('core', 'locations'): {'name': 'Locations & Facilities'},
    ('core', 'branches'): {'name': 'Branches & Studios'},
    ('core', 'departments'): {'name': 'Department Management'},

    ('crm', 'dashboard'): {'name': 'CRM Dashboard & Sales Analytics'},
    ('crm', 'leads'): {'name': 'Lead Management'},
    ('crm', 'pipeline'): {'name': 'Sales Pipeline'},
    ('crm', 'trials'): {'name': 'Free Trial Passes'},
    ('crm', 'campaigns'): {'name': 'SMS & WhatsApp Campaigns'},
    ('crm', 'follow-ups'): {'name': 'Tasks & Follow-ups'},
    ('crm', 'communications'): {'name': 'Communications'},
    ('crm', 'telephony'): {'name': 'Telephony & Calling'},
    ('crm', 'settings'): {'name': 'CRM Setup & Settings'},

    ('members', 'client-360'): {'name': 'Member 360 & Profiles'},
    ('members', 'memberships'): {'name': 'Membership Plans & Packages'},
    ('members', 'attendance'): {'name': 'Check-In & Attendance'},
    ('members', 'renewals'): {'name': 'Renewals & Upgrades'},
    ('members', 'freeze'): {'name': 'Membership Freeze'},
    ('members', 'transfers'): {'name': 'Branch Transfers'},

    ('ops', 'calendar'): {'name': 'Master Facility Calendar'},
    ('ops', 'classes'): {'name': 'Group Fitness Classes & Schedules'},
    ('ops', 'bookings'): {'name': 'Class Slot Bookings'},
    ('ops', 'personal-training'): {'name': 'Personal Training'},
    ('ops', 'pilates'): {'name': 'Pilates & Reformer'},
    ('ops', 'trainers'): {'name': 'Trainer Rosters & Shifts'},

    ('cms', 'banners'): {'name': 'Home Screen Banners'},
    ('cms', 'categories'): {'name': 'Program Categories & Media'},
    ('cms', 'media'): {'name': 'Media Library (Audio/Video/Image)'},

    ('performance', 'workouts'): {'name': 'Workout Tracking'},
    ('performance', 'analytics'): {'name': 'Performance Analytics'},
    ('performance', 'pr-tracker'): {'name': 'Personal Records Tracker'},
    ('performance', 'leaderboards'): {'name': 'Studio Leaderboards'},

    ('finance', 'invoices'): {'name': 'Invoices & Billing'},
    ('finance', 'payments'): {'name': 'Payments & Transactions'},
    ('finance', 'refunds'): {'name': 'Refunds & Adjustments'},
    ('finance', 'outstanding'): {'name': 'Dues & Outstanding Balances'},
    ('finance', 'expenses'): {'name': 'Expenses & Disbursements'},
    ('finance', 'revenue'): {'name': 'Revenue Analytics'},

    ('reports', 'business'): {'name': 'Business & Operational Reports'},
    ('reports', 'sales'): {'name': 'Sales Reports'},
    ('reports', 'members'): {'name': 'Member Lifecycle Reports'},
    ('reports', 'trainers'): {'name': 'Trainer Utilization Reports'},
    ('reports', 'financial'): {'name': 'Financial Reports'},
    ('reports', 'performance'): {'name': 'Performance Reports'},

    ('automation', 'workflows'): {'name': 'Automation Workflows'},
    ('automation', 'executions'): {'name': 'Trigger Executions & History'},

    ('cs', 'member-health'): {'name': 'Member Health & Retention'},
    ('cs', 'at-risk'): {'name': 'At-Risk Churn Detection'},
    ('cs', 'feedback'): {'name': 'Feedback & NPS'},
    ('cs', 'grievances'): {'name': 'Member Grievances & Tickets'},
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

    # Ensure all canonical modules exist in ModuleCatalog
    for m_code, m_meta in CANONICAL_MODULES.items():
        m_obj = ModuleCatalog.objects.using(db_alias).filter(module_code__iexact=m_code).first()
        if not m_obj:
            m_obj, _ = ModuleCatalog.objects.using(db_alias).get_or_create(
                module_code=m_code.lower(),
                defaults={
                    'name': m_meta['name'],
                    'description': m_meta.get('description', ''),
                    'is_enabled': True,
                    'status': 'ACTIVE',
                }
            )
        elif not m_obj.is_enabled or m_obj.status != 'ACTIVE':
            m_obj.is_enabled = True
            m_obj.status = 'ACTIVE'
            m_obj.save(using=db_alias, update_fields=['is_enabled', 'status'])

    # Ensure all canonical submodules exist in SubmoduleCatalog
    all_mods_current = {m.module_code.lower(): m for m in ModuleCatalog.objects.using(db_alias).all()}
    for (m_code, sm_code), sm_meta in CANONICAL_SUBMODULES.items():
        parent_mod = all_mods_current.get(m_code.lower())
        if parent_mod:
            sm_obj = SubmoduleCatalog.objects.using(db_alias).filter(
                module=parent_mod, submodule_code__iexact=sm_code
            ).first()
            if not sm_obj:
                SubmoduleCatalog.objects.using(db_alias).create(
                    module=parent_mod,
                    submodule_code=sm_code.lower(),
                    name=sm_meta['name'],
                    description=sm_meta.get('description', ''),
                    is_enabled=True,
                    status='ACTIVE',
                )
            elif not sm_obj.is_enabled or sm_obj.status != 'ACTIVE':
                sm_obj.is_enabled = True
                sm_obj.status = 'ACTIVE'
                sm_obj.save(using=db_alias, update_fields=['is_enabled', 'status'])

    # Also entitle modules in Master DB TenantModule if active tenant exists
    try:
        from apps.master.models_tenant import Tenant
        from apps.master.models_saas import ProductModule, TenantModule
        master_tenants = Tenant.objects.using('default').filter(status='ACTIVE')
        for t_obj in master_tenants:
            for p_mod in ProductModule.objects.using('default').filter(is_active=True):
                TenantModule.objects.using('default').update_or_create(
                    tenant=t_obj,
                    module=p_mod,
                    defaults={'is_enabled': True, 'status': 'ENABLED', 'availability_mode': 'ALL_BRANCHES'}
                )
    except Exception as tm_err:
        logger.debug("Notice: auto-entitle in master: %s", tm_err)

    # Load all available permissions and modules in the tenant DB
    all_modules = {m.module_code.lower(): m for m in ModuleCatalog.objects.using(db_alias).all()}
    all_submodules = {
        (sm.module.module_code.lower(), sm.submodule_code.lower()): sm
        for sm in SubmoduleCatalog.objects.using(db_alias).select_related('module').all()
    }

    # Auto-link any existing Permission records whose module/submodule foreign key is NULL
    for perm in Permission.objects.using(db_alias).filter(module__isnull=True):
        p_code = perm.permission_code or perm.code or ''
        parts = p_code.split('.')
        m_code = parts[0].lower() if parts else None
        sm_code = parts[1].lower() if len(parts) > 2 else None
        m_obj = all_modules.get(m_code)
        sm_obj = all_submodules.get((m_code, sm_code)) if sm_code else None
        update_fields = []
        if m_obj and perm.module_id != m_obj.id:
            perm.module = m_obj
            update_fields.append('module')
        if sm_obj and perm.submodule_id != sm_obj.id:
            perm.submodule = sm_obj
            update_fields.append('submodule')
        if update_fields:
            perm.save(using=db_alias, update_fields=update_fields)

    all_perms = {p.permission_code.lower(): p for p in Permission.objects.using(db_alias).select_related('module', 'submodule').all()}

    # Pre-seed any permissions from DEFAULT_ROLES_CONFIG that don't exist yet in tenant DB
    for r_conf in DEFAULT_ROLES_CONFIG.values():
        for p_code in r_conf.get('permissions', []):
            if p_code.lower() not in all_perms:
                parts = p_code.split('.')
                mod_code = parts[0]
                submod_code = parts[1] if len(parts) > 2 else None
                mod_obj = all_modules.get(mod_code.lower())
                submod_obj = all_submodules.get((mod_code.lower(), submod_code.lower())) if submod_code else None
                p_obj, _ = Permission.objects.using(db_alias).get_or_create(
                    permission_code=p_code,
                    defaults={
                        'module': mod_obj,
                        'submodule': submod_obj,
                        'code': p_code,
                        'label': p_code.replace('.', ' ').title(),
                        'action': parts[-1],
                    }
                )
                all_perms[p_code.lower()] = p_obj

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
                    if not p_obj:
                        parts = p_code.split('.')
                        mod_code = parts[0]
                        submod_code = parts[1] if len(parts) > 2 else None
                        mod_obj = all_modules.get(mod_code.lower())
                        submod_obj = all_submodules.get((mod_code.lower(), submod_code.lower())) if submod_code else None
                        p_obj, _ = Permission.objects.using(db_alias).get_or_create(
                            permission_code=p_code,
                            defaults={
                                'module': mod_obj,
                                'submodule': submod_obj,
                                'code': p_code,
                                'label': p_code,
                                'action': parts[-1],
                            }
                        )
                        all_perms[p_code.lower()] = p_obj
                    if p_obj:
                        target_perms.append(p_obj)

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

            # If overwrite_custom is True, revoke permissions that are no longer in target_perms
            if overwrite_custom and not config.get('all_permissions'):
                target_perm_ids = [p.id for p in target_perms]
                RolePermissionSetItem.objects.using(db_alias).filter(
                    permission_set=perm_set
                ).exclude(
                    permission_id__in=target_perm_ids
                ).update(granted=False, is_allowed=False)

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
            if overwrite_custom and not config.get('all_permissions'):
                enabled_mod_ids = [m.id for m in enabled_modules]
                RoleModuleAccess.objects.using(db_alias).filter(
                    role=role
                ).exclude(
                    module_id__in=enabled_mod_ids
                ).update(can_access=False, is_visible=False)

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
            if overwrite_custom and not config.get('all_permissions'):
                enabled_sm_ids = [sm.id for sm in enabled_submodules]
                RoleSubmoduleAccess.objects.using(db_alias).filter(
                    role=role
                ).exclude(
                    submodule_id__in=enabled_sm_ids
                ).update(can_access=False, is_visible=False)

            results[role_code] = {
                'role_id': str(role.id),
                'role_name': role.name,
                'created': created,
                'granted_permissions': granted_count,
                'modules_enabled': len(enabled_modules),
                'submodules_enabled': len(enabled_submodules),
            }

    return results
