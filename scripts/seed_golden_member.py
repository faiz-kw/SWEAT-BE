"""
backend/scripts/seed_golden_member.py
Canonical Seeder for Golden Demo Member: Sarah Connor (MEM-00101)
Ensures complete A-to-Z Member history (CRM, Order, Payment, Invoice, Membership, Entitlements, Booking, Attendance).
"""

import os
import sys
import uuid
from decimal import Decimal
from datetime import date, timedelta
from django.utils import timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(SCRIPT_DIR)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from apps.master.models import Tenant, TenantDataSource
from config.tenant_middleware import _register_tenant_connection
from config.routers import build_tenant_db_alias, set_tenant_db_alias

from apps.tenant_core.models_org import Organization, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_catalog import Package, PackageVersion, PackagePrice, Program
from apps.tenant_core.models_commerce import Order, OrderItem, PaymentTransaction, MemberInvoice
from apps.tenant_core.models_memberships import (
    Membership,
    MembershipContractSnapshot,
    MembershipEntitlement,
    MembershipEntitlementLedger,
    MembershipStatusHistory,
    MembershipBranchHistory,
)
from apps.tenant_core.models_classes import ClassTemplate, ClassOccurrence
from apps.tenant_core.models_bookings import Booking
from apps.tenant_core.models_attendance import AttendanceRecord
from apps.tenant_core.models_crm import (
    Lead,
    LeadConversion,
    LeadActivity,
    LeadNote,
)
from apps.tenant_core.services_commerce import CommerceService


def seed_golden_member():
    tenant = Tenant.objects.get(slug='sweat')
    ds = TenantDataSource.objects.get(tenant=tenant)
    db_alias = build_tenant_db_alias(tenant.id)
    _register_tenant_connection(db_alias, db_name=ds.db_name, data_source=ds, tenant_id=tenant.id)
    set_tenant_db_alias(db_alias)

    print(f"[*] Seeding Golden Demo Member in tenant '{tenant.slug}' (DB: {ds.db_name}, Alias: {db_alias})...")

    # 1. Organization & Branch
    admin_user = TenantUser.objects.using(db_alias).filter(email='admin@sweat.com').first()
    org = admin_user.organization if admin_user and admin_user.organization else Organization.objects.using(db_alias).first()
    branch = Branch.objects.using(db_alias).filter(organization=org).first()
    if not branch:
        branch = Branch.objects.using(db_alias).first()

    sales_rep = TenantUser.objects.using(db_alias).filter(organization=org, email='sales@sweat.com').first() or admin_user

    # 2. Member User Account
    sarah_email = 'sarah.connor@sweat.test'
    member_user, created = TenantUser.objects.using(db_alias).get_or_create(
        email=sarah_email,
        defaults={
            'organization': org,
            'first_name': 'Sarah',
            'last_name': 'Connor',
            'phone': '+919876543210',
            'status': 'ACTIVE',
            'is_login_allowed': True,
        }
    )
    member_user.organization = org
    member_user.first_name = 'Sarah'
    member_user.last_name = 'Connor'
    member_user.phone = '+919876543210'
    member_user.status = 'ACTIVE'
    member_user.save(using=db_alias)

    # 3. UserProfile (Member 360 Anchor)
    joining_date = timezone.now().date() - timedelta(days=45)
    profile, p_created = UserProfile.objects.using(db_alias).get_or_create(
        user=member_user,
        defaults={
            'member_number': 'MEM-00101',
            'first_name_snapshot': 'Sarah',
            'last_name_snapshot': 'Connor',
            'gender': 'F',
            'preferred_branch': branch,
            'member_status': 'ACTIVE',
            'joining_date': joining_date,
            'date_of_birth': date(1995, 5, 20),
            'acquisition_source': 'WEBSITE_FORM',
            'emergency_contact_json': {'name': 'John Connor', 'relation': 'Son', 'phone': '+919876500000'},
        }
    )
    profile.member_number = 'MEM-00101'
    profile.member_status = 'ACTIVE'
    profile.preferred_branch = branch
    profile.first_name_snapshot = 'Sarah'
    profile.last_name_snapshot = 'Connor'
    profile.save(using=db_alias)

    print(f"  [+] UserProfile initialized: {profile.member_number} - {profile.first_name_snapshot} {profile.last_name_snapshot}")

    # 4. CRM Lead Origin & History
    lead, l_created = Lead.objects.using(db_alias).get_or_create(
        organization=org,
        email_normalized=sarah_email,
        defaults={
            'branch': branch,
            'first_name': 'Sarah',
            'last_name': 'Connor',
            'phone_normalized': '+919876543210',
            'current_status': 'CONVERTED',
            'first_touch_source': 'WEBSITE',
            'assigned_sales_user': sales_rep,
            'converted_user_profile': profile,
            'created_at': timezone.now() - timedelta(days=50),
        }
    )
    lead.current_status = 'CONVERTED'
    lead.assigned_sales_user = sales_rep
    lead.converted_user_profile = profile
    lead.save(using=db_alias)

    LeadActivity.objects.using(db_alias).get_or_create(
        lead=lead,
        activity_type='CALL',
        defaults={
            'outcome': 'Consultation Completed',
            'notes': 'Discussed VIP fitness goals, personal trainer requirements, and strength conditioning program.',
            'performed_by_user': sales_rep,
            'activity_at': timezone.now() - timedelta(days=48),
        }
    )
    LeadActivity.objects.using(db_alias).get_or_create(
        lead=lead,
        activity_type='TRIAL',
        defaults={
            'outcome': 'Trial Attended',
            'notes': 'Completed trial Pilates session with Tom Trainer. High satisfaction rating expressed.',
            'performed_by_user': sales_rep,
            'activity_at': timezone.now() - timedelta(days=46),
        }
    )

    # Lead Conversion link
    LeadConversion.objects.using(db_alias).get_or_create(
        lead=lead,
        user_profile=profile,
        defaults={
            'converted_by_user': sales_rep,
            'converted_at': timezone.now() - timedelta(days=45),
            'conversion_source': 'TRIAL_SESSION',
        }
    )

    # 5. Catalog Package & Price
    pkg = Package.objects.using(db_alias).filter(organization=org, status='ACTIVE').first()
    if not pkg:
        prog = Program.objects.using(db_alias).first()
        pkg = Package.objects.using(db_alias).create(
            organization=org,
            name='Quarterly Unlimited Pass',
            code='PKG-QUARTERLY',
            program=prog,
            status='ACTIVE'
        )

    pkg_v1 = PackageVersion.objects.using(db_alias).filter(package=pkg).first()
    if not pkg_v1:
        pkg_v1 = PackageVersion.objects.using(db_alias).create(
            package=pkg,
            version_number=1,
            name_snapshot=pkg.name,
            total_days=90,
            validity_days=90,
            status='ACTIVE',
            created_by_user=admin_user,
        )

    pkg_price = PackagePrice.objects.using(db_alias).filter(package_version=pkg_v1).first()
    if not pkg_price:
        pkg_price = PackagePrice.objects.using(db_alias).create(
            package_version=pkg_v1,
            branch=branch,
            currency='INR',
            base_price=Decimal('15000.00'),
            display_price=Decimal('15000.00'),
            tax_percent=Decimal('18.00'),
            prices_include_tax=True,
            status='ACTIVE',
            created_by_user=admin_user,
        )

    # 6. Commercial Order & Partial Payment (Leaves Outstanding for Live Demo)
    # Total = 15000. Paid = 10000. Outstanding = 5000.
    existing_order = Order.objects.using(db_alias).filter(user_profile=profile).first()
    if not existing_order:
        existing_order = CommerceService.create_order(
            branch=branch,
            items_data=[{
                'item_type': 'PACKAGE',
                'package_id': pkg.id,
                'package_version_id': pkg_v1.id,
                'package_price_id': pkg_price.id,
                'item_name_snapshot': f"{pkg.name} (v{pkg_v1.version_number})",
                'unit_price': '15000.00',
                'quantity': '1.00',
                'tax_percent': '18.000',
            }],
            user_profile=profile,
            created_by=admin_user,
            db_alias=db_alias,
        )

        # Record partial payment of 10000
        CommerceService.record_payment(
            order_id=str(existing_order.id),
            amount=Decimal('10000.00'),
            provider='UPI',
            actor=admin_user,
            db_alias=db_alias,
        )

    # Ensure invoice exists
    inv = MemberInvoice.objects.using(db_alias).filter(order=existing_order).first()
    if not inv:
        inv = MemberInvoice.objects.using(db_alias).create(
            branch=branch,
            user_profile=profile,
            order=existing_order,
            invoice_number=f"INV-{date.today().year}-{uuid.uuid4().hex[:6].upper()}",
            subtotal=Decimal('12711.86'),
            tax_amount=Decimal('2288.14'),
            total_amount=Decimal('15000.00'),
            currency='INR',
            status='ISSUED',
            issued_at=timezone.now() - timedelta(days=45),
        )

    # 7. Active Membership & Contract Snapshot
    membership = Membership.objects.using(db_alias).filter(user_profile=profile).first()
    if not membership:
        order_item = existing_order.items.first()
        from apps.tenant_core.services_memberships import MembershipLifecycleService
        membership = MembershipLifecycleService.activate_membership_from_order(
            order=existing_order,
            order_item=order_item,
            start_date=timezone.now().date() - timedelta(days=45),
            db_alias=db_alias,
            created_by_user=admin_user,
        )

    # 8. Entitlements & Passbook Ledger
    ent_home = MembershipEntitlement.objects.using(db_alias).filter(
        membership=membership,
        entitlement_type='HOME_BRANCH_SESSION'
    ).first()
    if not ent_home:
        ent_home = MembershipEntitlement.objects.using(db_alias).create(
            membership=membership,
            entitlement_type='HOME_BRANCH_SESSION',
            allocated_units=Decimal('36.00'),
            consumed_units=Decimal('4.00'),
            is_unlimited=False,
            valid_from=timezone.now() - timedelta(days=45),
            valid_until=timezone.now() + timedelta(days=45),
            status='ACTIVE',
        )

    ent_cross = MembershipEntitlement.objects.using(db_alias).filter(
        membership=membership,
        entitlement_type='CROSS_BRANCH_SESSION'
    ).first()
    if not ent_cross:
        ent_cross = MembershipEntitlement.objects.using(db_alias).create(
            membership=membership,
            entitlement_type='CROSS_BRANCH_SESSION',
            allocated_units=Decimal('6.00'),
            consumed_units=Decimal('0.00'),
            is_unlimited=False,
            valid_from=timezone.now() - timedelta(days=45),
            valid_until=timezone.now() + timedelta(days=45),
            status='ACTIVE',
        )

    # Passbook Ledger Entries
    if not MembershipEntitlementLedger.objects.using(db_alias).filter(membership_entitlement=ent_home).exists():
        MembershipEntitlementLedger.objects.using(db_alias).create(
            membership_entitlement=ent_home,
            transaction_type='ALLOCATION',
            units=Decimal('36.00'),
            balance_after=Decimal('36.00'),
            reason_code='PACKAGE_ALLOCATION',
            reason_text='Quarterly membership package allocation',
            created_by_user=admin_user,
        )
        MembershipEntitlementLedger.objects.using(db_alias).create(
            membership_entitlement=ent_home,
            transaction_type='CONSUMPTION',
            units=Decimal('-4.00'),
            balance_after=Decimal('32.00'),
            reason_code='ATTENDANCE_CHECKIN',
            reason_text='Check-in attendance consumption (4 sessions)',
            created_by_user=admin_user,
        )

    # 9. Class Template & Occurrence for Bookings
    template, _ = ClassTemplate.objects.using(db_alias).get_or_create(
        organization=org,
        code='CLS-PILATES-FLOW',
        defaults={
            'name': 'Reformer Pilates Flow',
            'program': pkg.program,
            'status': 'ACTIVE',
            'default_duration_minutes': 60,
            'default_capacity': 15,
        }
    )

    start_occ = timezone.now() - timedelta(days=2)
    end_occ = start_occ + timedelta(hours=1)
    occ, _ = ClassOccurrence.objects.using(db_alias).get_or_create(
        class_template=template,
        branch=branch,
        occurrence_date=start_occ.date(),
        defaults={
            'start_at': start_occ,
            'end_at': end_occ,
            'status': 'COMPLETED',
            'capacity': 15,
        }
    )

    # Booking & Attendance
    booking = Booking.objects.using(db_alias).filter(user_profile=profile).first()
    if not booking:
        booking = Booking.objects.using(db_alias).create(
            booking_number=f"BK-{date.today().year}-{uuid.uuid4().hex[:6].upper()}",
            user_profile=profile,
            membership=membership,
            entitlement=ent_home,
            occurrence=occ,
            branch=branch,
            status='COMPLETED',
            booking_type='MEMBER',
            booking_source='WEB',
            booked_at=timezone.now() - timedelta(days=3),
        )

    attendance = AttendanceRecord.objects.using(db_alias).filter(user_profile=profile).first()
    if not attendance:
        AttendanceRecord.objects.using(db_alias).create(
            branch=branch,
            user_profile=profile,
            booking=booking,
            occurrence=occ,
            status='PRESENT',
            check_in_status='SUCCESSFUL',
            check_in_method='QR',
            check_in_at=start_occ + timedelta(minutes=5),
            marked_by_user=admin_user,
        )

    print(f"[OK] Successfully seeded Sarah Connor (MEM-00101) with complete A-to-Z lifecycle history!")
    print(f"  - Profile ID: {profile.id}")
    print(f"  - Order Total: Rs. 15,000 | Paid: Rs. 10,000 | Outstanding: Rs. 5,000")
    print(f"  - Home Sessions: 32 remaining / 36 allocated")
    print(f"  - Cross-Branch Sessions: 6 remaining / 6 allocated")
    print(f"  - Lead Converted: True (Assigned to {sales_rep.first_name} {sales_rep.last_name})")
    print(f"  - Bookings & Attendance: Attended & Verified")


if __name__ == '__main__':
    seed_golden_member()
