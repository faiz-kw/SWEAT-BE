'''
Production & Staging Deployment Seeder Management Command.
Executes an end-to-end, fully idempotent seed workflow across:
  1. Master DB SaaS Catalog (Metrics, Modules, Submodules, Permissions, Plans, Marketplace)
  2. Platform IAM Super Administrator Account
  3. Master Tenant Record, Domain & SaaS Subscription
  4. Tenant Database Provisioning / Connection & Dynamic Migration
  5. Tenant Product Catalog & RBAC Matrix Synchronization
  6. Canonical System Role Permissions (ORG_ADMIN, BRANCH_MANAGER, FRONT_DESK, TRAINER, etc.)
  7. Tenant Organizational Topology (Organization, Location, Branch)
  8. Tenant Staff & Administrative User Accounts with RBAC Role Assignments
  9. Default Fitness Programs & Membership Packages with Pricing & Branch Availability
 10. Default CRM Pipeline Lead Sources

Usage:
    python manage.py seed_deployment
    python manage.py seed_deployment --tenant-slug=sweat --admin-email=admin@sweat.com
'''

import sys
import uuid
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.core.management import call_command
from django.db import transaction, connections
from django.utils import timezone
from django.conf import settings

# Master DB Models
from apps.master.models_iam import (
    PlatformUser, PlatformRole, PlatformPermission,
    PlatformRolePermission, PlatformUserRole
)
from apps.master.models_tenant import Tenant, TenantDomain
from apps.master.models_saas import (
    SaasPlan, SaasPlanPrice, TenantSubscription
)
from apps.master.models_infra import TenantDataSource
from apps.master.provisioning import sync_tenant_catalog_and_rbac

# Tenant DB Models & Utilities
from config.tenant_middleware import _register_tenant_connection
from config.routers import build_tenant_db_alias, set_tenant_db_alias, get_tenant_db_alias
from apps.tenant_core.rbac_defaults import sync_default_role_permissions


class Command(BaseCommand):
    help = "Idempotently seeds all master catalog data, platform admins, primary tenant, RBAC, and starter packages for deployment."

    def add_arguments(self, parser):
        parser.add_argument(
            '--platform-email',
            type=str,
            default='admin.local@platform.local',
            help='Platform Superadmin email address (default: admin.local@platform.local)',
        )
        parser.add_argument(
            '--platform-password',
            type=str,
            default='PlatformAdmin@123!',
            help='Platform Superadmin password (default: PlatformAdmin@123!)',
        )
        parser.add_argument(
            '--tenant-name',
            type=str,
            default='SWEAT',
            help='Primary tenant commercial brand name (default: SWEAT)',
        )
        parser.add_argument(
            '--tenant-slug',
            type=str,
            default='sweat',
            help='Primary tenant URL slug (default: sweat)',
        )
        parser.add_argument(
            '--admin-email',
            type=str,
            default='admin@sweat.com',
            help='Tenant Administrator email address (default: admin@sweat.com)',
        )
        parser.add_argument(
            '--admin-password',
            type=str,
            default='SweatAdmin@123!',
            help='Tenant Administrator password (default: SweatAdmin@123!)',
        )
        parser.add_argument(
            '--reset-passwords',
            action='store_true',
            default=True,
            help='Force-reset passwords for seeded platform and tenant accounts to ensure predictable deployment access',
        )
        parser.add_argument(
            '--skip-packages',
            action='store_true',
            default=False,
            help='Skip creation of starter membership packages and programs',
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("=================================================================="))
        self.stdout.write(self.style.MIGRATE_HEADING("  PERFORMANCEOS / SWEAT - ENTERPRISE DEPLOYMENT SEEDER            "))
        self.stdout.write(self.style.MIGRATE_HEADING("=================================================================="))

        platform_email = options['platform_email']
        platform_password = options['platform_password']
        tenant_name = options['tenant_name']
        tenant_slug = options['tenant_slug'].lower().strip()
        admin_email = options['admin_email']
        admin_password = options['admin_password']
        reset_passwords = options['reset_passwords']
        skip_packages = options['skip_packages']

        # ------------------------------------------------------------------
        # Step 1: Run Catalog Seeding on Master DB
        # ------------------------------------------------------------------
        self.stdout.write("\n[1/7] Seeding Master DB Product & Permission Catalog...")
        try:
            call_command('seed_catalog', verbosity=0)
            self.stdout.write(self.style.SUCCESS("  [OK] Master Catalog (plans, modules, submodules, permissions) seeded."))
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f"  [ERROR] Failed to seed master catalog: {exc}"))
            raise

        # ------------------------------------------------------------------
        # Step 2: Ensure Platform Super Administrator
        # ------------------------------------------------------------------
        self.stdout.write("\n[2/7] Ensuring Platform Superadmin User...")
        platform_user, created_user = PlatformUser.objects.using('default').get_or_create(
            email=platform_email,
            defaults={
                'first_name': 'Platform',
                'last_name': 'Superadmin',
                'is_active': True,
                'is_staff': True,
                'is_superuser': True,
            }
        )
        if reset_passwords or created_user:
            platform_user.set_password(platform_password)
            platform_user.is_active = True
            platform_user.is_staff = True
            platform_user.is_superuser = True
            platform_user.save(using='default')
            self.stdout.write(self.style.SUCCESS(f"  [OK] Platform Superadmin configured: {platform_email} (Password set)"))
        else:
            self.stdout.write(self.style.SUCCESS(f"  [OK] Platform Superadmin verified: {platform_email}"))

        # Assign Platform SUPER_ADMIN role if exists
        super_role = PlatformRole.objects.using('default').filter(code='SUPER_ADMIN').first()
        if super_role:
            PlatformUserRole.objects.using('default').get_or_create(
                user=platform_user,
                role=super_role,
            )

        # ------------------------------------------------------------------
        # Step 3: Ensure Master Tenant Record, Domain & Subscription
        # ------------------------------------------------------------------
        self.stdout.write(f"\n[3/7] Ensuring Master Tenant Record for '{tenant_name}' ({tenant_slug})...")
        tenant, created_tenant = Tenant.objects.using('default').get_or_create(
            slug=tenant_slug,
            defaults={
                'name': tenant_name,
                'code': tenant_slug.upper()[:20],
                'status': 'ACTIVE',
                'country': 'IN',
                'currency': 'INR',
                'timezone': 'Asia/Kolkata',
                'default_language': 'en',
            }
        )
        if tenant.status != 'ACTIVE':
            tenant.status = 'ACTIVE'
            tenant.save(using='default')
        self.stdout.write(self.style.SUCCESS(f"  [OK] Master Tenant record active (ID: {tenant.id})"))

        # Configure Tenant Domain
        primary_domain = f"{tenant_slug}.performanceos.io"
        TenantDomain.objects.using('default').get_or_create(
            tenant=tenant,
            domain=primary_domain,
            defaults={'is_primary': True, 'is_verified': True, 'ssl_status': 'ACTIVE'}
        )
        # Also ensure localhost dev domain
        TenantDomain.objects.using('default').get_or_create(
            tenant=tenant,
            domain="localhost",
            defaults={'is_primary': False, 'is_verified': True, 'ssl_status': 'ACTIVE'}
        )

        # Ensure SaaS Subscription
        growth_plan = SaasPlan.objects.using('default').filter(code__in=['PLAN-ENTERPRISE', 'PLAN-GROWTH', 'PLAN-STARTER']).first()
        if growth_plan:
            plan_price = growth_plan.prices.first()
            TenantSubscription.objects.using('default').get_or_create(
                tenant=tenant,
                defaults={
                    'plan': growth_plan,
                    'plan_price': plan_price,
                    'billing_cycle': 'MONTHLY',
                    'status': 'ACTIVE',
                    'currency': 'INR',
                    'billing_amount': plan_price.amount if plan_price else Decimal('19999.00'),
                    'started_at': timezone.now(),
                    'current_period_start': timezone.now(),
                    'current_period_end': timezone.now() + timezone.timedelta(days=365),
                }
            )
            self.stdout.write(self.style.SUCCESS(f"  [OK] Subscribed to SaaS Plan: {growth_plan.name}"))

        # ------------------------------------------------------------------
        # Step 4: Resolve Tenant Database & Register Connection
        # ------------------------------------------------------------------
        self.stdout.write("\n[4/7] Connecting to Tenant Database & Applying Migrations...")
        data_source = TenantDataSource.objects.using('default').filter(tenant=tenant).first()
        if not data_source:
            default_db_name = f"tenant_{tenant_slug.replace('-', '_')}"
            data_source = TenantDataSource.objects.using('default').create(
                tenant=tenant,
                database_name=default_db_name,
                db_name=default_db_name,
                status='ACTIVE',
                connection_role='PRIMARY_RW',
            )
            self.stdout.write(f"  Created TenantDataSource with DB: {default_db_name}")

        tenant_db_name = data_source.database_name or data_source.db_name
        db_alias = build_tenant_db_alias(tenant.id)
        _register_tenant_connection(alias=db_alias, db_name=tenant_db_name, data_source=data_source)

        # Apply migrations to tenant DB
        try:
            call_command('migrate_all_tenants', tenant=tenant_slug, verbosity=0)
            self.stdout.write(self.style.SUCCESS(f"  [OK] Tenant database '{tenant_db_name}' migrations verified."))
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f"  ! migrate_all_tenants notice: {exc}"))

        # Synchronize Tenant Catalog & RBAC from Master
        sync_result = sync_tenant_catalog_and_rbac(db_alias=db_alias)
        self.stdout.write(self.style.SUCCESS(
            f"  [OK] Tenant Catalog synced: {sync_result.get('modules', 0)} modules, {sync_result.get('permissions', 0)} permissions."
        ))

        # Synchronize Default Role Permissions
        sync_default_role_permissions(db_alias=db_alias, overwrite_custom=False)
        self.stdout.write(self.style.SUCCESS("  [OK] Canonical system role permissions templates synchronized."))

        # ------------------------------------------------------------------
        # Steps 5-7: Tenant DB Context Scoped Operations
        # ------------------------------------------------------------------
        old_tenant_alias = get_tenant_db_alias()
        set_tenant_db_alias(db_alias)

        try:
            # --------------------------------------------------------------
            # Step 5: Seed Tenant Organizational Topology
            # --------------------------------------------------------------
            self.stdout.write("\n[5/7] Seeding Tenant Organizational Topology (Org, Location, Branch)...")
            from apps.tenant_core.models_org import Organization, Location, Branch
            from apps.tenant_core.models_govern import OrganizationSettings, BranchSettings

            org = Organization.objects.using(db_alias).first()
            if not org:
                org = Organization.objects.using(db_alias).create(
                    code=f"ORG-{tenant_slug.upper()}",
                    name=f"{tenant_name} International",
                    email=admin_email,
                    phone='+91 9876543210',
                    status='ACTIVE',
                )

            location = Location.objects.using(db_alias).first()
            if not location:
                location = Location.objects.using(db_alias).create(
                    organization=org,
                    code="LOC-MAIN",
                    name="Flagship Center",
                    status='ACTIVE',
                )

            branch = Branch.objects.using(db_alias).first()
            if not branch:
                branch = Branch.objects.using(db_alias).create(
                    organization=org,
                    location=location,
                    code="BR-FLAGSHIP",
                    name="Downtown Flagship",
                    address_line_1="Level 3, Performance Tower, Downtown",
                    phone='+91 9876543211',
                    timezone='Asia/Kolkata',
                    business_open_time='06:00:00',
                    business_close_time='22:00:00',
                    status='ACTIVE',
                )

            # Governance & Branch Settings
            OrganizationSettings.objects.using(db_alias).get_or_create(
                organization=org,
                defaults={
                    'currency': 'INR',
                    'date_format': 'DD/MM/YYYY',
                    'time_format': '12h',
                    'default_timezone': 'Asia/Kolkata',
                    'language': 'en',
                }
            )
            BranchSettings.objects.using(db_alias).get_or_create(
                branch=branch,
                defaults={
                    'business_open_time': '06:00:00',
                    'business_close_time': '22:00:00',
                    'contact_phone': '+91 9876543211',
                }
            )
            self.stdout.write(self.style.SUCCESS(f"  [OK] Organization: {org.name} | Branch: {branch.name} ({branch.code})"))

            # --------------------------------------------------------------
            # Step 6: Seed Staff & Administrative User Accounts
            # --------------------------------------------------------------
            self.stdout.write("\n[6/7] Seeding Staff & Administrative User Accounts...")
            from apps.tenant_core.models_users import TenantUser, UserBranch
            from apps.tenant_core.models_rbac import Role, RoleAssignment
            from apps.tenant_core.models_workforce import UserProfile

            staff_seed_matrix = [
                {
                    'email': admin_email,
                    'password': admin_password,
                    'first_name': 'Super',
                    'last_name': 'Administrator',
                    'role_code': 'ORG_ADMIN',
                    'user_type': 'STAFF',
                },
                {
                    'email': 'frontdesk@sweat.com',
                    'password': 'FrontDesk@123!',
                    'first_name': 'Fiona',
                    'last_name': 'FrontDesk',
                    'role_code': 'FRONT_DESK',
                    'user_type': 'STAFF',
                },
                {
                    'email': 'sales@sweat.com',
                    'password': 'SalesRep@123!',
                    'first_name': 'Sam',
                    'last_name': 'Sales',
                    'role_code': 'SALES_REP',
                    'user_type': 'STAFF',
                },
                {
                    'email': 'trainer@sweat.com',
                    'password': 'Trainer@123!',
                    'first_name': 'Tom',
                    'last_name': 'Trainer',
                    'role_code': 'TRAINER',
                    'user_type': 'STAFF',
                },
            ]

            roles_cache = {r.code: r for r in Role.objects.using(db_alias).filter(organization=org)}

            for account in staff_seed_matrix:
                u_email = account['email']
                user = TenantUser.objects.using(db_alias).filter(email__iexact=u_email).first()
                if not user:
                    user = TenantUser(
                        organization=org,
                        email=u_email,
                        first_name=account['first_name'],
                        last_name=account['last_name'],
                        user_type=account['user_type'],
                        status='ACTIVE',
                        home_branch=branch,
                        is_login_allowed=True,
                        activated_at=timezone.now(),
                    )
                    user.set_password(account['password'])
                    user.save(using=db_alias)
                else:
                    if reset_passwords:
                        user.set_password(account['password'])
                    user.status = 'ACTIVE'
                    user.is_login_allowed = True
                    if not user.home_branch_id:
                        user.home_branch = branch
                    user.save(using=db_alias)

                # Assign Branch
                UserBranch.objects.using(db_alias).get_or_create(
                    user=user,
                    branch=branch,
                    defaults={'relationship_type': 'PRIMARY', 'is_primary': True, 'status': 'ACTIVE'}
                )

                # Assign RBAC Role
                role_obj = roles_cache.get(account['role_code']) or Role.objects.using(db_alias).filter(code=account['role_code']).first()
                if role_obj:
                    RoleAssignment.objects.using(db_alias).get_or_create(
                        organization=org,
                        user=user,
                        role=role_obj,
                        defaults={'scope_type': 'ORGANIZATION', 'status': 'ACTIVE', 'is_active': True}
                    )

                # Ensure UserProfile
                UserProfile.objects.using(db_alias).get_or_create(
                    user=user,
                    defaults={
                        'first_name_snapshot': account['first_name'],
                        'last_name_snapshot': account['last_name'],
                        'preferred_branch': branch,
                        'member_status': 'ACTIVE',
                    }
                )
                self.stdout.write(f"  [OK] User seeded: {u_email.ljust(24)} | Role: {account['role_code']}")

            # --------------------------------------------------------------
            # Step 7: Seed Programs, Packages & CRM Defaults
            # --------------------------------------------------------------
            if not skip_packages:
                self.stdout.write("\n[7/7] Seeding Starter Programs, Membership Packages & CRM Defaults...")
                from apps.tenant_core.models_catalog import (
                    ProgramCategory, Program, Package, PackageVersion,
                    PackagePrice, PackageBranchAvailability
                )
                from apps.tenant_core.models_crm import LeadSource

                # CRM Lead Sources
                lead_sources = [
                    ('WALK_IN', 'Walk-in Inquiry', 'OFFLINE'),
                    ('WEBSITE', 'Official Website', 'DIGITAL'),
                    ('INSTAGRAM', 'Instagram & Meta Ads', 'DIGITAL'),
                    ('REFERRAL', 'Member Referral', 'REFERRAL'),
                    ('CORPORATE', 'Corporate Partner', 'PARTNERSHIP'),
                ]
                for s_code, s_name, s_type in lead_sources:
                    LeadSource.objects.using(db_alias).get_or_create(
                        organization=org,
                        code=s_code,
                        defaults={'name': s_name, 'source_type': s_type, 'status': 'ACTIVE'}
                    )

                # Program Categories & Programs
                cat_gym, _ = ProgramCategory.objects.using(db_alias).get_or_create(
                    organization=org,
                    code="CAT-GYM",
                    defaults={'name': "Strength & Gym Floor", 'status': 'ACTIVE'}
                )
                cat_group, _ = ProgramCategory.objects.using(db_alias).get_or_create(
                    organization=org,
                    code="CAT-STUDIO",
                    defaults={'name': "Group Studio & Pilates", 'status': 'ACTIVE'}
                )

                prog_gym, _ = Program.objects.using(db_alias).get_or_create(
                    organization=org,
                    code="PROG-GYM",
                    defaults={
                        'name': "General Gym & Strength Access",
                        'category': cat_gym,
                        'description': "Full access to free weights, machines, and cardio deck.",
                        'trial_allowed': True,
                        'status': 'ACTIVE',
                    }
                )
                prog_group, _ = Program.objects.using(db_alias).get_or_create(
                    organization=org,
                    code="PROG-GROUP",
                    defaults={
                        'name': "Group Studio & Functional Training",
                        'category': cat_group,
                        'description': "Access to Yoga, HIIT, Zumba, and Reformer Pilates classes.",
                        'trial_allowed': True,
                        'status': 'ACTIVE',
                    }
                )

                # Packages Matrix
                package_definitions = [
                    {
                        'code': 'PKG-VIP-ANNUAL',
                        'name': 'Annual VIP All-Access Membership',
                        'program': prog_gym,
                        'duration_value': 12,
                        'duration_unit': 'MONTH',
                        'price': Decimal('29999.00'),
                        'desc': '365 days of unrestricted gym floor access, locker facilities, and 12 guest passes.',
                    },
                    {
                        'code': 'PKG-MONTHLY-UNLIMITED',
                        'name': 'Monthly Unlimited Fitness Pass',
                        'program': prog_gym,
                        'duration_value': 1,
                        'duration_unit': 'MONTH',
                        'price': Decimal('3499.00'),
                        'desc': 'Flexible 30-day recurring membership with full facility access.',
                    },
                    {
                        'code': 'PKG-10-CLASS',
                        'name': '10-Class Group Studio Pack',
                        'program': prog_group,
                        'duration_value': 90,
                        'duration_unit': 'DAY',
                        'price': Decimal('4999.00'),
                        'desc': '10 class credits valid for any group fitness or yoga class over 90 days.',
                    },
                    {
                        'code': 'PKG-DAY-PASS',
                        'name': 'Single Day Guest Pass',
                        'program': prog_gym,
                        'duration_value': 1,
                        'duration_unit': 'DAY',
                        'price': Decimal('500.00'),
                        'desc': 'Full day access to gym facilities and shower amenities.',
                    },
                ]

                admin_user = TenantUser.objects.using(db_alias).filter(email=admin_email).first()

                for p_def in package_definitions:
                    pkg, _ = Package.objects.using(db_alias).get_or_create(
                        organization=org,
                        code=p_def['code'],
                        defaults={
                            'name': p_def['name'],
                            'program': p_def['program'],
                            'status': 'ACTIVE',
                        }
                    )

                    # Package Version 1
                    pkg_ver, _ = PackageVersion.objects.using(db_alias).get_or_create(
                        package=pkg,
                        version_number=1,
                        defaults={
                            'name_snapshot': p_def['name'],
                            'description_snapshot': p_def['desc'],
                            'duration_value': p_def['duration_value'],
                            'duration_unit': p_def['duration_unit'],
                            'total_days': p_def['duration_value'] * 30 if p_def['duration_unit'] == 'MONTH' else p_def['duration_value'],
                            'validity_days': p_def['duration_value'] * 30 if p_def['duration_unit'] == 'MONTH' else p_def['duration_value'],
                            'status': 'ACTIVE',
                            'effective_from': timezone.now(),
                            'published_at': timezone.now(),
                            'show_on_web': True,
                            'show_on_app': True,
                            'created_by_user': admin_user,
                        }
                    )

                    # Package Price
                    PackagePrice.objects.using(db_alias).get_or_create(
                        package_version=pkg_ver,
                        branch=branch,
                        defaults={
                            'currency': 'INR',
                            'base_price': p_def['price'],
                            'display_price': p_def['price'],
                            'prices_include_tax': True,
                            'tax_percent': Decimal('18.00'),
                            'effective_from': timezone.now(),
                            'status': 'ACTIVE',
                            'created_by_user': admin_user,
                        }
                    )

                    # Branch Availability
                    PackageBranchAvailability.objects.using(db_alias).get_or_create(
                        package=pkg,
                        branch=branch,
                        defaults={'status': 'ENABLED'}
                    )
                    self.stdout.write(f"  [OK] Package seeded: {pkg.code.ljust(22)} | Rs. {p_def['price']}")

        finally:
            set_tenant_db_alias(old_tenant_alias)

        # ------------------------------------------------------------------
        # FINAL DEPLOYMENT SUMMARY
        # ------------------------------------------------------------------
        self.stdout.write("\n" + "=" * 70)
        self.stdout.write(self.style.SUCCESS("[SUCCESS] DEPLOYMENT SEEDING COMPLETED SUCCESSFULLY!"))
        self.stdout.write("=" * 70)
        self.stdout.write(f"Tenant Brand:          {tenant.name} (Slug: {tenant.slug})")
        self.stdout.write(f"Master Database:       {settings.DATABASES['default']['NAME']}")
        self.stdout.write(f"Tenant Database:       {tenant_db_name} (Alias: {db_alias})")
        self.stdout.write(f"Branch:                {branch.name} ({branch.code})")
        self.stdout.write("-" * 70)
        self.stdout.write("CREDENTIALS SUMMARY (Copy & Save Securely):")
        self.stdout.write("  1. Platform Superadmin (Control Plane):")
        self.stdout.write(f"     Email:            {platform_email}")
        self.stdout.write(f"     Password:         {platform_password}")
        self.stdout.write(f"     Admin URL:        http://127.0.0.1:8000/admin/")
        self.stdout.write("")
        self.stdout.write("  2. Tenant Administrator (Business & Branch Operations):")
        self.stdout.write(f"     Email:            {admin_email}")
        self.stdout.write(f"     Password:         {admin_password}")
        self.stdout.write(f"     Tenant Slug:      {tenant_slug}")
        self.stdout.write(f"     App Login URL:    http://localhost:5173/login")
        self.stdout.write("")
        self.stdout.write("  3. Pre-Configured Staff Accounts (Tenant: sweat):")
        self.stdout.write("     - Front Desk:     frontdesk@sweat.com    / FrontDesk@123!")
        self.stdout.write("     - Sales Rep:      sales@sweat.com        / SalesRep@123!")
        self.stdout.write("     - Head Trainer:   trainer@sweat.com      / Trainer@123!")
        self.stdout.write("=" * 70)
