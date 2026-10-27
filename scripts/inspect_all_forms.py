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
from apps.tenant_core.models_meta_leads import MetaPageConnection, MetaLeadMapping
from apps.tenant_core.models_crm import LeadSource
from apps.tenant_core.models_org import Branch
from apps.tenant_core.meta_crypto import decrypt_token

tenant = Tenant.objects.using('default').filter(slug='sweat').first()
with tenant_database_context(tenant.id) as db_alias:
    lead_source = LeadSource.objects.using(db_alias).filter(source_type='META', status='ACTIVE').first()
    andheri = Branch.objects.using(db_alias).filter(code='SWEAT_ANDHERI').first()
    goregaon = Branch.objects.using(db_alias).filter(code='SWEAT_GOREGAON').first()
    malad = Branch.objects.using(db_alias).filter(code='SWEAT_MALAD').first()

    branches = {
        'andheri': str(andheri.id),
        'goregaon': str(goregaon.id),
        'malad': str(malad.id)
    }

    print(f"Lead Source: {lead_source.name} ({lead_source.id})")
    print(f"Branches: Andheri={branches['andheri']}, Goregaon={branches['goregaon']}, Malad={branches['malad']}")

    for page in MetaPageConnection.objects.using(db_alias).filter(page_id__in=['190779514893889', '1236293356233999']):
        token = decrypt_token(page.encrypted_page_access_token)
        forms_res = requests.get(
            f"https://graph.facebook.com/v21.0/{page.page_id}/leadgen_forms",
            params={"access_token": token, "fields": "id,name,status,leads_count,questions", "limit": 50}
        ).json()
        
        for f in forms_res.get('data', []):
            if f.get('status') == 'ACTIVE':
                form_id = f.get('id')
                form_name = f.get('name')
                questions = f.get('questions', [])
                print(f"\nPage: {page.page_name} | Form ID: {form_id} | Name: '{form_name}'")
                for q in questions:
                    q_type = q.get('type')
                    q_key = q.get('key')
                    q_label = q.get('label')
                    q_opts = [opt.get('key') or opt.get('value') for opt in q.get('options', [])]
                    print(f"  • [{q_type}] key='{q_key}' | label='{q_label}' | options={q_opts}")
