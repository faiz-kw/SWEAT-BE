import uuid
import datetime
from datetime import timedelta
import django
import os
import sys

# Setup django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
django.setup()

from django.utils import timezone
from django.conf import settings
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from config.tenant_middleware import _register_tenant_connection
from config.routers import build_tenant_db_alias, set_tenant_db_alias
from apps.tenant_core.models_org import Branch
from apps.tenant_core.models_crm import UserProfile
from apps.tenant_core.models_classes import ClassTemplate, ClassOccurrence
from apps.tenant_core.models_bookings import Booking
from apps.tenant_core.models_attendance import AccessPass
from apps.tenant_core.services_access_control import generate_booking_qr_token, hash_qr_token

tenant = Tenant.objects.get(slug='sweat')
ds = TenantDataSource.objects.get(tenant=tenant)
alias = build_tenant_db_alias(tenant.id)
_register_tenant_connection(alias, ds.db_name, data_source=ds, tenant_id=tenant.id)
set_tenant_db_alias(alias)

# 1. Branch: Andheri
andheri = Branch.objects.using(alias).get(code='SWEAT_ANDHERI')
print(f"Target Branch: {andheri.name} ({andheri.code}) - ID: {andheri.id}")

# 2. Member: Faiz Khan
profile = UserProfile.objects.using(alias).filter(user__email__icontains='faiz').first()
if not profile:
    profile = UserProfile.objects.using(alias).first()
user = profile.user
print(f"Member: {user.first_name} {user.last_name} ({user.email}) - Profile ID: {profile.id}")

# 3. Class Template
template = ClassTemplate.objects.using(alias).filter(name__icontains='Pilates').first()
if not template:
    template = ClassTemplate.objects.using(alias).filter(status='ACTIVE').first() or ClassTemplate.objects.using(alias).first()
print(f"Class Template: {template.name}")

# 4. Times for TODAY: Active window right now!
now = timezone.now()
start_at = now - timedelta(minutes=15)
end_at = now + timedelta(hours=2)
today_date = now.date()

occurrence = ClassOccurrence.objects.using(alias).create(
    class_template=template,
    branch=andheri,
    occurrence_date=today_date,
    start_at=start_at,
    end_at=end_at,
    delivery_mode='OFFLINE',
    capacity=20,
    status='OPEN',
    is_manual=True,
)
print(f"Created Occurrence: {occurrence.id} from {occurrence.start_at} to {occurrence.end_at}")

# 5. Create Confirmed Booking for Faiz
booking_number = f"BKG-{today_date.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
booking = Booking.objects.using(alias).create(
    booking_number=booking_number,
    user_profile=profile,
    occurrence=occurrence,
    branch=andheri,
    status='CONFIRMED',
    booked_at=now,
)
print(f"Created Booking: {booking.booking_number} (ID: {booking.id})")

# 6. Create Active AccessPass for this booking
compact_token = generate_booking_qr_token(booking.id, settings.SECRET_KEY)
t_hash = hash_qr_token(compact_token)

valid_from_dt = start_at - timedelta(minutes=60)
valid_until_dt = end_at + timedelta(minutes=60)

access_pass, created = AccessPass.objects.using(alias).update_or_create(
    booking=booking,
    defaults={
        'user_profile': profile,
        'branch': andheri,
        'token_hash': t_hash,
        'pass_type': 'CLASS_BOOKING',
        'valid_from': valid_from_dt,
        'valid_until': valid_until_dt,
        'status': 'ACTIVE',
        'updated_at': now,
    }
)

print(f"AccessPass: {access_pass.id} (Status: {access_pass.status})")
print(f"Token Hash: {access_pass.token_hash}")
print(f"Valid From: {valid_from_dt}")
print(f"Valid Until: {valid_until_dt}")
print(f"Compact QR Token: {compact_token}")
print("\nSUCCESS! Class, Booking, and AccessPass created successfully for Faiz at Andheri!")
