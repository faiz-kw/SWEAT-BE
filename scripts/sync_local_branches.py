import os
import sys
from pathlib import Path
from datetime import date, datetime, time, timedelta
import zoneinfo

BASE_DIR = Path('F:/fitness-command-center/backend')
sys.path.insert(0, str(BASE_DIR))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()

from apps.tenant_core.models_org import Branch, Organization
from apps.tenant_core.models_classes import ClassCategory, ClassTemplate, ClassOccurrence
from apps.master.models_tenant import Tenant
from apps.tenant_core.context import tenant_database_context
from django.utils import timezone

def sync_local_branches():
    print("=== SYNCING CLASSES TO LOCAL BRANCHES ===")
    tenant = Tenant.objects.using('default').first()
    if not tenant:
        print("No local tenant found.")
        return

    with tenant_database_context(str(tenant.id)):
        org = Organization.objects.first()
        branches = list(Branch.objects.filter(status='ACTIVE'))
        print(f"Local Org: {org.name if org else 'None'}")
        print(f"Active Local Branches: {[b.name for b in branches]}")

        classes_spec = [
            {
                'name': 'Morning Yoga Flow',
                'code': 'DEMO-AND-YOGA',
                'start_time': time(9, 0),
                'end_time': time(10, 0),
            },
            {
                'name': 'Strength Basics',
                'code': 'DEMO-AND-STRENGTH',
                'start_time': time(10, 0),
                'end_time': time(11, 0),
            },
            {
                'name': 'Mobility and Core',
                'code': 'DEMO-AND-MOBILITY',
                'start_time': time(11, 0),
                'end_time': time(12, 0),
            },
            {
                'name': 'Functional Circuit',
                'code': 'DEMO-AND-FUNCTIONAL',
                'start_time': time(12, 0),
                'end_time': time(13, 0),
            },
            {
                'name': 'Evening HIIT',
                'code': 'DEMO-AND-HIIT',
                'start_time': time(18, 0),
                'end_time': time(19, 0),
            },
        ]

        # Ensure templates exist locally with allow_trial=True, allow_waitlist=True
        templates = []
        for spec in classes_spec:
            tmpl, created = ClassTemplate.objects.get_or_create(
                organization=org,
                code=spec['code'],
                defaults={
                    'name': spec['name'],
                    'default_duration_minutes': 60,
                    'default_capacity': 15,
                    'default_trial_capacity': 3,
                    'default_waitlist_capacity': 5,
                    'allow_booking': True,
                    'allow_trial': True,
                    'allow_waitlist': True,
                    'allow_reschedule': True,
                    'status': 'ACTIVE',
                }
            )
            if not created:
                tmpl.allow_trial = True
                tmpl.allow_waitlist = True
                tmpl.default_trial_capacity = 3
                tmpl.default_waitlist_capacity = 5
                tmpl.status = 'ACTIVE'
                tmpl.save()
            templates.append((tmpl, spec))
            print(f"Local Template ready: {tmpl.name} (allow_trial={tmpl.allow_trial}, allow_waitlist={tmpl.allow_waitlist})")

        # Now create occurrences for all local branches from 2026-10-19 to 2026-10-22
        dates = [date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22)]
        tz = zoneinfo.ZoneInfo('Asia/Kolkata')
        
        local_created = 0
        local_existed = 0

        for d in dates:
            for branch in branches:
                for tmpl, spec in templates:
                    start_dt = datetime.combine(d, spec['start_time']).replace(tzinfo=tz)
                    end_dt = datetime.combine(d, spec['end_time']).replace(tzinfo=tz)
                    
                    cutoff_hour = max(0, spec['start_time'].hour - 2)
                    cutoff_dt = datetime.combine(d, time(cutoff_hour, 0)).replace(tzinfo=tz)
                    booking_open_dt = datetime.combine(date(2026, 10, 1), time(0, 0)).replace(tzinfo=tz)
                    booking_close_dt = start_dt

                    occ, created = ClassOccurrence.objects.get_or_create(
                        branch=branch,
                        class_template=tmpl,
                        occurrence_date=d,
                        start_at=start_dt,
                        defaults={
                            'end_at': end_dt,
                            'capacity': 15,
                            'trial_capacity': 3,
                            'waitlist_capacity': 5,
                            'delivery_mode': 'OFFLINE',
                            'booking_open_at': booking_open_dt,
                            'booking_close_at': booking_close_dt,
                            'cancellation_cutoff_at': cutoff_dt,
                            'status': 'SCHEDULED',
                        }
                    )
                    if created:
                        local_created += 1
                    else:
                        occ.trial_capacity = 3
                        occ.waitlist_capacity = 5
                        occ.save()
                        local_existed += 1

        print(f"Local Occurrences: {local_created} created, {local_existed} updated with trial & waitlist.")

if __name__ == '__main__':
    sync_local_branches()
