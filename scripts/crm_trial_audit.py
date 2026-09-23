import os
import sys

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()

from apps.master.models_tenant import Tenant
from apps.tenant_core.context import tenant_database_context
from apps.tenant_core.models_crm import Lead, TrialBooking, LeadStatusHistory

def inspect_inconsistent(repair=False):
    tenants = Tenant.objects.using('default').filter(status='ACTIVE')
    for t in tenants:
        slug = getattr(t, 'slug', '')
        print(f"--- Tenant: {t.name} ({t.id}) [{slug}] ---")
        with tenant_database_context(t.id):
            leads = Lead.objects.all().filter(current_status='TRIAL_BOOKED')
            bad = []
            for l in leads:
                has_booking = TrialBooking.objects.filter(
                    lead=l,
                    status__in=['BOOKED', 'CONFIRMED', 'ATTENDED', 'COMPLETED']
                ).exists()
                if not has_booking:
                    hist = LeadStatusHistory.objects.filter(lead=l).order_by('-changed_at')
                    last_valid = 'NEW_LEAD'
                    for h in hist:
                        if h.to_status != 'TRIAL_BOOKED':
                            last_valid = h.to_status
                            break
                        elif h.from_status and h.from_status != 'TRIAL_BOOKED':
                            last_valid = h.from_status
                            break
                    bad.append((l, last_valid))
            
            print(f"Inconsistent trial-stage Leads found: {len(bad)}")
            for l, last_valid in bad:
                print(f"  Lead ID: {l.id} | Name: {l.first_name} {l.last_name} | Phone: {l.phone_normalized} | Current: {l.current_status} | Fallback: {last_valid}")
                if repair:
                    l.current_status = last_valid
                    l.save(update_fields=['current_status', 'updated_at'])
                    print(f"  --> Repaired to {last_valid}")

if __name__ == '__main__':
    repair_flag = '--repair' in sys.argv
    inspect_inconsistent(repair=repair_flag)
