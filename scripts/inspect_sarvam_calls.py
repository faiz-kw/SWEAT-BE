import os
import sys
import django
import requests
from datetime import timedelta

if sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.conf import settings
from django.utils import timezone

api_key = getattr(settings, 'SARVAM_API_KEY', '') or os.getenv('SARVAM_API_KEY', '')
org_id = getattr(settings, 'SARVAM_ORG_ID', '') or os.getenv('SARVAM_ORG_ID', '')
workspace_id = getattr(settings, 'SARVAM_WORKSPACE_ID', '') or os.getenv('SARVAM_WORKSPACE_ID', '')
app_id = getattr(settings, 'SARVAM_APP_ID', '') or os.getenv('SARVAM_APP_ID', '')

now = timezone.now()
list_url = f"https://apps.sarvam.ai/api/analytics/v1/{org_id}/{workspace_id}/{app_id}/interactions"
params = {
    'start_datetime': (now - timedelta(days=7)).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'end_datetime': (now + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'include_transcript': 'true',
    'limit': 20
}

r = requests.get(list_url, headers={'X-API-Key': api_key}, params=params, timeout=25)
print("Sarvam Interactions Status:", r.status_code)
data = r.json()
items = data.get('items', [])
print(f"Total interactions returned: {len(items)}")

for it in items:
    iid = it.get('interaction_id')
    uphone = it.get('user_phone_number') or it.get('phone_number')
    dur = it.get('call_duration')
    reason = it.get('call_end_reason')
    start = it.get('start_datetime') or it.get('created_at')
    print(f"• ID: '{iid}' | Phone: {uphone} | Duration: {dur}s | Reason: {reason} | Start: {start}")

    # Check recording size for this interaction
    encoded_iid = str(iid).replace('/', '%2F')
    rec_url = f"https://apps.sarvam.ai/api/analytics/v1/{org_id}/{workspace_id}/{app_id}/recordings/{encoded_iid}"
    try:
        head_res = requests.head(rec_url, headers={'X-API-Key': api_key}, timeout=10)
        print(f"    Recording HEAD status: {head_res.status_code}, length: {head_res.headers.get('content-length')}")
    except Exception as e:
        print(f"    Recording check failed: {e}")
