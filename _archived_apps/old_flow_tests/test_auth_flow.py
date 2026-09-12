"""
Verification script for PerformanceOS Phase 0 Authentication & Architecture Contract.
Tests all endpoints directly using Django test client.
"""

import json
import base64
import os
import sys
import django

# Setup Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.test import Client

def decode_jwt_payload(token_str):
    """Decodes middle part of JWT without signature verification."""
    payload_b64 = token_str.split('.')[1]
    # Add padding if required
    payload_b64 += '=' * (-len(payload_b64) % 4)
    decoded = base64.urlsafe_b64decode(payload_b64.encode('utf-8'))
    return json.loads(decoded.decode('utf-8'))

def run_tests():
    print("=" * 60)
    print("Starting Phase 0 Backend Verification Tests")
    print("=" * 60)

    client = Client()

    # -------------------------------------------------------------
    # 1. Test Login (POST /api/v1/auth/login/)
    # -------------------------------------------------------------
    print("\n[TEST 1] Testing POST /api/v1/auth/login/ ...")
    login_data = {
        'email': 'admin@yourgym.com',
        'password': '123'

    }
    response = client.post(
        '/api/v1/auth/login/',
        data=json.dumps(login_data),
        content_type='application/json'
    )

    if response.status_code != 200:
        print("LOGIN ERROR RESPONSE:", response.content.decode('utf-8', errors='ignore'))
        sys.exit(1)

    res_json = response.json()
    assert 'access' in res_json, "Access token missing in response"

    access_token = res_json['access']
    claims = decode_jwt_payload(access_token)

    print("  [OK] HTTP Status 200 OK")
    print(f"  [OK] Access Token received (length: {len(access_token)})")
    print(f"  [OK] JWT Payload Claims: {json.dumps(claims, indent=2)}")

    # Verify exact contract claims
    assert claims.get('sub') == 'USR-001', f"Expected sub='USR-001', got {claims.get('sub')}"
    assert claims.get('tid') == 'TEN-001', f"Expected tid='TEN-001', got {claims.get('tid')}"
    assert claims.get('role') == 'Super Admin', f"Expected role='Super Admin', got {claims.get('role')}"
    assert 'LOC-001' in claims.get('loc', []), f"Expected LOC-001 in loc list: {claims.get('loc')}"
    assert claims.get('act_loc') == 'LOC-001', f"Expected act_loc='LOC-001', got {claims.get('act_loc')}"

    # Verify HttpOnly refresh cookie
    assert 'refresh' in response.cookies, "HttpOnly refresh cookie was not set!"
    refresh_cookie = response.cookies['refresh']
    assert refresh_cookie['httponly'] is True, "Refresh cookie is not HttpOnly!"
    refresh_token = refresh_cookie.value
    print(f"  [OK] HttpOnly refresh cookie received (path: {refresh_cookie['path']}, httponly: {refresh_cookie['httponly']})")

    # -------------------------------------------------------------
    # 2. Test Me Profile (GET /api/v1/auth/me/)
    # -------------------------------------------------------------
    print("\n[TEST 2] Testing GET /api/v1/auth/me/ with Bearer Token ...")
    response_me = client.get(
        '/api/v1/auth/me/',
        HTTP_AUTHORIZATION=f'Bearer {access_token}'
    )
    assert response_me.status_code == 200, f"Me endpoint failed: {response_me.content}"
    me_json = response_me.json()
    print(f"  [OK] User profile: {json.dumps(me_json, indent=2)}")
    assert me_json['email'] == 'admin@yourgym.com'
    assert me_json['tenant_id'] == 'TEN-001'

    # -------------------------------------------------------------
    # 3. Test Token Refresh (POST /api/v1/auth/token/refresh/)
    # -------------------------------------------------------------
    print("\n[TEST 3] Testing POST /api/v1/auth/token/refresh/ via HttpOnly Cookie ...")
    # Client automatically maintains cookies from previous response
    response_refresh = client.post(
        '/api/v1/auth/token/refresh/',
        data=json.dumps({}),
        content_type='application/json'
    )
    assert response_refresh.status_code == 200, f"Refresh failed: {response_refresh.content}"
    refresh_json = response_refresh.json()
    assert 'access' in refresh_json, "New access token missing in refresh response"
    new_access_token = refresh_json['access']
    print(f"  [OK] New Access Token received via cookie refresh")

    # Verify rotated refresh cookie
    assert 'refresh' in response_refresh.cookies, "Rotated refresh cookie not found!"
    rotated_refresh_token = response_refresh.cookies['refresh'].value
    print(f"  [OK] Refresh Token Rotation (RTR) successful")

    # -------------------------------------------------------------
    # 4. Test Logout (POST /api/v1/auth/logout/)
    # -------------------------------------------------------------
    print("\n[TEST 4] Testing POST /api/v1/auth/logout/ ...")
    response_logout = client.post(
        '/api/v1/auth/logout/',
        data=json.dumps({}),
        content_type='application/json'
    )
    assert response_logout.status_code == 200, f"Logout failed: {response_logout.content}"
    print("  [OK] Logout successful")

    # Verify old refresh token is blacklisted
    print("  Testing that old refresh token is blacklisted...")
    client.cookies['refresh'] = refresh_token
    response_reuse = client.post(
        '/api/v1/auth/token/refresh/',
        data=json.dumps({}),
        content_type='application/json'
    )
    assert response_reuse.status_code == 401, f"Expected 401 for blacklisted token, got {response_reuse.status_code}"
    print("  [OK] Blacklisted token rejected with 401 Unauthorized")

    # -------------------------------------------------------------
    # 5. Test OpenAPI Schema (GET /api/schema/)
    # -------------------------------------------------------------
    print("\n[TEST 5] Testing OpenAPI 3.1 Schema Generation (GET /api/schema/) ...")
    response_schema = client.get('/api/schema/')
    assert response_schema.status_code == 200, f"OpenAPI schema failed: {response_schema.status_code}"
    print(f"  [OK] OpenAPI Schema generated successfully (Size: {len(response_schema.content)} bytes)")

    print("\n" + "=" * 60)
    print("ALL 5 PHASE 0 BACKEND TESTS PASSED SUCCESSFULLY! (100% OK)")
    print("=" * 60)

if __name__ == '__main__':
    run_tests()
