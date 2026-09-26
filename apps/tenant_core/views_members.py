"""
apps/tenant_core/views_members.py — Comprehensive Production ViewSet for Members, Member Directory, Member 360 & Lifecycle Actions

Provides the REST endpoints for:
- GET  /api/v1/tenant/members/ (Search, filters, quick-views, backend pagination, directory summary stats)
- POST /api/v1/tenant/members/ (Create member, link from lead)
- GET  /api/v1/tenant/members/<id>/ (Retrieve member detail)
- PATCH /api/v1/tenant/members/<id>/ (Update member profile)
- DELETE /api/v1/tenant/members/<id>/ (Deactivate member)
- GET  /api/v1/tenant/members/<id>/360/ (Complete 6-section Member 360 read model)
- GET  /api/v1/tenant/members/<id>/timeline/ (Unified chronological audit & domain timeline)
- GET  /api/v1/tenant/members/<id>/available-actions/ (Backend eligibility for lifecycle operations)
- POST /api/v1/tenant/members/<id>/renew/ (Renew membership)
- POST /api/v1/tenant/members/<id>/upgrade/ (Upgrade package)
- POST /api/v1/tenant/members/<id>/extend/ (Extend validity date)
- POST /api/v1/tenant/members/<id>/freeze/ (Freeze membership window)
- POST /api/v1/tenant/members/<id>/unfreeze/ (Unfreeze active freeze early)
- POST /api/v1/tenant/members/<id>/transfer/ (Transfer home branch)
- POST /api/v1/tenant/members/<id>/cancel/ (Cancel membership)
- POST /api/v1/tenant/members/<id>/adjust-entitlement/ (Controlled session ledger adjustment)
- POST /api/v1/tenant/members/<id>/collect-outstanding/ (Record outstanding payment)
- POST /api/v1/tenant/members/<id>/check-in/ (Member attendance check-in)
- GET  /api/v1/tenant/members/plans/ (Catalog membership plans)
"""

import logging
import uuid
from decimal import Decimal
from datetime import datetime, date, timedelta
from django.db import transaction, models
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db.models import Q, Sum
from config.routers import get_tenant_db_alias

from .models_org import Branch, Organization
from .models_users import TenantUser
from .models_workforce import UserProfile
from .models_memberships import (
    Membership,
    MembershipContractSnapshot,
    MembershipEntitlement,
    MembershipEntitlementLedger,
    MembershipBranchHistory,
    MembershipStatusHistory,
    MembershipFreeze,
    MembershipPackageHistory,
)
from .models_catalog import Package, PackageVersion, PackagePrice, Program
from .models_attendance import AttendanceRecord, AccessEvent
from .models_bookings import Booking
from .models_commerce import Order, OrderItem, PaymentTransaction, Refund, MemberInvoice
from .models_crm import (
    Lead,
    LeadConversion,
    LeadStatusHistory,
    LeadNote,
    LeadActivity,
    TrialBooking,
    IntakeSubmission,
    IntakeAnswer,
)
from .models_audit_outbox import BusinessAuditEvent
from .services_memberships import MembershipLifecycleService
from .services_commerce import CommerceService
from .services_reliability import record_business_audit
from .permissions import RequireActiveTenantAndOrg, TenantRBACPermission

logger = logging.getLogger(__name__)


def _get_db(request):
    return (
        get_tenant_db_alias()
        or getattr(getattr(request, 'user', None), '_db_alias', None)
        or getattr(request, '_tenant_db_alias', None)
        or 'default'
    )


def _get_org(request):
    org = getattr(request, 'organization', None)
    if not org:
        user = getattr(request, 'user', None)
        if user and hasattr(user, 'organization') and user.organization:
            return user.organization
        alias = _get_db(request)
        if user and getattr(user, 'organization_id', None):
            return Organization.objects.using(alias).filter(id=user.organization_id).first()
        return Organization.objects.using(alias).filter(status='ACTIVE').first()
    return org


def calculate_member_outstanding(profile: UserProfile, alias: str = 'default') -> Decimal:
    """
    Computes total outstanding balance across all non-cancelled, non-draft orders for a member.
    Accounts for payable orders (PENDING_PAYMENT, PARTIALLY_PAID), successful payments, and refunds.
    """
    orders = Order.objects.using(alias).filter(
        user_profile=profile
    ).exclude(status__in=['CANCELLED', 'REFUNDED', 'DRAFT'])

    total_outstanding = Decimal('0.00')
    for order in orders:
        if order.status in ['PENDING_PAYMENT', 'PARTIALLY_PAID']:
            paid_sum = PaymentTransaction.objects.using(alias).filter(
                order=order, status='SUCCESS'
            ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
            refund_sum = Refund.objects.using(alias).filter(
                order=order, status='SUCCESS'
            ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
            net_paid = max(Decimal('0.00'), paid_sum - refund_sum)
            diff = order.total_amount - net_paid
            if diff > Decimal('0.00'):
                total_outstanding += diff

    return total_outstanding


def get_member_sessions_summary(profile: UserProfile, active_m: Membership = None, alias: str = 'default'):
    """
    Computes remaining sessions breakdown (Home branch, Cross branch, and all types).
    """
    home_sessions = 0
    cross_sessions = 0
    detailed_balances = []

    if not active_m:
        active_m = (
            Membership.objects.using(alias)
            .filter(user_profile=profile)
            .order_by('-created_at')
            .first()
        )

    if active_m:
        ents = MembershipEntitlement.objects.using(alias).filter(membership=active_m)
        for ent in ents:
            rem = ent.remaining_units
            rem_val = float(rem) if rem is not None else 999.0
            detailed_balances.append({
                'id': str(ent.id),
                'entitlement_type': ent.entitlement_type,
                'allocated': float(ent.allocated_units) if ent.allocated_units else (None if ent.is_unlimited else 0.0),
                'consumed': float(ent.consumed_units),
                'remaining': rem_val if not ent.is_unlimited else None,
                'is_unlimited': ent.is_unlimited,
                'status': ent.status,
                'valid_from': str(ent.valid_from) if ent.valid_from else None,
                'valid_until': str(ent.valid_until) if ent.valid_until else None,
            })
            et_lower = (ent.entitlement_type or '').lower()
            if 'cross' in et_lower:
                cross_sessions += int(rem_val) if not ent.is_unlimited else 999
            elif 'home' in et_lower or 'session' in et_lower or 'class' in et_lower:
                home_sessions += int(rem_val) if not ent.is_unlimited else 999

    return home_sessions, cross_sessions, detailed_balances


def serialize_member(profile: UserProfile, alias: str = 'default') -> dict:
    user = profile.user
    first_name = profile.first_name_snapshot or getattr(user, 'first_name', '')
    last_name = profile.last_name_snapshot or getattr(user, 'last_name', '')
    full_name = f"{first_name} {last_name}".strip() or getattr(user, 'full_name', '') or getattr(user, 'email', '') or 'Member'

    # Age calculation
    age = 28
    if profile.date_of_birth:
        today = timezone.now().date()
        dob = profile.date_of_birth
        age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))

    # Active or latest membership
    active_m = (
        Membership.objects.using(alias)
        .filter(user_profile=profile)
        .select_related('package', 'package__program', 'package_version', 'home_branch', 'purchase_branch')
        .order_by('-created_at')
        .first()
    )

    membership_name = "Standard Membership"
    active_plan = None
    program_name = "Standard"
    program_id = None
    package_name = "None"
    package_id = None
    package_version_name = "v1"
    membership_status = profile.member_status or 'ACTIVE'
    start_date = None
    expiry_date = None

    if active_m:
        pkg = active_m.package
        prog = active_m.program or (pkg.program if pkg else None)
        membership_name = pkg.name if pkg else "Standard Membership"
        package_name = membership_name
        package_id = str(pkg.id) if pkg else None
        if prog:
            program_name = prog.name
            program_id = str(prog.id)
        if active_m.package_version:
            package_version_name = f"v{active_m.package_version.version_number}"
        membership_status = active_m.status
        start_date = str(active_m.start_date) if active_m.start_date else None
        expiry_date = str(active_m.end_date) if active_m.end_date else None
        active_plan = {
            'id': str(active_m.id),
            'plan_name': membership_name,
            'status': active_m.status,
            'start_date': start_date or '',
            'end_date': expiry_date or '',
        }

    branch = profile.preferred_branch

    # Emergency contact string
    emergency_contact = ""
    if profile.emergency_contact_json:
        if isinstance(profile.emergency_contact_json, dict):
            emergency_contact = (
                profile.emergency_contact_json.get('name')
                or profile.emergency_contact_json.get('phone')
                or profile.emergency_contact_json.get('contact')
                or str(profile.emergency_contact_json)
            )
        else:
            emergency_contact = str(profile.emergency_contact_json)

    # Entitlement calculation
    home_sessions, cross_sessions, _ = get_member_sessions_summary(profile, active_m, alias)

    # Outstanding balance
    outstanding_balance = calculate_member_outstanding(profile, alias)

    # Attendance stats
    thirty_days_ago = timezone.now() - timedelta(days=30)
    att_qs = AttendanceRecord.objects.using(alias).filter(user_profile=profile)
    att_count_30d = att_qs.filter(created_at__gte=thirty_days_ago).count()
    last_record = att_qs.order_by('-created_at').first()
    last_visit = str(last_record.check_in_at.date()) if (last_record and last_record.check_in_at) else (
        str(last_record.created_at.date()) if last_record else str(profile.joining_date or timezone.now().date())
    )

    # Assigned trainer
    assigned_trainer = getattr(profile, 'primary_coach_name', None) or 'Unassigned'

    return {
        'id': str(profile.id),
        'tenant_id': str(user.organization_id) if user and getattr(user, 'organization_id', None) else None,
        'location': str(branch.id) if branch else None,
        'location_name': branch.name if branch else None,
        'home_branch_id': str(branch.id) if branch else None,
        'home_branch': branch.name if branch else 'Main Branch',
        'name': full_name,
        'first_name': first_name,
        'last_name': last_name,
        'phone': getattr(user, 'phone', '') or '',
        'email': getattr(user, 'email', '') or '',
        'gender': profile.gender or 'M',
        'age': age,
        'status': profile.member_status or 'ACTIVE',
        'membership_status': membership_status,
        'membership': membership_name,
        'membership_id': str(active_m.id) if active_m else None,
        'membership_number': profile.member_number or (active_m.membership_number if active_m else ''),
        'program_id': program_id,
        'program_name': program_name,
        'package_id': package_id,
        'package_name': package_name,
        'package_version_name': package_version_name,
        'start_date': start_date,
        'expiry_date': expiry_date,
        'home_sessions_remaining': home_sessions,
        'cross_branch_sessions_remaining': cross_sessions,
        'outstanding_balance': float(outstanding_balance),
        'assigned_trainer': assigned_trainer,
        'active_plan': active_plan,
        'joined_at': str(profile.joining_date or profile.created_at.date() if profile.created_at else timezone.now().date()),
        'emergency_contact': emergency_contact,
        'source': profile.acquisition_source or 'DIRECT',
        'fitness_goal': 'General Fitness',
        'health_score': 80,
        'performance_score': 75,
        'risk_level': 'Low',
        'attendance_count_30d': att_count_30d,
        'last_visit': last_visit,
    }


def get_member_available_actions(profile: UserProfile, active_m: Membership, outstanding: Decimal) -> list:
    """
    Computes backend-authoritative eligible actions for a member.
    """
    actions = []
    today = timezone.now().date()

    if active_m:
        st = active_m.status
        if st in ['ACTIVE', 'EXPIRED'] or (active_m.end_date and (active_m.end_date - today).days <= 30):
            actions.append({'action': 'RENEW', 'label': 'Renew Membership', 'eligible': True})
        if st == 'ACTIVE':
            actions.append({'action': 'UPGRADE', 'label': 'Upgrade Package', 'eligible': True})
            actions.append({'action': 'EXTEND', 'label': 'Extend Validity', 'eligible': True})
            actions.append({'action': 'FREEZE', 'label': 'Freeze Membership', 'eligible': True})
        if st == 'FROZEN':
            actions.append({'action': 'UNFREEZE', 'label': 'Unfreeze Membership', 'eligible': True})
        if st in ['ACTIVE', 'FROZEN']:
            actions.append({'action': 'TRANSFER', 'label': 'Transfer Home Branch', 'eligible': True})
            actions.append({'action': 'CANCEL', 'label': 'Cancel Membership', 'eligible': True})

        actions.append({'action': 'ADJUST_ENTITLEMENT', 'label': 'Adjust Sessions', 'eligible': True})
    else:
        actions.append({'action': 'REJOIN', 'label': 'Rejoin / New Plan', 'eligible': True})

    if outstanding > Decimal('0.00'):
        actions.append({'action': 'COLLECT_PAYMENT', 'label': f'Collect Outstanding (₹{outstanding})', 'eligible': True})

    actions.append({'action': 'CHECK_IN', 'label': 'Record Check-in', 'eligible': True})
    return actions


def build_member_timeline(profile: UserProfile, alias: str = 'default', limit: int = 100) -> list:
    """
    Pure read-model aggregation assembling a unified chronological timeline across CRM, Commerce,
    Memberships, Passbook, Bookings, Attendance, and Audit without duplicating source-of-truth records.
    """
    events = []
    user = profile.user

    # 1. CRM Lead Origin & History (if user has linked leads)
    linked_leads = []
    try:
        filters = Q(converted_user_profile=profile)
        if user and user.email:
            filters = filters | Q(email_normalized=user.email.strip().lower())
        if user and user.phone:
            filters = filters | Q(phone_normalized=user.phone.strip())
        linked_leads = list(Lead.objects.using(alias).filter(filters).distinct().order_by('-created_at')[:3])
    except Exception:
        linked_leads = []

    for lead in linked_leads:
        events.append({
            'id': f"lead-create-{lead.id}",
            'category': 'CRM',
            'event_type': 'LEAD_CREATED',
            'title': 'Lead Created',
            'description': f"Prospect created via {lead.first_touch_source or 'Direct'} ({lead.first_name} {lead.last_name})",
            'occurred_at': lead.created_at.isoformat() if lead.created_at else timezone.now().isoformat(),
            'actor': 'System',
            'badge_color': 'blue',
            'metadata': {'lead_id': str(lead.id), 'source': lead.first_touch_source},
        })

        for act in LeadActivity.objects.using(alias).filter(lead=lead).order_by('-activity_at')[:10]:
            events.append({
                'id': f"lead-act-{act.id}",
                'category': 'CRM',
                'event_type': act.activity_type,
                'title': f"Sales Activity: {act.activity_type.replace('_', ' ').title()}",
                'description': f"{act.outcome + ': ' if act.outcome else ''}{act.notes or ''}".strip(),
                'occurred_at': (act.activity_at or act.created_at).isoformat(),
                'actor': act.performed_by_user.first_name if act.performed_by_user else 'Sales Rep',
                'badge_color': 'indigo',
                'metadata': {'lead_id': str(lead.id)},
            })

        for trial in TrialBooking.objects.using(alias).filter(lead=lead).order_by('-created_at')[:5]:
            events.append({
                'id': f"trial-{trial.id}",
                'category': 'CRM',
                'event_type': f"TRIAL_{trial.current_status}",
                'title': f"Trial {trial.current_status.replace('_', ' ').title()}",
                'description': f"Trial booking scheduled for {trial.scheduled_start.date() if trial.scheduled_start else 'upcoming'}",
                'occurred_at': trial.created_at.isoformat() if trial.created_at else timezone.now().isoformat(),
                'actor': 'Staff',
                'badge_color': 'teal',
                'metadata': {'trial_id': str(trial.id), 'status': trial.current_status},
            })

    # Lead Conversions
    for conv in LeadConversion.objects.using(alias).filter(user_profile=profile).order_by('-converted_at'):
        events.append({
            'id': f"conv-{conv.id}",
            'category': 'CRM',
            'event_type': 'LEAD_CONVERTED',
            'title': 'Converted to Member',
            'description': f"Prospect converted into active member profile. Package Version: {conv.package_version_id or 'Purchased'}",
            'occurred_at': conv.converted_at.isoformat() if conv.converted_at else timezone.now().isoformat(),
            'actor': 'System',
            'badge_color': 'emerald',
            'metadata': {'conversion_id': str(conv.id)},
        })

    # 2. Commerce Orders
    for ord_obj in Order.objects.using(alias).filter(user_profile=profile).prefetch_related('items').order_by('-created_at'):
        item_names = ", ".join(i.item_name_snapshot for i in ord_obj.items.all()) or "Order Items"
        events.append({
            'id': f"order-{ord_obj.id}",
            'category': 'COMMERCE',
            'event_type': 'ORDER_CREATED',
            'title': f"Order {ord_obj.order_number}",
            'description': f"{ord_obj.order_type.replace('_', ' ').title()} - {item_names} (Total: ₹{ord_obj.total_amount} {ord_obj.currency}) [{ord_obj.status}]",
            'occurred_at': ord_obj.created_at.isoformat(),
            'actor': 'Front Desk' if ord_obj.source == 'FRONT_DESK' else 'System',
            'badge_color': 'amber',
            'metadata': {'order_id': str(ord_obj.id), 'total_amount': str(ord_obj.total_amount), 'status': ord_obj.status},
        })

    # 3. Payments
    for ptxn in PaymentTransaction.objects.using(alias).filter(user_profile=profile).order_by('-created_at'):
        events.append({
            'id': f"pay-{ptxn.id}",
            'category': 'COMMERCE',
            'event_type': 'PAYMENT_RECORDED',
            'title': f"Payment Received: ₹{ptxn.amount}",
            'description': f"Paid via {ptxn.provider} ({ptxn.payment_method or 'Cash'}) for Order {ptxn.order.order_number if ptxn.order else ''}",
            'occurred_at': ptxn.paid_at.isoformat() if ptxn.paid_at else ptxn.created_at.isoformat(),
            'actor': 'Finance / POS',
            'badge_color': 'emerald',
            'metadata': {'payment_id': str(ptxn.id), 'amount': str(ptxn.amount), 'provider': ptxn.provider},
        })

    # 4. Invoices
    for inv in MemberInvoice.objects.using(alias).filter(user_profile=profile).order_by('-issued_at'):
        events.append({
            'id': f"inv-{inv.id}",
            'category': 'COMMERCE',
            'event_type': 'INVOICE_ISSUED',
            'title': f"Invoice Issued: {inv.invoice_number}",
            'description': f"Official tax invoice generated for ₹{inv.total_amount} [{inv.status}]",
            'occurred_at': inv.issued_at.isoformat() if inv.issued_at else inv.created_at.isoformat(),
            'actor': 'System',
            'badge_color': 'cyan',
            'metadata': {'invoice_id': str(inv.id), 'total_amount': str(inv.total_amount)},
        })

    # 5. Memberships & Lifecycle Transitions
    for mem in Membership.objects.using(alias).filter(user_profile=profile).select_related('package', 'package_version'):
        events.append({
            'id': f"mem-act-{mem.id}",
            'category': 'MEMBERSHIP',
            'event_type': 'MEMBERSHIP_ACTIVATED',
            'title': f"Membership Activated: {mem.package.name if mem.package else 'Plan'}",
            'description': f"Active from {mem.start_date} to {mem.end_date} at {mem.home_branch.name if mem.home_branch else 'Branch'}",
            'occurred_at': mem.activated_at.isoformat() if mem.activated_at else mem.created_at.isoformat(),
            'actor': 'System',
            'badge_color': 'green',
            'metadata': {'membership_id': str(mem.id), 'number': mem.membership_number},
        })

        for sh in MembershipStatusHistory.objects.using(alias).filter(membership=mem).order_by('-created_at'):
            events.append({
                'id': f"mem-st-{sh.id}",
                'category': 'MEMBERSHIP',
                'event_type': f"STATUS_{sh.to_status}",
                'title': f"Membership Status: {sh.to_status}",
                'description': sh.reason_text or f"Status changed from {sh.from_status} to {sh.to_status}",
                'occurred_at': sh.created_at.isoformat(),
                'actor': 'Staff',
                'badge_color': 'purple',
                'metadata': {'from_status': sh.from_status, 'to_status': sh.to_status},
            })

        for bh in MembershipBranchHistory.objects.using(alias).filter(membership=mem).order_by('-created_at'):
            if bh.change_type != 'INITIAL':
                events.append({
                    'id': f"mem-bh-{bh.id}",
                    'category': 'MEMBERSHIP',
                    'event_type': 'BRANCH_TRANSFER',
                    'title': 'Branch Transfer',
                    'description': bh.reason or f"Transferred to {bh.to_branch.name}",
                    'occurred_at': bh.created_at.isoformat(),
                    'actor': 'Staff',
                    'badge_color': 'indigo',
                    'metadata': {'to_branch': bh.to_branch.name},
                })

        for frz in MembershipFreeze.objects.using(alias).filter(membership=mem).order_by('-created_at'):
            events.append({
                'id': f"mem-frz-{frz.id}",
                'category': 'MEMBERSHIP',
                'event_type': 'MEMBERSHIP_FROZEN',
                'title': f"Membership Freeze ({frz.status})",
                'description': f"Frozen from {frz.freeze_from} to {frz.freeze_until}. Extended validity by {frz.extend_membership_days or 0} days.",
                'occurred_at': frz.created_at.isoformat(),
                'actor': 'Staff',
                'badge_color': 'sky',
                'metadata': {'freeze_from': str(frz.freeze_from), 'freeze_until': str(frz.freeze_until)},
            })

    # 6. Entitlement Passbook Movements
    for led in MembershipEntitlementLedger.objects.using(alias).filter(
        membership_entitlement__membership__user_profile=profile
    ).select_related('membership_entitlement').order_by('-created_at')[:30]:
        events.append({
            'id': f"led-{led.id}",
            'category': 'ENTITLEMENT',
            'event_type': f"ENTITLEMENT_{led.transaction_type}",
            'title': f"Session {led.transaction_type.capitalize()}: {led.units:+}",
            'description': f"{led.membership_entitlement.entitlement_type} | {led.reason_text or led.reason_code or 'Balance update'} (New balance: {led.balance_after})",
            'occurred_at': led.created_at.isoformat(),
            'actor': 'System / Staff',
            'badge_color': 'blue' if led.transaction_type == 'ALLOCATION' else ('emerald' if led.transaction_type == 'REVERSAL' else 'orange'),
            'metadata': {'units': str(led.units), 'balance_after': str(led.balance_after), 'type': led.membership_entitlement.entitlement_type},
        })

    # 7. Bookings & Attendance
    for bk in Booking.objects.using(alias).filter(user_profile=profile).select_related('occurrence', 'occurrence__class_template', 'branch').order_by('-booked_at')[:20]:
        cls_name = bk.occurrence.class_template.name if (bk.occurrence and bk.occurrence.class_template) else "Class Session"
        events.append({
            'id': f"bk-{bk.id}",
            'category': 'BOOKING',
            'event_type': f"BOOKING_{bk.status}",
            'title': f"Booking: {cls_name} [{bk.status}]",
            'description': f"Booked for {bk.occurrence.start_at if bk.occurrence else 'upcoming'} at {bk.branch.name if bk.branch else 'Branch'}",
            'occurred_at': bk.booked_at.isoformat() if bk.booked_at else bk.created_at.isoformat(),
            'actor': 'Member / Front Desk',
            'badge_color': 'violet',
            'metadata': {'booking_id': str(bk.id), 'status': bk.status},
        })

    for att in AttendanceRecord.objects.using(alias).filter(user_profile=profile).select_related('branch').order_by('-created_at')[:20]:
        events.append({
            'id': f"att-{att.id}",
            'category': 'ATTENDANCE',
            'event_type': f"ATTENDANCE_{att.status}",
            'title': f"Check-in: {att.status}",
            'description': f"Checked in at {att.branch.name if att.branch else 'Branch'} via {att.check_in_method}",
            'occurred_at': att.check_in_at.isoformat() if att.check_in_at else att.created_at.isoformat(),
            'actor': 'Front Desk' if att.check_in_method == 'FRONT_DESK' else 'QR Scan',
            'badge_color': 'teal' if att.status == 'PRESENT' else 'rose',
            'metadata': {'attendance_id': str(att.id), 'method': att.check_in_method},
        })

    # Sort all events chronologically descending
    events.sort(key=lambda x: x['occurred_at'], reverse=True)
    return events[:limit]


def build_member_360_aggregate(profile: UserProfile, alias: str = 'default') -> dict:
    """
    Builds the complete authoritative 6-section Member 360 payload.
    """
    user = profile.user
    base_member = serialize_member(profile, alias)

    active_m = (
        Membership.objects.using(alias)
        .filter(user_profile=profile)
        .select_related('package', 'package__program', 'package_version', 'home_branch', 'purchase_branch')
        .order_by('-created_at')
        .first()
    )

    outstanding = Decimal(str(base_member['outstanding_balance']))

    # Alerts
    alerts = []
    if outstanding > Decimal('0.00'):
        alerts.append({
            'id': 'alert-outstanding',
            'type': 'PAYMENT_DUE',
            'severity': 'warning',
            'title': 'Outstanding Payment Due',
            'message': f"Member has an unpaid balance of ₹{outstanding:,.2f}.",
            'action': 'COLLECT_PAYMENT',
        })

    if active_m:
        today = timezone.now().date()
        if active_m.status == 'FROZEN':
            alerts.append({
                'id': 'alert-frozen',
                'type': 'MEMBERSHIP_FROZEN',
                'severity': 'info',
                'title': 'Membership Frozen',
                'message': "Membership is currently paused.",
                'action': 'UNFREEZE',
            })
        elif active_m.end_date:
            days_left = (active_m.end_date - today).days
            if days_left < 0:
                alerts.append({
                    'id': 'alert-expired',
                    'type': 'MEMBERSHIP_EXPIRED',
                    'severity': 'destructive',
                    'title': 'Membership Expired',
                    'message': f"Expired {abs(days_left)} days ago on {active_m.end_date}.",
                    'action': 'RENEW',
                })
            elif days_left <= 15:
                alerts.append({
                    'id': 'alert-expiring-soon',
                    'type': 'EXPIRING_SOON',
                    'severity': 'warning',
                    'title': 'Expiring Soon',
                    'message': f"Expires in {days_left} day(s) on {active_m.end_date}.",
                    'action': 'RENEW',
                })

    available_actions = get_member_available_actions(profile, active_m, outstanding)

    # 1. Header
    header = {
        'name': base_member['name'],
        'member_number': base_member['membership_number'],
        'status': base_member['membership_status'],
        'home_branch': base_member['home_branch'],
        'home_branch_id': base_member['home_branch_id'],
        'current_program': base_member['program_name'],
        'current_package': base_member['package_name'],
        'current_package_version': base_member['package_version_name'],
        'expiry_date': base_member['expiry_date'],
        'home_sessions_remaining': base_member['home_sessions_remaining'],
        'cross_branch_sessions_remaining': base_member['cross_branch_sessions_remaining'],
        'outstanding_balance': base_member['outstanding_balance'],
        'assigned_trainer': base_member['assigned_trainer'],
        'alerts': alerts,
        'available_actions': available_actions,
    }

    # 2. Timeline
    timeline_events = build_member_timeline(profile, alias, limit=100)

    # 3. Memberships
    all_memberships = (
        Membership.objects.using(alias)
        .filter(user_profile=profile)
        .select_related('package', 'package__program', 'package_version', 'home_branch', 'purchase_branch')
        .order_by('-created_at')
    )

    active_membership_data = None
    upcoming_membership_data = []
    history_membership_data = []

    for m in all_memberships:
        contract = getattr(m, 'contract_snapshot', None)
        contract_data = None
        if contract:
            contract_data = {
                'id': str(contract.id),
                'package_name': contract.package_name_snapshot,
                'purchase_price': float(contract.purchase_price),
                'discount_amount': float(contract.discount_amount),
                'tax_amount': float(contract.tax_amount),
                'final_amount': float(contract.final_amount),
                'currency': contract.currency,
                'duration_value': contract.duration_value,
                'duration_unit': contract.duration_unit,
                'start_date': str(contract.start_date),
                'end_date': str(contract.end_date),
                'entitlements_snapshot': contract.entitlements_snapshot,
            }

        m_obj = {
            'id': str(m.id),
            'membership_number': m.membership_number,
            'package_name': m.package.name if m.package else 'Plan',
            'package_version_name': f"v{m.package_version.version_number}" if m.package_version else 'v1',
            'program_name': (m.program.name if m.program else (m.package.program.name if (m.package and m.package.program) else 'Standard')),
            'status': m.status,
            'start_date': str(m.start_date),
            'end_date': str(m.end_date),
            'home_branch': m.home_branch.name if m.home_branch else 'Branch',
            'purchase_branch': m.purchase_branch.name if m.purchase_branch else 'Branch',
            'activated_at': m.activated_at.isoformat() if m.activated_at else None,
            'contract_snapshot': contract_data,
        }

        if m.id == getattr(active_m, 'id', None):
            active_membership_data = m_obj
        elif m.start_date and m.start_date > timezone.now().date():
            upcoming_membership_data.append(m_obj)
        else:
            history_membership_data.append(m_obj)

    # Freezes & history
    freezes = [
        {
            'id': str(f.id),
            'freeze_from': str(f.freeze_from),
            'freeze_until': str(f.freeze_until),
            'days': f.extend_membership_days,
            'status': f.status,
            'reason': f.reason_text,
            'approved_by': f.approved_by_user.email if f.approved_by_user else 'Staff',
        }
        for f in MembershipFreeze.objects.using(alias).filter(membership__user_profile=profile).order_by('-created_at')
    ]

    branch_history = [
        {
            'id': str(bh.id),
            'from_branch': bh.from_branch.name if bh.from_branch else None,
            'to_branch': bh.to_branch.name if bh.to_branch else 'Branch',
            'change_type': bh.change_type,
            'reason': bh.reason,
            'effective_at': bh.effective_at.isoformat(),
        }
        for bh in MembershipBranchHistory.objects.using(alias).filter(membership__user_profile=profile).order_by('-created_at')
    ]

    status_history = [
        {
            'id': str(sh.id),
            'from_status': sh.from_status,
            'to_status': sh.to_status,
            'reason': sh.reason_text or sh.reason_code,
            'changed_at': sh.changed_at.isoformat(),
        }
        for sh in MembershipStatusHistory.objects.using(alias).filter(membership__user_profile=profile).order_by('-created_at')
    ]

    # 4. Passbook (Entitlement balances & append-only ledger)
    _, _, balances = get_member_sessions_summary(profile, active_m, alias)
    ledger_entries = [
        {
            'id': str(led.id),
            'occurred_at': led.created_at.isoformat(),
            'transaction_type': led.transaction_type,
            'entitlement_type': led.membership_entitlement.entitlement_type if led.membership_entitlement else 'Session',
            'units': float(led.units),
            'balance_after': float(led.balance_after) if led.balance_after is not None else None,
            'reason': led.reason_text or led.reason_code or '',
            'booking_id': str(led.booking_id) if led.booking_id else None,
            'actor': led.created_by_user.email if led.created_by_user else 'System',
        }
        for led in MembershipEntitlementLedger.objects.using(alias).filter(
            membership_entitlement__membership__user_profile=profile
        ).select_related('membership_entitlement', 'created_by_user').order_by('-created_at')
    ]

    # 5. Bookings & Attendance
    now_dt = timezone.now()
    bookings_qs = (
        Booking.objects.using(alias)
        .filter(user_profile=profile)
        .select_related('occurrence', 'occurrence__class_template', 'branch')
        .order_by('-booked_at')
    )

    upcoming_bookings = []
    past_bookings = []

    for bk in bookings_qs:
        occ = bk.occurrence
        tpl = occ.class_template if occ else None
        bk_obj = {
            'id': str(bk.id),
            'booking_number': bk.booking_number,
            'class_name': tpl.name if tpl else 'Class Session',
            'branch_name': bk.branch.name if bk.branch else 'Branch',
            'status': bk.status,
            'booking_source': bk.booking_source,
            'date_time': occ.start_at.isoformat() if (occ and occ.start_at) else bk.booked_at.isoformat(),
            'trainer_name': 'Assigned Trainer',
        }
        if occ and occ.start_at and occ.start_at >= now_dt and bk.status in ['CONFIRMED', 'WAITLISTED', 'RESERVED']:
            upcoming_bookings.append(bk_obj)
        else:
            past_bookings.append(bk_obj)

    attendance_records = [
        {
            'id': str(att.id),
            'date_time': (att.check_in_at or att.created_at).isoformat(),
            'branch_name': att.branch.name if att.branch else 'Branch',
            'status': att.status,
            'check_in_method': att.check_in_method,
            'marked_by': att.marked_by_user.email if att.marked_by_user else 'Front Desk',
        }
        for att in AttendanceRecord.objects.using(alias).filter(user_profile=profile).select_related('branch', 'marked_by_user').order_by('-created_at')[:50]
    ]

    # 6. Finance
    orders_data = []
    total_invoiced = Decimal('0.00')
    total_paid = Decimal('0.00')

    for ord_obj in Order.objects.using(alias).filter(user_profile=profile).prefetch_related('items').order_by('-created_at'):
        paid_for_order = PaymentTransaction.objects.using(alias).filter(
            order=ord_obj, status='SUCCESS'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

        bal = max(Decimal('0.00'), ord_obj.total_amount - paid_for_order)
        if ord_obj.status not in ['CANCELLED', 'REFUNDED']:
            total_invoiced += ord_obj.total_amount
            total_paid += paid_for_order

        orders_data.append({
            'id': str(ord_obj.id),
            'order_number': ord_obj.order_number,
            'order_type': ord_obj.order_type,
            'status': ord_obj.status,
            'subtotal': float(ord_obj.subtotal),
            'discount_amount': float(ord_obj.discount_amount),
            'tax_amount': float(ord_obj.tax_amount),
            'total_amount': float(ord_obj.total_amount),
            'paid_amount': float(paid_for_order),
            'outstanding_amount': float(bal),
            'currency': ord_obj.currency,
            'created_at': ord_obj.created_at.isoformat(),
            'items': [
                {
                    'id': str(it.id),
                    'item_name': it.item_name_snapshot,
                    'quantity': float(it.quantity),
                    'unit_price': float(it.unit_price_snapshot),
                    'total_amount': float(it.total_amount),
                }
                for it in ord_obj.items.all()
            ]
        })

    payments_data = [
        {
            'id': str(pt.id),
            'order_id': str(pt.order_id) if pt.order_id else None,
            'order_number': pt.order.order_number if pt.order else '',
            'provider': pt.provider,
            'payment_method': pt.payment_method or pt.provider,
            'amount': float(pt.amount),
            'currency': pt.currency,
            'status': pt.status,
            'paid_at': pt.paid_at.isoformat() if pt.paid_at else pt.created_at.isoformat(),
        }
        for pt in PaymentTransaction.objects.using(alias).filter(user_profile=profile).select_related('order').order_by('-created_at')
    ]

    invoices_data = [
        {
            'id': str(inv.id),
            'invoice_number': inv.invoice_number,
            'order_number': inv.order.order_number if inv.order else '',
            'total_amount': float(inv.total_amount),
            'currency': inv.currency,
            'status': inv.status,
            'issued_at': inv.issued_at.isoformat(),
            'download_url': f"/api/v1/tenant/member-invoices/{inv.id}/",
        }
        for inv in MemberInvoice.objects.using(alias).filter(user_profile=profile).select_related('order').order_by('-issued_at')
    ]

    refunds_data = [
        {
            'id': str(rf.id),
            'order_number': rf.order.order_number if rf.order else '',
            'amount': float(rf.amount),
            'reason': rf.reason_text or rf.reason_code or 'Refund processed',
            'status': rf.status,
            'created_at': rf.created_at.isoformat(),
        }
        for rf in Refund.objects.using(alias).filter(order__user_profile=profile).select_related('order').order_by('-created_at')
    ]

    total_refunded = sum(r['amount'] for r in refunds_data if r['status'] == 'SUCCESS')

    # 7. Health & Forms (Intake submissions)
    submissions_data = []
    intake_subs = (
        IntakeSubmission.objects.using(alias)
        .filter(user_profile=profile)
        .select_related('intake_form')
        .prefetch_related('answers', 'answers__question')
        .order_by('-submitted_at')
    )
    for sub in intake_subs:
        answers = []
        for ans in sub.answers.all():
            val = ans.text_value or ans.numeric_value or ans.boolean_value or ans.date_value or ans.json_value
            answers.append({
                'question_id': str(ans.question.id),
                'question_text': ans.question.question_text,
                'question_type': ans.question.question_type,
                'answer': str(val) if val is not None else '',
            })
        submissions_data.append({
            'id': str(sub.id),
            'form_title': sub.intake_form.title if sub.intake_form else 'Health Questionnaire',
            'submitted_at': sub.submitted_at.isoformat(),
            'answers': answers,
        })

    # Next booking for Overview
    next_bk = upcoming_bookings[0] if upcoming_bookings else None

    overview = {
        'contact': {
            'full_name': base_member['name'],
            'email': base_member['email'],
            'phone': base_member['phone'],
            'gender': base_member['gender'],
            'age': base_member['age'],
            'joined_at': base_member['joined_at'],
            'emergency_contact': base_member['emergency_contact'],
            'acquisition_source': base_member['source'],
        },
        'home_branch': {
            'id': base_member['home_branch_id'],
            'name': base_member['home_branch'],
        },
        'current_membership': active_membership_data,
        'entitlement_summary': balances,
        'outstanding_balance': float(outstanding),
        'next_booking': next_bk,
        'assigned_trainer': base_member['assigned_trainer'],
        'alerts': alerts,
        'recent_activities': timeline_events[:5],
    }

    return {
        'member': base_member,
        'header': header,
        'overview': overview,
        'timeline': timeline_events,
        'memberships': {
            'active': active_membership_data,
            'upcoming': upcoming_membership_data,
            'history': history_membership_data,
            'freezes': freezes,
            'branch_history': branch_history,
            'status_history': status_history,
        },
        'passbook': {
            'balances': balances,
            'ledger': ledger_entries,
        },
        'bookings_and_attendance': {
            'upcoming_bookings': upcoming_bookings,
            'past_bookings': past_bookings,
            'attendance_records': attendance_records,
            'stats': {
                'visits_30d': base_member['attendance_count_30d'],
                'last_visit': base_member['last_visit'],
                'total_bookings': len(past_bookings) + len(upcoming_bookings),
            }
        },
        'finance': {
            'summary': {
                'total_invoiced': float(total_invoiced),
                'total_paid': float(total_paid),
                'total_outstanding': float(outstanding),
                'total_refunded': float(total_refunded),
            },
            'orders': orders_data,
            'payments': payments_data,
            'invoices': invoices_data,
            'refunds': refunds_data,
        },
        'health_and_forms': {
            'submissions': submissions_data,
        },
    }


class MemberViewSet(viewsets.ViewSet):
    """
    Production ViewSet for Members with Server-Side Search, Filters, Pagination,
    Member 360 Aggregation, Timeline, and Authoritative Lifecycle Actions.
    """
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'core'
    required_submodule = 'users'
    required_permission = 'core.users.view'
    permission_action_map = {
        'create': 'core.users.create',
        'update': 'core.users.edit',
        'partial_update': 'core.users.edit',
        'destroy': 'core.users.delete',
        'check_in': 'core.users.edit',
        'plans': 'core.users.view',
        'payment_methods': 'core.users.view',
        'member_360': 'core.users.view',
        'timeline': 'core.users.view',
        'available_actions': 'core.users.view',
        'renew': 'core.users.edit',
        'upgrade': 'core.users.edit',
        'extend': 'core.users.edit',
        'freeze': 'core.users.edit',
        'unfreeze': 'core.users.edit',
        'transfer': 'core.users.edit',
        'cancel': 'core.users.edit',
        'rejoin': 'core.users.edit',
        'adjust_entitlement': 'core.users.edit',
        'collect_outstanding': 'finance.payments.create',
    }

    @classmethod
    def get_member_queryset(cls, alias: str, org=None):
        """
        Returns the authoritative queryset of genuine customer members.
        Enforces positive member criteria:
          - explicit member_number
          - OR explicit customer acquisition_source
          - OR attached commercial memberships
          - OR linked lead conversions
          - OR commerce orders
        Staff-only accounts without genuine customer identity are cleanly excluded.
        """
        qs = UserProfile.objects.using(alias).select_related('user', 'preferred_branch')
        if org:
            qs = qs.filter(user__organization=org)

        member_condition = (
            (Q(member_number__isnull=False) & ~Q(member_number=''))
            | (Q(acquisition_source__isnull=False) & ~Q(acquisition_source=''))
            | Q(memberships__isnull=False)
            | Q(lead_conversions__isnull=False)
            | Q(orders__isnull=False)
        )
        return qs.filter(member_condition).distinct()

    def get_member_profile(self, alias: str, org, pk):
        """
        Authoritative lookup for a genuine customer member by UUID pk or member_number.
        Returns None if pk belongs to a staff-only user without member identity.
        """
        qs = self.get_member_queryset(alias, org)
        profile = None
        try:
            profile = qs.filter(id=pk).first()
        except Exception:
            profile = None
        if not profile:
            profile = qs.filter(member_number=pk).first()
        return profile

    def list(self, request):
        alias = _get_db(request)
        org = _get_org(request)
        qs = self.get_member_queryset(alias, org)
        today = timezone.now().date()

        # Branch / Location filter: current / home member branch
        location = request.query_params.get('location') or request.query_params.get('locationId') or request.query_params.get('branch_id')
        if location and location not in ['all', 'ALL']:
            qs = qs.filter(Q(preferred_branch_id=location) | Q(memberships__home_branch_id=location))

        # Program filter
        program_id = request.query_params.get('program_id')
        if program_id and program_id not in ['all', 'ALL']:
            qs = qs.filter(memberships__program_id=program_id)

        # Package filter
        package_id = request.query_params.get('package_id')
        if package_id and package_id not in ['all', 'ALL']:
            qs = qs.filter(memberships__package_id=package_id)

        # Status filter
        status_param = request.query_params.get('status')
        if status_param and status_param not in ['all', 'ALL']:
            st = status_param.upper()
            if st == 'ACTIVE':
                qs = qs.filter(
                    memberships__status='ACTIVE'
                ).filter(
                    Q(memberships__end_date__gte=today) | Q(memberships__end_date__isnull=True)
                ).exclude(
                    memberships__status='FROZEN'
                )
            elif st == 'FROZEN':
                qs = qs.filter(
                    Q(memberships__status='FROZEN') |
                    (Q(memberships__freezes__status='ACTIVE') & Q(memberships__freezes__freeze_from__lte=today) & Q(memberships__freezes__freeze_until__gte=today))
                )
            elif st == 'EXPIRED':
                qs = qs.filter(
                    Q(memberships__status='EXPIRED')
                    | (Q(memberships__status='ACTIVE') & Q(memberships__end_date__lt=today))
                    | Q(member_status='EXPIRED')
                ).exclude(
                    memberships__status='ACTIVE',
                    memberships__end_date__gte=today
                ).exclude(
                    memberships__status='FROZEN'
                )
            elif st == 'CANCELLED':
                qs = qs.filter(memberships__status='CANCELLED')
            elif st == 'INACTIVE':
                qs = qs.filter(Q(member_status='INACTIVE') | Q(user__status='INACTIVE'))
            else:
                qs = qs.filter(Q(member_status__iexact=status_param) | Q(memberships__status__iexact=status_param))

        # Search across Member ID / Member Number, Name, Phone, Email
        search = request.query_params.get('search')
        if search:
            s = search.strip()
            if s:
                parts = s.split()
                if len(parts) >= 2:
                    name_q = (
                        (Q(user__first_name__icontains=parts[0]) & Q(user__last_name__icontains=parts[1]))
                        | (Q(first_name_snapshot__icontains=parts[0]) & Q(last_name_snapshot__icontains=parts[1]))
                    )
                else:
                    name_q = (
                        Q(first_name_snapshot__icontains=s)
                        | Q(last_name_snapshot__icontains=s)
                        | Q(user__first_name__icontains=s)
                        | Q(user__last_name__icontains=s)
                    )

                qs = qs.filter(
                    name_q
                    | Q(user__email__icontains=s)
                    | Q(user__phone__icontains=s)
                    | Q(member_number__icontains=s)
                    | Q(memberships__membership_number__icontains=s)
                )

        # Quick Views filter
        quick_view = request.query_params.get('quick_view')
        if quick_view == 'active':
            # Canonical active membership: currently ACTIVE, not past end_date, not FROZEN
            qs = qs.filter(
                memberships__status='ACTIVE'
            ).filter(
                Q(memberships__end_date__gte=today) | Q(memberships__end_date__isnull=True)
            ).exclude(
                memberships__status='FROZEN'
            )
        elif quick_view == 'frozen':
            # Canonical frozen: currently effective freeze lifecycle
            qs = qs.filter(
                Q(memberships__status='FROZEN') |
                (Q(memberships__freezes__status='ACTIVE') & Q(memberships__freezes__freeze_from__lte=today) & Q(memberships__freezes__freeze_until__gte=today))
            )
        elif quick_view == 'expiring_soon':
            # Active membership expiring between today and today + 30 days, not frozen
            soon = today + timedelta(days=30)
            qs = qs.filter(
                memberships__status='ACTIVE',
                memberships__end_date__gte=today,
                memberships__end_date__lte=soon
            ).exclude(
                memberships__status='FROZEN'
            )
        elif quick_view == 'expired':
            # Expired membership, but active current membership wins over old expired historical membership
            qs = qs.filter(
                Q(memberships__status='EXPIRED')
                | (Q(memberships__status='ACTIVE') & Q(memberships__end_date__lt=today))
                | Q(member_status='EXPIRED')
            ).exclude(
                memberships__status='ACTIVE',
                memberships__end_date__gte=today
            ).exclude(
                memberships__status='FROZEN'
            )
        elif quick_view == 'outstanding':
            # Server-calculated outstanding > 0, excluding cancelled/refunded/fully paid
            candidate_ids = list(qs.filter(
                orders__status__in=['PENDING_PAYMENT', 'PARTIALLY_PAID']
            ).values_list('id', flat=True).distinct())

            outstanding_ids = []
            for p_id in candidate_ids:
                p = qs.filter(id=p_id).first()
                if p and calculate_member_outstanding(p, alias) > Decimal('0.00'):
                    outstanding_ids.append(p_id)

            qs = qs.filter(id__in=outstanding_ids)

        qs = qs.distinct().order_by('-created_at')

        # Pagination support
        try:
            page = max(1, int(request.query_params.get('page', 1)))
        except (ValueError, TypeError):
            page = 1

        try:
            page_size = min(100, max(1, int(request.query_params.get('page_size', 20))))
        except (ValueError, TypeError):
            page_size = 20

        total_count = qs.count()
        total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 1

        # If requested page exceeds total_pages and we have records, self-heal to page 1
        if total_count > 0 and (page - 1) * page_size >= total_count:
            page = 1

        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        page_qs = qs[start_idx:end_idx]

        results = [serialize_member(p, alias) for p in page_qs]

        # If client explicitly specifies flat format
        if request.query_params.get('format') == 'flat':
            return Response(results, status=status.HTTP_200_OK)

        return Response({
            'count': total_count,
            'page': page,
            'page_size': page_size,
            'total_pages': total_pages,
            'results': results,
        }, status=status.HTTP_200_OK)

    def retrieve(self, request, pk=None):
        alias = _get_db(request)
        org = _get_org(request)
        profile = self.get_member_profile(alias, org, pk)
        if not profile:
            return Response({'detail': 'Member not found.'}, status=status.HTTP_404_NOT_FOUND)

        return Response(serialize_member(profile, alias), status=status.HTTP_200_OK)

    @action(detail=True, methods=['get'], url_path='360')
    def member_360(self, request, pk=None):
        """
        Returns the unified 6-section Member 360 aggregate payload.
        """
        alias = _get_db(request)
        org = _get_org(request)
        profile = self.get_member_profile(alias, org, pk)
        if not profile:
            return Response({'detail': 'Member not found.'}, status=status.HTTP_404_NOT_FOUND)

        data = build_member_360_aggregate(profile, alias)
        return Response(data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get'], url_path='timeline')
    def timeline(self, request, pk=None):
        """
        Returns the unified read-model chronological timeline for a member.
        """
        alias = _get_db(request)
        org = _get_org(request)
        profile = self.get_member_profile(alias, org, pk)
        if not profile:
            return Response({'detail': 'Member not found.'}, status=status.HTTP_404_NOT_FOUND)

        limit = int(request.query_params.get('limit', 100))
        timeline_events = build_member_timeline(profile, alias, limit=limit)
        return Response({'results': timeline_events, 'count': len(timeline_events)}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get'], url_path='available-actions')
    def available_actions(self, request, pk=None):
        """
        Returns the list of actions permitted for this member based on state.
        """
        alias = _get_db(request)
        org = _get_org(request)
        profile = self.get_member_profile(alias, org, pk)
        if not profile:
            return Response({'detail': 'Member not found.'}, status=status.HTTP_404_NOT_FOUND)

        active_m = Membership.objects.using(alias).filter(user_profile=profile).order_by('-created_at').first()
        outstanding = calculate_member_outstanding(profile, alias)
        actions = get_member_available_actions(profile, active_m, outstanding)
        return Response({'available_actions': actions}, status=status.HTTP_200_OK)

    def create(self, request):
        alias = _get_db(request)
        org = _get_org(request)
        data = request.data

        name = str(data.get('name', '')).strip()
        phone = str(data.get('phone', '')).strip()
        email = str(data.get('email', '')).strip()
        gender = str(data.get('gender', 'M'))
        status_val = str(data.get('status', 'Active'))
        location_id = data.get('location') or data.get('locationId') or data.get('home_branch_id')
        emergency_contact = data.get('emergency_contact') or data.get('emergencyContact', '')
        from_lead_id = data.get('from_lead_id') or data.get('fromLeadId')

        # Branch resolution
        branch = None
        if location_id:
            branch = Branch.objects.using(alias).filter(id=location_id).first()
        if not branch and org:
            branch = Branch.objects.using(alias).filter(organization=org).first()

        parts = name.split(None, 1)
        first_name = parts[0] if parts else 'Member'
        last_name = parts[1] if len(parts) > 1 else ''

        with transaction.atomic(using=alias):
            user = None
            if email:
                user = TenantUser.objects.using(alias).filter(organization=org, email__iexact=email).first()
            if not user and phone:
                user = TenantUser.objects.using(alias).filter(organization=org, phone=phone).first()

            if not user:
                user_id = uuid.uuid4()
                user = TenantUser.objects.using(alias).create(
                    id=user_id,
                    organization=org,
                    email=email or f"member_{user_id.hex[:8]}@tenant.internal",
                    phone=phone,
                    first_name=first_name,
                    last_name=last_name,
                    display_name=name or f"{first_name} {last_name}".strip(),
                    user_type='MEMBER',
                    status='INVITED',
                    is_login_allowed=True,
                    password_hash='',
                )

            mem_number = f"MEM-{uuid.uuid4().hex[:6].upper()}"
            profile, created = UserProfile.objects.using(alias).get_or_create(
                user=user,
                defaults={
                    'member_number': mem_number,
                    'first_name_snapshot': first_name,
                    'last_name_snapshot': last_name,
                    'gender': gender,
                    'preferred_branch': branch,
                    'joining_date': timezone.now().date(),
                    'member_type': 'MEMBER',
                    'member_status': 'ACTIVE' if 'act' in status_val.lower() else 'INACTIVE',
                    'emergency_contact_json': {'contact': emergency_contact} if emergency_contact else {},
                    'acquisition_source': 'MANUAL_CREATE',
                }
            )
            # If user profile already existed (e.g. staff becoming member), ensure member markers are established
            if not created:
                updated_fields = []
                if not profile.member_number:
                    profile.member_number = mem_number
                    updated_fields.append('member_number')
                if not profile.acquisition_source:
                    profile.acquisition_source = 'MANUAL_CREATE'
                    updated_fields.append('acquisition_source')
                if branch and not profile.preferred_branch:
                    profile.preferred_branch = branch
                    updated_fields.append('preferred_branch')
                if updated_fields:
                    profile.save(using=alias, update_fields=updated_fields)

            # Auto-convert linked Lead if present
            if from_lead_id:
                lead = Lead.objects.using(alias).filter(id=from_lead_id).first()
                if lead and lead.current_status != 'CONVERTED':
                    lead.current_status = 'CONVERTED'
                    lead.save(using=alias)

        return Response(serialize_member(profile, alias), status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None):
        alias = _get_db(request)
        org = _get_org(request)
        profile = self.get_member_profile(alias, org, pk)
        if not profile:
            return Response({'detail': 'Member not found.'}, status=status.HTTP_404_NOT_FOUND)

        data = request.data
        if 'name' in data:
            full = str(data['name']).strip()
            parts = full.split(None, 1)
            profile.first_name_snapshot = parts[0] if parts else ''
            profile.last_name_snapshot = parts[1] if len(parts) > 1 else ''
            if profile.user:
                profile.user.first_name = profile.first_name_snapshot
                profile.user.last_name = profile.last_name_snapshot
                profile.user.full_name = full
                profile.user.save(using=alias)
        if 'phone' in data and profile.user:
            profile.user.phone = data['phone']
            profile.user.save(using=alias)
        if 'email' in data and profile.user:
            profile.user.email = data['email']
            profile.user.save(using=alias)
        if 'gender' in data:
            profile.gender = data['gender']
        if 'status' in data:
            st = str(data['status']).upper()
            profile.member_status = 'ACTIVE' if 'ACT' in st else ('FROZEN' if 'FROZ' in st else 'INACTIVE')
        if 'location' in data or 'locationId' in data or 'home_branch_id' in data:
            loc_id = data.get('location') or data.get('locationId') or data.get('home_branch_id')
            branch = Branch.objects.using(alias).filter(id=loc_id).first()
            if branch:
                profile.preferred_branch = branch
        if 'emergencyContact' in data or 'emergency_contact' in data:
            c = data.get('emergencyContact') or data.get('emergency_contact')
            profile.emergency_contact_json = {'contact': c}

        profile.save(using=alias)
        return Response(serialize_member(profile, alias), status=status.HTTP_200_OK)

    def destroy(self, request, pk=None):
        alias = _get_db(request)
        org = _get_org(request)
        profile = self.get_member_profile(alias, org, pk)
        if not profile:
            return Response({'detail': 'Member not found.'}, status=status.HTTP_404_NOT_FOUND)

        profile.member_status = 'INACTIVE'
        profile.save(using=alias)
        return Response(status=status.HTTP_204_NO_CONTENT)

    # -------------------------------------------------------------------------
    # LIFECYCLE ACTIONS
    # -------------------------------------------------------------------------

    @action(detail=True, methods=['post'], url_path='renew')
    def renew(self, request, pk=None):
        """
        Extends or renews active membership using backend policy.
        """
        alias = _get_db(request)
        org = _get_org(request)
        profile = self.get_member_profile(alias, org, pk)
        if not profile:
            return Response({'detail': 'Member not found.'}, status=status.HTTP_404_NOT_FOUND)

        active_m = Membership.objects.using(alias).filter(user_profile=profile).order_by('-created_at').first()
        if not active_m:
            return Response({'error': 'No existing membership found to renew.'}, status=status.HTTP_400_BAD_REQUEST)

        months = int(request.data.get('months', 1))
        days = months * 30
        reason = request.data.get('reason', f'Membership renewed for {months} month(s)')

        try:
            mem = MembershipLifecycleService.extend_membership(
                membership=active_m,
                days=days,
                reason_text=reason,
                approved_by_user=request.user,
                db_alias=alias,
            )
            return Response({
                'success': True,
                'message': f"Renewed until {mem.end_date}",
                'member': serialize_member(profile, alias),
            }, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='upgrade')
    def upgrade(self, request, pk=None):
        """
        Upgrades membership to target package version.
        """
        alias = _get_db(request)
        org = _get_org(request)
        profile = self.get_member_profile(alias, org, pk)
        if not profile:
            return Response({'detail': 'Member not found.'}, status=status.HTTP_404_NOT_FOUND)

        active_m = Membership.objects.using(alias).filter(user_profile=profile, status='ACTIVE').order_by('-created_at').first()
        if not active_m:
            return Response({'error': 'No active membership to upgrade.'}, status=status.HTTP_400_BAD_REQUEST)

        target_pkg_id = request.data.get('package_id')
        target_pkg_ver_id = request.data.get('package_version_id')
        reason = request.data.get('reason', 'Package upgrade')

        target_pkg = Package.objects.using(alias).filter(id=target_pkg_id).first() if target_pkg_id else None
        target_ver = PackageVersion.objects.using(alias).filter(id=target_pkg_ver_id).first() if target_pkg_ver_id else (
            target_pkg.versions.using(alias).filter(status='ACTIVE').order_by('-version_number').first() if target_pkg else None
        )

        if not target_pkg or not target_ver:
            return Response({'error': 'Target package and version are required.'}, status=status.HTTP_400_BAD_REQUEST)

        # Update membership package directly
        from_pkg = active_m.package
        from_ver = active_m.package_version
        active_m.package = target_pkg
        active_m.package_version = target_ver
        active_m.save(using=alias, update_fields=['package', 'package_version', 'updated_at'])

        MembershipPackageHistory.objects.using(alias).create(
            membership=active_m,
            from_package=from_pkg,
            from_package_version=from_ver,
            to_package=target_pkg,
            to_package_version=target_ver,
            change_type='UPGRADE',
            changed_by_user=request.user,
            reason=reason,
        )

        return Response({
            'success': True,
            'message': f"Upgraded to {target_pkg.name} ({target_ver.version_number})",
            'member': serialize_member(profile, alias),
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='extend')
    def extend(self, request, pk=None):
        """
        Extends membership validity by N days.
        """
        alias = _get_db(request)
        org = _get_org(request)
        profile = self.get_member_profile(alias, org, pk)
        if not profile:
            return Response({'detail': 'Member not found.'}, status=status.HTTP_404_NOT_FOUND)

        active_m = Membership.objects.using(alias).filter(user_profile=profile).order_by('-created_at').first()
        if not active_m:
            return Response({'error': 'No membership found to extend.'}, status=status.HTTP_400_BAD_REQUEST)

        days = int(request.data.get('days', 7))
        reason = request.data.get('reason', f'Extended by {days} days')

        try:
            mem = MembershipLifecycleService.extend_membership(
                membership=active_m,
                days=days,
                reason_text=reason,
                approved_by_user=request.user,
                db_alias=alias,
            )
            return Response({
                'success': True,
                'message': f"Extended until {mem.end_date}",
                'member': serialize_member(profile, alias),
            }, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='freeze')
    def freeze(self, request, pk=None):
        """
        Applies a membership freeze window.
        """
        alias = _get_db(request)
        org = _get_org(request)
        profile = self.get_member_profile(alias, org, pk)
        if not profile:
            return Response({'detail': 'Member not found.'}, status=status.HTTP_404_NOT_FOUND)

        active_m = Membership.objects.using(alias).filter(user_profile=profile, status='ACTIVE').order_by('-created_at').first()
        if not active_m:
            return Response({'error': 'No active membership to freeze.'}, status=status.HTTP_400_BAD_REQUEST)

        freeze_from = request.data.get('freeze_from')
        freeze_until = request.data.get('freeze_until')
        reason = request.data.get('reason', 'Member requested freeze')

        if not freeze_from or not freeze_until:
            return Response({'error': 'freeze_from and freeze_until dates are required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            from_d = datetime.strptime(freeze_from[:10], '%Y-%m-%d').date()
            until_d = datetime.strptime(freeze_until[:10], '%Y-%m-%d').date()
            MembershipLifecycleService.apply_freeze(
                membership=active_m,
                freeze_from=from_d,
                freeze_until=until_d,
                reason_text=reason,
                approved_by_user=request.user,
                db_alias=alias,
            )
            profile.member_status = 'FROZEN'
            profile.save(using=alias, update_fields=['member_status'])
            return Response({
                'success': True,
                'message': f"Membership frozen from {from_d} to {until_d}",
                'member': serialize_member(profile, alias),
            }, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='unfreeze')
    def unfreeze(self, request, pk=None):
        """
        Prematurely unfreezes a frozen membership.
        """
        alias = _get_db(request)
        org = _get_org(request)
        profile = self.get_member_profile(alias, org, pk)
        if not profile:
            return Response({'detail': 'Member not found.'}, status=status.HTTP_404_NOT_FOUND)

        mem = Membership.objects.using(alias).filter(user_profile=profile, status='FROZEN').order_by('-created_at').first()
        if not mem:
            mem = Membership.objects.using(alias).filter(user_profile=profile).order_by('-created_at').first()

        try:
            MembershipLifecycleService.unfreeze_membership(
                membership=mem,
                reason_text=request.data.get('reason', 'Unfreeze by staff'),
                approved_by_user=request.user,
                db_alias=alias,
            )
            profile.member_status = 'ACTIVE'
            profile.save(using=alias, update_fields=['member_status'])
            return Response({
                'success': True,
                'message': "Membership unfreezed and restored to Active status",
                'member': serialize_member(profile, alias),
            }, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='transfer')
    def transfer(self, request, pk=None):
        """
        Transfers home branch of member and membership.
        """
        alias = _get_db(request)
        org = _get_org(request)
        profile = self.get_member_profile(alias, org, pk)
        if not profile:
            return Response({'detail': 'Member not found.'}, status=status.HTTP_404_NOT_FOUND)

        branch_id = request.data.get('branch_id')
        target_branch = Branch.objects.using(alias).filter(id=branch_id).first()
        if not target_branch:
            return Response({'error': 'Valid target branch_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

        active_m = Membership.objects.using(alias).filter(user_profile=profile).order_by('-created_at').first()
        if active_m:
            MembershipLifecycleService.transfer_home_branch(
                membership=active_m,
                target_branch=target_branch,
                reason_text=request.data.get('reason', 'Home branch transfer'),
                actor_user=request.user,
                db_alias=alias,
            )
        else:
            profile.preferred_branch = target_branch
            profile.save(using=alias, update_fields=['preferred_branch'])

        return Response({
            'success': True,
            'message': f"Home branch transferred to {target_branch.name}",
            'member': serialize_member(profile, alias),
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='cancel')
    def cancel(self, request, pk=None):
        """
        Cancels active membership.
        """
        alias = _get_db(request)
        org = _get_org(request)
        profile = self.get_member_profile(alias, org, pk)
        if not profile:
            return Response({'detail': 'Member not found.'}, status=status.HTTP_404_NOT_FOUND)

        active_m = Membership.objects.using(alias).filter(user_profile=profile, status='ACTIVE').order_by('-created_at').first()
        if not active_m:
            return Response({'error': 'No active membership to cancel.'}, status=status.HTTP_400_BAD_REQUEST)

        reason = request.data.get('reason', 'Cancelled by member/staff')
        MembershipLifecycleService.cancel_membership(
            membership=active_m,
            reason_text=reason,
            actor_user=request.user,
            db_alias=alias,
        )
        profile.member_status = 'INACTIVE'
        profile.save(using=alias, update_fields=['member_status'])

        return Response({
            'success': True,
            'message': "Membership has been cancelled.",
            'member': serialize_member(profile, alias),
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='adjust-entitlement')
    def adjust_entitlement(self, request, pk=None):
        """
        Controlled adjustment of session entitlement units with append-only ledger entry.
        """
        alias = _get_db(request)
        org = _get_org(request)
        profile = self.get_member_profile(alias, org, pk)
        if not profile:
            return Response({'detail': 'Member not found.'}, status=status.HTTP_404_NOT_FOUND)

        active_m = Membership.objects.using(alias).filter(user_profile=profile).order_by('-created_at').first()
        if not active_m:
            return Response({'error': 'No membership found to adjust.'}, status=status.HTTP_400_BAD_REQUEST)

        entitlement_type = request.data.get('entitlement_type', 'HOME_BRANCH_SESSION')
        units_delta = Decimal(str(request.data.get('units_delta', '1.0')))
        reason_code = request.data.get('reason_code', 'STAFF_ADJUSTMENT')
        reason_text = request.data.get('reason_text', 'Admin session credit adjustment')

        try:
            ledger = MembershipLifecycleService.adjust_entitlement(
                membership=active_m,
                entitlement_type=entitlement_type,
                units_delta=units_delta,
                reason_code=reason_code,
                reason_text=reason_text,
                actor_user=request.user,
                db_alias=alias,
            )
            return Response({
                'success': True,
                'message': f"Adjusted {units_delta:+f} {entitlement_type} units (Balance: {ledger.balance_after})",
                'balance_after': float(ledger.balance_after) if ledger.balance_after is not None else None,
                'member': serialize_member(profile, alias),
            }, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='rejoin')
    def rejoin(self, request, pk=None):
        """
        Rejoins an inactive, expired, or cancelled member by creating a new commercial order
        and activating a new membership contract while reusing the existing UserProfile identity.
        Preserves all prior membership and transaction history.
        """
        alias = _get_db(request)
        org = _get_org(request)
        profile = self.get_member_profile(alias, org, pk)
        if not profile:
            return Response({'detail': 'Member not found.'}, status=status.HTTP_404_NOT_FOUND)

        package_id = request.data.get('package_id')
        package_version_id = request.data.get('package_version_id')
        branch_id = request.data.get('branch_id') or getattr(profile.preferred_branch, 'id', None)
        payment_amount = request.data.get('payment_amount')
        payment_provider = request.data.get('payment_provider', 'CASH')
        payment_method = request.data.get('payment_method', 'CASH')
        reason = request.data.get('reason', 'Member Rejoin')

        if not package_id:
            return Response({'error': 'package_id is required for rejoin.'}, status=status.HTTP_400_BAD_REQUEST)

        package = Package.objects.using(alias).filter(id=package_id).first()
        if not package:
            return Response({'error': 'Package not found.'}, status=status.HTTP_404_NOT_FOUND)

        if package_version_id:
            package_version = PackageVersion.objects.using(alias).filter(id=package_version_id, package=package).first()
        else:
            package_version = package.versions.using(alias).filter(status='ACTIVE').order_by('-version_number').first()

        if not package_version:
            return Response({'error': 'Active package version not found for package.'}, status=status.HTTP_400_BAD_REQUEST)

        price_obj = package_version.prices.using(alias).filter(status='ACTIVE').first()
        unit_price = price_obj.base_price if price_obj else Decimal('5000.00')

        branch = Branch.objects.using(alias).filter(id=branch_id).first() if branch_id else profile.preferred_branch
        if not branch and org:
            branch = Branch.objects.using(alias).filter(organization=org).first()

        with transaction.atomic(using=alias):
            old_status = profile.member_status
            profile.member_status = 'ACTIVE'
            if branch:
                profile.preferred_branch = branch
            profile.save(using=alias, update_fields=['member_status', 'preferred_branch', 'updated_at'])

            # Create canonical Order with order_type='REJOIN'
            order = CommerceService.create_order(
                branch=branch,
                items_data=[{
                    'item_type': 'PACKAGE',
                    'package_id': package.id,
                    'package_version_id': package_version.id,
                    'package_price_id': price_obj.id if price_obj else None,
                    'item_name_snapshot': f"{package.name} (v{package_version.version_number})",
                    'quantity': Decimal('1.00'),
                    'unit_price': unit_price,
                    'discount_amount': Decimal('0.00'),
                    'tax_percent': Decimal('0.000'),
                }],
                user_profile=profile,
                order_type='REJOIN',
                source='FRONT_DESK',
                currency='INR',
                notes=reason,
                created_by=request.user,
                db_alias=alias,
            )

            # Record payment if provided
            pay_amt = Decimal(str(payment_amount)) if payment_amount is not None else order.total_amount
            if pay_amt > Decimal('0.00'):
                CommerceService.record_payment(
                    order_id=str(order.id),
                    amount=pay_amt,
                    provider=payment_provider,
                    payment_method=payment_method,
                    actor=request.user,
                    db_alias=alias,
                )

            # Activate new Membership contract snapshot & entitlements
            order_item = order.items.first()
            membership = MembershipLifecycleService.activate_membership_from_order(
                order=order,
                order_item=order_item,
                start_date=timezone.now().date(),
                db_alias=alias,
                created_by_user=request.user,
            )

            # Record status history tracking the Rejoin transition
            MembershipStatusHistory.objects.using(alias).create(
                membership=membership,
                from_status=old_status or 'INACTIVE',
                to_status='ACTIVE',
                reason_code='REJOIN',
                reason_text=f"Rejoined: {reason}",
                changed_by_user=request.user,
            )

            # Audit event
            record_business_audit(
                organization=org,
                branch=branch,
                module='memberships',
                action_code='MEMBER_REJOINED',
                entity_type='UserProfile',
                entity_id=profile.id,
                actor_user=request.user,
                event_description=f"Member {profile.member_number} rejoined under plan {package.name}",
                db_alias=alias,
            )

        return Response({
            'success': True,
            'message': f"Member successfully rejoined under {package.name}.",
            'membership_id': str(membership.id),
            'order_id': str(order.id),
            'member': serialize_member(profile, alias),
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='collect-outstanding')
    def collect_outstanding(self, request, pk=None):
        """
        Collects payment against an order with outstanding balance.
        Requires finance payment collection authority (finance.payments.create).
        Enforces idempotency and balance cap validation.
        """
        alias = _get_db(request)
        org = _get_org(request)
        profile = self.get_member_profile(alias, org, pk)
        if not profile:
            return Response({'detail': 'Member not found.'}, status=status.HTTP_404_NOT_FOUND)

        # Enforce canonical payment permission: finance.payments.create
        user = request.user
        has_finance_perm = False
        if getattr(user, 'is_superuser', False) or getattr(user, '_auth_type', None) == 'platform':
            has_finance_perm = True
        else:
            from .rbac_engine import RBACAuthorizationEngine
            allowed, _, _ = RBACAuthorizationEngine.evaluate(
                user=user,
                required_permission='finance.payments.create',
                branch_id=str(profile.preferred_branch_id) if profile.preferred_branch_id else None,
                request=request,
            )
            has_finance_perm = allowed

        if not has_finance_perm:
            return Response(
                {'error': 'Permission denied: payment confirmation permission (finance.payments.create) required.'},
                status=status.HTTP_403_FORBIDDEN
            )

        order_id = request.data.get('order_id')
        amount = request.data.get('amount')
        provider = request.data.get('provider', 'CASH')
        payment_method = request.data.get('payment_method', 'CASH')
        idempotency_key = request.data.get('idempotency_key') or request.headers.get('Idempotency-Key')

        if not order_id:
            # Pick first order with outstanding balance
            pending_order = Order.objects.using(alias).filter(
                user_profile=profile, status__in=['PENDING_PAYMENT', 'PARTIALLY_PAID']
            ).order_by('-created_at').first()
            if pending_order:
                order_id = str(pending_order.id)
            else:
                return Response({'error': 'No pending order found to collect payment for.'}, status=status.HTTP_400_BAD_REQUEST)

        order = Order.objects.using(alias).filter(id=order_id, user_profile=profile).first()
        if not order:
            return Response({'error': 'Order not found for this member.'}, status=status.HTTP_404_NOT_FOUND)

        # Compute current order remaining balance
        paid_sum = PaymentTransaction.objects.using(alias).filter(order=order, status='SUCCESS').aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        refund_sum = Refund.objects.using(alias).filter(order=order, status='SUCCESS').aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        net_paid = max(Decimal('0.00'), paid_sum - refund_sum)
        rem = max(Decimal('0.00'), order.total_amount - net_paid)

        if not amount:
            amt = rem
        else:
            amt = Decimal(str(amount))

        if amt <= Decimal('0.00'):
            return Response({'error': 'Payment amount must be greater than zero.'}, status=status.HTTP_400_BAD_REQUEST)

        if amt > rem:
            return Response({'error': f'Payment amount (₹{amt}) cannot exceed order outstanding balance (₹{rem}).'}, status=status.HTTP_400_BAD_REQUEST)

        # Idempotency check: replay existing payment if idempotency_key was already processed
        if idempotency_key:
            existing_txn = PaymentTransaction.objects.using(alias).filter(
                idempotency_key=idempotency_key, status='SUCCESS'
            ).first()
            if existing_txn:
                invoice = MemberInvoice.objects.using(alias).filter(order=order).first()
                return Response({
                    'success': True,
                    'message': f"Payment of ₹{existing_txn.amount} already recorded (idempotent replay).",
                    'transaction_id': str(existing_txn.id),
                    'invoice_id': str(invoice.id) if invoice else None,
                    'member': serialize_member(profile, alias),
                }, status=status.HTTP_200_OK)

        try:
            txn, invoice = CommerceService.record_payment(
                order_id=str(order.id),
                amount=amt,
                provider=provider,
                payment_method=payment_method,
                idempotency_key=idempotency_key,
                actor=request.user,
                db_alias=alias,
            )
            return Response({
                'success': True,
                'message': f"Payment of ₹{amt} recorded successfully via {provider}.",
                'transaction_id': str(txn.id),
                'invoice_id': str(invoice.id) if invoice else None,
                'member': serialize_member(profile, alias),
            }, status=status.HTTP_201_CREATED)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='check-in')
    def check_in(self, request, pk=None):
        """
        Records member attendance check-in and consumes entitlement session if active.
        """
        alias = _get_db(request)
        org = _get_org(request)
        profile = self.get_member_profile(alias, org, pk)
        if not profile:
            return Response({'detail': 'Member not found.'}, status=status.HTTP_404_NOT_FOUND)

        loc_id = request.data.get('location') or request.data.get('branch_id')
        method = request.data.get('method', 'Front Desk')
        branch = None
        if loc_id:
            branch = Branch.objects.using(alias).filter(id=loc_id).first()
        if not branch:
            branch = profile.preferred_branch or Branch.objects.using(alias).filter(organization=org).first()

        record = None
        if branch:
            record = AttendanceRecord.objects.using(alias).create(
                user_profile=profile,
                branch=branch,
                status='PRESENT',
                check_in_status='SUCCESSFUL',
                check_in_method='FRONT_DESK' if 'desk' in method.lower() else 'QR',
                check_in_at=timezone.now(),
            )

            # Consume entitlement session from active membership if available
            active_m = (
                Membership.objects.using(alias)
                .filter(user_profile=profile, status='ACTIVE')
                .order_by('-created_at')
                .first()
            )
            if active_m:
                # Prioritize home branch session entitlement if check-in is at home branch
                is_home = (branch and active_m.home_branch and branch.id == active_m.home_branch.id)
                ent = None
                if is_home:
                    ent = MembershipEntitlement.objects.using(alias).filter(
                        membership=active_m, status='ACTIVE', entitlement_type__icontains='HOME'
                    ).first()
                if not ent:
                    ent = MembershipEntitlement.objects.using(alias).filter(
                        membership=active_m, status='ACTIVE'
                    ).first()
                if ent:
                    try:
                        MembershipLifecycleService.consume_entitlement(
                            membership=active_m,
                            entitlement_type=ent.entitlement_type,
                            units=Decimal('1.00'),
                            reason_text=f"Check-in at {branch.name}",
                            created_by_user=request.user,
                            db_alias=alias,
                        )
                    except Exception as e:
                        logger.warning(f"Entitlement consumption skipped on check-in: {e}")

        return Response({
            'success': True,
            'message': f"Check-in recorded for {profile.first_name_snapshot or 'Member'} at {branch.name if branch else 'Branch'}",
            'record': {
                'id': str(record.id) if record else None,
                'check_in_time': str(timezone.now()),
                'branch_id': str(branch.id) if branch else None,
            },
        }, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['get'], url_path='payment-methods')
    def payment_methods(self, request):
        """
        Returns active payment methods derived from backend configuration and active integrations.
        Never relies on unconfigured frontend static enums.
        """
        alias = _get_db(request)
        from .models_infra import TenantIntegration

        # Canonical front desk payment methods
        methods = [
            {'id': 'CASH', 'label': 'Cash (Front Desk)', 'provider': 'CASH', 'category': 'OFFLINE'},
            {'id': 'BANK_TRANSFER', 'label': 'Bank Transfer / NEFT', 'provider': 'BANK_TRANSFER', 'category': 'OFFLINE'},
            {'id': 'UPI', 'label': 'UPI / QR Scan', 'provider': 'CASH', 'category': 'OFFLINE'},
        ]

        # Query configured and active payment gateways
        try:
            active_gateways = TenantIntegration.objects.using(alias).filter(
                integration_type='PAYMENT', status='ACTIVE'
            )
            for gw in active_gateways:
                p_upper = gw.provider.upper()
                if 'RAZORPAY' in p_upper:
                    methods.append({'id': 'RAZORPAY', 'label': 'Razorpay Online Gateway', 'provider': 'RAZORPAY', 'category': 'ONLINE'})
                elif 'ICICI' in p_upper or 'POS' in p_upper:
                    methods.append({'id': 'ICICI_POS', 'label': 'ICICI POS Machine', 'provider': 'ICICI_POS', 'category': 'CARD_MACHINE'})
                elif 'STRIPE' in p_upper:
                    methods.append({'id': 'STRIPE', 'label': 'Stripe Gateway', 'provider': 'STRIPE', 'category': 'ONLINE'})
        except Exception:
            pass

        return Response({'payment_methods': methods}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='plans')
    def plans(self, request):
        alias = _get_db(request)
        org = _get_org(request)
        packages = Package.objects.using(alias).filter(status='ACTIVE')
        if org:
            packages = packages.filter(organization=org)

        out = []
        for pkg in packages.order_by('name'):
            pv = pkg.versions.using(alias).filter(status='ACTIVE').order_by('-version_number').first()
            price_val = 5000.0
            currency = 'INR'
            duration_months = 1
            if pv:
                pr = pv.prices.using(alias).filter(status='ACTIVE').first()
                if pr:
                    price_val = float(pr.base_price)
                    currency = pr.currency
                duration_months = pv.duration_value if pv.duration_value else 1

            out.append({
                'id': str(pkg.id),
                'package_id': str(pkg.id),
                'package_version_id': str(pv.id) if pv else None,
                'name': pkg.name,
                'category': 'Membership',
                'duration_months': duration_months,
                'price': price_val,
                'currency': currency,
                'total_sessions': None,
                'is_active': True,
                'description': pkg.description or '',
            })
        return Response(out, status=status.HTTP_200_OK)

