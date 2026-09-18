"""
Django management command to seed Phase 1 Master DB Catalog Data.
Usage: python manage.py seed_catalog
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from apps.master.models_saas import (
    ResourceMetric, ProductModule, ProductSubmodule,
    TenantPermissionCatalog, SaasPlan, SaasPlanPrice,
    SaasPlanModule, SaasPlanResourceLimit
)
from apps.master.models_market import MarketplaceIntegration
from apps.master.models_iam import (
    PlatformDepartment, PlatformRole, PlatformModule,
    PlatformPermission, PlatformRoleModuleAccess, PlatformRolePermission
)


class Command(BaseCommand):
    help = 'Seeds dynamic catalog data (plans, modules, permissions, limits, marketplace) into PostgreSQL master DB'

    def handle(self, *args, **options):
        self.stdout.write("[SEED] Starting dynamic catalog seeding...")

        with transaction.atomic(using='default'):
            # 1. Resource Metrics
            metrics_data = [
                {'code': 'ACTIVE_MEMBERS', 'name': 'Active Managed Members', 'unit': 'members', 'description': 'Max active gym members with valid memberships'},
                {'code': 'LOCATIONS', 'name': 'Studio Branches / Locations', 'unit': 'branches', 'description': 'Max active studio locations'},
                {'code': 'ACTIVE_USERS', 'name': 'Active Staff & Trainer Users', 'unit': 'accounts', 'description': 'Max active and invited staff/coach user accounts'},
                {'code': 'STORAGE_MB', 'name': 'Media & Document Storage', 'unit': 'MB', 'description': 'S3 object storage for gym assets and member contracts'},
                {'code': 'AI_VOICE_MINUTES', 'name': 'AI Voice Calling Minutes', 'unit': 'minutes', 'description': 'Automated conversational calling minutes'},
                {'code': 'API_REQUESTS', 'name': 'Monthly API Calls', 'unit': 'requests', 'description': 'External API and webhook quota'},
            ]

            metrics_map = {}
            for m in metrics_data:
                obj, created = ResourceMetric.objects.using('default').update_or_create(
                    code=m['code'],
                    defaults={
                        'name': m['name'],
                        'unit': m['unit'],
                        'description': m['description'],
                        'is_billable': True,
                        'is_active': True,
                    }
                )
                metrics_map[m['code']] = obj
                self.stdout.write(f"  [Metric] {'Created' if created else 'Updated'}: {obj.code}")

            # 2. Product Modules & Submodules
            modules_data = [
                {
                    'code': 'crm', 'name': 'CRM & Sales Automation', 'icon': 'Target', 'is_core': False, 'sort_order': 1,
                    'description': 'Lead capture, pipeline stages, trial bookings, and promotional campaigns.',
                    'submodules': [
                        ('leads', 'Lead Management', 'Capture walk-ins and digital enquiries'),
                        ('pipeline', 'Sales Pipeline', 'Kanban deal stages and conversion tracking'),
                        ('trials', 'Free Trial Passes', 'Trial booking and attendance logging'),
                        ('campaigns', 'SMS & WhatsApp Campaigns', 'Automated broadcast campaigns'),
                        ('ai-calling', 'AI Voice Calling', 'Autonomous AI calling assistant for follow-ups'),
                        ('follow-ups', 'Tasks & Reminders', 'Scheduled sales touchpoints'),
                    ]
                },
                {
                    'code': 'members', 'name': 'Member Lifecycle Management', 'icon': 'Users', 'is_core': True, 'sort_order': 2,
                    'description': 'Member 360, membership tiers, renewals, freeze, and attendance check-in.',
                    'submodules': [
                        ('client-360', 'Member 360', 'Comprehensive member profile and health history'),
                        ('memberships', 'Membership Plans', 'Package creation, billing cycles, and quotas'),
                        ('renewals', 'Renewals & Upgrades', 'Upcoming expiry and renewal automation'),
                        ('attendance', 'Check-In & Attendance', 'QR code, biometric, and RFID check-in'),
                        ('freeze', 'Membership Freeze', 'Temporary membership pause requests'),
                        ('transfers', 'Branch Transfers', 'Cross-branch transfer requests'),
                    ]
                },
                {
                    'code': 'ops', 'name': 'Studio Operations & Classes', 'icon': 'Calendar', 'is_core': True, 'sort_order': 3,
                    'description': 'Class scheduling, trainer roster, room allocation, and personal training.',
                    'submodules': [
                        ('calendar', 'Master Facility Calendar', 'Unified daily view of all studios'),
                        ('classes', 'Group Fitness Classes', 'Yoga, HIIT, Zumba, and Spinning schedules'),
                        ('bookings', 'Class Slot Bookings', 'Roster capacity and waitlist management'),
                        ('personal-training', 'Personal Training', 'PT slot allocation and package deductions'),
                        ('pilates', 'Pilates & Reformer', 'Specialized reformer equipment booking'),
                        ('trainers', 'Trainer Rosters', 'Shift assignments and trainer attendance'),
                    ]
                },
                {
                    'code': 'finance', 'name': 'Finance, Billing & POS', 'icon': 'CreditCard', 'is_core': True, 'sort_order': 4,
                    'description': 'Tax invoices, POS collections, payment gateways, expenses, and revenue reports.',
                    'submodules': [
                        ('invoices', 'Tax Invoices', 'GST-compliant invoice generation'),
                        ('payments', 'Payment Collections', 'Cash, UPI, Card, and Gateway receipts'),
                        ('refunds', 'Refund Processing', 'Cancellations and partial refunds'),
                        ('outstanding', 'Dues & Outstanding', 'Pending payment tracking and auto-reminders'),
                        ('expenses', 'Studio Expenses', 'Operational overheads and vendor payouts'),
                        ('revenue', 'Revenue Analytics', 'Daily, monthly, and annual financial breakdown'),
                    ]
                },
                {
                    'code': 'ai', 'name': 'AI Intelligence & Computer Vision', 'icon': 'Sparkles', 'is_core': False, 'sort_order': 5,
                    'description': 'Computer vision form analysis, AI workout generation, and predictive churn.',
                    'submodules': [
                        ('coach', 'AI Coach Copilot', 'Automated workout and recovery recommendations'),
                        ('trainer-copilot', 'Trainer Assistant', 'Session prep and member progress notes'),
                        ('computer-vision', 'Form & Rep Tracking', 'Camera-based exercise pose detection'),
                        ('live-sessions', 'Live Stream Workouts', 'Virtual class streaming and real-time heart rate'),
                        ('business-intelligence', 'Predictive BI', 'Churn risk scoring and demand forecasting'),
                    ]
                },
                {
                    'code': 'nutrition', 'name': 'Nutrition & Dietetics', 'icon': 'Apple', 'is_core': False, 'sort_order': 6,
                    'description': 'Personalized diet plans, macro tracking, and dietitian consultations.',
                    'submodules': [
                        ('diet-plans', 'Diet Plan Builder', 'Calorie and macro distribution charts'),
                        ('consultations', 'Dietitian Sessions', '1-on-1 consultation booking'),
                        ('food-logs', 'Member Food Journal', 'Daily meal logging and review'),
                        ('supplements', 'Supplement Prescriptions', 'Recommended vitamins and protein protocols'),
                    ]
                },
                {
                    'code': 'inventory', 'name': 'Inventory & Pro Shop POS', 'icon': 'Package', 'is_core': False, 'sort_order': 7,
                    'description': 'Supplements, merchandise, retail stock control, and purchase orders.',
                    'submodules': [
                        ('products', 'Product Catalog', 'SKU management, barcoding, and pricing'),
                        ('stock', 'Live Stock Levels', 'Real-time branch inventory counts'),
                        ('purchases', 'Purchase Orders', 'Vendor procurement and stock in-take'),
                        ('expiry', 'Batch & Expiry Tracker', 'Supplement expiry alerts'),
                    ]
                },
                {
                    'code': 'cs', 'name': 'Customer Success & Retention', 'icon': 'HeartPulse', 'is_core': False, 'sort_order': 8,
                    'description': 'Member health scoring, drop-off alerts, grievance tickets, and NPS feedback.',
                    'submodules': [
                        ('member-health', 'Member Health Index', 'Attendance frequency drop alerts'),
                        ('at-risk', 'At-Risk Members', 'Intervention alerts before cancellation'),
                        ('feedback', 'NPS & Feedback', 'Post-workout feedback ratings'),
                        ('grievances', 'Helpdesk Tickets', 'Service issues and staff escalations'),
                    ]
                },
                {
                    'code': 'performance', 'name': 'Athlete Performance & Tracking', 'icon': 'Activity', 'is_core': False, 'sort_order': 9,
                    'description': 'Workout telemetry, biomechanics scoring, wearable IoT, and leaderboards.',
                    'submodules': [
                        ('workouts', 'Workout Logging', 'Session logs, sets, reps, and RPE'),
                        ('analytics', 'Body Analytics', 'Volume, frequency, and progressive overload graphs'),
                        ('wearables', 'Wearable IoT', 'Garmin, Apple Health, and Whoop biometric sync'),
                        ('leaderboards', 'Leaderboards', 'Gym-floor rankings and challenge scores'),
                        ('pr-tracker', 'PR Tracker', 'Personal records for compound lifts'),
                    ]
                },
                {
                    'code': 'coaching', 'name': 'Coaching & Athlete Development', 'icon': 'Dumbbell', 'is_core': False, 'sort_order': 10,
                    'description': 'Personal trainer allocations and workout program builders.',
                    'submodules': [
                        ('trainers', 'Trainers Roster', 'Certified coach directory and availability'),
                        ('online-coaches', 'Online Coaches', 'Remote training and digital check-ins'),
                        ('nutrition-coaches', 'Nutrition Coaches', 'Certified nutritionists and meal consultants'),
                        ('program-builder', 'Program Builder', 'Custom periodized workout programming'),
                    ]
                },
                {
                    'code': 'support', 'name': 'Support & Grievance Desk', 'icon': 'LifeBuoy', 'is_core': False, 'sort_order': 11,
                    'description': 'Help desk ticketing, issue escalation matrices, and SLA management.',
                    'submodules': [
                        ('tickets', 'Support Tickets', 'Member inquiries and service tickets'),
                        ('escalations', 'Escalations Matrix', 'High-priority grievance tracking'),
                        ('sla', 'SLA Monitor', 'Resolution time compliance dashboards'),
                    ]
                },
                {
                    'code': 'marketing', 'name': 'Marketing & Growth Engine', 'icon': 'Megaphone', 'is_core': False, 'sort_order': 12,
                    'description': 'Lead magnets, broadcast SMS/WhatsApp campaigns, reviews, and referral programs.',
                    'submodules': [
                        ('campaigns', 'Broadcast Campaigns', 'Automated promotional broadcasts'),
                        ('lead-magnets', 'Lead Magnets', 'Free passes, diet guides, and funnel assets'),
                        ('referrals', 'Referral Program', 'Member-get-member reward tracking'),
                        ('reviews', 'Reviews & Reputation', 'Google Business and Trustpilot review management'),
                    ]
                },
                {
                    'code': 'automation', 'name': 'Workflow & Rule Automation', 'icon': 'Workflow', 'is_core': False, 'sort_order': 13,
                    'description': 'Trigger-action workflows, automated member approvals, and webhook rules.',
                    'submodules': [
                        ('workflows', 'Active Workflows', 'Visual trigger-action flow sequences'),
                        ('triggers', 'Event Triggers', 'Check-in, expiry, and payment event hooks'),
                        ('approvals', 'Manager Approvals', 'Refund, freeze, and discount approval queues'),
                        ('notifications', 'Notifications Log', 'Push, SMS, and WhatsApp sent delivery logs'),
                        ('rules', 'Business Rules Engine', 'Access gating and automated billing rules'),
                    ]
                },
                {
                    'code': 'reports', 'name': 'Analytics & Executive BI', 'icon': 'BarChart3', 'is_core': False, 'sort_order': 14,
                    'description': 'Deep business reporting, revenue cohort analytics, and athletic performance KPIs.',
                    'submodules': [
                        ('business', 'Business Reports', 'High-level studio performance and revenue overview'),
                        ('sales', 'Sales Reports', 'Conversion rates, rep leaderboards, and pipeline velocity'),
                        ('members', 'Member Reports', 'Churn, retention, cohort, and attendance breakdowns'),
                        ('trainers', 'Trainer Reports', 'PT utilization, client ratings, and session delivery'),
                        ('financial', 'Financial Reports', 'GST filing, expense summaries, and collection ledger'),
                        ('performance', 'Performance Reports', 'Workout volume and member fitness milestone trends'),
                    ]
                },
            ]

            modules_map = {}
            for m_data in modules_data:
                submods = m_data.pop('submodules')
                mod, created = ProductModule.objects.using('default').update_or_create(
                    code=m_data['code'],
                    defaults=m_data
                )
                modules_map[mod.code] = mod
                self.stdout.write(f"  [Module] {'Created' if created else 'Updated'}: {mod.name}")

                for sub_code, sub_name, sub_desc in submods:
                    submod, s_created = ProductSubmodule.objects.using('default').update_or_create(
                        module=mod,
                        code=sub_code,
                        defaults={'name': sub_name, 'description': sub_desc}
                    )

                    for action in ['view', 'create', 'edit', 'delete']:
                        perm_code = f"{mod.code}.{sub_code}.{action}"
                        TenantPermissionCatalog.objects.using('default').update_or_create(
                            code=perm_code,
                            defaults={
                                'module': mod,
                                'submodule': submod,
                                'action': action,
                                'label': f"Can {action} {sub_name}",
                                'is_active': True,
                            }
                        )

            # 3. SaaS Subscription Plans
            plans_data = [
                {
                    'code': 'PLAN-STARTER',
                    'name': 'Starter Studio Plan',
                    'tier': 'Starter',
                    'description': 'Essential foundation for boutique fitness studios and single-location gyms.',
                    'is_public': True,
                    'is_popular': False,
                    'trial_days': 14,
                    'sort_order': 1,
                    'prices': [
                        ('MONTHLY', 4999.00),
                        ('ANNUAL', 49990.00),
                    ],
                    'included_modules': ['members', 'ops', 'finance'],
                    'limits': {
                        'ACTIVE_MEMBERS': 300,
                        'LOCATIONS': 1,
                        'ACTIVE_USERS': 5,
                        'STORAGE_MB': 2000,
                        'AI_VOICE_MINUTES': 0,
                        'API_REQUESTS': 5000,
                    }
                },
                {
                    'code': 'PLAN-GROWTH',
                    'name': 'Growth Multi-Studio Plan',
                    'tier': 'Growth',
                    'description': 'Advanced automation, CRM, and inventory for scaling fitness clubs and chains.',
                    'is_public': True,
                    'is_popular': True,
                    'trial_days': 14,
                    'sort_order': 2,
                    'prices': [
                        ('MONTHLY', 9999.00),
                        ('ANNUAL', 99990.00),
                    ],
                    'included_modules': ['crm', 'members', 'ops', 'finance', 'nutrition', 'inventory', 'cs'],
                    'limits': {
                        'ACTIVE_MEMBERS': 1200,
                        'LOCATIONS': 3,
                        'ACTIVE_USERS': 20,
                        'STORAGE_MB': 10000,
                        'AI_VOICE_MINUTES': 500,
                        'API_REQUESTS': 25000,
                    }
                },
                {
                    'code': 'PLAN-ENTERPRISE',
                    'name': 'Enterprise Chain Plan',
                    'tier': 'Enterprise',
                    'description': 'Full AI capabilities, unrestricted scaling, custom domains, and dedicated cloud.',
                    'is_public': True,
                    'is_popular': False,
                    'trial_days': 30,
                    'sort_order': 3,
                    'prices': [
                        ('MONTHLY', 24999.00),
                        ('ANNUAL', 249990.00),
                    ],
                    'included_modules': ['crm', 'members', 'ops', 'finance', 'ai', 'nutrition', 'inventory', 'cs'],
                    'limits': {
                        'ACTIVE_MEMBERS': -1,
                        'LOCATIONS': -1,
                        'ACTIVE_USERS': -1,
                        'STORAGE_MB': 50000,
                        'AI_VOICE_MINUTES': 5000,
                        'API_REQUESTS': 100000,
                    }
                },
            ]

            for p_data in plans_data:
                prices = p_data.pop('prices')
                inc_mods = p_data.pop('included_modules')
                limits = p_data.pop('limits')

                plan, created = SaasPlan.objects.using('default').update_or_create(
                    code=p_data['code'],
                    defaults=p_data
                )
                self.stdout.write(f"  [Plan] {'Created' if created else 'Updated'}: {plan.name} ({plan.code})")

                for cycle, amount in prices:
                    SaasPlanPrice.objects.using('default').update_or_create(
                        plan=plan,
                        billing_cycle=cycle,
                        currency='INR',
                        defaults={'amount': amount, 'is_active': True}
                    )

                for mod_code in inc_mods:
                    if mod_code in modules_map:
                        SaasPlanModule.objects.using('default').update_or_create(
                            plan=plan,
                            module=modules_map[mod_code],
                            defaults={'is_included': True}
                        )

                for m_code, limit_val in limits.items():
                    if m_code in metrics_map:
                        SaasPlanResourceLimit.objects.using('default').update_or_create(
                            plan=plan,
                            metric=metrics_map[m_code],
                            defaults={'limit_value': limit_val, 'soft_limit_pct': 80}
                        )

            # 4. Marketplace Integrations
            marketplace_data = [
                {
                    'name': 'WhatsApp Business Cloud API',
                    'code': 'INT-WHATSAPP',
                    'integration_type': 'WHATSAPP',
                    'description': 'Send automated booking confirmations, OTP check-ins, and renewals via WhatsApp.',
                    'provider': 'Meta',
                    'is_free': True,
                    'status': 'ACTIVE',
                },
                {
                    'name': 'Razorpay Payments',
                    'code': 'INT-RAZORPAY',
                    'integration_type': 'PAYMENT',
                    'description': 'Seamless UPI, Cards, and Netbanking payments with automated webhook reconciliation.',
                    'provider': 'Razorpay',
                    'is_free': True,
                    'status': 'ACTIVE',
                },
                {
                    'name': 'Stripe International',
                    'code': 'INT-STRIPE',
                    'integration_type': 'PAYMENT',
                    'description': 'Global card payments and automated recurring subscription billing.',
                    'provider': 'Stripe',
                    'is_free': True,
                    'status': 'ACTIVE',
                },
                {
                    'name': 'Zoom Video Classes',
                    'code': 'INT-ZOOM',
                    'integration_type': 'OTHER',
                    'description': 'Automatically create Zoom links for online group sessions and virtual PT.',
                    'provider': 'Zoom Video Communications',
                    'is_free': False,
                    'price_monthly': 999.00,
                    'status': 'ACTIVE',
                },
                {
                    'name': 'Google Calendar Sync',
                    'code': 'INT-GCAL',
                    'integration_type': 'OTHER',
                    'description': '2-way synchronization between trainer shifts and Google Calendar.',
                    'provider': 'Google LLC',
                    'is_free': True,
                    'status': 'ACTIVE',
                },
            ]

            for int_data in marketplace_data:
                item, created = MarketplaceIntegration.objects.using('default').update_or_create(
                    code=int_data['code'],
                    defaults=int_data
                )
                self.stdout.write(f"  [Marketplace] {'Created' if created else 'Updated'}: {item.name}")

            # 5. Platform Departments & Roles
            dept_data = [
                ('ENG', 'Engineering', 'Core infrastructure and platform maintenance'),
                ('OPS', 'Platform Operations', 'Tenant provisioning and system monitoring'),
                ('BILLING', 'Finance & Billing', 'SaaS subscriptions, dunning, and invoicing'),
                ('SUPPORT', 'Customer Support', 'Tier 2 & 3 tenant issue resolution'),
            ]

            for d_code, d_name, d_desc in dept_data:
                PlatformDepartment.objects.using('default').update_or_create(
                    code=d_code,
                    defaults={'name': d_name, 'description': d_desc, 'is_active': True}
                )

            roles_data = [
                ('SUPER_ADMIN', 'Platform Super Administrator', 'Complete system control over all tenants, billing, and infrastructure', True),
                ('SUPPORT_LEAD', 'Support Operations Lead', 'Tenant support and diagnostics access', False),
                ('BILLING_ADMIN', 'SaaS Billing Specialist', 'Subscription plans, invoices, and payment management', False),
                ('BILLING_SPECIALIST', 'SaaS Billing Specialist', 'Subscription plans, invoices, and payment management', False),
            ]

            role_objs = {}
            for r_code, r_name, r_desc, r_system in roles_data:
                r_obj, _ = PlatformRole.objects.using('default').update_or_create(
                    code=r_code,
                    defaults={
                        'name': r_name,
                        'description': r_desc,
                        'is_system': r_system,
                        'is_active': True,
                    }
                )
                role_objs[r_code] = r_obj
                self.stdout.write(f"  [Role] Created/Updated: {r_name}")

            # 6. Canonical Platform Modules & Permissions
            plat_modules_data = [
                ('tenants', 'Tenant Management', 'Tenant accounts, module entitlements, and provisioning', 'Building2', 1),
                ('billing', 'SaaS Billing & Subscriptions', 'Subscription plans, pricing, invoices, and payment tracking', 'CreditCard', 2),
                ('marketplace', 'Marketplace & Integrations', 'Partner ecosystem apps and tenant installations', 'Store', 3),
                ('modules', 'Product Module Catalog', 'Platform product module definitions and availability', 'Grid', 4),
                ('iam', 'Platform Identity & Access', 'Platform staff users, departments, and roles', 'Shield', 5),
            ]

            plat_mod_objs = {}
            for code, name, desc, icon, sort_order in plat_modules_data:
                mod, _ = PlatformModule.objects.using('default').update_or_create(
                    code=code,
                    defaults={
                        'name': name,
                        'description': desc,
                        'icon': icon,
                        'sort_order': sort_order,
                        'is_active': True,
                    }
                )
                plat_mod_objs[code] = mod

            plat_perms_data = [
                ('tenants.view', 'tenants', 'view', 'View Tenants', 'View tenant accounts, usage metrics, and module entitlements'),
                ('tenants.create', 'tenants', 'create', 'Create Tenant', 'Create and register new tenant accounts'),
                ('tenants.edit', 'tenants', 'edit', 'Edit Tenant', 'Update tenant details, status, modules, and branch assignments'),
                ('tenants.delete', 'tenants', 'delete', 'Delete Tenant', 'Deactivate or suspend tenant accounts'),
                ('tenants.provision', 'tenants', 'provision', 'Provision Tenant', 'Execute automated tenant database and onboarding provisioning'),

                ('billing.view', 'billing', 'view', 'View Billing', 'View subscription plans, tenant subscriptions, and invoices'),
                ('billing.create', 'billing', 'create', 'Create Billing', 'Create subscription plans and tenant subscriptions'),
                ('billing.edit', 'billing', 'edit', 'Edit Billing', 'Update plans, pricing, and change tenant subscription plans'),
                ('billing.delete', 'billing', 'delete', 'Delete Billing', 'Cancel subscriptions and deactivate plans'),

                ('marketplace.view', 'marketplace', 'view', 'View Marketplace', 'View partner integrations and tenant installation status'),
                ('marketplace.create', 'marketplace', 'create', 'Create Integration', 'Publish new marketplace integrations'),
                ('marketplace.edit', 'marketplace', 'edit', 'Edit Integration', 'Toggle installations and update integration configuration'),
                ('marketplace.delete', 'marketplace', 'delete', 'Delete Integration', 'Remove partner integrations from marketplace'),

                ('modules.view', 'modules', 'view', 'View Product Modules', 'Inspect product module catalog'),
                ('modules.edit', 'modules', 'edit', 'Edit Product Modules', 'Configure product module definitions and availability'),

                ('iam.view', 'iam', 'view', 'View Platform IAM', 'Inspect platform staff users and roles'),
                ('iam.create', 'iam', 'create', 'Create Platform User', 'Invite and create platform staff accounts'),
                ('iam.edit', 'iam', 'edit', 'Edit Platform User', 'Update platform staff profiles and role assignments'),
                ('iam.delete', 'iam', 'delete', 'Delete Platform User', 'Deactivate or remove platform staff accounts'),
            ]

            plat_perm_objs = {}
            for code, mod_code, action, label, desc in plat_perms_data:
                mod = plat_mod_objs[mod_code]
                perm, _ = PlatformPermission.objects.using('default').update_or_create(
                    code=code,
                    defaults={'module': mod, 'action': action, 'label': label, 'description': desc}
                )
                plat_perm_objs[code] = perm

            # Role Module Access & Role Permissions
            super_admin = role_objs['SUPER_ADMIN']
            for mod in plat_mod_objs.values():
                PlatformRoleModuleAccess.objects.using('default').update_or_create(
                    role=super_admin, module=mod, defaults={'can_access': True}
                )
            for perm in plat_perm_objs.values():
                PlatformRolePermission.objects.using('default').update_or_create(
                    role=super_admin, permission=perm, defaults={'granted': True}
                )

            # BILLING_ADMIN: billing.* + tenants.view
            r_admin = role_objs['BILLING_ADMIN']
            for mod_key in ['billing', 'tenants']:
                PlatformRoleModuleAccess.objects.using('default').update_or_create(
                    role=r_admin, module=plat_mod_objs[mod_key], defaults={'can_access': True}
                )
            for code in ['billing.view', 'billing.create', 'billing.edit', 'billing.delete', 'tenants.view']:
                PlatformRolePermission.objects.using('default').update_or_create(
                    role=r_admin, permission=plat_perm_objs[code], defaults={'granted': True}
                )

            # BILLING_SPECIALIST: billing.* only
            r_spec = role_objs['BILLING_SPECIALIST']
            PlatformRoleModuleAccess.objects.using('default').update_or_create(
                role=r_spec, module=plat_mod_objs['billing'], defaults={'can_access': True}
            )
            for code in ['billing.view', 'billing.create', 'billing.edit', 'billing.delete']:
                PlatformRolePermission.objects.using('default').update_or_create(
                    role=r_spec, permission=plat_perm_objs[code], defaults={'granted': True}
                )

            support_lead = role_objs['SUPPORT_LEAD']
            support_perm_codes = ['tenants.view', 'billing.view', 'marketplace.view', 'modules.view']
            for mod_key in ['tenants', 'billing', 'marketplace', 'modules']:
                PlatformRoleModuleAccess.objects.using('default').update_or_create(
                    role=support_lead, module=plat_mod_objs[mod_key], defaults={'can_access': True}
                )
            for code in support_perm_codes:
                PlatformRolePermission.objects.using('default').update_or_create(
                    role=support_lead, permission=plat_perm_objs[code], defaults={'granted': True}
                )

        self.stdout.write(self.style.SUCCESS("[SUCCESS] All catalog data successfully seeded into PostgreSQL!"))
