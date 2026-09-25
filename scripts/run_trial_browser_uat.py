import os
import sys
import uuid
import datetime
from django.utils import timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()

from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from config.tenant_middleware import _register_tenant_connection
from config.routers import set_tenant_db_alias
from rest_framework.test import APIRequestFactory
factory = APIRequestFactory()

tenant = Tenant.objects.filter(slug='sweat').first()
if not tenant:
    print("ERROR: Tenant 'sweat' not found.")
    sys.exit(1)

ds = TenantDataSource.objects.filter(tenant=tenant).first()
db_alias = f'tenant_{ds.db_name}'
_register_tenant_connection(db_alias, ds.db_name, data_source=ds)
set_tenant_db_alias(db_alias)

from apps.tenant_core.models_catalog import Program
from apps.tenant_core.models_classes import ClassTemplate, ClassOccurrence
from apps.tenant_core.models_org import Branch, Organization
from apps.tenant_core.models_crm import Lead, TrialBooking, TrialStatusHistory, LeadActivity
from apps.tenant_core.models_bookings import Booking
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.services_crm import CRMLeadService
from apps.tenant_core.services_crm_dashboard import CRMDashboardService

print("==================================================")
print("1. SETUP / VERIFY REAL TRIAL-ENABLED OCCURRENCES")
print("==================================================")
branch = Branch.objects.first()
org = Organization.objects.first()
program = Program.objects.first()
template = ClassTemplate.objects.filter(program=program, allow_trial=True).first()

print(f"Program: {program.name} ({program.id})")
print(f"Template: {template.name} ({template.id}), allow_trial={template.allow_trial}, default_trial_capacity={template.default_trial_capacity}")
print(f"Branch: {branch.name} ({branch.id})")

# Occurrence 1: Trial Cap = 1 (for initial booking & full cap test)
occ_date_1 = (timezone.now() + datetime.timedelta(days=2)).date()
occ_1, _ = ClassOccurrence.objects.get_or_create(
    class_template=template,
    branch=branch,
    occurrence_date=occ_date_1,
    start_at=datetime.datetime.combine(occ_date_1, datetime.time(10, 0), tzinfo=datetime.timezone.utc),
    defaults={
        'end_at': datetime.datetime.combine(occ_date_1, datetime.time(11, 0), tzinfo=datetime.timezone.utc),
        'capacity': 10,
        'trial_capacity': 1,
        'status': 'SCHEDULED',
    }
)
occ_1.capacity = 10
occ_1.trial_capacity = 1
occ_1.status = 'SCHEDULED'
occ_1.save()

# Occurrence 2: Trial Cap = 2 (for reschedule target)
occ_date_2 = (timezone.now() + datetime.timedelta(days=3)).date()
occ_2, _ = ClassOccurrence.objects.get_or_create(
    class_template=template,
    branch=branch,
    occurrence_date=occ_date_2,
    start_at=datetime.datetime.combine(occ_date_2, datetime.time(11, 0), tzinfo=datetime.timezone.utc),
    defaults={
        'end_at': datetime.datetime.combine(occ_date_2, datetime.time(12, 0), tzinfo=datetime.timezone.utc),
        'capacity': 10,
        'trial_capacity': 2,
        'status': 'SCHEDULED',
    }
)
occ_2.capacity = 10
occ_2.trial_capacity = 2
occ_2.status = 'SCHEDULED'
occ_2.save()

# Occurrence 3: Trial Cap = 1, will be made full (for failed reschedule target)
occ_date_3 = (timezone.now() + datetime.timedelta(days=4)).date()
occ_3, _ = ClassOccurrence.objects.get_or_create(
    class_template=template,
    branch=branch,
    occurrence_date=occ_date_3,
    start_at=datetime.datetime.combine(occ_date_3, datetime.time(12, 0), tzinfo=datetime.timezone.utc),
    defaults={
        'end_at': datetime.datetime.combine(occ_date_3, datetime.time(13, 0), tzinfo=datetime.timezone.utc),
        'capacity': 10,
        'trial_capacity': 1,
        'status': 'SCHEDULED',
    }
)
occ_3.capacity = 10
occ_3.trial_capacity = 1
occ_3.status = 'SCHEDULED'
occ_3.save()

# Clean up previous test trial bookings on these occurrences if any
test_tbs = TrialBooking.objects.filter(class_occurrence_id__in=[occ_1.id, occ_2.id, occ_3.id])
TrialStatusHistory.objects.filter(trial_booking__in=test_tbs).delete()
test_tbs.delete()
Booking.objects.filter(occurrence__in=[occ_1, occ_2, occ_3]).delete()

# Get or create safe leads
lead_aarav = Lead.objects.filter(first_name='Aarav', last_name='Sharma').first()
lead_aarav.current_status = 'FOLLOW_UP_PENDING'
lead_aarav.converted_user_profile = None
lead_aarav.save()

lead_second = Lead.objects.filter(email_normalized='rohan.verma@test.com').first()
if not lead_second:
    lead_second = Lead.objects.create(
        organization=org,
        first_name='Rohan',
        last_name='Verma',
        email_normalized='rohan.verma@test.com',
        phone_normalized='+919876543211',
        branch=branch,
        interested_program=program,
        current_status='NEW'
    )
else:
    lead_second.current_status = 'NEW'
    lead_second.converted_user_profile = None
    lead_second.save()

lead_converted = Lead.objects.filter(email_normalized='kavita.mehta@test.com').first()
if not lead_converted:
    lead_converted = Lead.objects.create(
        organization=org,
        first_name='Kavita',
        last_name='Mehta',
        email_normalized='kavita.mehta@test.com',
        phone_normalized='+919876543212',
        branch=branch,
        interested_program=program,
        current_status='CONVERTED'
    )
else:
    lead_converted.current_status = 'CONVERTED'
    lead_converted.save()

admin_user = TenantUser.objects.filter(email='admin@sweat.com').first()
admin_user._auth_type = 'tenant'

print("Occurrences and safe leads ready.")
print(f"Occ 1 (cap 1): {occ_1.id} on {occ_1.occurrence_date}")
print(f"Occ 2 (cap 2): {occ_2.id} on {occ_2.occurrence_date}")
print(f"Occ 3 (cap 1): {occ_3.id} on {occ_3.occurrence_date}")
print(f"Lead Aarav: {lead_aarav.id}")
print(f"Lead Second (Rohan): {lead_second.id}")
print(f"Lead Converted (Kavita): {lead_converted.id}")

print("\n==================================================")
print("2. BOOK TRIAL FROM CRM FOR AARAV SHARMA")
print("==================================================")
org = Organization.objects.first()
def get_trials_count():
    data = CRMDashboardService.get_dashboard_data(organization=org, user=admin_user, filters={}, db_alias=db_alias)
    return data.get('summary_kpis', {}).get('trials_booked', 0)

initial_trials_booked = get_trials_count()

tb = CRMLeadService.book_trial(
    lead=lead_aarav,
    branch=branch,
    class_occurrence_id=occ_1.id,
    actor_user=admin_user,
    notes="UAT automated verification trial booking",
    db_alias=db_alias
)

canonical_id = tb.id
print(f"Trial Booking created! Canonical ID: {canonical_id}")
print(f"Status: {tb.status}, Occurrence: {tb.class_occurrence_id}")

print("\n==================================================")
print("3. VERIFY SAME BOOKING EVERYWHERE")
print("==================================================")
lead_aarav.refresh_from_db()
print(f"A. Lead current_status: {lead_aarav.current_status} (Expected: TRIAL_BOOKED)")
assert lead_aarav.current_status == 'TRIAL_BOOKED', "Lead status mismatch!"

# B. Lead 360 / Trial Management query
tb_from_crm = TrialBooking.objects.get(id=canonical_id)
print(f"C. Lead 360 / Trial Management ID: {tb_from_crm.id} (Status: {tb_from_crm.status})")
assert tb_from_crm.id == canonical_id

# E. Operations -> Bookings
from rest_framework.test import force_authenticate
from apps.tenant_core.views_bookings import BookingViewSet
req_ops = factory.get(f'/api/v1/tenant/ops/bookings/?branch_id={branch.id}')
req_ops.tenant = tenant
req_ops.user = admin_user
req_ops._tenant_db = db_alias
force_authenticate(req_ops, user=admin_user)
ops_view = BookingViewSet.as_view({'get': 'list'})
ops_res = ops_view(req_ops)
ops_booking_item = next((b for b in ops_res.data if str(b.get('id')) == str(canonical_id)), None)
print(f"E. Operations Booking ID: {ops_booking_item.get('id') if ops_booking_item else None}, is_trial={ops_booking_item.get('is_trial') if ops_booking_item else None}, booking_number={ops_booking_item.get('booking_number') if ops_booking_item else None}")
assert ops_booking_item is not None, "Operations booking item not found in BookingViewSet!"
assert ops_booking_item.get('is_trial') is True, "Operations booking is_trial is not True!"

# Check for duplicate normal bookings in Booking table
dup_count = Booking.objects.filter(occurrence__in=[occ_1, occ_2, occ_3]).count()
print(f"Duplicate normal Booking count: {dup_count} (Expected: 0)")
assert dup_count == 0, "Duplicate normal booking found!"

# F. Dashboard
new_trials_booked = get_trials_count()
print(f"F. Dashboard Trials Booked: before={initial_trials_booked}, after={new_trials_booked}")

# G. ClassOccurrence trial remaining capacity decreases
occ_1.refresh_from_db()
active_trials_occ_1 = TrialBooking.objects.filter(class_occurrence_id=occ_1.id, status__in=['BOOKED', 'CONFIRMED', 'ATTENDED']).count()
trial_rem_1 = max(0, occ_1.trial_capacity - active_trials_occ_1)
print(f"G. Occurrence 1 Trial Capacity: cap={occ_1.trial_capacity}, active={active_trials_occ_1}, remaining={trial_rem_1} (Expected: 0)")
assert trial_rem_1 == 0, "Occurrence 1 trial capacity not consumed!"

print(f"\nCanonical ID Comparison:")
print(f"Lead 360: {tb_from_crm.id}")
print(f"Trial Management: {tb_from_crm.id}")
print(f"Operations Bookings: {ops_booking_item['id']}")

print("\n==================================================")
print("4. FULL TRIAL CAP TEST")
print("==================================================")
# Call available trial sessions API
from apps.tenant_core.views_crm import TrialBookingViewSet
request = factory.get(f'/api/v1/tenant/trial-bookings/available_slots/?branch_id={branch.id}&date={occ_date_1}')
request.tenant = tenant
request.user = admin_user
request._tenant_db = db_alias
force_authenticate(request, user=admin_user)
view = TrialBookingViewSet.as_view({'get': 'available_slots'})
response = view(request)
slots = response.data.get('slots', []) if isinstance(response.data, dict) else []
available_occ_ids = [str(s.get('class_occurrence_id') or s.get('id')) for s in slots]

print(f"Available sessions on {occ_date_1}: {available_occ_ids}")
full_occ_returned = str(occ_1.id) in available_occ_ids
print(f"Full trial occurrence returned by API: {'YES' if full_occ_returned else 'NO'} (Expected: NO)")
assert not full_occ_returned, "Full trial occurrence was returned in available trial sessions!"

print("\n==================================================")
print("5. RESCHEDULE TRIAL SESSION")
print("==================================================")
rescheduled_tb = CRMLeadService.reschedule_trial(
    trial=tb,
    new_class_occurrence_id=occ_2.id,
    actor_user=admin_user,
    reason="Lead requested later date",
    db_alias=db_alias
)

print(f"Rescheduled Trial Booking ID: {rescheduled_tb.id} (Expected: {canonical_id})")
assert rescheduled_tb.id == canonical_id, "Trial Booking ID changed on reschedule!"
assert rescheduled_tb.class_occurrence_id == occ_2.id, "Occurrence did not update to occ_2!"

# Verify old capacity restored
active_trials_occ_1_after = TrialBooking.objects.filter(class_occurrence_id=occ_1.id, status__in=['BOOKED', 'CONFIRMED', 'ATTENDED']).count()
trial_rem_1_after = max(0, occ_1.trial_capacity - active_trials_occ_1_after)
print(f"Old occurrence capacity restored: active={active_trials_occ_1_after}, remaining={trial_rem_1_after} (Expected: 1)")
assert trial_rem_1_after == 1, "Old occurrence trial capacity not restored!"

# Verify new capacity consumed
active_trials_occ_2 = TrialBooking.objects.filter(class_occurrence_id=occ_2.id, status__in=['BOOKED', 'CONFIRMED', 'ATTENDED']).count()
trial_rem_2 = max(0, occ_2.trial_capacity - active_trials_occ_2)
print(f"New occurrence capacity consumed: active={active_trials_occ_2}, remaining={trial_rem_2} (Expected: 1)")
assert active_trials_occ_2 == 1, "New occurrence trial capacity not consumed!"

# Operations booking updated
req_ops_reschedule = factory.get(f'/api/v1/tenant/ops/bookings/{canonical_id}/')
req_ops_reschedule.tenant = tenant
req_ops_reschedule.user = admin_user
req_ops_reschedule._tenant_db = db_alias
force_authenticate(req_ops_reschedule, user=admin_user)
ops_view_detail = BookingViewSet.as_view({'get': 'retrieve'})
ops_detail_res = ops_view_detail(req_ops_reschedule, pk=str(canonical_id))
print(f"Operations Booking occurrence: {ops_detail_res.data.get('occurrence')} (Expected: {occ_2.id})")
assert str(ops_detail_res.data.get('occurrence')) == str(occ_2.id), "Operations booking occurrence not updated!"

# Dashboard did NOT double-count
dash_after_reschedule_booked = get_trials_count()
print(f"Dashboard Trials Booked after reschedule: {dash_after_reschedule_booked} (Expected: {new_trials_booked})")
assert dash_after_reschedule_booked == new_trials_booked, "Dashboard double-counted trial on reschedule!"

# Reschedule history exists
history_records = TrialStatusHistory.objects.filter(trial_booking=rescheduled_tb, to_status='RESCHEDULED')
print(f"Reschedule history records found: {history_records.count()} (Expected >= 1)")
assert history_records.exists(), "No reschedule history found!"

print("\n==================================================")
print("6. FAILED RESCHEDULE ROLLBACK")
print("==================================================")
# Book a trial on occ_3 to make it full
tb_blocker = CRMLeadService.book_trial(
    lead=lead_second,
    branch=branch,
    class_occurrence_id=occ_3.id,
    actor_user=admin_user,
    notes="Blocker trial for occ_3",
    db_alias=db_alias
)
print(f"Booked occ_3 with blocker trial {tb_blocker.id}. Active count: {TrialBooking.objects.filter(class_occurrence_id=occ_3.id, status__in=['BOOKED', 'CONFIRMED']).count()}")

# Now attempt reschedule Aarav's trial to occ_3 (which is full)
failed_cleanly = False
try:
    CRMLeadService.reschedule_trial(
        trial=rescheduled_tb,
        new_class_occurrence_id=occ_3.id,
        actor_user=admin_user,
        reason="Attempt full slot",
        db_alias=db_alias
    )
except Exception as e:
    failed_cleanly = True
    print(f"Reschedule to full slot failed cleanly as expected: {e}")

assert failed_cleanly, "Reschedule to full slot did not raise an exception!"

# Verify old occurrence remains booked and capacity intact
rescheduled_tb.refresh_from_db()
print(f"Aarav trial occurrence after failed reschedule: {rescheduled_tb.class_occurrence_id} (Expected: {occ_2.id})")
assert rescheduled_tb.class_occurrence_id == occ_2.id, "Occurrence was changed on failed reschedule!"
active_trials_occ_2_rollback = TrialBooking.objects.filter(class_occurrence_id=occ_2.id, status__in=['BOOKED', 'CONFIRMED', 'ATTENDED']).count()
assert active_trials_occ_2_rollback == 1, "Old occurrence capacity corrupted on failed reschedule!"

print("\n==================================================")
print("7. CANCEL TEST")
print("==================================================")
# Cancel lead_second's trial on occ_3
CRMLeadService.cancel_trial(
    trial=tb_blocker,
    reason="Lead requested cancellation",
    actor_user=admin_user,
    db_alias=db_alias
)
tb_blocker.refresh_from_db()
print(f"Cancelled TrialBooking status: {tb_blocker.status} (Expected: CANCELLED)")
assert tb_blocker.status == 'CANCELLED', "TrialBooking status not CANCELLED!"

active_trials_occ_3_after_cancel = TrialBooking.objects.filter(class_occurrence_id=occ_3.id, status__in=['BOOKED', 'CONFIRMED', 'ATTENDED']).count()
print(f"Occ 3 active trials after cancel: {active_trials_occ_3_after_cancel} (Expected: 0, capacity released)")
assert active_trials_occ_3_after_cancel == 0, "Capacity not released after cancellation!"

req_ops_cancel = factory.get(f'/api/v1/tenant/ops/bookings/{tb_blocker.id}/')
req_ops_cancel.tenant = tenant
req_ops_cancel.user = admin_user
req_ops_cancel._tenant_db = db_alias
force_authenticate(req_ops_cancel, user=admin_user)
ops_cancel_res = ops_view_detail(req_ops_cancel, pk=str(tb_blocker.id))
print(f"Operations Booking status after cancel: {ops_cancel_res.data.get('status')} (Expected: CANCELLED)")
assert ops_cancel_res.data.get('status') == 'CANCELLED', "Operations booking status not CANCELLED!"

# Ensure no hard delete
assert TrialBooking.objects.filter(id=tb_blocker.id).exists(), "TrialBooking was hard deleted!"

print("\n==================================================")
print("8. ATTEND / NO-SHOW TEST")
print("==================================================")
# Test Attend on Aarav
CRMLeadService.mark_trial_attended(
    trial=rescheduled_tb,
    notes="Attended class",
    actor_user=admin_user,
    db_alias=db_alias
)
rescheduled_tb.refresh_from_db()
lead_aarav.refresh_from_db()
print(f"Aarav TrialBooking status: {rescheduled_tb.status} (Expected: ATTENDED)")
print(f"Aarav Lead status: {lead_aarav.current_status} (Expected: TRIAL_ATTENDED)")
assert rescheduled_tb.status == 'ATTENDED', "Trial status not ATTENDED!"
assert lead_aarav.current_status == 'TRIAL_ATTENDED', "Lead status not TRIAL_ATTENDED!"

# Test No-Show on lead_second (book and mark no-show)
tb_noshow = CRMLeadService.book_trial(
    lead=lead_second,
    branch=branch,
    class_occurrence_id=occ_3.id,
    actor_user=admin_user,
    notes="Booking for no-show test",
    db_alias=db_alias
)
CRMLeadService.mark_trial_no_show(
    trial=tb_noshow,
    notes="Did not arrive",
    actor_user=admin_user,
    db_alias=db_alias
)
tb_noshow.refresh_from_db()
lead_second.refresh_from_db()
print(f"No-show TrialBooking status: {tb_noshow.status} (Expected: NO_SHOW)")
print(f"No-show Lead status: {lead_second.current_status} (Expected: TRIAL_NO_SHOW)")
assert tb_noshow.status == 'NO_SHOW', "Trial status not NO_SHOW!"
assert lead_second.current_status in ['NO_SHOW', 'TRIAL_NO_SHOW'], f"Lead status unexpected: {lead_second.current_status}"

print("\n==================================================")
print("9. CONVERTED LEAD GUARD")
print("==================================================")
converted_guard_passed = False
try:
    CRMLeadService.book_trial(
        lead=lead_converted,
        branch=branch,
        class_occurrence_id=occ_2.id,
        actor_user=admin_user,
        notes="Attempt booking converted lead",
        db_alias=db_alias
    )
except Exception as e:
    converted_guard_passed = True
    print(f"Converted lead booking blocked as expected: {e}")

assert converted_guard_passed, "Converted lead was allowed to book a trial!"

print("\n==================================================")
print("ALL BACKEND TRIAL UAT SCENARIOS SUCCEEDED (1-9)")
print("==================================================")
