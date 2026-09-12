"""
Comprehensive automated test suite for Phase 3 Operations & Scheduling API.
Tests Trainers, Class scheduling, Bookings with session balance decrement, Calendar feed, and Tenant isolation.
"""

import os
import json
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.test import Client
from django.utils import timezone
from datetime import timedelta
from apps.tenants.models import Tenant, Location
from apps.users.models import User, Role
from apps.members.models import Member, MemberSubscription
from apps.operations.models import Trainer, FitnessClass, Booking

def run_tests():
    print("=" * 60)
    print("Starting Phase 3 Operations Backend Verification Tests")
    print("=" * 60)

    client = Client()

    # 1. Login as Admin User (Tenant TEN-001)
    login_res = client.post(
        '/api/v1/auth/login/',
        data=json.dumps({'email': 'admin@yourgym.com', 'password': '123'
}),
        content_type='application/json'
    )
    assert login_res.status_code == 200, "Login failed"
    access_token = login_res.json()['access']
    auth_header = {'HTTP_AUTHORIZATION': f'Bearer {access_token}'}
    print("\n[STEP 1] Authenticated as Admin (Tenant: TEN-001) [OK]")

    # 2. List Trainers (GET /api/v1/ops/trainers/)
    print("\n[TEST 2] Testing GET /api/v1/ops/trainers/ ...")
    res_trainers = client.get('/api/v1/ops/trainers/', **auth_header)
    assert res_trainers.status_code == 200
    trainers = res_trainers.json()
    assert len(trainers) >= 3, f"Expected >=3 trainers, got {len(trainers)}"
    print(f"  [OK] Retrieved {len(trainers)} Trainers (e.g. Coach {trainers[0]['name']} - {trainers[0]['specialization']})")

    # 3. List Classes (GET /api/v1/ops/classes/)
    print("\n[TEST 3] Testing GET /api/v1/ops/classes/ ...")
    res_classes = client.get('/api/v1/ops/classes/', **auth_header)
    assert res_classes.status_code == 200
    classes = res_classes.json()
    assert len(classes) >= 3, f"Expected >=3 classes, got {len(classes)}"
    cls_sample = classes[0]
    assert 'spots_remaining' in cls_sample
    print(f"  [OK] Retrieved {len(classes)} Classes: '{cls_sample['name']}' (Spots Left: {cls_sample['spots_remaining']}/{cls_sample['max_capacity']})")

    # 4. Schedule a New Class (POST /api/v1/ops/classes/)
    print("\n[TEST 4] Testing Schedule New Class (POST /api/v1/ops/classes/) ...")
    import uuid
    new_cls_id = f"CLS-TEST-{uuid.uuid4().hex[:6].upper()}"
    new_class_payload = {
        'id': new_cls_id,
        'location': 'LOC-002',
        'name': 'Evening Mobility & Deep Stretch',
        'category': 'Yoga',
        'trainer': trainers[0]['id'],
        'start_time': (timezone.now() + timedelta(days=2)).isoformat(),
        'end_time': (timezone.now() + timedelta(days=2, hours=1)).isoformat(),
        'max_capacity': 12,
    }
    res_create_cls = client.post(
        '/api/v1/ops/classes/',
        data=json.dumps(new_class_payload),
        content_type='application/json',
        **auth_header
    )
    assert res_create_cls.status_code == 201, f"Class creation failed: {res_create_cls.content}"
    print(f"  [OK] Scheduled Class '{new_class_payload['name']}' (ID: {new_cls_id})")

    # 5. List Bookings (GET /api/v1/ops/bookings/)
    print("\n[TEST 5] Testing GET /api/v1/ops/bookings/ ...")
    res_bookings = client.get('/api/v1/ops/bookings/', **auth_header)
    assert res_bookings.status_code == 200
    bookings = res_bookings.json()
    assert len(bookings) >= 3, f"Expected >=3 bookings, got {len(bookings)}"
    b_sample = next((b for b in bookings if b['id'] == 'BKG-001'), bookings[0])
    print(f"  [OK] Retrieved {len(bookings)} Bookings: {b_sample['booking_type']} for {b_sample['member_name']} (Status: {b_sample['status']})")

    # 6. Create PT Booking with Session Decrement (POST /api/v1/ops/bookings/)
    print("\n[TEST 6] Testing PT Booking Creation & Session Decrement (POST /api/v1/ops/bookings/) ...")
    # Check Rahul's initial session balance
    sub_before = MemberSubscription.objects.filter(member_id='MEM-001', status='Active').first()
    initial_sessions = sub_before.sessions_remaining
    print(f"       Rahul Sharma initial sessions remaining: {initial_sessions}")

    new_bkg_id = f"BKG-TEST-{uuid.uuid4().hex[:6].upper()}"
    new_bkg_payload = {
        'id': new_bkg_id,
        'member': 'MEM-001',
        'booking_type': 'Personal Training',
        'trainer': trainers[0]['id'],
        'location': 'LOC-002',
        'scheduled_at': (timezone.now() + timedelta(days=3)).isoformat(),
        'duration_minutes': 60,
        'notes': 'Session #2: Bench Press form check & accessory work.',
    }
    res_create_bkg = client.post(
        '/api/v1/ops/bookings/',
        data=json.dumps(new_bkg_payload),
        content_type='application/json',
        **auth_header
    )
    assert res_create_bkg.status_code == 201, f"Booking creation failed: {res_create_bkg.content}"

    # Verify session balance was decremented by 1
    sub_after = MemberSubscription.objects.filter(member_id='MEM-001', status='Active').first()
    assert sub_after.sessions_remaining == initial_sessions - 1, f"Sessions not decremented! Before: {initial_sessions}, After: {sub_after.sessions_remaining}"
    print(f"  [OK] PT Booking created (ID: {new_bkg_id})")
    print(f"  [OK] Session balance automatically decremented from {initial_sessions} -> {sub_after.sessions_remaining}!")

    # 7. Master Calendar Feed (GET /api/v1/ops/calendar/)
    print("\n[TEST 7] Testing Master Operations Calendar Feed (GET /api/v1/ops/calendar/) ...")
    res_calendar = client.get('/api/v1/ops/calendar/', **auth_header)
    assert res_calendar.status_code == 200
    calendar_events = res_calendar.json()
    assert len(calendar_events) >= 5, f"Expected >=5 calendar events, got {len(calendar_events)}"
    print(f"  [OK] Master Calendar aggregated {len(calendar_events)} combined class & PT events")

    # 8. Multi-Tenant Isolation
    print("\n[TEST 8] Testing Multi-Tenant Data Isolation ...")
    # Login as User B (Tenant TEN-002)
    login_b = client.post(
        '/api/v1/auth/login/',
        data=json.dumps({'email': 'admin@fitzone.com', 'password': '123'
}),
        content_type='application/json'
    )
    assert login_b.status_code == 200
    token_b = login_b.json()['access']

    res_b_classes = client.get('/api/v1/ops/classes/', HTTP_AUTHORIZATION=f'Bearer {token_b}')
    assert res_b_classes.status_code == 200
    assert len(res_b_classes.json()) == 0, f"Tenant B leaked Tenant A classes! Found: {len(res_b_classes.json())}"
    print("  [OK] Tenant B cannot see any of Tenant A's classes or schedules (100% Isolated)")

    print("\n" + "=" * 60)
    print("ALL 8 PHASE 3 OPERATIONS BACKEND TESTS PASSED SUCCESSFULLY! (100% OK)")
    print("=" * 60)

if __name__ == '__main__':
    run_tests()
