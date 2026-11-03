import os
import sys
import django

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from config.tenant_middleware import _register_tenant_connection
from apps.tenant_core.context import set_tenant_db_alias
from apps.tenant_core.models_classes import (
    ClassOccurrence,
    ClassOccurrenceTrainer,
    ClassContentAssignment,
)
from apps.tenant_core.models_bookings import Booking, AttendanceRecord, WaitlistEvent

tenant = Tenant.objects.filter(slug='sweat').first()
ds = TenantDataSource.objects.filter(tenant=tenant).first()
alias = f'tenant_{tenant.id.hex}'
db_name = getattr(ds, 'database_name', '') or getattr(ds, 'db_name', '') or 'tenant_sweat_uat'
_register_tenant_connection(alias, db_name=db_name, data_source=ds, tenant_id=tenant.id)
set_tenant_db_alias(alias)

nov_occs = ClassOccurrence.objects.using(alias).filter(
    occurrence_date__gte='2026-11-01', occurrence_date__lte='2026-11-30'
)
occ_ids = list(nov_occs.values_list('id', flat=True))
print(f'Targeting {len(occ_ids)} November 2026 class occurrences for cleanup.')

# Delete related protected records
ca_del, _ = ClassContentAssignment.objects.using(alias).filter(occurrence_id__in=occ_ids).delete()
print(f'Deleted {ca_del} content/video assignments.')

att_del, _ = AttendanceRecord.objects.using(alias).filter(occurrence_id__in=occ_ids).delete()
print(f'Deleted {att_del} attendance records.')

b_del, _ = Booking.objects.using(alias).filter(occurrence_id__in=occ_ids).delete()
print(f'Deleted {b_del} bookings.')

w_del, _ = WaitlistEvent.objects.using(alias).filter(occurrence_id__in=occ_ids).delete()
print(f'Deleted {w_del} waitlist events.')

cot_del, _ = ClassOccurrenceTrainer.objects.using(alias).filter(occurrence_id__in=occ_ids).delete()
print(f'Deleted {cot_del} trainer assignments.')

occ_del, _ = nov_occs.delete()
print(f'Deleted {occ_del} class occurrences.')

# Also check remaining
remaining = ClassOccurrence.objects.using(alias).filter(
    occurrence_date__gte='2026-11-01', occurrence_date__lte='2026-11-30'
).count()
print(f'Remaining November occurrences: {remaining}')
print('November slate successfully wiped clean.')
