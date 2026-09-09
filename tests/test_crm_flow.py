"""
Comprehensive test suite for Phase 1 CRM & Leads API.
Tests CRUD, Kanban pipeline aggregation, stage transitions, activities, and multi-tenant isolation.
"""

import os
import json
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.test import Client
from apps.tenants.models import Tenant, Location
from apps.users.models import User, Role
from apps.crm.models import Lead, LeadActivity, LeadStage

def run_tests():
    print("=" * 60)
    print("Starting Phase 1 CRM Backend Verification Tests")
    print("=" * 60)

    client = Client()

    # 1. Login as Admin User (Tenant TEN-001)
    login_res = client.post(
        '/api/v1/auth/login/',
        data=json.dumps({'email': 'admin@yourgym.com', 'password': '123'}),
        content_type='application/json'
    )

    assert login_res.status_code == 200, "Login failed"
    access_token = login_res.json()['access']
    auth_header = {'HTTP_AUTHORIZATION': f'Bearer {access_token}'}
    print("\n[STEP 1] Authenticated as Admin (Tenant: TEN-001) [OK]")

    # 2. List Leads (GET /api/v1/crm/leads/)
    print("\n[TEST 2] Testing GET /api/v1/crm/leads/ ...")
    res_list = client.get('/api/v1/crm/leads/', **auth_header)
    assert res_list.status_code == 200
    leads = res_list.json()
    print(f"  [OK] Retrieved {len(leads)} leads")
    rahul = next((l for l in leads if l['id'] == 'LED-001'), None)
    assert rahul is not None, "Rahul Sharma (LED-001) missing!"
    print(f"  [OK] Found canonical lead: {rahul['name']} (Stage: {rahul['stage']})")

    # 3. Create a New Lead (POST /api/v1/crm/leads/)
    print("\n[TEST 3] Testing POST /api/v1/crm/leads/ (Create Lead) ...")
    import uuid
    test_lead_id = f"LED-TEST-{uuid.uuid4().hex[:6].upper()}"
    new_lead_payload = {
        'id': test_lead_id,
        'location': 'LOC-001',
        'name': 'Deepak Verma',
        'phone': '+91 98111 22334',
        'email': 'deepak.v@example.com',
        'source': 'Instagram',
        'interested_service': 'Personal Training',
        'goal': 'Marathon Training',
        'budget': 35000.00,
        'notes': 'Interested in endurance coaching.',
    }

    res_create = client.post(
        '/api/v1/crm/leads/',
        data=json.dumps(new_lead_payload),
        content_type='application/json',
        **auth_header
    )
    assert res_create.status_code == 201, f"Create lead failed: {res_create.content}"
    created_lead = res_create.json()
    assert created_lead['tenant_id'] == 'TEN-001', "Lead not assigned to authenticated tenant!"
    print(f"  [OK] Created Lead {created_lead['id']} auto-bound to Tenant {created_lead['tenant_id']}")

    # 4. Stage Transition & Automatic Activity Logging (PATCH /api/v1/crm/leads/LED-001/)
    print("\n[TEST 4] Testing Lead Stage Transition (PATCH /api/v1/crm/leads/LED-001/) ...")
    res_patch = client.patch(
        '/api/v1/crm/leads/LED-001/',
        data=json.dumps({'stage': 'Trial Attended'}),
        content_type='application/json',
        **auth_header
    )
    assert res_patch.status_code == 200
    assert res_patch.json()['stage'] == 'Trial Attended'
    print("  [OK] Rahul Sharma stage moved to 'Trial Attended'")

    # 5. Log Activity (POST /api/v1/crm/leads/LED-001/activities/)
    print("\n[TEST 5] Testing Activity Logging (POST /api/v1/crm/leads/LED-001/activities/) ...")
    res_activity = client.post(
        '/api/v1/crm/leads/LED-001/activities/',
        data=json.dumps({
            'activity_type': 'Trial',
            'summary': 'Trial attended on Saturday 10 AM. Coach feedback: Recommended 12-Week Strength Program with WELCOME10 coupon.',
        }),
        content_type='application/json',
        **auth_header
    )
    assert res_activity.status_code == 201
    print("  [OK] Activity logged successfully")

    # 6. Retrieve Lead Details with Activities (GET /api/v1/crm/leads/LED-001/)
    print("\n[TEST 6] Testing Lead Detail with Activities Timeline (GET /api/v1/crm/leads/LED-001/) ...")
    res_detail = client.get('/api/v1/crm/leads/LED-001/', **auth_header)
    assert res_detail.status_code == 200
    detail = res_detail.json()
    assert len(detail['activities']) >= 2, "Activities missing in detail view"
    print(f"  [OK] Lead has {len(detail['activities'])} activity history records")

    # 7. Test Kanban Pipeline Aggregation (GET /api/v1/crm/leads/pipeline/)
    print("\n[TEST 7] Testing Kanban Pipeline Summary (GET /api/v1/crm/leads/pipeline/) ...")
    res_pipeline = client.get('/api/v1/crm/leads/pipeline/', **auth_header)
    assert res_pipeline.status_code == 200
    pipeline_data = res_pipeline.json()
    assert 'Trial Attended' in pipeline_data
    print(f"  [OK] Pipeline stages aggregated: {list(pipeline_data.keys())}")

    # 8. Test Multi-Tenant Data Isolation
    print("\n[TEST 8] Testing Multi-Tenant Data Isolation (Tenant A vs Tenant B) ...")
    # Create Tenant B and User B
    tenant_b, _ = Tenant.objects.get_or_create(id='TEN-002', defaults={'name': 'FitZone Gym', 'slug': 'fitzone'})
    user_b = User.objects.filter(email='admin@fitzone.com').first()
    if not user_b:
        user_b = User.objects.create_user(
            password='123',
            first_name='Admin',
            last_name='Fitzone',
            tenant=tenant_b,
            role='Admin',
            is_staff=True,
        )

    # Login as User B
    login_b = client.post(
        '/api/v1/auth/login/',
        data=json.dumps({'email': 'admin@fitzone.com', 'password': '123'}),
        content_type='application/json'
    )
    assert login_b.status_code == 200
    token_b = login_b.json()['access']

    # User B lists leads
    res_b_leads = client.get('/api/v1/crm/leads/', HTTP_AUTHORIZATION=f'Bearer {token_b}')
    assert res_b_leads.status_code == 200
    b_leads = res_b_leads.json()
    assert len(b_leads) == 0, f"Tenant B leaked Tenant A leads! Found: {len(b_leads)}"
    print("  [OK] Tenant B cannot see any of Tenant A's leads (100% Isolated)")

    print("\n" + "=" * 60)
    print("ALL 8 PHASE 1 CRM BACKEND TESTS PASSED SUCCESSFULLY! (100% OK)")
    print("=" * 60)

if __name__ == '__main__':
    run_tests()
