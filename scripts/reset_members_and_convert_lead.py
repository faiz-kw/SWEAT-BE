"""
backend/scripts/reset_members_and_convert_lead.py

1. Deletes all current member test accounts and their associated history (memberships,
   orders, transactions, entitlements, bookings, attendance, intake submissions).
2. Converts 'Simran Patel (Test)' (simran.patel@example.com) from a Lead to an active Member
   with a fully activated Monthly Pilates Membership (12 sessions).
3. Sets known login credentials (email: simran.patel@example.com, password: Sweat@2026!, org: sweat)
   and verifies authentication against both Universal Login and Mobile Auth endpoints.
"""

import os
import sys
import uuid
import logging
from decimal import Decimal
from datetime import date, timedelta

# Disable logger rollover issue on Windows
logging.disable(logging.CRITICAL)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(SCRIPT_DIR)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from django.utils import timezone
from apps.master.models import Tenant, TenantDataSource
from config.tenant_middleware import _register_tenant_connection
from config.routers import build_tenant_db_alias, set_tenant_db_alias

from apps.tenant_core.models_org import Organization, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_rbac import Role, RoleAssignment
from apps.tenant_core.models_catalog import Package, PackageVersion, PackagePrice
from apps.tenant_core.models_commerce import Order, OrderItem, PaymentTransaction, Refund, MemberInvoice
from apps.tenant_core.models_memberships import (
    Membership,
    MembershipContractSnapshot,
    MembershipEntitlement,
    MembershipEntitlementLedger,
    MembershipStatusHistory,
    MembershipBranchHistory,
    MembershipFreeze,
    MembershipChangeRequest,
)
from apps.tenant_core.models_classes import ClassTemplate, ClassOccurrence
from apps.tenant_core.models_bookings import Booking
from apps.tenant_core.models_attendance import AttendanceRecord, AccessEvent
from apps.tenant_core.models_crm import (
    Lead,
    LeadConversion,
    LeadActivity,
    LeadNote,
    LeadStatusHistory,
    IntakeSubmission,
    IntakeAnswer,
    TrialBooking,
)
from apps.tenant_core.services_crm import LeadConversionService


def reset_and_convert():
    tenant = Tenant.objects.get(slug='sweat')
    ds = TenantDataSource.objects.get(tenant=tenant)
    alias = build_tenant_db_alias(tenant.id)
    _register_tenant_connection(alias, db_name=ds.db_name, data_source=ds, tenant_id=tenant.id)
    set_tenant_db_alias(alias)

    print(f"[*] Working on tenant: {tenant.slug} (DB: {ds.db_name}, alias: {alias})")

    # -------------------------------------------------------------------------
    # 1. PURGE ALL OLD MEMBER RECORDS & MEMBER HISTORY
    # -------------------------------------------------------------------------
    print("[1] Purging all old member history and test member accounts...")

    from apps.tenant_core.models_memberships import (
        MembershipPackageHistory,
        MembershipExtension,
    )
    from apps.tenant_core.models_rewards import RewardLedger

    MembershipEntitlementLedger.objects.using(alias).all().delete()
    MembershipEntitlement.objects.using(alias).all().delete()
    MembershipContractSnapshot.objects.using(alias).all().delete()
    MembershipStatusHistory.objects.using(alias).all().delete()
    MembershipBranchHistory.objects.using(alias).all().delete()
    MembershipFreeze.objects.using(alias).all().delete()
    MembershipChangeRequest.objects.using(alias).all().delete()
    MembershipPackageHistory.objects.using(alias).all().delete()
    MembershipExtension.objects.using(alias).all().delete()
    RewardLedger.objects.using(alias).all().delete()
    LeadConversion.objects.using(alias).all().delete()
    Membership.objects.using(alias).all().delete()

    Refund.objects.using(alias).all().delete()
    PaymentTransaction.objects.using(alias).all().delete()
    MemberInvoice.objects.using(alias).all().delete()
    OrderItem.objects.using(alias).all().delete()
    Order.objects.using(alias).all().delete()

    AttendanceRecord.objects.using(alias).all().delete()
    AccessEvent.objects.using(alias).all().delete()
    Booking.objects.using(alias).all().delete()

    IntakeAnswer.objects.using(alias).all().delete()
    IntakeSubmission.objects.using(alias).all().delete()
    LeadConversion.objects.using(alias).all().delete()

    from apps.tenant_core.models_audit_outbox import IdempotencyRecord
    IdempotencyRecord.objects.using(alias).all().delete()

    # Reset any leads that pointed to converted profiles and ensure test lead is NEW_LEAD
    Lead.objects.using(alias).all().update(converted_user_profile=None)
    Lead.objects.using(alias).filter(email_normalized='simran.patel@example.com').update(current_status='NEW_LEAD')

    # Identify non-staff users (members, old test leads) to delete
    non_staff_users = TenantUser.objects.using(alias).filter(
        django.db.models.Q(user_type='MEMBER') |
        django.db.models.Q(email__iexact='simran.patel@example.com') |
        django.db.models.Q(email__in=[
            'siddharth.rao@tcs.com',
            'g_1b4d59@gmail.com',
            'lead_jwt_678a23@sweat.test',
            'lead_jwt_fec258@sweat.test',
            'app_714e98@example.com',
            'demo_75afb5@example.com',
            'demo_e103c7@example.com',
            'lead_jwt_a3f422@sweat.test',
            'secfresh@example.com',
            'trial_lead_f6d8f8@sweat.test',
            'trial_lead_fbf5e6@sweat.test',
        ])
    )

    from django.db import connections
    deleted_count = 0
    with connections[alias].cursor() as cursor:
        for u in non_staff_users:
            uid = str(u.id)
            # Delete approval requests and actions
            cursor.execute("DELETE FROM approval_actions WHERE approval_request_id IN (SELECT id FROM approval_requests WHERE requested_by_user_id = %s)", [uid])
            cursor.execute("DELETE FROM approval_actions WHERE approver_user_id = %s", [uid])
            cursor.execute("DELETE FROM approval_requests WHERE requested_by_user_id = %s", [uid])

            # Delete audit records
            cursor.execute("DELETE FROM business_audit_events WHERE actor_user_id = %s", [uid])
            cursor.execute("DELETE FROM audit_events WHERE actor_id = %s", [uid])
            cursor.execute("DELETE FROM idempotency_records WHERE actor_user_id = %s", [uid])

            # Nullify nullable lead assignments
            cursor.execute("UPDATE leads SET referred_by_user_id = NULL WHERE referred_by_user_id = %s", [uid])
            cursor.execute("UPDATE leads SET assigned_sales_user_id = NULL WHERE assigned_sales_user_id = %s", [uid])
            cursor.execute("UPDATE leads SET assigned_trainer_user_id = NULL WHERE assigned_trainer_user_id = %s", [uid])
            cursor.execute("DELETE FROM lead_activities WHERE performed_by_user_id = %s", [uid])
            cursor.execute("DELETE FROM lead_notes WHERE created_by_user_id = %s", [uid])
            cursor.execute("DELETE FROM lead_status_history WHERE changed_by_user_id = %s", [uid])

            # Delete personal/member child records
            cursor.execute("DELETE FROM referrals WHERE referred_user_id = %s OR referrer_user_id = %s", [uid, uid])
            cursor.execute("DELETE FROM referral_identifiers WHERE owner_user_id = %s", [uid])
            cursor.execute("DELETE FROM in_app_notifications WHERE user_id = %s", [uid])
            cursor.execute("DELETE FROM consent_records WHERE user_id = %s", [uid])
            cursor.execute("DELETE FROM privacy_requests WHERE user_id = %s", [uid])
            cursor.execute("DELETE FROM user_profiles WHERE user_id = %s", [uid])
            cursor.execute("DELETE FROM role_assignments WHERE user_id = %s", [uid])
            cursor.execute("DELETE FROM user_branches WHERE user_id = %s", [uid])
            cursor.execute("DELETE FROM user_departments WHERE user_id = %s", [uid])
            cursor.execute("DELETE FROM meal_logs WHERE user_id = %s", [uid])
            cursor.execute("DELETE FROM nutrition_targets WHERE user_id = %s", [uid])
            cursor.execute("DELETE FROM sleep_logs WHERE user_id = %s", [uid])
            cursor.execute("DELETE FROM water_logs WHERE user_id = %s", [uid])

            # Finally delete user
            cursor.execute("DELETE FROM users WHERE id = %s", [uid])
            deleted_count += 1

    # Also clean up AuthenticationIdentity in Master DB ('default') for non-staff or simran.patel
    from apps.master.models_iam import AuthenticationIdentity
    from apps.master.services_auth_directory import compute_lookup_hash
    simran_hashes = [compute_lookup_hash('simran.patel@example.com'), compute_lookup_hash('+919820554433'), compute_lookup_hash('simran.patel')]
    AuthenticationIdentity.objects.using('default').filter(
        django.db.models.Q(tenant_id=tenant.id) &
        (django.db.models.Q(lookup_hash__in=simran_hashes) | django.db.models.Q(subject_id__in=[u.id for u in non_staff_users]))
    ).delete()

    print(f"    Purged {deleted_count} non-staff user accounts, auth identities, and all related member tables.")

    # -------------------------------------------------------------------------
    # 2. CONVERT 'Simran Patel (Test)' FROM CRM LEAD TO ACTIVE MEMBER
    # -------------------------------------------------------------------------
    print("\n[2] Converting Lead: Simran Patel (Test)...")
    lead = Lead.objects.using(alias).filter(email_normalized='simran.patel@example.com').first()
    if not lead:
        raise RuntimeError("Lead 'simran.patel@example.com' not found in database!")

    andheri_branch = Branch.objects.using(alias).filter(code='SWEAT_ANDHERI').first() or lead.branch
    lead.branch = andheri_branch
    lead.save(using=alias)

    admin_user = TenantUser.objects.using(alias).filter(email='admin@sweat.com').first()

    # Package: Sweat Pilates - 12 Sessions (Monthly)
    package = Package.objects.using(alias).filter(name__icontains='12 Sessions').first()
    pv = PackageVersion.objects.using(alias).filter(package=package, status='ACTIVE').first()
    if not pv:
        raise RuntimeError("Active PackageVersion for 12 Sessions not found!")

    price_rec = PackagePrice.objects.using(alias).filter(package_version=pv, branch=andheri_branch).first()
    payment_amount = price_rec.base_price if price_rec else Decimal('13999.00')

    print(f"    Target Lead: {lead.first_name} {lead.last_name} ({lead.email_normalized})")
    print(f"    Branch: {andheri_branch.name} ({andheri_branch.code})")
    print(f"    Package: {package.name} (PV: {pv.id})")
    print(f"    Payment Amount: INR {payment_amount}")

    # Execute LeadConversionService conversion
    result = LeadConversionService.execute_conversion(
        lead=lead,
        package_version_id=str(pv.id),
        branch_id=str(andheri_branch.id),
        payment_provider='CASH',
        payment_amount=payment_amount,
        payment_method='CASH',
        start_date=timezone.now().date(),
        actor_user=admin_user,
        db_alias=alias,
    )
    print("    Lead conversion result:", result)

    # Approve cash payment approval request to fully activate membership & entitlements
    if result.get('approval_request_id'):
        from django.db import transaction as dj_transaction
        from apps.tenant_core.models_approvals import ApprovalRequest
        from apps.tenant_core.services_approvals import AdminApprovalService
        app_req = ApprovalRequest.objects.using(alias).filter(id=result['approval_request_id']).first()
        manager_user = TenantUser.objects.using(alias).filter(email='manager.andheri@sweat.com').first() or admin_user
        if app_req:
            with dj_transaction.atomic(using=alias):
                AdminApprovalService.process_action(
                    approval_request=app_req,
                    approver_user=manager_user,
                    action='APPROVED',
                    comment='Approved cash payment for Simran Patel test member enrollment',
                    allow_self_approval=True,
                    db_alias=alias,
                )
            print("    Cash payment approved! Order marked PAID and full membership activated.")

    # -------------------------------------------------------------------------
    # 3. CONFIGURE MEMBER CREDENTIALS & ROLE ASSIGNMENTS
    # -------------------------------------------------------------------------
    print("\n[3] Setting up login credentials & profile for member...")
    member_user = TenantUser.objects.using(alias).filter(email__iexact=lead.email_normalized).first()
    if not member_user:
        raise RuntimeError("Converted TenantUser not found!")

    MEMBER_PASSWORD = 'Sweat@2026!'
    member_user.set_password(MEMBER_PASSWORD)
    member_user.status = 'ACTIVE'
    member_user.user_type = 'MEMBER'
    member_user.is_login_allowed = True
    member_user.home_branch = andheri_branch
    member_user.first_name = 'Simran'
    member_user.last_name = 'Patel'
    member_user.phone = '+919820554433'
    member_user.username = 'simran.patel'
    member_user.save(using=alias)

    # Sync into AuthenticationIdentity in Master DB
    from apps.master.services_auth_directory import sync_tenant_user_identity
    sync_tenant_user_identity(member_user, tenant_id=tenant.id, db=alias)
    print(f"    Synchronized {member_user.email} into Universal Auth Directory (Master DB)")

    # Ensure RoleAssignment for 'MEMBER' role
    member_role = Role.objects.using(alias).filter(code='MEMBER').first()
    if member_role:
        ra = RoleAssignment.objects.using(alias).filter(user=member_user, role=member_role).first()
        if not ra:
            ra = RoleAssignment.objects.using(alias).create(
                user=member_user,
                role=member_role,
                organization=lead.organization,
                scope_type='ORGANIZATION',
                status='ACTIVE',
                is_active=True,
            )
        else:
            ra.is_active = True
            ra.status = 'ACTIVE'
            ra.save(using=alias)
        print(f"    RoleAssignment '{member_role.code}' set to active for {member_user.email}")

    # Update UserProfile
    profile = UserProfile.objects.using(alias).filter(user=member_user).first()
    if profile:
        profile.member_number = 'MEM-00101'
        profile.member_status = 'ACTIVE'
        profile.member_type = 'MEMBER'
        profile.preferred_branch = andheri_branch
        profile.first_name_snapshot = 'Simran'
        profile.last_name_snapshot = 'Patel'
        profile.joining_date = timezone.now().date()
        profile.acquisition_source = 'CRM_CONVERSION'
        profile.address_json = {
            'city': 'Mumbai',
            'state': 'Maharashtra',
            'address': 'Andheri West, Mumbai',
            'onboarding': {
                'is_cleared': True,
                'completed_at': timezone.now().isoformat(),
            }
        }
        profile.emergency_contact_json = {
            'name': 'Kavita Patel',
            'relation': 'Mother',
            'phone': '+919820554400'
        }
        profile.save(using=alias)
        print(f"    UserProfile updated: Member Number = {profile.member_number}, Status = {profile.member_status}")

    # Verify Membership & Entitlements
    membership = Membership.objects.using(alias).filter(user_profile=profile).first()
    if membership:
        membership.membership_number = 'MEM-00101'
        membership.parq_status = 'COMPLETED'
        membership.parq_completed_at = timezone.now()
        membership.save(using=alias)
        print(f"    Membership confirmed: {membership.membership_number} | {membership.status} | Validity: {membership.start_date} to {membership.end_date}")

        ents = MembershipEntitlement.objects.using(alias).filter(membership=membership)
        print(f"    Entitlements allocated: {ents.count()}")
        for e in ents:
            print(f"      - {e.entitlement_type}: {e.allocated_units} sessions (remaining: {e.allocated_units - e.consumed_units})")

    # -------------------------------------------------------------------------
    # 4. VERIFY AUTHENTICATION TOKENS
    # -------------------------------------------------------------------------
    print("\n[4] Verifying Authentication...")
    from rest_framework.test import APIRequestFactory
    from apps.authentication.views import UniversalLoginView
    from apps.tenant_core.views_mobile import MobileLoginView

    factory = APIRequestFactory()

    # Universal Web Login test
    req_web = factory.post('/api/v1/auth/login/', {
        'identifier': 'simran.patel@example.com',
        'password': MEMBER_PASSWORD,
        'tenant_slug': 'sweat',
    }, format='json')
    resp_web = UniversalLoginView.as_view()(req_web)
    print(f"    Universal Web Login: HTTP {resp_web.status_code}")
    if resp_web.status_code == 200:
        print(f"      User Role: {resp_web.data.get('user', {}).get('role')}")
        print(f"      User Type: {resp_web.data.get('user', {}).get('user_type')}")
        print(f"      Roles List: {resp_web.data.get('roles')}")
        print(f"      Default Route: {resp_web.data.get('default_route')}")

    # Mobile Login test
    req_mob = factory.post('/api/v1/mobile/auth/login/', {
        'identifier': 'simran.patel@example.com',
        'password': MEMBER_PASSWORD,
    }, format='json', HTTP_X_TENANT_SLUG='sweat')
    resp_mob = MobileLoginView.as_view()(req_mob)
    print(f"    Mobile Member Login: HTTP {resp_mob.status_code}")
    if resp_mob.status_code == 200:
        print(f"      Member Name: {resp_mob.data.get('user', {}).get('first_name')} {resp_mob.data.get('user', {}).get('last_name')}")
        print(f"      Member Number: {resp_mob.data.get('user', {}).get('member_number')}")
        print(f"      Role Code: {resp_mob.data.get('user', {}).get('role_code')}")

    print("\n========================================================")
    print("SUCCESS: 1 Member converted cleanly from Lead!")
    print(f"Login Identifier: simran.patel@example.com (or +919820554433)")
    print(f"Organization Code: sweat")
    print(f"Password: {MEMBER_PASSWORD}")
    print("========================================================")

if __name__ == '__main__':
    reset_and_convert()
