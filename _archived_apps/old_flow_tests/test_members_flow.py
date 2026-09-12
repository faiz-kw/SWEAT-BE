"""
Comprehensive automated test suite for Phase 2 Members & Client 360 API.
Tests Member listing, Client 360 detail, attendance check-in, lead-to-member conversion, and tenant isolation.
"""

import os
import json
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.test import Client
from apps.tenants.models import Tenant, Location
from apps.users.models import User, Role
from apps.crm.models import Lead, LeadStage, LeadStatus
from apps.members.models import Member, MemberSubscription, Attendance

def run_tests():
    print("=" * 60)
    print("Starting Phase 2 Members Backend Verification Tests")
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

    # 2. List Membership Plans (GET /api/v1/members/plans/)
    print("\n[TEST 2] Testing GET /api/v1/members/plans/ ...")
    res_plans = client.get('/api/v1/members/plans/', **auth_header)
    assert res_plans.status_code == 200
    plans = res_plans.json()
    assert len(plans) >= 4, f"Expected >=4 plans, got {len(plans)}"
    print(f"  [OK] Retrieved {len(plans)} Membership Plans (e.g. {plans[0]['name']})")

    # 3. List Members (GET /api/v1/members/)
    print("\n[TEST 3] Testing GET /api/v1/members/ ...")
    res_members = client.get('/api/v1/members/', **auth_header)
    assert res_members.status_code == 200
    members = res_members.json()
    assert len(members) >= 5, f"Expected >=5 members, got {len(members)}"
    rahul = next((m for m in members if m['id'] == 'MEM-001'), None)
    assert rahul is not None, "Rahul Sharma (MEM-001) missing!"
    print(f"  [OK] Found canonical member: {rahul['name']} (Active Plan: {rahul['active_plan']['plan_name']})")

    # Filter by status: Active
    res_active = client.get('/api/v1/members/?status=Active', **auth_header)
    assert res_active.status_code == 200
    for m in res_active.json():
        assert m['status'] == 'Active', f"Non-active member returned in filter: {m['status']}"
    print(f"  [OK] Filter ?status=Active returned {len(res_active.json())} active members")

    # 4. Client 360 View (GET /api/v1/members/MEM-001/)
    print("\n[TEST 4] Testing Client 360 Profile (GET /api/v1/members/MEM-001/) ...")
    res_360 = client.get('/api/v1/members/MEM-001/', **auth_header)
    assert res_360.status_code == 200
    p360 = res_360.json()
    assert 'subscriptions' in p360 and len(p360['subscriptions']) >= 1
    assert 'recent_attendance' in p360 and len(p360['recent_attendance']) >= 1
    print(f"  [OK] Client 360 View loaded: {p360['name']}")
    print(f"       Subscribed to: {p360['subscriptions'][0]['plan_name']} (Remaining Sessions: {p360['subscriptions'][0]['sessions_remaining']})")
    print(f"       30-Day Check-in Count: {p360['attendance_count_30d']}")

    # 5. Member Attendance Check-In (POST /api/v1/members/MEM-001/check-in/)
    print("\n[TEST 5] Testing Attendance Check-In (POST /api/v1/members/MEM-001/check-in/) ...")
    res_checkin = client.post(
        '/api/v1/members/MEM-001/check-in/',
        data=json.dumps({'method': 'QR Code'}),
        content_type='application/json',
        **auth_header
    )
    assert res_checkin.status_code == 201, f"Check-in failed: {res_checkin.content}"
    checkin_data = res_checkin.json()
    assert checkin_data['member'] == 'MEM-001'
    print(f"  [OK] Check-in logged via {checkin_data['method']} at {checkin_data['location_name']}")

    # 6. Create Member & Convert Lead (POST /api/v1/members/)
    print("\n[TEST 6] Testing Lead-to-Member Conversion (POST /api/v1/members/) ...")
    import uuid
    new_mem_id = f"MEM-TEST-{uuid.uuid4().hex[:6].upper()}"
    new_member_payload = {
        'id': new_mem_id,
        'location': 'LOC-002',
        'name': 'Kavya Singhania',
        'phone': '+91 97111 55667',
        'email': 'kavya.s@corp.in',
        'gender': 'F',
        'age': 27,
        'status': 'Active',
        'fitness_goal': 'Mobility & Strength',
        'from_lead_id': 'LED-002',  # Converts Priya Patel lead
    }
    res_create_mem = client.post(
        '/api/v1/members/',
        data=json.dumps(new_member_payload),
        content_type='application/json',
        **auth_header
    )
    assert res_create_mem.status_code == 201, f"Member creation failed: {res_create_mem.content}"

    # Verify that the linked Lead LED-002 is now Converted / Won
    priya_lead = Lead.objects.filter(id='LED-002').first()
    assert priya_lead.stage == LeadStage.CONVERTED, f"Lead stage not updated: {priya_lead.stage}"
    assert priya_lead.status == LeadStatus.WON, f"Lead status not updated: {priya_lead.status}"
    print(f"  [OK] Member {new_mem_id} created, Lead LED-002 auto-converted to Stage 'Converted' / Status 'Won'")

    # 7. Multi-Tenant Isolation (Tenant A vs Tenant B)
    print("\n[TEST 7] Testing Multi-Tenant Data Isolation ...")
    # Login as User B (Tenant TEN-002)
    login_b = client.post(
        '/api/v1/auth/login/',
        data=json.dumps({'email': 'admin@fitzone.com', 'password': '123'
}),
        content_type='application/json'
    )
    assert login_b.status_code == 200
    token_b = login_b.json()['access']

    res_b_members = client.get('/api/v1/members/', HTTP_AUTHORIZATION=f'Bearer {token_b}')
    assert res_b_members.status_code == 200
    assert len(res_b_members.json()) == 0, f"Tenant B leaked Tenant A members! Found: {len(res_b_members.json())}"
    print("  [OK] Tenant B cannot see any of Tenant A's members (100% Isolated)")

    print("\n" + "=" * 60)
    print("ALL 7 PHASE 2 MEMBERS BACKEND TESTS PASSED SUCCESSFULLY! (100% OK)")
    print("=" * 60)

if __name__ == '__main__':
    run_tests()
