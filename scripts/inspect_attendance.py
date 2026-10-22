import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.master.models_tenant import Tenant
from apps.tenant_core.context import tenant_database_context
from apps.tenant_core.models_bookings import AttendancePolicySet, AttendancePenaltyRule, BookingPolicySet, Booking
from apps.tenant_core.models_attendance import AttendanceRecord, AccessEvent
from apps.tenant_core.models_classes import ClassOccurrence
from apps.tenant_core.models_org import Organization, Branch
from apps.tenant_core.models_govern import OrganizationSettings, BranchSettings
from django.utils import timezone

t = Tenant.objects.filter(status='ACTIVE').first()
print(f"Found active tenant: {t.name} (id={t.id})")
with tenant_database_context(t.id) as alias:
    print(f'Tenant DB alias: {alias}')
    print('=== ORG & BRANCH SETTINGS ===')
    for org in Organization.objects.using(alias).all():
        print(f"Org: {org.name} ({org.id})")
        settings_obj = OrganizationSettings.objects.using(alias).filter(organization=org).first()
        if settings_obj:
            print(f"  Org attendance_config: {settings_obj.attendance_config}")
            print(f"  Org booking_config: {settings_obj.booking_config}")
    for br in Branch.objects.using(alias).all():
        print(f"Branch: {br.name} ({br.id})")
        settings_obj = BranchSettings.objects.using(alias).filter(branch=br).first()
        if settings_obj:
            print(f"  Branch attendance_config: {settings_obj.attendance_config}")
            print(f"  Branch booking_config: {settings_obj.booking_config}")

    print('\n=== BOOKING POLICY SETS ===')
    for bp in BookingPolicySet.objects.using(alias).all():
        print(f'BookingPolicy: {bp.id} | status={bp.status} | late_entry_minutes={bp.late_entry_minutes} | auto_waitlist={bp.auto_waitlist_promotion}')

    print('\n=== ATTENDANCE POLICY SETS ===')
    for ap in AttendancePolicySet.objects.using(alias).all():
        print(f'AttendancePolicy: {ap.name} | normal_max={ap.normal_max_advance_bookings} | restriction_threshold={ap.restriction_threshold} | no_show_mark_after={ap.no_show_mark_after_minutes}')
        for r in ap.penalty_rules.all():
            print(f'   Rule: {r.rule_code} | units={r.base_session_units}+{r.additional_penalty_units} | restore={r.restore_booked_session}')

    print('\n=== BOOKINGS FOR TODAY ===')
    now = timezone.now()
    print(f'Current time: {now}')
    for b in Booking.objects.using(alias).filter(occurrence__occurrence_date='2026-10-21'):
        print(f'Booking: {b.booking_number} | status: {b.status} | member: {b.user_profile} | occ: {b.occurrence.class_template.name} ({b.occurrence.start_at} - {b.occurrence.end_at})')
        for att in b.attendance_records.all():
            print(f'   AttendanceRecord: status={att.status} | check_in_status={att.check_in_status} | check_in_at={att.check_in_at} | check_out_at={att.check_out_at}')
        for acc in AccessEvent.objects.using(alias).filter(booking=b):
            print(f'   AccessEvent: {acc.event_type} | dir={acc.direction} | time={acc.event_at} | recon={acc.reconciliation_status}')
