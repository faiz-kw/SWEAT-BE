"""
Seed script for Platform (Super Admin) and Administration Starter Core Models.
Run with: python manage.py seed_admin_platform
"""

from django.core.management.base import BaseCommand
from django.db import models
from apps.tenants.models import Tenant, Location, PlatformPlan, TenantBranding, TenantUsage
from apps.users.models import User, Role, RoleDefinition, PermissionDefinition, RolePermission
from apps.administration.models import Service, ServiceCategory, TenantSettings, CustomForm, SecurityPolicy, AuditLog, AuditAction


class Command(BaseCommand):
    help = 'Seeds Platform Plans, RBAC Permissions, Roles, Services, Settings, and Forms'

    def handle(self, *args, **kwargs):
        self.stdout.write('Seeding Platform & Administration starter models...')

        # 1. Seed Platform Plans
        plans_data = [
            {
                'id': 'PLAN-STARTER',
                'name': 'Starter Studio',
                'code': 'starter',
                'description': 'Essential gym operations for single-location boutique studios.',
                'price_monthly': 7999.00,
                'price_annual': 79990.00,
                'currency': 'INR',
                'max_locations': 1,
                'max_members': 500,
                'max_trainers': 5,
                'ai_voice_minutes': 50,
                'features': ['CRM & Leads', 'Member Check-ins', 'Class Scheduling', 'Basic Invoicing'],
                'is_popular': False
            },
            {
                'id': 'PLAN-GROWTH',
                'name': 'Growth Pro',
                'code': 'growth',
                'description': 'Advanced multi-service fitness centers with personal training and AI automations.',
                'price_monthly': 19999.00,
                'price_annual': 199990.00,
                'currency': 'INR',
                'max_locations': 5,
                'max_members': 2500,
                'max_trainers': 25,
                'ai_voice_minutes': 300,
                'features': ['Multi-Location Hub', 'AI Voice Calling', 'Trainer Copilot', 'Nutrition Builder', 'WhatsApp Automated Alerts', 'Full Financials'],
                'is_popular': True
            },
            {
                'id': 'PLAN-ENTERPRISE',
                'name': 'Enterprise Scale',
                'code': 'enterprise',
                'description': 'Large gym chains and fitness franchises requiring white-label branding and custom AI workflows.',
                'price_monthly': 49999.00,
                'price_annual': 499990.00,
                'currency': 'INR',
                'max_locations': 50,
                'max_members': 50000,
                'max_trainers': 200,
                'ai_voice_minutes': 2000,
                'features': ['Unlimited Locations', 'Custom Subdomains & White-Label', 'Computer Vision Form Analysis', 'Custom Integrations & Webhooks', 'Dedicated Account Manager', '24/7 SLA Support'],
                'is_popular': False
            }
        ]

        for p_data in plans_data:
            PlatformPlan.objects.update_or_create(id=p_data['id'], defaults=p_data)
        self.stdout.write(self.style.SUCCESS('[OK] Platform Plans seeded.'))

        # 2. Get or ensure Primary Tenant
        tenant = Tenant.objects.filter(id='TEN-001').first()
        if not tenant:
            tenant = Tenant.objects.create(
                id='TEN-001',
                name='Elevate Fitness & Performance',
                slug='elevate-fitness',
                status='Active',
                tier='Growth',
                plan=PlatformPlan.objects.filter(id='PLAN-GROWTH').first(),
                contact_email='admin@elevatefitness.com',
                phone='+91 98765 43210',
                currency='INR',
                timezone='Asia/Kolkata',
                max_locations=5,
                max_members=2500
            )

        # Primary Location
        loc1 = Location.objects.filter(id='LOC-001').first()
        if not loc1:
            loc1 = Location.objects.create(
                id='LOC-001',
                tenant=tenant,
                name='Indiranagar Flagship Studio',
                city='Bengaluru',
                address='100ft Road, HAL 2nd Stage, Indiranagar',
                phone='+91 80 4123 4567',
                capacity=200,
                operating_hours='06:00 - 22:00'
            )

        loc2 = Location.objects.filter(id='LOC-002').first()
        if not loc2:
            loc2 = Location.objects.create(
                id='LOC-002',
                tenant=tenant,
                name='Koramangala Performance Center',
                city='Bengaluru',
                address='80ft Road, 4th Block, Koramangala',
                phone='+91 80 4123 4568',
                capacity=150,
                operating_hours='06:00 - 22:00'
            )

        # Branding & Usage
        TenantBranding.objects.update_or_create(
            tenant=tenant,
            defaults={
                'app_name': 'Elevate PerformanceOS',
                'primary_color': '#0f766e',
                'accent_color': '#f59e0b',
                'custom_domain': 'app.elevatefitness.in',
                'cname_verified': True
            }
        )
        TenantUsage.objects.update_or_create(
            tenant=tenant,
            defaults={
                'active_members_count': 1420,
                'locations_count': 2,
                'trainers_count': 18,
                'storage_used_mb': 124.5,
                'ai_minutes_used': 145
            }
        )

        # 3. Seed Comprehensive Granular Permissions
        permissions_catalog = [
            # Platform Permissions (scope='platform')
            ('platform.tenants.view', 'Platform', 'view', 'View Tenant Directory', 'platform', 'View all customer tenant accounts and subscription status'),
            ('platform.tenants.manage', 'Platform', 'manage', 'Manage & Onboard Tenants', 'platform', 'Create, update, suspend, or delete tenant organizations'),
            ('platform.plans.manage', 'Platform', 'manage', 'Manage SaaS Plans & Limits', 'platform', 'Configure tier pricing, quotas, and allowed features'),
            ('platform.roles.manage', 'Platform', 'manage', 'Manage Platform Roles', 'platform', 'Create and modify global platform administrator roles'),
            ('platform.audit.view', 'Platform', 'view', 'Inspect Platform Audit Logs', 'platform', 'Review cross-tenant administrative audit events'),
            ('platform.marketplace.manage', 'Platform', 'manage', 'Manage Marketplace Apps', 'platform', 'Publish and curate partner integrations'),

            # Tenant RBAC & Administration (scope='tenant')
            ('users.view', 'Users', 'view', 'View Staff & Team Members', 'tenant', 'Access staff directory and role clearances'),
            ('users.create', 'Users', 'create', 'Invite / Create Staff Members', 'tenant', 'Add new staff and send email invitations'),
            ('users.edit', 'Users', 'edit', 'Edit Staff Details & Roles', 'tenant', 'Update user profiles, assigned studios, and permissions'),
            ('users.deactivate', 'Users', 'deactivate', 'Deactivate / Remove Staff', 'tenant', 'Suspend or remove staff access'),
            ('roles.view', 'Roles', 'view', 'View Configured Roles', 'tenant', 'Inspect built-in and custom tenant roles'),
            ('roles.manage', 'Roles', 'manage', 'Create & Edit Custom Roles', 'tenant', 'Design custom roles and configure capability matrices'),
            ('permissions.view', 'Permissions', 'view', 'Inspect Capability Matrix', 'tenant', 'View granular permission breakdown across roles'),
            ('locations.view', 'Locations', 'view', 'View Studio Locations', 'tenant', 'List active facilities and studio branches'),
            ('locations.manage', 'Locations', 'manage', 'Manage Studio Locations', 'tenant', 'Create or edit physical facilities and capacity'),
            ('admin.services.manage', 'Services', 'manage', 'Manage Services & Amenities', 'tenant', 'Configure billable fitness services and class catalogs'),
            ('admin.config.manage', 'Configuration', 'manage', 'Update Gym Configuration', 'tenant', 'Edit booking policies, tax rates, and operating hours'),
            ('admin.audit.view', 'Audit Logs', 'view', 'View Tenant Audit Trail', 'tenant', 'Inspect sensitive business and staff operation logs'),
            ('admin.security.manage', 'Security', 'manage', 'Manage Security Policies', 'tenant', 'Configure password rules, session timeouts, and MFA'),

            # Tenant CRM & Sales (scope='tenant')
            ('crm.leads.view', 'CRM', 'view', 'View Leads & Pipeline', 'tenant', 'Access CRM Kanban and lead directory'),
            ('crm.leads.create', 'CRM', 'create', 'Create New Leads', 'tenant', 'Capture inquiries and manual lead entries'),
            ('crm.leads.edit', 'CRM', 'edit', 'Edit Leads & Stages', 'tenant', 'Update lead status, tags, and assigned sales reps'),
            ('crm.leads.delete', 'CRM', 'delete', 'Delete Leads', 'tenant', 'Permanently remove lead entries'),
            ('crm.leads.export', 'CRM', 'export', 'Export Leads to CSV', 'tenant', 'Download customer contact lists'),

            # Tenant Members (scope='tenant')
            ('members.view', 'Members', 'view', 'View Members & Profiles', 'tenant', 'Search and inspect client profiles and membership status'),
            ('members.create', 'Members', 'create', 'Onboard New Members', 'tenant', 'Register members and assign membership packages'),
            ('members.edit', 'Members', 'edit', 'Edit Member Details', 'tenant', 'Modify contact info, emergency contacts, and custom attributes'),
            ('members.freeze', 'Members', 'manage', 'Freeze / Pause Membership', 'tenant', 'Apply temporary membership holds and freezes'),
            ('members.delete', 'Members', 'delete', 'Cancel / Terminate Member', 'tenant', 'Process membership terminations'),

            # Tenant Operations & Scheduling (scope='tenant')
            ('ops.schedule.view', 'Operations', 'view', 'View Classes & Calendar', 'tenant', 'Inspect studio class schedules and trainer availability'),
            ('ops.schedule.manage', 'Operations', 'manage', 'Manage Class Schedules', 'tenant', 'Create, reschedule, or cancel group sessions'),
            ('ops.bookings.manage', 'Operations', 'manage', 'Book & Cancel Client Sessions', 'tenant', 'Reserve slots and process client bookings'),
            ('ops.attendance.log', 'Operations', 'create', 'Mark Attendance & Check-ins', 'tenant', 'Record RFID / QR / front desk attendance'),

            # Tenant Finance & Invoicing (scope='tenant')
            ('finance.invoices.view', 'Finance', 'view', 'View Invoices & Payments', 'tenant', 'Inspect billing history, receipts, and collections'),
            ('finance.invoices.create', 'Finance', 'create', 'Generate Invoices & Collect Payments', 'tenant', 'Issue invoices and record manual / gateway payments'),
            ('finance.refunds.manage', 'Finance', 'manage', 'Process Refunds & Vouchers', 'tenant', 'Issue credits, cancellations, and refunds'),
            ('finance.reports.export', 'Finance', 'export', 'Export Financial Statements', 'tenant', 'Download GST tax and revenue reports'),
        ]

        created_perms = {}
        for p_id, module, action, label, p_scope, desc in permissions_catalog:
            perm_obj, _ = PermissionDefinition.objects.update_or_create(
                id=p_id,
                defaults={
                    'module': module,
                    'action': action,
                    'label': label,
                    'scope': p_scope,
                    'description': desc
                }
            )
            created_perms[p_id] = perm_obj
        self.stdout.write(self.style.SUCCESS(f'[OK] {len(created_perms)} Granular Permissions seeded.'))

        # 4. Seed Standard Roles (Platform and Tenant)
        roles_data = [
            # Platform Scope Roles (is_system=True, scope='platform')
            ('ROLE-SUPER-ADMIN', 'Super Admin', 'super_admin', 'platform', 'Full platform super-admin access across all tenants with wildcard capabilities.', True),
            ('ROLE-PLATFORM-ADMIN', 'Platform Administrator', 'platform_admin', 'platform', 'Platform operations, tenant onboarding, and billing management.', True),
            ('ROLE-PLATFORM-SUPPORT', 'Platform Support Specialist', 'platform_support', 'platform', 'Read-only cross-tenant diagnostics and customer support.', True),
            ('ROLE-PLATFORM-AUDITOR', 'Platform Compliance Auditor', 'platform_auditor', 'platform', 'Access to security policies, compliance reports, and audit logs.', True),

            # Tenant Scope Roles (is_system=True, scope='tenant')
            ('ROLE-TENANT-OWNER', 'Tenant Owner', 'tenant_owner', 'tenant', 'Full administrative authority over gym organization, billing, and locations.', True),
            ('ROLE-ADMIN', 'Studio Owner / Admin', 'admin', 'tenant', 'Full operational and administrative access within the tenant organization.', True),
            ('ROLE-MANAGER', 'Studio Manager', 'studio_manager', 'tenant', 'Operations, staff oversight, class scheduling, and member support.', True),
            ('ROLE-SALES', 'Sales Representative', 'sales', 'tenant', 'Lead capture, CRM pipeline management, follow-ups, and conversions.', True),
            ('ROLE-FRONT-DESK', 'Front Desk Staff', 'front_desk', 'tenant', 'Check-ins, POS counter transactions, attendance, and basic bookings.', True),
            ('ROLE-TRAINER', 'Personal Trainer / Coach', 'trainer', 'tenant', 'Client workout programming, assessments, and session tracking.', True),
            ('ROLE-FINANCE', 'Finance Officer', 'finance', 'tenant', 'Invoices, payments, refunds, taxes, and accounting ledger exports.', True),
        ]

        created_roles = {}
        for r_id, r_name, r_code, r_scope, r_desc, is_sys in roles_data:
            role_obj, _ = RoleDefinition.objects.update_or_create(
                id=r_id,
                defaults={
                    'name': r_name,
                    'code': r_code,
                    'scope': r_scope,
                    'description': r_desc,
                    'is_system': is_sys,
                    'tenant': None
                }
            )
            created_roles[r_code] = role_obj

            # Map permissions according to role code
            for p_id, perm_obj in created_perms.items():
                granted = False
                if r_code == 'super_admin':
                    granted = True
                elif r_code == 'platform_admin':
                    granted = perm_obj.scope == 'platform'
                elif r_code == 'platform_support':
                    granted = perm_obj.action == 'view'
                elif r_code == 'platform_auditor':
                    granted = p_id in ['platform.audit.view', 'admin.audit.view']
                elif r_code in ['tenant_owner', 'admin']:
                    granted = perm_obj.scope == 'tenant'
                elif r_code == 'studio_manager':
                    granted = perm_obj.scope == 'tenant' and not p_id.startswith(('admin.security', 'finance.refunds'))
                elif r_code == 'front_desk':
                    granted = p_id in ['ops.attendance.log', 'ops.bookings.manage', 'members.view', 'finance.invoices.create', 'locations.view']
                elif r_code == 'trainer':
                    granted = p_id in ['ops.schedule.view', 'ops.bookings.manage', 'ops.attendance.log', 'members.view', 'locations.view']
                elif r_code == 'sales':
                    granted = p_id.startswith('crm.') or p_id in ['members.view', 'finance.invoices.create', 'locations.view']
                elif r_code == 'finance':
                    granted = p_id.startswith('finance.') or p_id in ['members.view', 'locations.view', 'admin.audit.view']

                RolePermission.objects.update_or_create(
                    role=role_obj,
                    permission=perm_obj,
                    defaults={'granted': granted}
                )

        self.stdout.write(self.style.SUCCESS('[OK] Platform & Tenant Roles with Permission Matrix seeded.'))

        # Backfill existing users with role definitions
        super_admin_role = created_roles.get('super_admin')
        admin_role = created_roles.get('admin')
        
        User.objects.filter(models.Q(id='USR-ADMIN') | models.Q(email='admin') | models.Q(role='Super Admin')).update(
            role='Super Admin',
            role_definition=super_admin_role
        )
        User.objects.filter(role='Admin').exclude(id__in=['USR-ADMIN', 'admin']).update(
            role='Admin',
            role_definition=admin_role
        )
        self.stdout.write(self.style.SUCCESS('[OK] Existing user accounts linked to role definitions.'))


        # 5. Seed Services Catalog
        services_data = [
            ('SVC-001', 'Personal Training (1-on-1)', ServiceCategory.PERSONAL_TRAINING, '60-min personalized session with certified master trainer.', 60, 2500.00, 1),
            ('SVC-002', 'Reformer Pilates Class', ServiceCategory.PILATES, 'High-intensity core & stability session on state-of-the-art reformer beds.', 50, 1200.00, 8),
            ('SVC-003', 'Comprehensive Fitness Assessment', ServiceCategory.FITNESS, 'InBody 770 scan, functional movement screen, and mobility breakdown.', 45, 1500.00, 1),
            ('SVC-004', 'Clinical Sports Nutrition Consult', ServiceCategory.NUTRITION, 'Personalized macro & meal plan design by a clinical nutritionist.', 60, 3000.00, 1),
            ('SVC-005', 'Infrared Sauna & Recovery Suite', ServiceCategory.RECOVERY, 'Private infrared heat therapy for muscle repair and detoxification.', 30, 800.00, 2),
            ('SVC-006', 'High-Octane Group Strength', ServiceCategory.FITNESS, 'Functional strength and athletic conditioning group class.', 45, 600.00, 20),
        ]

        for s_id, s_name, s_cat, s_desc, s_dur, s_price, s_cap in services_data:
            Service.objects.update_or_create(
                id=s_id,
                tenant=tenant,
                defaults={
                    'name': s_name,
                    'category': s_cat,
                    'description': s_desc,
                    'duration_minutes': s_dur,
                    'price': s_price,
                    'capacity': s_cap,
                    'location': loc1,
                    'is_active': True
                }
            )
        self.stdout.write(self.style.SUCCESS('[OK] Services Catalog seeded.'))

        # 6. Seed Tenant Settings & Security Policy
        TenantSettings.objects.update_or_create(
            tenant=tenant,
            defaults={
                'currency': 'INR',
                'tax_rate_gst': 18.00,
                'tax_id_number': '29ABCDE1234F1Z5',
                'booking_cancellation_window_hours': 12,
                'late_cancellation_fee': 250.00,
                'allow_guest_passes': True,
                'guest_passes_per_month': 2,
                'membership_grace_period_days': 7,
                'allow_member_freeze': True,
                'max_freeze_days_per_year': 60,
                'business_open_time': '06:00',
                'business_close_time': '22:00'
            }
        )

        SecurityPolicy.objects.update_or_create(
            tenant=tenant,
            defaults={
                'enforce_mfa': False,
                'session_timeout_minutes': 120,
                'password_min_length': 8,
                'require_special_character': True,
                'max_failed_attempts_lockout': 5
            }
        )

        # 7. Seed Custom Forms
        CustomForm.objects.update_or_create(
            id='FORM-PARQ',
            tenant=tenant,
            defaults={
                'title': 'Physical Activity Readiness Questionnaire (PAR-Q+)',
                'code': 'parq_plus',
                'description': 'Mandatory medical and cardiovascular clearance form before gym commencement.',
                'is_mandatory': True,
                'fields_schema': [
                    {'name': 'heart_condition', 'label': 'Has your doctor ever said you have a heart condition?', 'type': 'boolean', 'required': True},
                    {'name': 'chest_pain', 'label': 'Do you feel pain in your chest when performing physical activity?', 'type': 'boolean', 'required': True},
                    {'name': 'dizziness', 'label': 'Do you ever lose balance because of dizziness or consciousness?', 'type': 'boolean', 'required': True},
                    {'name': 'bone_joint_problem', 'label': 'Do you have a bone or joint problem that could be aggravated?', 'type': 'boolean', 'required': True},
                    {'name': 'doctor_meds', 'label': 'Are you currently taking any prescription medications for blood pressure or heart condition?', 'type': 'boolean', 'required': True},
                ]
            }
        )

        # 8. Seed Initial Audit Log
        AuditLog.objects.create(
            id='AUD-001',
            tenant=tenant,
            user_email='admin@elevatefitness.com',
            action=AuditAction.CREATE,
            module='Platform',
            entity_type='Tenant',
            entity_id=tenant.id,
            description='Tenant organization Elevate Fitness successfully provisioned.',
            ip_address='127.0.0.1'
        )

        self.stdout.write(self.style.SUCCESS('[OK] PerformanceOS Starter Core Models completely seeded.'))
