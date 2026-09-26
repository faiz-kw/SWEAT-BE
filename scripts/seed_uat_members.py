"""
backend/scripts/seed_uat_members.py
Seeds canonical member profiles into tenant_sweat_uat for browser UAT testing.
Covers:
A. Active Membership
B. Expiring Soon (ending in 5 days)
C. Frozen (active freeze window)
D. Expired
E. Outstanding Balance (₹30,000 order - ₹20,000 payment = ₹10,000 due)
F. Fully Paid (₹29,999 order - ₹29,999 payment = ₹0.00 due)
G. Historical Expired + Current Active (Active wins)
H. Member in SWEAT OVERALL branch
"""

import os
import sys
import django
import uuid
from decimal import Decimal
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.utils import timezone
from apps.master.models import Tenant, TenantDataSource
from config.tenant_middleware import _register_tenant_connection, build_tenant_db_alias
from apps.tenant_core.context import set_tenant_db_alias
from apps.tenant_core.models_org import Organization, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_catalog import Package, PackageVersion, PackagePrice
from apps.tenant_core.models_memberships import Membership, MembershipFreeze
from apps.tenant_core.models_commerce import PaymentTransaction
from apps.tenant_core.services_commerce import CommerceService
from apps.tenant_core.services_memberships import MembershipLifecycleService


def seed_uat():
    t = Tenant.objects.get(slug='sweat')
    ds = TenantDataSource.objects.get(tenant=t)
    alias = build_tenant_db_alias(str(t.id))
    _register_tenant_connection(alias, ds.db_name, data_source=ds, tenant_id=str(t.id))
    set_tenant_db_alias(alias)

    admin_user = TenantUser.objects.using(alias).get(email='admin@sweat.com')
    org = admin_user.organization

    branches = list(Branch.objects.using(alias).all())
    branch_bootcamp = next(b for b in branches if 'BOOTCAMP' in b.name)
    branch_overall = next(b for b in branches if 'OVERALL' in b.name)
    branch_overall.organization = org
    branch_overall.save(using=alias)

    pkg_annual = Package.objects.using(alias).get(name__icontains='Annual VIP')
    pkg_annual.organization = org
    pkg_annual.save(using=alias)
    pv_annual = PackageVersion.objects.using(alias).filter(package=pkg_annual).first()
    price_annual = pv_annual.prices.first()

    pkg_monthly = Package.objects.using(alias).get(name__icontains='Monthly Unlimited')
    pkg_monthly.organization = org
    pkg_monthly.save(using=alias)
    pv_monthly = PackageVersion.objects.using(alias).filter(package=pkg_monthly).first()
    price_monthly = pv_monthly.prices.first()

    today = timezone.now().date()

    def get_or_create_member(email, first_name, last_name, phone, branch, member_number):
        u, _ = TenantUser.objects.using(alias).get_or_create(
            email=email,
            defaults={
                'organization': org,
                'first_name': first_name,
                'last_name': last_name,
                'phone': phone,
                'status': 'ACTIVE',
                'is_login_allowed': True,
                'user_type': 'MEMBER',
            }
        )
        p, _ = UserProfile.objects.using(alias).get_or_create(
            user=u,
            defaults={
                'member_number': member_number,
                'first_name_snapshot': first_name,
                'last_name_snapshot': last_name,
                'preferred_branch': branch,
                'member_type': 'MEMBER',
                'member_status': 'ACTIVE',
                'acquisition_source': 'WALK_IN',
                'joining_date': today - timedelta(days=60),
            }
        )
        u.organization = org
        u.user_type = 'MEMBER'
        u.save(using=alias)
        p.preferred_branch = branch
        p.member_number = member_number
        p.save(using=alias)
        return p

    print("Seeding UAT members...")

    # A. Active Membership (Aarav Sharma)
    p_aarav = get_or_create_member('aarav.sharma@sweat.test', 'Aarav', 'Sharma', '+919876500010', branch_bootcamp, 'MEM-AARAV-01')
    if not Membership.objects.using(alias).filter(user_profile=p_aarav, status='ACTIVE').exists():
        Membership.objects.using(alias).create(
            user_profile=p_aarav,
            program=pkg_annual.program,
            package=pkg_annual,
            package_version=pv_annual,
            package_price=price_annual,
            purchase_branch=branch_bootcamp,
            home_branch=branch_bootcamp,
            membership_number='MSHIP-AARAV-01',
            start_date=today - timedelta(days=30),
            end_date=today + timedelta(days=335),
            status='ACTIVE',
            activated_at=timezone.now(),
        )

    # B. Expiring Soon (Priya Patel, ends in 5 days)
    p_priya = get_or_create_member('priya.patel@sweat.test', 'Priya', 'Patel', '+919876500020', branch_bootcamp, 'MEM-PRIYA-02')
    Membership.objects.using(alias).filter(user_profile=p_priya).delete()
    Membership.objects.using(alias).create(
        user_profile=p_priya,
        program=pkg_monthly.program,
        package=pkg_monthly,
        package_version=pv_monthly,
        package_price=price_monthly,
        purchase_branch=branch_bootcamp,
        home_branch=branch_bootcamp,
        membership_number='MSHIP-PRIYA-02',
        start_date=today - timedelta(days=25),
        end_date=today + timedelta(days=5),
        status='ACTIVE',
        activated_at=timezone.now(),
    )

    # C. Frozen (Rohan Verma)
    p_rohan = get_or_create_member('rohan.verma@sweat.test', 'Rohan', 'Verma', '+919876500030', branch_bootcamp, 'MEM-ROHAN-03')
    MembershipFreeze.objects.using(alias).filter(membership__user_profile=p_rohan).delete()
    Membership.objects.using(alias).filter(user_profile=p_rohan).delete()
    mem_rohan = Membership.objects.using(alias).create(
        user_profile=p_rohan,
        program=pkg_annual.program,
        package=pkg_annual,
        package_version=pv_annual,
        package_price=price_annual,
        purchase_branch=branch_bootcamp,
        home_branch=branch_bootcamp,
        membership_number='MSHIP-ROHAN-03',
        start_date=today - timedelta(days=60),
        end_date=today + timedelta(days=305),
        status='FROZEN',
        activated_at=timezone.now(),
    )
    MembershipFreeze.objects.using(alias).create(
        membership=mem_rohan,
        freeze_from=today - timedelta(days=3),
        freeze_until=today + timedelta(days=14),
        status='ACTIVE',
        reason_code='MEDICAL_LEAVE',
        reason_text='Temporary muscle recovery',
        approved_by_user=admin_user,
    )

    # D. Expired (Ananya Iyer)
    p_ananya = get_or_create_member('ananya.iyer@sweat.test', 'Ananya', 'Iyer', '+919876500040', branch_bootcamp, 'MEM-ANANYA-04')
    Membership.objects.using(alias).filter(user_profile=p_ananya).delete()
    Membership.objects.using(alias).create(
        user_profile=p_ananya,
        program=pkg_monthly.program,
        package=pkg_monthly,
        package_version=pv_monthly,
        package_price=price_monthly,
        purchase_branch=branch_bootcamp,
        home_branch=branch_bootcamp,
        membership_number='MSHIP-ANANYA-04',
        start_date=today - timedelta(days=60),
        end_date=today - timedelta(days=30),
        status='EXPIRED',
        activated_at=timezone.now(),
    )

    # E. Outstanding Balance (Vikram Malhotra: Order ₹30,000, Paid ₹20,000, Outstanding ₹10,000)
    p_vikram = get_or_create_member('vikram.malhotra@sweat.test', 'Vikram', 'Malhotra', '+919876500050', branch_bootcamp, 'MEM-VIKRAM-05')
    if not Membership.objects.using(alias).filter(user_profile=p_vikram).exists():
        Membership.objects.using(alias).create(
            user_profile=p_vikram,
            program=pkg_annual.program,
            package=pkg_annual,
            package_version=pv_annual,
            package_price=price_annual,
            purchase_branch=branch_bootcamp,
            home_branch=branch_bootcamp,
            membership_number='MSHIP-VIKRAM-05',
            start_date=today - timedelta(days=10),
            end_date=today + timedelta(days=355),
            status='ACTIVE',
            activated_at=timezone.now(),
        )
    ord_vikram = CommerceService.create_order(
        branch=branch_bootcamp,
        items_data=[{
            'item_type': 'PACKAGE',
            'package_id': pkg_annual.id,
            'package_version_id': pv_annual.id,
            'package_price_id': price_annual.id,
            'item_name_snapshot': 'Annual Membership Balance Due',
            'unit_price': '30000.00',
            'quantity': '1.00',
            'tax_percent': '0.000',
        }],
        user_profile=p_vikram,
        created_by=admin_user,
        db_alias=alias,
    )
    ord_vikram.status = 'PARTIALLY_PAID'
    ord_vikram.save(using=alias)
    PaymentTransaction.objects.using(alias).create(
        order=ord_vikram,
        user_profile=p_vikram,
        amount=Decimal('20000.00'),
        currency='INR',
        provider='CASH',
        status='SUCCESS',
    )

    # F. Fully Paid (Neha Gupta: Order ₹29,999, Paid ₹29,999, Outstanding ₹0)
    p_neha = get_or_create_member('neha.gupta@sweat.test', 'Neha', 'Gupta', '+919876500060', branch_bootcamp, 'MEM-NEHA-06')
    if not Membership.objects.using(alias).filter(user_profile=p_neha).exists():
        Membership.objects.using(alias).create(
            user_profile=p_neha,
            program=pkg_annual.program,
            package=pkg_annual,
            package_version=pv_annual,
            package_price=price_annual,
            purchase_branch=branch_bootcamp,
            home_branch=branch_bootcamp,
            membership_number='MSHIP-NEHA-06',
            start_date=today - timedelta(days=15),
            end_date=today + timedelta(days=350),
            status='ACTIVE',
            activated_at=timezone.now(),
        )
    ord_neha = CommerceService.create_order(
        branch=branch_bootcamp,
        items_data=[{
            'item_type': 'PACKAGE',
            'package_id': pkg_annual.id,
            'package_version_id': pv_annual.id,
            'package_price_id': price_annual.id,
            'item_name_snapshot': 'Annual VIP All-Access',
            'unit_price': '29999.00',
            'quantity': '1.00',
            'tax_percent': '0.000',
        }],
        user_profile=p_neha,
        created_by=admin_user,
        db_alias=alias,
    )
    ord_neha.status = 'PAID'
    ord_neha.save(using=alias)
    PaymentTransaction.objects.using(alias).create(
        order=ord_neha,
        user_profile=p_neha,
        amount=Decimal('29999.00'),
        currency='INR',
        provider='CASH',
        status='SUCCESS',
    )

    # G. Historical Expired + Current Active (Kabir Mehta)
    p_kabir = get_or_create_member('kabir.mehta@sweat.test', 'Kabir', 'Mehta', '+919876500070', branch_bootcamp, 'MEM-KABIR-07')
    Membership.objects.using(alias).filter(user_profile=p_kabir).delete()
    # Expired past membership
    Membership.objects.using(alias).create(
        user_profile=p_kabir,
        program=pkg_monthly.program,
        package=pkg_monthly,
        package_version=pv_monthly,
        package_price=price_monthly,
        purchase_branch=branch_bootcamp,
        home_branch=branch_bootcamp,
        membership_number='MSHIP-KABIR-OLD',
        start_date=today - timedelta(days=90),
        end_date=today - timedelta(days=30),
        status='EXPIRED',
        activated_at=timezone.now() - timedelta(days=90),
    )
    # New active membership
    Membership.objects.using(alias).create(
        user_profile=p_kabir,
        program=pkg_annual.program,
        package=pkg_annual,
        package_version=pv_annual,
        package_price=price_annual,
        purchase_branch=branch_bootcamp,
        home_branch=branch_bootcamp,
        membership_number='MSHIP-KABIR-NEW',
        start_date=today - timedelta(days=10),
        end_date=today + timedelta(days=355),
        status='ACTIVE',
        activated_at=timezone.now(),
    )

    # H. Member in SWEAT OVERALL branch (Rhea Kapoor)
    p_rhea = get_or_create_member('rhea.kapoor@sweat.test', 'Rhea', 'Kapoor', '+919876500080', branch_overall, 'MEM-RHEA-08')
    if not Membership.objects.using(alias).filter(user_profile=p_rhea).exists():
        Membership.objects.using(alias).create(
            user_profile=p_rhea,
            program=pkg_annual.program,
            package=pkg_annual,
            package_version=pv_annual,
            package_price=price_annual,
            purchase_branch=branch_overall,
            home_branch=branch_overall,
            membership_number='MSHIP-RHEA-08',
            start_date=today - timedelta(days=5),
            end_date=today + timedelta(days=360),
            status='ACTIVE',
            activated_at=timezone.now(),
        )

    print("UAT members seeded successfully.")


if __name__ == '__main__':
    seed_uat()
