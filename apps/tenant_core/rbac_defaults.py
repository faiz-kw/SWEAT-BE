"""
Canonical Default Role Definitions & RBAC Synchronization Engine.
Defines configurable backend default permissions for all standard roles.
All role permissions persist in the Tenant DB:
  Role, RolePermissionSet, RolePermissionSetItem, RoleModuleAccess, RoleSubmoduleAccess

Tenant Organization Admins can customize these role permissions at any time via /admin/permissions.
"""

import logging
import uuid
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
            # Ops View & In-slot Bookings
            'ops.classes.view', 'ops.calendar.view', 'ops.bookings.view', 'ops.bookings.create',
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
    'support': {'name': 'Support & Grievances', 'description': 'Helpdesk tickets, SLA tracking, and issue escalation.'},
    'nutrition': {'name': 'Nutrition & Meal Plans', 'description': 'Diet plans, nutritional consultations, food logging, and supplements.'},
    'inventory': {'name': 'Inventory & Stock Management', 'description': 'Products, warehouse stock levels, purchase orders, and stock movements.'},
    'coaching': {'name': 'Coaching & Training Services', 'description': 'Trainer profiles, online coaching schedules, and nutrition coaches.'},
    'ai': {'name': 'AI & Intelligence', 'description': 'AI Coach, Trainer Copilot, Business Intelligence, and CV workout form tracking.'},
    'marketing': {'name': 'Marketing & Campaigns', 'description': 'Marketing campaigns, target audiences, and communication templates.'},
    'community': {'name': 'Community Hub', 'description': 'Member social feed, challenges, and leaderboards.'},
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
    ('core', 'audit'): {'name': 'Audit Logs & Governance'},
    ('core', 'security'): {'name': 'Security & Access Control'},

    ('crm', 'dashboard'): {'name': 'CRM Dashboard & Sales Analytics'},
    ('crm', 'leads'): {'name': 'Lead Management'},
    ('crm', 'pipeline'): {'name': 'Sales Pipeline'},
    ('crm', 'trials'): {'name': 'Free Trial Passes'},
    ('crm', 'activities'): {'name': 'Sales Activities'},
    ('crm', 'follow-ups'): {'name': 'Tasks & Follow-ups'},
    ('crm', 'coupons'): {'name': 'Coupons & Discounts'},
    ('crm', 'campaigns'): {'name': 'SMS & WhatsApp Campaigns'},
    ('crm', 'communications'): {'name': 'Communications'},
    ('crm', 'telephony'): {'name': 'Telephony & Calling'},
    ('crm', 'settings'): {'name': 'CRM Setup & Settings'},

    ('members', 'directory'): {'name': 'Member Directory'},
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
    ('ops', 'programs'): {'name': 'Fitness Programs'},

    ('cms', 'banners'): {'name': 'Home Screen Banners'},
    ('cms', 'categories'): {'name': 'Program Categories & Media'},
    ('cms', 'media'): {'name': 'Media Library (Audio/Video/Image)'},

    ('performance', 'workouts'): {'name': 'Workout Tracking'},
    ('performance', 'analytics'): {'name': 'Performance Analytics'},
    ('performance', 'assessments'): {'name': 'Assessments & Evaluations'},
    ('performance', 'pr-tracker'): {'name': 'Personal Records Tracker'},
    ('performance', 'wearables'): {'name': 'Wearables & Recovery'},
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
    ('automation', 'approvals'): {'name': 'Approvals & Requests'},
    ('automation', 'notifications'): {'name': 'Trigger Notifications'},
    ('automation', 'rules'): {'name': 'Business Rules'},
    ('automation', 'executions'): {'name': 'Trigger Executions & History'},

    ('cs', 'member-health'): {'name': 'Member Health & Retention'},
    ('cs', 'at-risk'): {'name': 'At-Risk Churn Detection'},
    ('cs', 'feedback'): {'name': 'Feedback & NPS'},
    ('cs', 'grievances'): {'name': 'Member Grievances & Tickets'},
    ('cs', 'retention'): {'name': 'Retention Strategies'},

    ('support', 'tickets'): {'name': 'Support Tickets'},
    ('support', 'escalations'): {'name': 'Support Escalations'},
    ('support', 'sla'): {'name': 'SLA Monitor'},

    ('nutrition', 'diet-plans'): {'name': 'Diet Plans'},
    ('nutrition', 'consultations'): {'name': 'Diet Consultations'},
    ('nutrition', 'food-logs'): {'name': 'Food Logs'},
    ('nutrition', 'supplements'): {'name': 'Supplements'},

    ('inventory', 'products'): {'name': 'Products Catalogue'},
    ('inventory', 'stock'): {'name': 'Stock Inventory'},
    ('inventory', 'purchases'): {'name': 'Purchases & Orders'},
    ('inventory', 'expiry'): {'name': 'Stock Expiry Tracking'},

    ('coaching', 'trainers'): {'name': 'Trainers Management'},
    ('coaching', 'online-coaches'): {'name': 'Online Coaches'},
    ('coaching', 'nutrition-coaches'): {'name': 'Nutrition Coaches'},

    ('ai', 'coach'): {'name': 'AI Coach'},
    ('ai', 'trainer-copilot'): {'name': 'Trainer Copilot'},
    ('ai', 'business-intelligence'): {'name': 'AI Business Intelligence'},
    ('ai', 'computer-vision'): {'name': 'Computer Vision Form Tracking'},
    ('ai', 'live-sessions'): {'name': 'Live Sessions & Tracking'},

    ('marketing', 'campaigns'): {'name': 'Marketing Campaigns'},
    ('marketing', 'audiences'): {'name': 'Target Audiences'},
    ('marketing', 'templates'): {'name': 'Marketing Templates'},

    ('community', 'hub'): {'name': 'Community Hub'},
}

CANONICAL_TAB_PERMISSIONS = [
    # Members Module
    ('members', 'directory', 'members.directory.view', 'View Member Directory', 'view'),
    ('members', 'client-360', 'members.client-360.view', 'View Member 360 & Profiles', 'view'),
    ('members', 'client-360', 'members.client-360.create', 'Create Member Profiles', 'create'),
    ('members', 'client-360', 'members.client-360.edit', 'Edit Member Profiles', 'edit'),
    ('members', 'client-360', 'members.client-360.delete', 'Archive / Delete Member Profiles', 'delete'),
    ('members', 'memberships', 'members.memberships.view', 'View Memberships & Packages', 'view'),
    ('members', 'memberships', 'members.memberships.create', 'Assign / Create Memberships', 'create'),
    ('members', 'memberships', 'members.memberships.edit', 'Edit Memberships & Manage Status', 'edit'),
    ('members', 'renewals', 'members.renewals.view', 'View Renewals Desk', 'view'),
    ('members', 'renewals', 'members.renewals.create', 'Process Membership Renewals', 'create'),
    ('members', 'freeze', 'members.freeze.view', 'View Membership Freezes', 'view'),
    ('members', 'freeze', 'members.freeze.create', 'Apply Membership Freeze', 'create'),
    ('members', 'freeze', 'members.freeze.edit', 'Update / Cancel Freeze', 'edit'),
    ('members', 'transfers', 'members.transfers.view', 'View Branch Transfers', 'view'),
    ('members', 'transfers', 'members.transfers.create', 'Initiate Branch Transfer', 'create'),
    ('members', 'attendance', 'members.attendance.view', 'View Attendance Logs', 'view'),
    ('members', 'attendance', 'members.attendance.create', 'Check-In Members & Record Attendance', 'create'),
    ('members', 'attendance', 'members.attendance.edit', 'Modify Attendance Records', 'edit'),

    # Operations (Ops) Module
    ('ops', 'classes', 'ops.classes.view', 'View Class Schedules & Timetables', 'view'),
    ('ops', 'classes', 'ops.classes.create', 'Create Class Slots & Templates', 'create'),
    ('ops', 'classes', 'ops.classes.edit', 'Edit Class Slots & Capacities', 'edit'),
    ('ops', 'classes', 'ops.classes.delete', 'Delete Class Slots', 'delete'),
    ('ops', 'classes', 'ops.classes.cancel', 'Cancel Class Occurrences', 'cancel'),
    ('ops', 'bookings', 'ops.bookings.view', 'View Bookings & Rosters', 'view'),
    ('ops', 'bookings', 'ops.bookings.create', 'Book Members & Trial Passes', 'create'),
    ('ops', 'bookings', 'ops.bookings.edit', 'Manage Bookings & Attendance', 'edit'),
    ('ops', 'bookings', 'ops.bookings.delete', 'Cancel Bookings', 'delete'),
    ('ops', 'calendar', 'ops.calendar.view', 'View Facility Calendar', 'view'),
    ('ops', 'calendar', 'ops.calendar.create', 'Create Calendar Events', 'create'),
    ('ops', 'calendar', 'ops.calendar.edit', 'Edit Calendar Events', 'edit'),
    ('ops', 'trainers', 'ops.trainers.view', 'View Trainers & Rosters', 'view'),
    ('ops', 'trainers', 'ops.trainers.create', 'Assign Trainers to Shifts', 'create'),
    ('ops', 'trainers', 'ops.trainers.edit', 'Edit Trainer Schedules', 'edit'),
    ('ops', 'personal-training', 'ops.personal-training.view', 'View Personal Training Sessions', 'view'),
    ('ops', 'personal-training', 'ops.personal-training.create', 'Book Personal Training Sessions', 'create'),
    ('ops', 'personal-training', 'ops.personal-training.edit', 'Manage PT Sessions', 'edit'),
    ('ops', 'pilates', 'ops.pilates.view', 'View Pilates & Reformer Studio', 'view'),
    ('ops', 'pilates', 'ops.pilates.create', 'Schedule Pilates Classes', 'create'),
    ('ops', 'pilates', 'ops.pilates.edit', 'Manage Pilates Classes', 'edit'),
    ('ops', 'programs', 'ops.programs.view', 'View Fitness Programs', 'view'),

    # CRM & Sales Automation Module
    ('crm', 'dashboard', 'crm.dashboard.view', 'View CRM Dashboard & Sales Analytics', 'view'),
    ('crm', 'leads', 'crm.leads.view', 'View Leads Desk', 'view'),
    ('crm', 'leads', 'crm.leads.create', 'Create Leads', 'create'),
    ('crm', 'leads', 'crm.leads.edit', 'Edit Leads', 'edit'),
    ('crm', 'leads', 'crm.leads.delete', 'Delete Leads', 'delete'),
    ('crm', 'leads', 'crm.leads.convert', 'Convert Leads to Members', 'convert'),
    ('crm', 'pipeline', 'crm.pipeline.view', 'View Sales Pipeline Deals', 'view'),
    ('crm', 'pipeline', 'crm.pipeline.create', 'Create Pipeline Deals', 'create'),
    ('crm', 'pipeline', 'crm.pipeline.edit', 'Move / Update Deals', 'edit'),
    ('crm', 'trials', 'crm.trials.view', 'View Free Trial Passes', 'view'),
    ('crm', 'trials', 'crm.trials.create', 'Book Trial Passes', 'create'),
    ('crm', 'trials', 'crm.trials.edit', 'Reschedule / Update Trials', 'edit'),
    ('crm', 'activities', 'crm.activities.view', 'View Sales Activities', 'view'),
    ('crm', 'campaigns', 'crm.campaigns.view', 'View Campaigns', 'view'),
    ('crm', 'campaigns', 'crm.campaigns.create', 'Create SMS & WhatsApp Campaigns', 'create'),
    ('crm', 'campaigns', 'crm.campaigns.edit', 'Edit Campaigns', 'edit'),
    ('crm', 'coupons', 'crm.coupons.view', 'View Coupons & Discounts', 'view'),
    ('crm', 'follow-ups', 'crm.follow-ups.view', 'View Follow-ups & Tasks', 'view'),
    ('crm', 'follow-ups', 'crm.follow-ups.create', 'Log Follow-ups & Activities', 'create'),
    ('crm', 'follow-ups', 'crm.follow-ups.edit', 'Update Tasks & Follow-ups', 'edit'),
    ('crm', 'communications', 'crm.communications.view', 'View Communications', 'view'),
    ('crm', 'communications', 'crm.communications.send', 'Send Messages (WhatsApp/SMS/Email)', 'send'),
    ('crm', 'telephony', 'crm.telephony.access', 'Telephony & Outbound Calling (WebPhone)', 'access'),
    ('crm', 'settings', 'crm.settings.view', 'View CRM Settings', 'view'),
    ('crm', 'settings', 'crm.settings.edit', 'Edit CRM Settings', 'edit'),

    # Finance, Billing & POS Module
    ('finance', 'invoices', 'finance.invoices.view', 'View Invoices & Billing', 'view'),
    ('finance', 'invoices', 'finance.invoices.create', 'Generate Invoices', 'create'),
    ('finance', 'invoices', 'finance.invoices.edit', 'Edit Invoices', 'edit'),
    ('finance', 'invoices', 'finance.invoices.delete', 'Cancel / Void Invoices', 'delete'),
    ('finance', 'payments', 'finance.payments.view', 'View Payments & Transactions', 'view'),
    ('finance', 'payments', 'finance.payments.create', 'Collect Payments & POS Transactions', 'create'),
    ('finance', 'payments', 'finance.payments.edit', 'Reconcile Payments', 'edit'),
    ('finance', 'payments', 'finance.payments.delete', 'Void / Delete Payments', 'delete'),
    ('finance', 'refunds', 'finance.refunds.view', 'View Refunds', 'view'),
    ('finance', 'refunds', 'finance.refunds.create', 'Process Refunds', 'create'),
    ('finance', 'refunds', 'finance.refunds.edit', 'Update Refund Requests', 'edit'),
    ('finance', 'outstanding', 'finance.outstanding.view', 'View Dues & Outstanding Balances', 'view'),
    ('finance', 'outstanding', 'finance.outstanding.create', 'Record Dues', 'create'),
    ('finance', 'outstanding', 'finance.outstanding.edit', 'Clear / Adjust Dues', 'edit'),
    ('finance', 'expenses', 'finance.expenses.view', 'View Expenses & Disbursements', 'view'),
    ('finance', 'expenses', 'finance.expenses.create', 'Log Expenses', 'create'),
    ('finance', 'expenses', 'finance.expenses.edit', 'Edit Expenses', 'edit'),
    ('finance', 'revenue', 'finance.revenue.view', 'View Revenue Analytics', 'view'),

    # Reports Module
    ('reports', 'business', 'reports.business.view', 'View Business & Operational Reports', 'view'),
    ('reports', 'sales', 'reports.sales.view', 'View Sales Reports', 'view'),
    ('reports', 'members', 'reports.members.view', 'View Member Lifecycle Reports', 'view'),
    ('reports', 'trainers', 'reports.trainers.view', 'View Trainer Utilization Reports', 'view'),
    ('reports', 'financial', 'reports.financial.view', 'View Financial Reports', 'view'),
    ('reports', 'performance', 'reports.performance.view', 'View Performance Reports', 'view'),

    # Automation & Workflows Module
    ('automation', 'workflows', 'automation.workflows.view', 'View Automation Workflows', 'view'),
    ('automation', 'workflows', 'automation.workflows.create', 'Create Automation Workflows', 'create'),
    ('automation', 'workflows', 'automation.workflows.edit', 'Edit Workflows & Triggers', 'edit'),
    ('automation', 'approvals', 'automation.approvals.view', 'View Automation Approvals', 'view'),
    ('automation', 'notifications', 'automation.notifications.view', 'View Trigger Notifications', 'view'),
    ('automation', 'rules', 'automation.rules.view', 'View Automation Rules', 'view'),
    ('automation', 'executions', 'automation.executions.view', 'View Trigger Executions & History', 'view'),

    # Customer Success (CS) Module
    ('cs', 'member-health', 'cs.member-health.view', 'View Member Health & Retention', 'view'),
    ('cs', 'at-risk', 'cs.at-risk.view', 'View At-Risk Churn Detection', 'view'),
    ('cs', 'feedback', 'cs.feedback.view', 'View Feedback & NPS Scores', 'view'),
    ('cs', 'grievances', 'cs.grievances.view', 'View Member Grievances & Tickets', 'view'),
    ('cs', 'grievances', 'cs.grievances.create', 'Log Grievance Ticket', 'create'),
    ('cs', 'grievances', 'cs.grievances.edit', 'Resolve / Update Grievance Ticket', 'edit'),
    ('cs', 'retention', 'cs.retention.view', 'View Retention Analytics', 'view'),

    # Support & Grievance Module
    ('support', 'tickets', 'support.tickets.view', 'View Support Tickets', 'view'),
    ('support', 'escalations', 'support.escalations.view', 'View Support Escalations', 'view'),
    ('support', 'sla', 'support.sla.view', 'View Support SLA Monitor', 'view'),

    # Nutrition Module
    ('nutrition', 'diet-plans', 'nutrition.diet-plans.view', 'View Nutrition Diet Plans', 'view'),
    ('nutrition', 'consultations', 'nutrition.consultations.view', 'View Nutrition Consultations', 'view'),
    ('nutrition', 'food-logs', 'nutrition.food-logs.view', 'View Member Food Logs', 'view'),
    ('nutrition', 'supplements', 'nutrition.supplements.view', 'View Nutrition Supplements', 'view'),

    # Inventory Module
    ('inventory', 'products', 'inventory.products.view', 'View Inventory Products', 'view'),
    ('inventory', 'stock', 'inventory.stock.view', 'View Stock & Warehouses', 'view'),
    ('inventory', 'purchases', 'inventory.purchases.view', 'View Purchases & Suppliers', 'view'),
    ('inventory', 'expiry', 'inventory.expiry.view', 'View Stock Expiry Logs', 'view'),

    # Coaching Module
    ('coaching', 'trainers', 'coaching.trainers.view', 'View Coaching Trainers', 'view'),
    ('coaching', 'online-coaches', 'coaching.online-coaches.view', 'View Online Coaches', 'view'),
    ('coaching', 'nutrition-coaches', 'coaching.nutrition-coaches.view', 'View Nutrition Coaches', 'view'),

    # AI Module
    ('ai', 'coach', 'ai.coach.view', 'View AI Coach Workspaces', 'view'),
    ('ai', 'trainer-copilot', 'ai.trainer-copilot.view', 'View Trainer Copilot', 'view'),
    ('ai', 'business-intelligence', 'ai.business-intelligence.view', 'View AI Business Intelligence', 'view'),
    ('ai', 'computer-vision', 'ai.computer-vision.view', 'View Computer Vision Form Tracking', 'view'),
    ('ai', 'live-sessions', 'ai.live-sessions.view', 'View Live Session Tracking', 'view'),

    # Marketing Module
    ('marketing', 'campaigns', 'marketing.campaigns.view', 'View Marketing Campaigns', 'view'),

    # Content Management System (CMS) Module
    ('cms', 'banners', 'cms.banners.view', 'View Home Screen Banners', 'view'),
    ('cms', 'banners', 'cms.banners.manage', 'Manage Home Screen Banners', 'manage'),
    ('cms', 'categories', 'cms.categories.view', 'View Program Categories', 'view'),
    ('cms', 'categories', 'cms.categories.manage', 'Manage Program Categories', 'manage'),
    ('cms', 'media', 'cms.media.view', 'View Media Library', 'view'),
    ('cms', 'media', 'cms.media.manage', 'Manage Media Library', 'manage'),

    # Performance & Workouts Module
    ('performance', 'workouts', 'performance.workouts.view', 'View Workout Logs', 'view'),
    ('performance', 'workouts', 'performance.workouts.create', 'Log Workout Data', 'create'),
    ('performance', 'workouts', 'performance.workouts.edit', 'Edit Workout Records', 'edit'),
    ('performance', 'analytics', 'performance.analytics.view', 'View Performance Analytics', 'view'),
    ('performance', 'assessments', 'performance.assessments.view', 'View Member Assessments', 'view'),
    ('performance', 'wearables', 'performance.wearables.view', 'View Wearables & Recovery', 'view'),
    ('performance', 'pr-tracker', 'performance.pr-tracker.view', 'View Personal Records Tracker', 'view'),
    ('performance', 'pr-tracker', 'performance.pr-tracker.create', 'Record PRs', 'create'),
    ('performance', 'pr-tracker', 'performance.pr-tracker.edit', 'Update PRs', 'edit'),
    ('performance', 'leaderboards', 'performance.leaderboards.view', 'View Studio Leaderboards', 'view'),

    # Core System & Administration Module
    ('core', 'settings', 'core.settings.view', 'View System Settings', 'view'),
    ('core', 'settings', 'core.settings.edit', 'Edit System Settings', 'edit'),
    ('core', 'users', 'core.users.view', 'View Tenant Users', 'view'),
    ('core', 'users', 'core.users.create', 'Create Tenant Users', 'create'),
    ('core', 'users', 'core.users.edit', 'Edit Tenant Users', 'edit'),
    ('core', 'users', 'core.users.delete', 'Deactivate / Delete Users', 'delete'),
    ('core', 'roles', 'core.roles.view', 'View Roles', 'view'),
    ('core', 'roles', 'core.roles.create', 'Create Tenant Roles', 'create'),
    ('core', 'roles', 'core.roles.edit', 'Edit Tenant Roles', 'edit'),
    ('core', 'roles', 'core.roles.delete', 'Delete Tenant Roles', 'delete'),
    ('core', 'roles', 'core.roles.assign', 'Assign & Revoke Roles', 'assign'),
    ('core', 'permissions', 'core.permissions.view', 'View Permissions Catalogue', 'view'),
    ('core', 'permissions', 'core.permissions.manage', 'Manage Permissions', 'manage'),
    ('core', 'files', 'core.files.view', 'View Storage Files', 'view'),
    ('core', 'files', 'core.files.create', 'Upload Storage Files', 'create'),
    ('core', 'files', 'core.files.delete', 'Delete Storage Files', 'delete'),
    ('core', 'branches', 'core.branches.view', 'View Branches', 'view'),
    ('core', 'branches', 'core.branches.create', 'Create Branches', 'create'),
    ('core', 'branches', 'core.branches.edit', 'Edit Branches', 'edit'),
    ('core', 'departments', 'core.departments.view', 'View Departments', 'view'),
    ('core', 'departments', 'core.departments.create', 'Create Departments', 'create'),
    ('core', 'departments', 'core.departments.edit', 'Edit Departments', 'edit'),
    ('core', 'audit', 'core.audit.view', 'View Audit Logs', 'view'),
    ('core', 'security', 'core.security.view', 'View Security & Sessions', 'view'),

    # Community Module
    ('community', 'hub', 'community.view', 'View Community Hub', 'view'),
]


def sync_default_role_permissions(
    db_alias: str,
    org=None,
    overwrite_custom: bool = False,
    roles_filter: Optional[List[str]] = None,
) -> Dict[str, dict]:
    """
    Idempotently sync canonical system roles, modules, submodules, and full action permissions catalog
    into the specified tenant database.
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

    # 1. Ensure all canonical modules exist in ModuleCatalog
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

    # 2. Ensure all canonical submodules exist in SubmoduleCatalog
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

    # 3. Entitle modules in Master DB TenantModule if active tenant exists
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

    # 4. Load all available modules and submodules
    all_modules = {m.module_code.lower(): m for m in ModuleCatalog.objects.using(db_alias).all()}
    all_submodules = {
        (sm.module.module_code.lower(), sm.submodule_code.lower()): sm
        for sm in SubmoduleCatalog.objects.using(db_alias).select_related('module').all()
    }

    # 5. Auto-link any existing Permission records whose module/submodule foreign key is NULL
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

    all_perms = {
        (p.permission_code or p.code or '').lower(): p
        for p in Permission.objects.using(db_alias).select_related('module', 'submodule').all()
    }

    # 6. Seed all canonical tab permissions
    for mod_code, submod_code, p_code, label, action in CANONICAL_TAB_PERMISSIONS:
        p_key = p_code.lower()
        p_obj = all_perms.get(p_key)
        mod_obj = all_modules.get(mod_code.lower())
        submod_obj = all_submodules.get((mod_code.lower(), submod_code.lower())) if submod_code else None

        if not mod_obj:
            mod_obj, _ = ModuleCatalog.objects.using(db_alias).get_or_create(
                module_code=mod_code.lower(),
                defaults={
                    'name': CANONICAL_MODULES.get(mod_code.lower(), {}).get('name', mod_code.upper()),
                    'is_enabled': True,
                    'status': 'ACTIVE',
                }
            )
            all_modules[mod_code.lower()] = mod_obj

        if not p_obj:
            src_perm_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"sweat.permission.{p_key}")
            existing_by_src = Permission.objects.using(db_alias).filter(source_permission_id=src_perm_id).first()
            if existing_by_src:
                p_obj = existing_by_src
                p_obj.permission_code = p_code
                p_obj.code = p_code
                p_obj.save(using=db_alias, update_fields=['permission_code', 'code'])
            else:
                p_obj, _ = Permission.objects.using(db_alias).get_or_create(
                    permission_code=p_code,
                    defaults={
                        'source_permission_id': src_perm_id,
                        'module': mod_obj,
                        'submodule': submod_obj,
                        'code': p_code,
                        'label': label,
                        'action': action,
                        'is_active': True,
                        'status': 'ACTIVE',
                    }
                )
            all_perms[p_key] = p_obj
        else:
            update_fields = []
            if mod_obj and p_obj.module_id != mod_obj.id:
                p_obj.module = mod_obj
                update_fields.append('module')
            if submod_obj and p_obj.submodule_id != submod_obj.id:
                p_obj.submodule = submod_obj
                update_fields.append('submodule')
            if label and p_obj.label != label:
                p_obj.label = label
                update_fields.append('label')
            if not p_obj.is_active:
                p_obj.is_active = True
                update_fields.append('is_active')
            if p_obj.status != 'ACTIVE':
                p_obj.status = 'ACTIVE'
                update_fields.append('status')
            if update_fields:
                p_obj.save(using=db_alias, update_fields=update_fields)

    # 7. Seed any remaining permissions from DEFAULT_ROLES_CONFIG
    for r_conf in DEFAULT_ROLES_CONFIG.values():
        for p_code in r_conf.get('permissions', []):
            p_key = p_code.lower()
            if p_key not in all_perms:
                parts = p_code.split('.')
                mod_code = parts[0]
                submod_code = parts[1] if len(parts) > 2 else None
                mod_obj = all_modules.get(mod_code.lower())
                submod_obj = all_submodules.get((mod_code.lower(), submod_code.lower())) if submod_code else None
                if not mod_obj:
                    mod_obj, _ = ModuleCatalog.objects.using(db_alias).get_or_create(
                        module_code=mod_code.lower(),
                        defaults={
                            'name': CANONICAL_MODULES.get(mod_code.lower(), {}).get('name', mod_code.upper()),
                            'is_enabled': True,
                            'status': 'ACTIVE',
                        }
                    )
                    all_modules[mod_code.lower()] = mod_obj
                src_perm_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"sweat.permission.{p_key}")
                p_obj, _ = Permission.objects.using(db_alias).get_or_create(
                    permission_code=p_code,
                    defaults={
                        'source_permission_id': src_perm_id,
                        'module': mod_obj,
                        'submodule': submod_obj,
                        'code': p_code,
                        'label': p_code.replace('.', ' ').title(),
                        'action': parts[-1],
                        'is_active': True,
                        'status': 'ACTIVE',
                    }
                )
                all_perms[p_key] = p_obj

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
                        if not mod_obj:
                            mod_obj, _ = ModuleCatalog.objects.using(db_alias).get_or_create(
                                module_code=mod_code.lower(),
                                defaults={
                                    'name': CANONICAL_MODULES.get(mod_code.lower(), {}).get('name', mod_code.upper()),
                                    'is_enabled': True,
                                    'status': 'ACTIVE',
                                }
                            )
                            all_modules[mod_code.lower()] = mod_obj
                        src_perm_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"sweat.permission.{p_code.lower()}")
                        p_obj, _ = Permission.objects.using(db_alias).get_or_create(
                            permission_code=p_code,
                            defaults={
                                'source_permission_id': src_perm_id,
                                'module': mod_obj,
                                'submodule': submod_obj,
                                'code': p_code,
                                'label': p_code.replace('.', ' ').title(),
                                'action': parts[-1],
                                'is_active': True,
                                'status': 'ACTIVE',
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
