"""
Management command to seed realistic, relational SaaS Platform Core and Administration data.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
import datetime
import uuid

from apps.tenants.models import (
    Tenant, Location, PlatformPlan, TenantBranding, TenantUsage,
    MarketplaceApp, TenantAppInstallation, HistoricalUsageSnapshot,
    TenantStatus, TenantTier, MarketplaceCategory
)
from apps.users.models import User, Role, RoleDefinition, PermissionDefinition, RolePermission
from apps.administration.models import (
    Service, ServiceCategory, TenantSettings, CustomForm, ApiKey, AuditLog, AuditAction, SecurityPolicy
)
from apps.members.models import Member, MemberStatus
from apps.finance.models import Invoice, Payment



class Command(BaseCommand):
    help = "Seed platform-wide plans, tenants, usage records, marketplace apps, and administrative data."

    @transaction.atomic
    def handle(self, *args, **options):
        self.stdout.write(self.style.NOTICE("Seeding Platform Plans..."))
        plans = [
            {
                'id': 'PLAN-STARTER',
                'name': 'Starter',
                'code': 'STARTER',
                'description': 'Essential tier for boutique studios and single-location fitness centers.',
                'price_monthly': 7999.00,
                'price_annual': 79990.00,
                'max_locations': 1,
                'max_members': 500,
                'max_trainers': 10,
                'ai_voice_minutes': 100,
                'features': ['CRM & Leads', 'Member Check-ins', 'Class Booking', 'POS Invoicing'],
                'is_popular': False,
            },
            {
                'id': 'PLAN-GROWTH',
                'name': 'Growth',
                'code': 'GROWTH',
                'description': 'Multi-location operations with AI calling, advanced reporting, and mobile check-in.',
                'price_monthly': 19999.00,
                'price_annual': 199990.00,
                'max_locations': 5,
                'max_members': 2500,
                'max_trainers': 25,
                'ai_voice_minutes': 300,
                'features': ['Multi-Studio Management', 'Automated WhatsApp & Email', 'AI TeleCMI Voice Calling', 'Trainer Commission Tracking', 'Financial ERP Sync'],
                'is_popular': True,
            },
            {
                'id': 'PLAN-ENTERPRISE',
                'name': 'Enterprise',
                'code': 'ENTERPRISE',
                'description': 'Franchise networks with dedicated instance, unlimited locations, custom white-label, and SLA.',
                'price_monthly': 49999.00,
                'price_annual': 499990.00,
                'max_locations': 50,
                'max_members': 50000,
                'max_trainers': 500,
                'ai_voice_minutes': 5000,
                'features': ['Unlimited Sub-Branches', 'White-Label Mobile App & Portal', 'Custom Domain & CNAME', 'Dedicated Infrastructure', '24/7 Priority SLA & Account Director'],
                'is_popular': False,
            },
        ]

        plan_objs = {}
        for p in plans:
            obj, _ = PlatformPlan.objects.update_or_create(
                id=p['id'],
                defaults=p
            )
            plan_objs[p['code']] = obj

        self.stdout.write(self.style.NOTICE("Seeding Marketplace Catalog Apps..."))
        marketplace_catalog = [
            {
                'id': 'app-sarvam',
                'name': 'Sarvam AI Voice Engine',
                'category': MarketplaceCategory.AI_VOICE,
                'icon_text': 'SV',
                'developer': 'Sarvam AI',
                'description': 'Multilingual Indian voice agents for automated lead qualification, trial booking, and renewal follow-ups in Hindi, Tamil, Telugu, and English.',
                'price_monthly': 0.00,
                'required_tier': TenantTier.GROWTH,
                'is_popular': True,
            },
            {
                'id': 'app-razorpay',
                'name': 'Razorpay Subscriptions & POS',
                'category': MarketplaceCategory.PAYMENTS,
                'icon_text': 'RP',
                'developer': 'Razorpay Inc.',
                'description': 'Automated recurring UPI autopay, debit/credit cards, and POS counter billing with instantaneous GST invoice generation.',
                'price_monthly': 0.00,
                'required_tier': TenantTier.STARTER,
                'is_popular': True,
            },
            {
                'id': 'app-gupshup',
                'name': 'Gupshup WhatsApp Business API',
                'category': MarketplaceCategory.MESSAGING,
                'icon_text': 'WA',
                'developer': 'Gupshup',
                'description': 'Official WhatsApp messaging for instant booking confirmations, diet plan PDFs, workout reminders, and renewal nudges.',
                'price_monthly': 0.00,
                'required_tier': TenantTier.STARTER,
                'is_popular': True,
            },
            {
                'id': 'app-kisi',
                'name': 'Kisi Turnstile & RFID Gates',
                'category': MarketplaceCategory.ACCESS_CONTROL,
                'icon_text': 'KS',
                'developer': 'Kisi Cloud Access',
                'description': 'Hardware cloud access controller syncing member subscription validity directly with gym turnstiles and biometric scanners.',
                'price_monthly': 1499.00,
                'required_tier': TenantTier.GROWTH,
                'is_popular': False,
            },
            {
                'id': 'app-stripe',
                'name': 'Stripe Global Billing',
                'category': MarketplaceCategory.PAYMENTS,
                'icon_text': 'ST',
                'developer': 'Stripe',
                'description': 'Multi-currency recurring subscription billing for international members and online coaching clients.',
                'price_monthly': 999.00,
                'required_tier': TenantTier.GROWTH,
                'is_popular': False,
            },
            {
                'id': 'app-quickbooks',
                'name': 'QuickBooks Online',
                'category': MarketplaceCategory.ACCOUNTING,
                'icon_text': 'QB',
                'developer': 'Intuit',
                'description': 'Automated real-time synchronization of daily sales, invoices, refunds, and payroll expenses to QuickBooks ERP.',
                'price_monthly': 1999.00,
                'required_tier': TenantTier.ENTERPRISE,
                'is_popular': False,
            },
        ]

        marketplace_objs = {}
        for app in marketplace_catalog:
            app_obj, _ = MarketplaceApp.objects.update_or_create(
                id=app['id'],
                defaults=app
            )
            marketplace_objs[app['id']] = app_obj

        self.stdout.write(self.style.NOTICE("Seeding Multi-Tenant Brands & Resources..."))
        tenants_data = [
            {
                'id': 'TEN-APEX-01',
                'name': 'Apex Athletics Global',
                'slug': 'apex-athletics',
                'tier': TenantTier.ENTERPRISE,
                'plan': plan_objs['ENTERPRISE'],
                'status': TenantStatus.ACTIVE,
                'contact_email': 'hq@apexathletics.in',
                'phone': '+91 80 4455 6677',
                'website': 'https://apexathletics.in',
                'currency': 'INR',
                'timezone': 'Asia/Kolkata',
                'max_locations': 50,
                'max_members': 5000,
                'enabled_modules': ['/dashboard', '/crm/leads', '/members', '/ops/classes', '/finance/revenue', '/ai/copilot', '/platform'],
                'branding': {
                    'app_name': 'Apex Performance OS',
                    'primary_color': '#0f766e',
                    'accent_color': '#f59e0b',
                    'custom_domain': 'app.apexathletics.in',
                    'cname_verified': True,
                    'email_footer': 'Apex Athletics India Private Limited — Empowering Peak Human Potential',
                },
                'locations': [
                    {'id': 'LOC-APEX-IND', 'name': 'Indiranagar Flagship', 'city': 'Bengaluru', 'address': '100ft Road, HAL 2nd Stage', 'capacity': 250},
                    {'id': 'LOC-APEX-KOR', 'name': 'Koramangala Studio', 'city': 'Bengaluru', 'address': '80ft Road, 4th Block', 'capacity': 180},
                    {'id': 'LOC-APEX-BND', 'name': 'Bandra West Arena', 'city': 'Mumbai', 'address': 'Pali Hill, Bandra West', 'capacity': 220},
                ],
                'usage': {
                    'active_members_count': 3420,
                    'locations_count': 3,
                    'trainers_count': 42,
                    'storage_used_mb': 14200.0,
                    'ai_minutes_used': 1850,
                    'api_requests_count': 1240000,
                },
                'apps': ['app-sarvam', 'app-razorpay', 'app-gupshup', 'app-kisi', 'app-quickbooks'],
            },
            {
                'id': 'TEN-PULSE-02',
                'name': 'Pulse Reformer & Pilates',
                'slug': 'pulse-pilates',
                'tier': TenantTier.GROWTH,
                'plan': plan_objs['GROWTH'],
                'status': TenantStatus.ACTIVE,
                'contact_email': 'hello@pulsepilates.in',
                'phone': '+91 80 9988 7766',
                'website': 'https://pulsepilates.in',
                'currency': 'INR',
                'timezone': 'Asia/Kolkata',
                'max_locations': 5,
                'max_members': 2500,
                'enabled_modules': ['/dashboard', '/crm/leads', '/members', '/ops/pilates', '/finance/revenue'],
                'branding': {
                    'app_name': 'Pulse Studio Hub',
                    'primary_color': '#e11d48',
                    'accent_color': '#fb7185',
                    'custom_domain': 'members.pulsepilates.in',
                    'cname_verified': True,
                    'email_footer': 'Pulse Pilates Studios — Form. Flow. Fitness.',
                },
                'locations': [
                    {'id': 'LOC-PULSE-LAV', 'name': 'Lavelle Road Sanctum', 'city': 'Bengaluru', 'address': 'Lavelle Road, Shanthala Nagar', 'capacity': 40},
                    {'id': 'LOC-PULSE-JUB', 'name': 'Jubilee Hills Boutique', 'city': 'Hyderabad', 'address': 'Road No. 36, Jubilee Hills', 'capacity': 35},
                ],
                'usage': {
                    'active_members_count': 2280,
                    'locations_count': 2,
                    'trainers_count': 18,
                    'storage_used_mb': 4680.0,
                    'ai_minutes_used': 285,
                    'api_requests_count': 235000,
                },
                'apps': ['app-razorpay', 'app-gupshup'],
            },
            {
                'id': 'TEN-IRON-03',
                'name': 'Iron Vault Strength Club',
                'slug': 'iron-vault',
                'tier': TenantTier.STARTER,
                'plan': plan_objs['STARTER'],
                'status': TenantStatus.ACTIVE,
                'contact_email': 'contact@ironvaultgym.in',
                'phone': '+91 80 1122 3344',
                'website': 'https://ironvaultgym.in',
                'currency': 'INR',
                'timezone': 'Asia/Kolkata',
                'max_locations': 1,
                'max_members': 500,
                'enabled_modules': ['/dashboard', '/crm/leads', '/members', '/finance/revenue'],
                'branding': {
                    'app_name': 'Iron Vault App',
                    'primary_color': '#2563eb',
                    'accent_color': '#f97316',
                    'custom_domain': '',
                    'cname_verified': False,
                    'email_footer': 'Iron Vault Strength & Conditioning Club',
                },
                'locations': [
                    {'id': 'LOC-IRON-WHF', 'name': 'Whitefield Powerhouse', 'city': 'Bengaluru', 'address': 'ITPL Main Road, Whitefield', 'capacity': 160},
                ],
                'usage': {
                    'active_members_count': 510, # Over limit for test
                    'locations_count': 1,
                    'trainers_count': 8,
                    'storage_used_mb': 1050.0,
                    'ai_minutes_used': 105, # Over limit for test
                    'api_requests_count': 54000,
                },
                'apps': ['app-razorpay'],
            },
            {
                'id': 'TEN-ZENITH-04',
                'name': 'Zenith Yoga & Recovery',
                'slug': 'zenith-yoga',
                'tier': TenantTier.GROWTH,
                'plan': plan_objs['GROWTH'],
                'status': TenantStatus.TRIAL,
                'contact_email': 'admin@zenithyoga.in',
                'phone': '+91 80 5566 7788',
                'website': 'https://zenithyoga.in',
                'currency': 'INR',
                'timezone': 'Asia/Kolkata',
                'max_locations': 5,
                'max_members': 2500,
                'enabled_modules': ['/dashboard', '/members', '/ops/calendar'],
                'branding': {
                    'app_name': 'Zenith Wellness Portal',
                    'primary_color': '#0284c7',
                    'accent_color': '#10b981',
                    'custom_domain': '',
                    'cname_verified': False,
                    'email_footer': 'Zenith Yoga & Mindful Recovery Retreats',
                },
                'locations': [
                    {'id': 'LOC-ZEN-HSR', 'name': 'HSR Sector 2 Sanctuary', 'city': 'Bengaluru', 'address': '17th Cross, HSR Sector 2', 'capacity': 50},
                ],
                'usage': {
                    'active_members_count': 450,
                    'locations_count': 1,
                    'trainers_count': 6,
                    'storage_used_mb': 820.0,
                    'ai_minutes_used': 45,
                    'api_requests_count': 32000,
                },
                'apps': ['app-razorpay', 'app-gupshup'],
            },
        ]

        today = timezone.now().date()

        for t_info in tenants_data:
            tenant, _ = Tenant.objects.update_or_create(
                id=t_info['id'],
                defaults={
                    'name': t_info['name'],
                    'slug': t_info['slug'],
                    'tier': t_info['tier'],
                    'plan': t_info['plan'],
                    'status': t_info['status'],
                    'contact_email': t_info['contact_email'],
                    'phone': t_info['phone'],
                    'website': t_info['website'],
                    'currency': t_info['currency'],
                    'timezone': t_info['timezone'],
                    'max_locations': t_info['max_locations'],
                    'max_members': t_info['max_members'],
                    'enabled_modules': t_info['enabled_modules'],
                    'is_active': (t_info['status'] in [TenantStatus.ACTIVE, TenantStatus.TRIAL]),
                }
            )

            # Branding
            TenantBranding.objects.update_or_create(
                tenant=tenant,
                defaults=t_info['branding']
            )

            # Locations
            for loc in t_info['locations']:
                Location.objects.update_or_create(
                    id=loc['id'],
                    defaults={
                        'tenant': tenant,
                        'name': loc['name'],
                        'city': loc['city'],
                        'address': loc['address'],
                        'capacity': loc['capacity'],
                        'is_active': True,
                    }
                )

            # Usage
            u_info = t_info['usage']
            TenantUsage.objects.update_or_create(
                tenant=tenant,
                defaults={
                    'active_members_count': u_info['active_members_count'],
                    'locations_count': u_info['locations_count'],
                    'trainers_count': u_info['trainers_count'],
                    'storage_used_mb': u_info['storage_used_mb'],
                    'ai_minutes_used': u_info['ai_minutes_used'],
                    'api_requests_count': u_info['api_requests_count'],
                    'last_calculated_at': timezone.now(),
                }
            )

            # 7-day Historical snapshots
            for day_offset in range(7, 0, -1):
                snap_date = today - datetime.timedelta(days=day_offset)
                growth_factor = (10 - day_offset) / 10.0
                HistoricalUsageSnapshot.objects.update_or_create(
                    tenant=tenant,
                    snapshot_date=snap_date,
                    defaults={
                        'members_count': int(u_info['active_members_count'] * growth_factor),
                        'storage_used_mb': round(u_info['storage_used_mb'] * growth_factor, 1),
                        'ai_minutes_used': int(u_info['ai_minutes_used'] * growth_factor),
                        'api_requests_count': int(u_info['api_requests_count'] * growth_factor),
                    }
                )

            # App Installations
            for app_id in t_info['apps']:
                app_obj = marketplace_objs.get(app_id)
                if app_obj:
                    TenantAppInstallation.objects.update_or_create(
                        tenant=tenant,
                        app=app_obj,
                        defaults={
                            'id': f"INST-{tenant.id.replace('TEN-', '')}-{app_id.replace('app-', '').upper()}",
                            'is_active': True,
                        }
                    )

            # Settings
            TenantSettings.objects.update_or_create(
                tenant=tenant,
                defaults={
                    'currency': tenant.currency,
                    'tax_rate_gst': 18.00,
                    'tax_id_number': f"29AAACB202{tenant.id[-2:]}P1Z5",
                    'booking_cancellation_window_hours': 12,
                    'late_cancellation_fee': 250.00,
                    'allow_guest_passes': True,
                    'guest_passes_per_month': 2,
                    'membership_grace_period_days': 7,
                    'allow_member_freeze': True,
                    'max_freeze_days_per_year': 60,
                    'business_open_time': '06:00',
                    'business_close_time': '22:00',
                }
            )

            # Security Policy
            SecurityPolicy.objects.update_or_create(
                tenant=tenant,
                defaults={
                    'enforce_mfa': (tenant.tier == TenantTier.ENTERPRISE),
                    'session_timeout_minutes': 60,
                    'password_min_length': 8,
                    'require_special_character': True,
                    'max_failed_attempts_lockout': 5,
                }
            )

        self.stdout.write(self.style.NOTICE("Seeding Audit Log Activity..."))
        AuditLog.objects.all().delete()
        audit_records = [
            {
                'id': 'AUD-001',
                'tenant': Tenant.objects.get(id='TEN-APEX-01'),
                'user_email': 'admin@yourgym.com',
                'action': AuditAction.CREATE,
                'module': 'Platform',
                'entity_type': 'PlatformPlan',
                'entity_id': 'PLAN-GROWTH',
                'description': 'Super Admin created Platform Plan tier Growth (₹19,999/mo).',
            },
            {
                'id': 'AUD-002',
                'tenant': Tenant.objects.get(id='TEN-APEX-01'),
                'user_email': 'admin@yourgym.com',
                'action': AuditAction.UPDATE,
                'module': 'Platform',
                'entity_type': 'Tenant',
                'entity_id': 'TEN-APEX-01',
                'description': 'Apex Athletics upgraded to Enterprise subscription quota (5,000 members).',
            },
            {
                'id': 'AUD-003',
                'tenant': Tenant.objects.get(id='TEN-PULSE-02'),
                'user_email': 'admin@yourgym.com',
                'action': AuditAction.UPDATE,
                'module': 'Marketplace',
                'entity_type': 'IntegrationApp',
                'entity_id': 'app-gupshup',
                'description': 'Activated Gupshup WhatsApp Business connector for Pulse Reformer & Pilates.',
            },
            {
                'id': 'AUD-004',
                'tenant': Tenant.objects.get(id='TEN-IRON-03'),
                'user_email': 'contact@ironvaultgym.in',
                'action': AuditAction.LOGIN,
                'module': 'Authentication',
                'entity_type': 'User',
                'entity_id': 'USR-IRON-ADMIN',
                'description': 'Tenant Admin authenticated from IP 49.37.142.88.',
            },
        ]

        for a in audit_records:
            AuditLog.objects.create(
                id=a['id'],
                tenant=a['tenant'],
                user_email=a['user_email'],
                action=a['action'],
                module=a['module'],
                entity_type=a['entity_type'],
                entity_id=a['entity_id'],
                description=a['description'],
                ip_address='127.0.0.1'
            )

        self.stdout.write(self.style.SUCCESS("Successfully seeded comprehensive Platform Core & Administration data!"))
