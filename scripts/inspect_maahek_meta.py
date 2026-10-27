import os
import sys
import django
import requests

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

from apps.master.models_tenant import Tenant
from apps.tenant_core.context import tenant_database_context
from apps.tenant_core.models_meta_leads import MetaConnection, MetaPageConnection
from apps.tenant_core.meta_crypto import decrypt_token

tenant = Tenant.objects.using('default').filter(slug='sweat').first()
with tenant_database_context(tenant.id) as db_alias:
    pages = MetaPageConnection.objects.using(db_alias).all()
    print(f"Total Pages in DB: {len(pages)}")

    for p in pages:
        token = decrypt_token(p.encrypted_page_access_token)
        print(f"\n=======================================================")
        print(f"PAGE: {p.page_name} (ID: {p.page_id})")
        print(f"=======================================================")

        # 1. Fetch Page Details & Linked Instagram
        res = requests.get(
            f"https://graph.facebook.com/v21.0/{p.page_id}",
            params={
                "access_token": token,
                "fields": "id,name,about,category,followers_count,link,instagram_business_account{id,username,name,profile_picture_url},business"
            }
        ).json()
        if "error" in res:
            print(f"  Error fetching page info: {res['error'].get('message')}")
        else:
            print(f"  • Category: {res.get('category')}")
            print(f"  • Followers: {res.get('followers_count')}")
            print(f"  • Link: {res.get('link')}")
            b = res.get('business')
            if b:
                print(f"  • Business Manager: {b.get('name')} (ID: {b.get('id')})")
            ig = res.get("instagram_business_account")
            if ig:
                print(f"  • Linked Instagram: @{ig.get('username')} (Name: '{ig.get('name')}', ID: {ig.get('id')})")
            else:
                print(f"  • Linked Instagram: None")

        # 2. Fetch Lead Gen Forms
        forms_res = requests.get(
            f"https://graph.facebook.com/v21.0/{p.page_id}/leadgen_forms",
            params={
                "access_token": token,
                "fields": "id,name,status,leads_count,created_time",
                "limit": 50
            }
        ).json()
        forms = forms_res.get("data", [])
        print(f"  • Lead Forms ({len(forms)}):")
        for f in forms:
            print(f"      - [{f.get('status')}] '{f.get('name')}' (ID: {f.get('id')}, Leads: {f.get('leads_count')}, Created: {f.get('created_time')})")

        # 3. Check /page_id/ad_campaigns or /page_id/promoted_posts or debug_token
        try:
            debug_res = requests.get(
                "https://graph.facebook.com/v21.0/debug_token",
                params={
                    "input_token": token,
                    "access_token": token
                }
            ).json()
            if "data" in debug_res:
                d = debug_res["data"]
                print(f"  • Token Details:")
                print(f"      - App ID: {d.get('app_id')}")
                print(f"      - User ID: {d.get('user_id')}")
                print(f"      - Expires At: {d.get('expires_at')}")
                print(f"      - Scopes: {', '.join(d.get('scopes', []))}")
                print(f"      - Granular Scopes: {d.get('granular_scopes')}")
        except Exception as e:
            print(f"  • Debug token error: {e}")
