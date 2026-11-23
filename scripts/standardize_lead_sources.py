"""
Script to standardize and clean up CRM Lead Sources for SWEAT Fitness & Wellness.
- Archives (deactivates) 900+ messy migration artifacts.
- Seeds / activates the clean, standard 12-source taxonomy.
- Updates MetaLeadMapping to point to the clean Meta Ads source.
- Safely remaps historical leads so CRM dashboard analytics group cleanly.
"""

import os
import sys
from pathlib import Path

# Add backend directory to path
backend_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(backend_dir))

import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.db import transaction, models
from apps.master.models_tenant import Tenant
from apps.master.metering import resolve_tenant_db_alias
from config.routers import set_tenant_db_alias
from apps.tenant_core.models_crm import LeadSource, Lead
from apps.tenant_core.models_org import Organization
from apps.tenant_core.models_meta_leads import MetaLeadMapping


CLEAN_SOURCES_DEF = [
    {
        'code': 'WALK_IN',
        'name': 'Walk-In',
        'source_type': 'WALK_IN',
    },
    {
        'code': 'PHONE_CALL',
        'name': 'Inbound Phone Call',
        'source_type': 'PHONE',
    },
    {
        'code': 'MEMBER_REFERRAL',
        'name': 'Member Referral',
        'source_type': 'REFERRAL',
    },
    {
        'code': 'META_ADS',
        'name': 'Meta Ads (Instagram / Facebook)',
        'source_type': 'META',
    },
    {
        'code': 'INSTAGRAM_ORGANIC',
        'name': 'Instagram Organic',
        'source_type': 'META',
    },
    {
        'code': 'GOOGLE_SEARCH',
        'name': 'Google Search / Maps',
        'source_type': 'GOOGLE',
    },
    {
        'code': 'WEBSITE_ENQUIRY',
        'name': 'Website Enquiry',
        'source_type': 'WEBSITE',
    },
    {
        'code': 'WHATSAPP_INBOUND',
        'name': 'WhatsApp Inbound',
        'source_type': 'WHATSAPP',
    },
    {
        'code': 'MOBILE_APP',
        'name': 'Mobile App',
        'source_type': 'MOBILE_APP',
    },
    {
        'code': 'CORPORATE_TIEUP',
        'name': 'Corporate Tie-Up / B2B',
        'source_type': 'OTHER',
    },
    {
        'code': 'STAFF_OUTREACH',
        'name': 'Trainer / Staff Outreach',
        'source_type': 'TRAINER',
    },
    {
        'code': 'EVENT_ACTIVATION',
        'name': 'Event / Fitness Activation',
        'source_type': 'CAMPAIGN',
    },
    {
        'code': 'OTHER_SOURCE',
        'name': 'Other / Miscellaneous',
        'source_type': 'OTHER',
    },
]


def run():
    print("=" * 60)
    print("STARTING CRM LEAD SOURCE STANDARDIZATION")
    print("=" * 60)

    # 1. Resolve tenant & database
    tenant = Tenant.objects.get(id='d778f252-6fe0-459f-86a6-b75fc395ff24')
    alias = resolve_tenant_db_alias(tenant)
    set_tenant_db_alias(alias)
    org = Organization.objects.using(alias).first()

    print(f"Tenant: {tenant.name} | DB Alias: {alias}")
    print(f"Organization: {org.name} ({org.id})")

    with transaction.atomic(using=alias):
        # 2. Deactivate all existing sources first
        total_before = LeadSource.objects.using(alias).filter(organization=org).count()
        deactivated = LeadSource.objects.using(alias).filter(organization=org).update(status='INACTIVE')
        print(f"Deactivated {deactivated} of {total_before} existing sources.")

        # 3. Seed / Activate clean sources
        active_sources_map = {}
        for src_def in CLEAN_SOURCES_DEF:
            code = src_def['code']
            obj, created = LeadSource.objects.using(alias).get_or_create(
                organization=org,
                code=code,
                defaults={
                    'name': src_def['name'],
                    'source_type': src_def['source_type'],
                    'status': 'ACTIVE',
                }
            )
            if not created:
                obj.name = src_def['name']
                obj.source_type = src_def['source_type']
                obj.status = 'ACTIVE'
                obj.save(using=alias, update_fields=['name', 'source_type', 'status', 'updated_at'])
            active_sources_map[code] = obj
            print(f"  [ACTIVE] {obj.name} (Code: {obj.code}, ID: {obj.id})")

        # 4. Update MetaLeadMapping to point to META_ADS source
        meta_source = active_sources_map['META_ADS']
        updated_mappings = MetaLeadMapping.objects.using(alias).update(lead_source=meta_source)
        print(f"Updated {updated_mappings} MetaLeadMapping record(s) to point to '{meta_source.name}'.")

        # 5. Smart Remap Leads
        print("\nSmart-remapping existing leads...")
        
        # Meta Ads: platform: ig, platform: fb, Facebook, FB, Insta, Instagram
        meta_old_sources = LeadSource.objects.using(alias).filter(organization=org).filter(
            models.Q(name__istartswith='platform:') |
            models.Q(name__icontains='facebook') |
            models.Q(name__icontains='insta') |
            models.Q(code__icontains='fb')
        ).exclude(id=meta_source.id)
        
        # Preserve campaign name into campaign_reference before remapping where campaign_reference is empty
        for old_s in meta_old_sources:
            if old_s.name.startswith('platform:'):
                Lead.objects.using(alias).filter(
                    lead_source=old_s,
                    campaign_reference__isnull=True
                ).update(campaign_reference=old_s.name[:200])
        
        remapped_meta = Lead.objects.using(alias).filter(lead_source__in=meta_old_sources).update(lead_source=meta_source)
        print(f"  Remapped {remapped_meta} leads -> {meta_source.name}")

        # Walk-in: Walk in, Walkin, WI
        walk_in_source = active_sources_map['WALK_IN']
        walk_in_old = LeadSource.objects.using(alias).filter(organization=org).filter(
            name__in=['Walk in', 'Walkin', 'WI', 'walk-in', 'walk in']
        ).exclude(id=walk_in_source.id)
        remapped_walkin = Lead.objects.using(alias).filter(lead_source__in=walk_in_old).update(lead_source=walk_in_source)
        print(f"  Remapped {remapped_walkin} leads -> {walk_in_source.name}")

        # Phone: Phone, Phone in
        phone_source = active_sources_map['PHONE_CALL']
        phone_old = LeadSource.objects.using(alias).filter(organization=org).filter(
            name__in=['Phone', 'Phone in', 'phone']
        ).exclude(id=phone_source.id)
        remapped_phone = Lead.objects.using(alias).filter(lead_source__in=phone_old).update(lead_source=phone_source)
        print(f"  Remapped {remapped_phone} leads -> {phone_source.name}")

        # WhatsApp: whatsapp, Wattsapp
        whatsapp_source = active_sources_map['WHATSAPP_INBOUND']
        wa_old = LeadSource.objects.using(alias).filter(organization=org).filter(
            name__in=['whatsapp', 'Wattsapp', 'WhatsApp', 'Whatsapp']
        ).exclude(id=whatsapp_source.id)
        remapped_wa = Lead.objects.using(alias).filter(lead_source__in=wa_old).update(lead_source=whatsapp_source)
        print(f"  Remapped {remapped_wa} leads -> {whatsapp_source.name}")

        # Referral: Referral, Referal, Word of Mouth
        referral_source = active_sources_map['MEMBER_REFERRAL']
        ref_old = LeadSource.objects.using(alias).filter(organization=org).filter(
            name__in=['Referral', 'Referal', 'Word of Mouth', 'referral']
        ).exclude(id=referral_source.id)
        remapped_ref = Lead.objects.using(alias).filter(lead_source__in=ref_old).update(lead_source=referral_source)
        print(f"  Remapped {remapped_ref} leads -> {referral_source.name}")

        # Website: Website, online, Payment page
        website_source = active_sources_map['WEBSITE_ENQUIRY']
        web_old = LeadSource.objects.using(alias).filter(organization=org).filter(
            name__in=['Website', 'online', 'Payment page', 'website']
        ).exclude(id=website_source.id)
        remapped_web = Lead.objects.using(alias).filter(lead_source__in=web_old).update(lead_source=website_source)
        print(f"  Remapped {remapped_web} leads -> {website_source.name}")

        # Google: Google
        google_source = active_sources_map['GOOGLE_SEARCH']
        goog_old = LeadSource.objects.using(alias).filter(organization=org).filter(
            name__in=['Google', 'google']
        ).exclude(id=google_source.id)
        remapped_goog = Lead.objects.using(alias).filter(lead_source__in=goog_old).update(lead_source=google_source)
        print(f"  Remapped {remapped_goog} leads -> {google_source.name}")

        # App: app
        app_source = active_sources_map['MOBILE_APP']
        app_old = LeadSource.objects.using(alias).filter(organization=org).filter(
            name__in=['app', 'App', 'Mobile App']
        ).exclude(id=app_source.id)
        remapped_app = Lead.objects.using(alias).filter(lead_source__in=app_old).update(lead_source=app_source)
        print(f"  Remapped {remapped_app} leads -> {app_source.name}")

        # Other/Junk: -, 0, admin panel, Incomplete transactions
        other_source = active_sources_map['OTHER_SOURCE']
        other_old = LeadSource.objects.using(alias).filter(organization=org).filter(
            name__in=['-', '0', 'admin panel', 'Other']
        ).exclude(id=other_source.id)
        remapped_other = Lead.objects.using(alias).filter(lead_source__in=other_old).update(lead_source=other_source)
        print(f"  Remapped {remapped_other} leads -> {other_source.name}")

        # Final Verification
        final_active = LeadSource.objects.using(alias).filter(organization=org, status='ACTIVE').count()
        final_inactive = LeadSource.objects.using(alias).filter(organization=org, status='INACTIVE').count()
        total_leads = Lead.objects.using(alias).filter(organization=org).count()
        unattributed_leads = Lead.objects.using(alias).filter(organization=org, lead_source__isnull=True).count()

        print("\n" + "=" * 60)
        print("VERIFICATION RESULTS:")
        print(f"  Active Lead Sources: {final_active} (Expected: {len(CLEAN_SOURCES_DEF)})")
        print(f"  Inactive Archived Sources: {final_inactive}")
        print(f"  Total Leads Preserved: {total_leads}")
        print(f"  Unattributed (null) Leads: {unattributed_leads}")
        print("=" * 60)


if __name__ == '__main__':
    run()
