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
from apps.tenant_core.models_meta_leads import MetaPageConnection
from apps.tenant_core.meta_crypto import decrypt_token

tenant = Tenant.objects.using('default').filter(slug='sweat').first()
with tenant_database_context(tenant.id) as db_alias:
    # Use the token from Sweat pilates page or Maahek Nair page which has ads_management & business_management scopes
    page = MetaPageConnection.objects.using(db_alias).filter(page_id='1265728509951597').first()
    token = decrypt_token(page.encrypted_page_access_token)

    business_ids = ['4366286193638217', '421439014988143', '1840697492660043', '5486751598126009']
    
    ad_account_ids = set()

    print("=== QUERYING BUSINESS MANAGERS ===")
    for bid in business_ids:
        print(f"\nChecking Business ID: {bid}")
        # 1. Owned ad accounts
        res1 = requests.get(
            f"https://graph.facebook.com/v21.0/{bid}/owned_ad_accounts",
            params={"access_token": token, "fields": "id,account_id,name,account_status,currency,amount_spent,timezone_name"}
        ).json()
        for act in res1.get('data', []):
            print(f"  • Owned Ad Account: {act.get('name')} ({act.get('id')}) | Status: {act.get('account_status')} | Spent: {act.get('amount_spent')} {act.get('currency')}")
            ad_account_ids.add(act.get('id'))
        if 'error' in res1:
            print(f"    owned_ad_accounts notice: {res1['error'].get('message')}")

        # 2. Client ad accounts
        res2 = requests.get(
            f"https://graph.facebook.com/v21.0/{bid}/client_ad_accounts",
            params={"access_token": token, "fields": "id,account_id,name,account_status,currency,amount_spent,timezone_name"}
        ).json()
        for act in res2.get('data', []):
            print(f"  • Client Ad Account: {act.get('name')} ({act.get('id')}) | Status: {act.get('account_status')} | Spent: {act.get('amount_spent')} {act.get('currency')}")
            ad_account_ids.add(act.get('id'))
        if 'error' in res2:
            print(f"    client_ad_accounts notice: {res2['error'].get('message')}")

    # Also check /me/adaccounts using this token
    res_me = requests.get(
        "https://graph.facebook.com/v21.0/me/adaccounts",
        params={"access_token": token, "fields": "id,account_id,name,account_status,currency,amount_spent,timezone_name"}
    ).json()
    for act in res_me.get('data', []):
        print(f"  • Direct Ad Account: {act.get('name')} ({act.get('id')}) | Status: {act.get('account_status')} | Spent: {act.get('amount_spent')}")
        ad_account_ids.add(act.get('id'))

    print(f"\n=== TOTAL UNIQUE AD ACCOUNTS IDENTIFIED: {len(ad_account_ids)} ===")

    # For each ad account, get all active campaigns & ads
    for act_id in ad_account_ids:
        print(f"\n-------------------------------------------------------------")
        print(f"AD ACCOUNT: {act_id}")
        print(f"-------------------------------------------------------------")

        # Campaigns
        c_res = requests.get(
            f"https://graph.facebook.com/v21.0/{act_id}/campaigns",
            params={
                "access_token": token,
                "fields": "id,name,status,effective_status,objective,daily_budget,lifetime_budget,start_time,stop_time",
                "limit": 50
            }
        ).json()
        campaigns = c_res.get('data', [])
        print(f"  Campaigns ({len(campaigns)}):")
        for c in campaigns:
            print(f"    • [{c.get('effective_status')}] '{c.get('name')}' (ID: {c.get('id')}, Objective: {c.get('objective')})")

        # Ads
        ads_res = requests.get(
            f"https://graph.facebook.com/v21.0/{act_id}/ads",
            params={
                "access_token": token,
                "fields": "id,name,status,effective_status,campaign{id,name},adset{id,name},creative{id,name,title,body}",
                "limit": 50
            }
        ).json()
        ads = ads_res.get('data', [])
        print(f"  Ads ({len(ads)}):")
        for a in ads:
            cname = a.get('campaign', {}).get('name', 'N/A')
            sname = a.get('adset', {}).get('name', 'N/A')
            print(f"    - [{a.get('effective_status')}] '{a.get('name')}' | Campaign: '{cname}' | AdSet: '{sname}' (ID: {a.get('id')})")
