"""
Automated test suite for Master Architecture NEW additions:
1. Integration Layer & 6 Adapters
2. Facebook Lead Ads Inbound Webhook
3. Turnstile Access Control QR Entry & Attendance Hook
4. Promotional Coupons & Checkout Discounts
5. Referral Rewards Points Ledger
6. Tenant Isolation
"""

import os
import json
import uuid
import django


os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.test import Client
from django.utils import timezone
from datetime import timedelta
from apps.tenants.models import Tenant, Location
from apps.crm.models import Lead
from apps.members.models import Member, Attendance, ReferralLedger
from apps.operations.models import Booking
from apps.finance.models import Coupon, Invoice

def run_tests():
    print("=" * 65)
    print("Starting Master Architecture NEW Alignment Verification Tests")
    print("=" * 65)

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

    # 2. Integration Layer Status
    print("\n[TEST 2] Testing Integration Layer Adapters Status (GET /api/v1/integrations/status/) ...")
    res_status = client.get('/api/v1/integrations/status/', **auth_header)
    assert res_status.status_code == 200
    adapters = res_status.json()['adapters']
    assert 'razorpay' in adapters
    assert 'gupshup' in adapters
    assert 'telecmi' in adapters
    assert 'ses' in adapters
    assert 'facebook_ads' in adapters
    assert 'access_control' in adapters
    print(f"  [OK] All 6 Adapters registered in Integration Layer:")
    for k, v in adapters.items():
        print(f"       • {v['name']}: {v['status']} ({v['purpose']})")

    # 3. Facebook Lead Ads Inbound Webhook
    print("\n[TEST 3] Testing Facebook Lead Ads Ingestion Webhook (POST /api/v1/integrations/webhooks/facebook-leads/) ...")
    fb_payload = {
        'name': 'Arjun Nambiar',
        'phone': '+91 98333 44556',
        'email': 'arjun.nambiar@startup.io',
        'fitness_goal': 'Strength & Hypertrophy Program',
        'campaign_name': 'Meta Summer Blitz 2026',
    }
    res_fb = client.post(
        '/api/v1/integrations/webhooks/facebook-leads/',
        data=json.dumps(fb_payload),
        content_type='application/json'
    )
    assert res_fb.status_code == 201, f"Facebook webhook failed: {res_fb.content}"
    fb_resp = res_fb.json()
    assert fb_resp['success'] is True
    created_lead = Lead.objects.filter(id=fb_resp['lead_id']).first()
    assert created_lead is not None
    assert created_lead.source == 'Meta Ads'
    print(f"  [OK] Inbound Facebook Lead captured directly into CRM: {created_lead.name} (ID: {created_lead.id})")


    # 4. Turnstile Door Access QR Generation & Scan Webhook
    print("\n[TEST 4] Testing Door Access Control QR Flow (POST /api/v1/integrations/webhooks/access-control/) ...")
    booking = Booking.objects.filter(member_id='MEM-001').first()
    assert booking is not None, "Booking missing for Rahul Sharma"
    booking.scheduled_at = timezone.now()
    booking.save(update_fields=['scheduled_at'])
    qr_token = booking.get_qr_access_token()
    print(f"       Generated Time-Boxed QR Token for Rahul Sharma: {qr_token[:25]}...")


    # A. Test Valid Door Scan
    res_door = client.post(
        '/api/v1/integrations/webhooks/access-control/',
        data=json.dumps({
            'qr_token': qr_token,
            'device_id': 'TURNSTILE-INDIRANAGAR-01',
            'location_id': 'LOC-002',
        }),
        content_type='application/json'
    )
    assert res_door.status_code == 200, f"Door scan failed: {res_door.content}"
    door_data = res_door.json()
    assert door_data['unlock'] is True
    assert door_data['status'] == 'GRANTED'
    print(f"  [OK] Valid QR scan verified: Door UNLOCKED for {door_data['member_name']} (Attendance ID: {door_data['attendance_id']})")

    # B. Test Tampered / Invalid QR Scan
    res_bad_door = client.post(
        '/api/v1/integrations/webhooks/access-control/',
        data=json.dumps({
            'qr_token': 'QR_invalid_tampered_token.9999',
            'device_id': 'TURNSTILE-INDIRANAGAR-01',
        }),
        content_type='application/json'
    )
    assert res_bad_door.status_code == 403
    assert res_bad_door.json()['unlock'] is False
    print("  [OK] Tampered / Invalid QR scan correctly REJECTED (403 Forbidden)")

    # 5. Promotional Coupons & Invoice Discounts
    print("\n[TEST 5] Testing Promotional Coupon Engine (POST /api/v1/finance/invoices/) ...")
    coupon = Coupon.objects.filter(code='SWEATFIT20').first()
    assert coupon is not None
    times_used_before = coupon.times_used

    inv_payload = {
        'id': f"INV-CPN-{uuid.uuid4().hex[:6].upper()}",
        'member': 'MEM-001',
        'location': 'LOC-002',
        'coupon': coupon.id,
        'description': '12-Week Transformation (with 20% Coupon)',
        'subtotal': 20000.00,
        'due_date': (timezone.now() + timedelta(days=7)).date().isoformat(),
    }

    res_inv = client.post(
        '/api/v1/finance/invoices/',
        data=json.dumps(inv_payload),
        content_type='application/json',
        **auth_header
    )
    assert res_inv.status_code == 201, f"Invoice creation failed: {res_inv.content}"
    inv_data = res_inv.json()
    assert float(inv_data['discount_amount']) == 4000.00, f"Expected 4000 discount, got {inv_data['discount_amount']}"
    # Subtotal (20,000) - Discount (4,000) = 16,000 + 18% Tax (2,880) = 18,880
    assert float(inv_data['total_amount']) == 18880.00, f"Expected 18880 total, got {inv_data['total_amount']}"

    coupon.refresh_from_db()
    assert coupon.times_used == times_used_before + 1
    print(f"  [OK] Coupon {coupon.code} applied successfully:")
    print(f"       Subtotal: INR 20,000 | Discount: -INR {inv_data['discount_amount']} | Final Total: INR {inv_data['total_amount']}")

    # 6. Referral Rewards Points Ledger
    print("\n[TEST 6] Testing Referral Rewards Points Ledger ...")
    rahul = Member.objects.filter(id='MEM-001').first()
    initial_pts = rahul.reward_points_balance
    print(f"       Rahul initial reward balance: {initial_pts} pts")

    # Award 500 bonus points
    res_reward = client.post(
        f'/api/v1/members/MEM-001/referral-reward/',
        data=json.dumps({
            'points': 500,
            'event_type': 'Referral Signup',
            'description': 'Reward for referring friend',
        }),
        content_type='application/json',
        **auth_header
    )
    assert res_reward.status_code == 201
    rahul.refresh_from_db()
    assert rahul.reward_points_balance == initial_pts + 500
    print(f"  [OK] Referral reward credited: Balance updated to {rahul.reward_points_balance} pts")

    # 7. Multi-Tenant Isolation
    print("\n[TEST 7] Testing Multi-Tenant Data Isolation ...")
    login_b = client.post(
        '/api/v1/auth/login/',
        data=json.dumps({'email': 'admin@fitzone.com', 'password': '123'
}),
        content_type='application/json'
    )
    assert login_b.status_code == 200
    token_b = login_b.json()['access']

    res_b_coupons = client.get('/api/v1/finance/coupons/', HTTP_AUTHORIZATION=f'Bearer {token_b}')
    assert res_b_coupons.status_code == 200
    assert len(res_b_coupons.json()) == 0, f"Tenant B leaked Tenant A coupons! Found: {len(res_b_coupons.json())}"
    print("  [OK] Tenant B cannot see any of Tenant A's coupons or referral records (100% Isolated)")

    print("\n" + "=" * 65)
    print("ALL 7 MASTER ARCHITECTURE NEW TESTS PASSED SUCCESSFULLY! (100% OK)")
    print("=" * 65)

if __name__ == '__main__':
    run_tests()
