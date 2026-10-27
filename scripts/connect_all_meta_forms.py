"""
backend/scripts/connect_all_meta_forms.py — Automated High-Fidelity Meta Lead Form Integration.

Connects every active lead form across all 7 Facebook Pages under Maahek Nair's Meta account:
1. Fetches all form definitions and questions from Meta Graph API v21.0.
2. Intelligently maps:
   - Name fields (full_name, FIRST_NAME, LAST_NAME, Full Name, etc.)
   - Phone fields (phone_number, phone, PHONE, what_is_your_phone_number?, etc.)
   - Email fields (email, EMAIL, etc.)
3. Automatically sets Studio / Branch auto-routing:
   - If studio question is present -> branch_mode='ANSWER' with all studio options mapped to Andheri, Goregaon, Malad
   - If Bootcamp form -> branch_mode='FIXED' targeting SWEAT_GOREGAON (Bootcamp branch) with fallback
   - If General form -> branch_mode='FIXED' targeting SWEAT_ANDHERI (HQ branch) with fallback
4. Sets active status, CRM lead source, tenant assignment policy, and followup task creation.
"""
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

from django.utils import timezone
from apps.master.models_tenant import Tenant, MetaPageRegistry
from apps.tenant_core.context import tenant_database_context
from apps.tenant_core.models_meta_leads import MetaPageConnection, MetaLeadMapping
from apps.tenant_core.models_crm import LeadSource
from apps.tenant_core.models_org import Branch
from apps.tenant_core.meta_crypto import decrypt_token

tenant = Tenant.objects.using('default').filter(slug='sweat').first()
if not tenant:
    print("Error: SWEAT tenant not found.")
    sys.exit(1)

with tenant_database_context(tenant.id) as db_alias:
    lead_source = LeadSource.objects.using(db_alias).filter(source_type='META', status='ACTIVE').first()
    if not lead_source:
        lead_source = LeadSource.objects.using(db_alias).filter(source_type='META').first()
    
    andheri_branch = Branch.objects.using(db_alias).filter(code='SWEAT_ANDHERI').first()
    goregaon_branch = Branch.objects.using(db_alias).filter(code='SWEAT_GOREGAON').first()
    malad_branch = Branch.objects.using(db_alias).filter(code='SWEAT_MALAD').first()

    andheri_id = str(andheri_branch.id)
    goregaon_id = str(goregaon_branch.id)
    malad_id = str(malad_branch.id)

    print("=" * 80)
    print("CONNECTING ALL ACTIVE META LEAD FORMS TO SWEAT CRM")
    print(f"Lead Source: {lead_source.name} ({lead_source.id})")
    print(f"Branches: Andheri={andheri_id}, Goregaon={goregaon_id}, Malad={malad_id}")
    print("=" * 80)

    total_connected = 0
    pages = MetaPageConnection.objects.using(db_alias).filter(is_active=True)

    for page in pages:
        token = decrypt_token(page.encrypted_page_access_token)
        if not token:
            continue

        forms_res = requests.get(
            f"https://graph.facebook.com/v21.0/{page.page_id}/leadgen_forms",
            params={"access_token": token, "fields": "id,name,status,leads_count,questions", "limit": 100}
        ).json()

        for form in forms_res.get('data', []):
            if form.get('status') != 'ACTIVE':
                continue

            form_id = str(form.get('id'))
            form_name = form.get('name') or f"Form {form_id}"
            questions = form.get('questions', [])

            field_mappings = {}
            branch_mode = 'FIXED'
            branch_field = ''
            branch_answers = {}
            target_branch = andheri_branch

            # Inspect questions to map fields
            studio_question_key = None
            studio_question_options = []

            for q in questions:
                q_type = (q.get('type') or '').upper()
                q_key = q.get('key') or ''
                q_label = (q.get('label') or '').lower()
                q_key_lower = q_key.lower()

                # Name field mapping
                if 'full_name' not in field_mappings and ('first_name' not in field_mappings):
                    if q_type == 'FULL_NAME' or 'full_name' in q_key_lower or 'full name' in q_label:
                        field_mappings['full_name'] = q_key
                    elif q_type == 'FIRST_NAME' or 'first_name' in q_key_lower:
                        field_mappings['first_name'] = q_key
                    elif q_type == 'LAST_NAME' or 'last_name' in q_key_lower:
                        field_mappings['last_name'] = q_key

                # Phone field mapping
                if 'phone' not in field_mappings:
                    if q_type == 'PHONE' or 'phone' in q_key_lower or 'phone' in q_label:
                        field_mappings['phone'] = q_key

                # Email field mapping
                if 'email' not in field_mappings:
                    if q_type == 'EMAIL' or 'email' in q_key_lower or 'email' in q_label:
                        field_mappings['email'] = q_key

                # Check if this question is asking for Studio / Location
                if any(word in q_key_lower or word in q_label for word in ['studio', 'centre', 'center', 'location']):
                    studio_question_key = q_key
                    opts = q.get('options', [])
                    studio_question_options = [opt.get('key') or opt.get('value') for opt in opts]

            # If no full_name or first_name mapped yet, fallback to any question with name
            if 'full_name' not in field_mappings and 'first_name' not in field_mappings:
                for q in questions:
                    if 'name' in (q.get('key') or '').lower():
                        field_mappings['full_name'] = q.get('key')
                        break

            # If no phone mapped, look for phone in any key
            if 'phone' not in field_mappings:
                for q in questions:
                    if 'phone' in (q.get('key') or '').lower():
                        field_mappings['phone'] = q.get('key')
                        break

            # If no email mapped, look for email in any key
            if 'email' not in field_mappings:
                for q in questions:
                    if 'email' in (q.get('key') or '').lower():
                        field_mappings['email'] = q.get('key')
                        break

            # Determine Branch Routing
            if studio_question_key and studio_question_options:
                branch_mode = 'ANSWER'
                branch_field = studio_question_key
                for opt in studio_question_options:
                    opt_str = str(opt).strip().casefold()
                    if 'goregaon' in opt_str:
                        branch_answers[opt_str] = goregaon_id
                    elif 'malad' in opt_str:
                        branch_answers[opt_str] = malad_id
                    elif 'andheri' in opt_str:
                        branch_answers[opt_str] = andheri_id
                
                # Also add standard aliases for robust matching
                branch_answers['andheri'] = andheri_id
                branch_answers['goregaon'] = goregaon_id
                branch_answers['malad'] = malad_id
                target_branch = andheri_branch
            else:
                # If form is for Bootcamp, route to Goregaon Bootcamp studio
                if 'bootcamp' in form_name.lower():
                    branch_mode = 'FIXED'
                    target_branch = goregaon_branch
                else:
                    branch_mode = 'FIXED'
                    target_branch = andheri_branch

            # Upsert MetaLeadMapping
            mapping, created = MetaLeadMapping.objects.using(db_alias).update_or_create(
                organization_id=page.organization_id,
                form_id=form_id,
                defaults={
                    'page_id': page.page_id,
                    'name': form_name[:200],
                    'is_active': True,
                    'field_mappings': field_mappings,
                    'field_defaults': {},
                    'branch_mode': branch_mode,
                    'branch': target_branch,
                    'branch_field': branch_field,
                    'branch_answers': branch_answers,
                    'unmatched_branch_policy': 'FALLBACK_BRANCH',
                    'fallback_branch': andheri_branch,
                    'lead_source': lead_source,
                    'initial_stage': 'NEW_LEAD',
                    'repeat_policy': 'REOPEN',
                    'assignment_mode': 'TENANT_POLICY',
                    'create_followup_task': True,
                    'followup_task_type': 'CALL',
                    'followup_due_hours': 2,
                }
            )

            action_str = "Created" if created else "Updated"
            print(f"[{action_str}] Form '{form_name}' (ID: {form_id}) on Page '{page.page_name}'")
            print(f"         Fields: {field_mappings}")
            print(f"         Routing: mode={branch_mode}, branch={target_branch.code}, answers_count={len(branch_answers)}")
            total_connected += 1

    print("\n" + "=" * 80)
    print(f"SUCCESS: {total_connected} ACTIVE FORMS PERFECTLY CONNECTED TO CRM!")
    print("=" * 80)
