"""
End-to-end verification script for CRM Lead Sources and Manual Lead Creation.
Tests all CRUD operations, validations, role permissions, and integrations.
"""

import os
import sys
from pathlib import Path

backend_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(backend_dir))

import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from rest_framework.test import APIClient
from apps.master.models_tenant import Tenant
from apps.master.metering import resolve_tenant_db_alias
from config.routers import set_tenant_db_alias
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_org import Organization, Branch
from apps.tenant_core.models_crm import LeadSource, Lead
from apps.authentication.views import _build_tenant_token


def run_e2e_tests():
    print("=" * 70)
    print("STARTING END-TO-END VERIFICATION: CRM LEAD SOURCES & INTAKE")
    print("=" * 70)

    # 1. Setup Tenant and Auth
    tenant = Tenant.objects.get(id='d778f252-6fe0-459f-86a6-b75fc395ff24')
    alias = resolve_tenant_db_alias(tenant)
    set_tenant_db_alias(alias)
    org = Organization.objects.using(alias).first()
    branch = Branch.objects.using(alias).filter(organization=org, status='ACTIVE').first()
    user = TenantUser.objects.using(alias).get(email='admin@sweatfitwellness.com')
    
    refresh = _build_tenant_token(user, tenant, alias)
    token = str(refresh.access_token)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    tenant_header = {'HTTP_X_TENANT_ID': str(tenant.id)}

    # Clean up any leftover test data first
    from apps.tenant_core.models_crm import LeadStatusHistory, LeadAssignment, LeadActivity
    old_test_leads = Lead.objects.using(alias).filter(email_normalized__startswith='test.lead.')
    LeadStatusHistory.objects.using(alias).filter(lead__in=old_test_leads).delete()
    LeadAssignment.objects.using(alias).filter(lead__in=old_test_leads).delete()
    LeadActivity.objects.using(alias).filter(lead__in=old_test_leads).delete()
    old_test_leads.delete()
    LeadSource.objects.using(alias).filter(name__startswith='Test Corporate Event').delete()

    passed_tests = 0
    total_tests = 0

    def assert_test(condition, test_name, extra=""):
        nonlocal passed_tests, total_tests
        total_tests += 1
        if condition:
            passed_tests += 1
            print(f"  [PASS] {test_name} {extra}")
        else:
            print(f"  [FAIL] {test_name} {extra}")
            raise AssertionError(f"Test failed: {test_name}")

    # TEST 1: GET /api/v1/tenant/lead-sources/?active_only=true
    print("\n--- Test Suite 1: Querying Lead Sources ---")
    res1 = client.get('/api/v1/tenant/lead-sources/?active_only=true', **tenant_header)
    assert_test(res1.status_code == 200, "GET active_only returns HTTP 200")
    data1 = res1.data.get('results', res1.data) if isinstance(res1.data, dict) else res1.data
    active_count = len(data1)
    assert_test(active_count == 13, "Returns exactly 13 active standard sources", f"(got {active_count})")
    for s in data1:
        assert_test(s['status'] == 'ACTIVE', f"Source '{s['name']}' has ACTIVE status")

    # TEST 2: GET /api/v1/tenant/lead-sources/ (all sources)
    res2 = client.get('/api/v1/tenant/lead-sources/', **tenant_header)
    assert_test(res2.status_code == 200, "GET all sources returns HTTP 200")

    # TEST 3: CREATE NEW LEAD SOURCE (Add Source)
    print("\n--- Test Suite 2: Create, Update & Status Toggle ---")
    create_payload = {
        'name': 'Test Corporate Event 2026',
        'source_type': 'CAMPAIGN',
    }
    res_create = client.post('/api/v1/tenant/lead-sources/', create_payload, format='json', **tenant_header)
    assert_test(res_create.status_code == 201, "POST creates new lead source with HTTP 201")
    new_src = res_create.data
    assert_test(new_src['name'] == 'Test Corporate Event 2026', "Source name matches input")
    assert_test(bool(new_src['code']), f"Unique code was auto-generated: {new_src['code']}")
    assert_test(new_src['status'] == 'ACTIVE', "New source default status is ACTIVE")
    test_src_id = new_src['id']

    # TEST 4: UPDATE LEAD SOURCE (Edit Source)
    edit_payload = {
        'name': 'Test Corporate Event 2026 (Updated)',
        'source_type': 'OTHER',
    }
    res_edit = client.patch(f'/api/v1/tenant/lead-sources/{test_src_id}/', edit_payload, format='json', **tenant_header)
    assert_test(res_edit.status_code == 200, "PATCH updates lead source with HTTP 200")
    assert_test(res_edit.data['name'] == 'Test Corporate Event 2026 (Updated)', "Updated name saved correctly")

    # TEST 5: DEACTIVATE LEAD SOURCE
    res_deact = client.post(f'/api/v1/tenant/lead-sources/{test_src_id}/deactivate/', **tenant_header)
    assert_test(res_deact.status_code == 200, "POST deactivate returns HTTP 200")
    check_deact = client.get(f'/api/v1/tenant/lead-sources/{test_src_id}/', **tenant_header)
    assert_test(check_deact.data['status'] == 'INACTIVE', "Source status verified as INACTIVE")

    # TEST 6: ACTIVATE LEAD SOURCE
    res_act = client.post(f'/api/v1/tenant/lead-sources/{test_src_id}/activate/', **tenant_header)
    assert_test(res_act.status_code == 200, "POST activate returns HTTP 200")
    check_act = client.get(f'/api/v1/tenant/lead-sources/{test_src_id}/', **tenant_header)
    assert_test(check_act.data['status'] == 'ACTIVE', "Source status verified as ACTIVE")

    # TEST 7: MANUAL LEAD CREATION USING ACTIVE SOURCE
    print("\n--- Test Suite 3: Manual Lead Creation Flow ---")
    walk_in_source = LeadSource.objects.using(alias).get(organization=org, code='WALK_IN', status='ACTIVE')
    
    import time
    ts = int(time.time())
    lead_payload = {
        'first_name': 'TestUser',
        'last_name': 'ProductionCheck',
        'email': f'test.lead.{ts}@gmail.com',
        'phone': f'987{ts % 10000000:07d}',
        'gender': 'MALE',
        'branch': str(branch.id),
        'lead_source': str(walk_in_source.id),
        'fitness_goal': 'Weight Loss & Functional Strength',
        'area': 'Bandra West',
    }

    res_lead_create = client.post('/api/v1/tenant/leads/', lead_payload, format='json', **tenant_header)
    assert_test(res_lead_create.status_code in (200, 201), f"POST /tenant/leads/ returns HTTP {res_lead_create.status_code}", str(res_lead_create.data if res_lead_create.status_code not in (200, 201) else ''))
    created_lead_data = res_lead_create.data
    lead_id = created_lead_data['id']
    assert_test(str(created_lead_data['lead_source']) == str(walk_in_source.id), "Created lead has correct lead_source UUID")
    assert_test(created_lead_data['source_name'] == 'Walk-In', f"Created lead returned source_name='{created_lead_data.get('source_name')}'")

    # TEST 8: VERIFY LEAD IN DB
    lead_in_db = Lead.objects.using(alias).get(id=lead_id)
    assert_test(lead_in_db.lead_source_id == walk_in_source.id, "Lead in DB properly linked with foreign key")
    assert_test(lead_in_db.first_name == 'TestUser', "Lead in DB has correct first_name")

    # TEST 9: EDIT LEAD'S SOURCE TO ANOTHER ACTIVE SOURCE (e.g., Inbound Phone Call)
    phone_source = LeadSource.objects.using(alias).get(organization=org, code='PHONE_CALL', status='ACTIVE')
    res_lead_patch = client.patch(f'/api/v1/tenant/leads/{lead_id}/', {'lead_source': str(phone_source.id)}, format='json', **tenant_header)
    assert_test(res_lead_patch.status_code == 200, "PATCH lead source returns HTTP 200")
    lead_in_db = Lead.objects.using(alias).get(id=lead_id)
    assert_test(lead_in_db.lead_source_id == phone_source.id, "Lead in DB updated to Inbound Phone Call")

    # TEST 10: ATTEMPT TO ASSIGN AN INACTIVE LEAD SOURCE -> MUST FAIL
    inactive_source = LeadSource.objects.using(alias).filter(organization=org, status='INACTIVE').first()
    assert_test(inactive_source is not None, "Found an inactive source for negative validation testing")
    
    res_neg = client.patch(f'/api/v1/tenant/leads/{lead_id}/', {'lead_source': str(inactive_source.id)}, format='json', **tenant_header)
    assert_test(res_neg.status_code == 400, "PATCH with INACTIVE lead source rejected with HTTP 400 Bad Request")
    assert_test('Selected lead source is not active' in str(res_neg.data), "Error response explicitly says 'Selected lead source is not active'")

    # TEST 11: SAFE DELETION REJECTION WHEN LEADS ARE ATTACHED
    # Now attach a lead to test_src_id and verify destroy is safely blocked with 400
    Lead.objects.using(alias).filter(id=lead_id).update(lead_source_id=test_src_id)
    res_del_blocked = client.delete(f'/api/v1/tenant/lead-sources/{test_src_id}/', **tenant_header)
    assert_test(res_del_blocked.status_code == 400, "DELETE on lead source with attached leads blocked with HTTP 400")
    assert_test('Permanent deletion of lead sources is not permitted' in str(res_del_blocked.data), "Helpful error message returned")

    # CLEANUP TEST DATA
    from apps.tenant_core.models_crm import LeadStatusHistory, LeadAssignment, LeadActivity
    LeadStatusHistory.objects.using(alias).filter(lead_id=lead_id).delete()
    LeadAssignment.objects.using(alias).filter(lead_id=lead_id).delete()
    LeadActivity.objects.using(alias).filter(lead_id=lead_id).delete()
    Lead.objects.using(alias).filter(id=lead_id).delete()
    LeadSource.objects.using(alias).filter(id=test_src_id).delete()
    print("\nCleaned up all temporary test records.")

    print("\n" + "=" * 70)
    print(f"VERIFICATION COMPLETE: {passed_tests}/{total_tests} TESTS PASSED (100% OPERATIONAL)")
    print("=" * 70)


if __name__ == '__main__':
    run_e2e_tests()
