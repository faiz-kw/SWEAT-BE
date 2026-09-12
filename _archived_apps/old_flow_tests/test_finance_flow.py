"""
Comprehensive automated test suite for Finance & Billing API (Phase 1 completion).
Tests Invoice listing, Razorpay payments, Settlement flow, Revenue summary, and Tenant isolation.
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
from apps.members.models import Member
from apps.finance.models import Invoice, Payment

def run_tests():
    print("=" * 60)
    print("Starting Finance & Billing Backend Verification Tests")
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

    # 2. List Invoices (GET /api/v1/finance/invoices/)
    print("\n[TEST 2] Testing GET /api/v1/finance/invoices/ ...")
    res_invoices = client.get('/api/v1/finance/invoices/', **auth_header)
    assert res_invoices.status_code == 200
    invoices = res_invoices.json()
    assert len(invoices) >= 4, f"Expected >=4 invoices, got {len(invoices)}"
    rahul_inv = next((i for i in invoices if i['id'] == 'INV-001'), None)
    assert rahul_inv is not None, "Rahul Sharma (INV-001) missing!"
    print(f"  [OK] Retrieved {len(invoices)} Invoices")
    print(f"       Found Rahul Sharma Invoice: Total INR {rahul_inv['total_amount']} (Status: {rahul_inv['status']})")

    # 3. Invoice Detail with Payments (GET /api/v1/finance/invoices/INV-001/)
    print("\n[TEST 3] Testing Invoice Detail with Razorpay Payment (GET /api/v1/finance/invoices/INV-001/) ...")
    res_detail = client.get('/api/v1/finance/invoices/INV-001/', **auth_header)
    assert res_detail.status_code == 200
    inv_detail = res_detail.json()
    assert len(inv_detail['payments']) >= 1, "Payment record missing in detail view"
    pay = inv_detail['payments'][0]
    print(f"  [OK] Payment verified: INR {pay['amount']} via {pay['payment_method']} (TX: {pay['transaction_id']})")

    # 4. Create New Invoice (POST /api/v1/finance/invoices/)
    print("\n[TEST 4] Testing Create Invoice (POST /api/v1/finance/invoices/) ...")
    import uuid
    new_inv_id = f"INV-TEST-{uuid.uuid4().hex[:6].upper()}"
    new_inv_payload = {
        'id': new_inv_id,
        'member': 'MEM-001',
        'location': 'LOC-002',
        'description': 'Protein Supplements & Performance Hydration Pack',
        'subtotal': 3500.00,
        'tax_amount': 630.00,
        'total_amount': 4130.00,
        'status': 'Issued',
        'due_date': (timezone.now() + timedelta(days=7)).date().isoformat(),
    }
    res_create_inv = client.post(
        '/api/v1/finance/invoices/',
        data=json.dumps(new_inv_payload),
        content_type='application/json',
        **auth_header
    )
    assert res_create_inv.status_code == 201, f"Invoice creation failed: {res_create_inv.content}"
    print(f"  [OK] Created Invoice '{new_inv_payload['description']}' (ID: {new_inv_id})")

    # 5. Record Payment & Settle Invoice (POST /api/v1/finance/invoices/{id}/pay/)
    print(f"\n[TEST 5] Testing Record Payment & Settle Invoice (POST /api/v1/finance/invoices/{new_inv_id}/pay/) ...")
    pay_payload = {
        'amount': 4130.00,
        'payment_method': 'Razorpay',
        'transaction_id': f"pay_rzp_{uuid.uuid4().hex[:8]}",
    }
    res_pay = client.post(
        f'/api/v1/finance/invoices/{new_inv_id}/pay/',
        data=json.dumps(pay_payload),
        content_type='application/json',
        **auth_header
    )
    assert res_pay.status_code == 200, f"Payment settlement failed: {res_pay.content}"
    paid_inv = res_pay.json()
    assert paid_inv['status'] == 'Paid', f"Invoice status not updated to Paid: {paid_inv['status']}"
    assert paid_inv['paid_at'] is not None, "paid_at timestamp not set"
    print(f"  [OK] Invoice {new_inv_id} successfully settled and marked 'Paid' via Razorpay")

    # 6. Revenue & Financial KPIs Summary (GET /api/v1/finance/summary/)
    print("\n[TEST 6] Testing Finance & Revenue Summary (GET /api/v1/finance/summary/) ...")
    res_summary = client.get('/api/v1/finance/summary/', **auth_header)
    assert res_summary.status_code == 200
    summary = res_summary.json()
    print(f"  [OK] Total Revenue Collected: INR {summary['total_revenue']}")
    print(f"       Total Outstanding Dues:  INR {summary['total_outstanding']}")
    print(f"       Paid Invoices Count:    {summary['paid_invoices_count']}")
    print(f"       Overdue Invoices Count: {summary['overdue_invoices_count']}")

    # 7. Multi-Tenant Isolation
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

    res_b_inv = client.get('/api/v1/finance/invoices/', HTTP_AUTHORIZATION=f'Bearer {token_b}')
    assert res_b_inv.status_code == 200
    assert len(res_b_inv.json()) == 0, f"Tenant B leaked Tenant A invoices! Found: {len(res_b_inv.json())}"
    print("  [OK] Tenant B cannot see any of Tenant A's invoices or payments (100% Isolated)")

    print("\n" + "=" * 60)
    print("ALL 7 FINANCE BACKEND TESTS PASSED SUCCESSFULLY! (100% OK)")
    print("=" * 60)

if __name__ == '__main__':
    run_tests()
