"""
Data migration: Clear prematurely-set converted_user_profile on unconverted leads.

Bug: views_crm.py (commit 96b93b7) set lead.converted_user_profile = profile
whenever an existing TenantUser was matched by phone/email during lead intake,
even though no program purchase had occurred. This made those leads appear as
fully Converted Members in the UI.

This script nulls out converted_user_profile_id on every lead where:
  - current_status is NOT one of the terminal conversion statuses, AND
  - No LeadConversion record exists for the lead.

Safe to run multiple times (idempotent). Run once after deploying the code fix.

Usage (inside tenant shell or management command):
    python manage.py shell < data_fix_lead_converted_profile.py

Or via Django management command targeting a specific tenant alias:
    from django_tenants.utils import schema_context
    with schema_context('tenant_sweat_demo'):
        exec(open('...this file...').read())
"""

from django.db import transaction

CONVERSION_STATUSES = ('CONVERTED', 'DEAL_DONE', 'SUBSCRIBED')


def fix_leads_for_alias(alias: str) -> dict:
    """
    Clear spurious converted_user_profile links on the given tenant DB alias.
    Returns a dict with counts: {'scanned', 'cleared', 'skipped_has_conversion'}.
    """
    from apps.tenant_core.models_crm import Lead, LeadConversion

    # Leads that have converted_user_profile set but are NOT in a terminal state
    candidates = Lead.objects.using(alias).filter(
        converted_user_profile__isnull=False,
    ).exclude(
        current_status__in=CONVERSION_STATUSES,
    ).values_list('id', flat=True)

    candidate_ids = list(candidates)
    scanned = len(candidate_ids)

    if not candidate_ids:
        return {'scanned': 0, 'cleared': 0, 'skipped_has_conversion': 0}

    # Of those, exclude any that DO have a real LeadConversion (belt-and-suspenders)
    have_real_conversion = set(
        LeadConversion.objects.using(alias)
        .filter(lead_id__in=candidate_ids)
        .values_list('lead_id', flat=True)
    )

    to_clear = [lid for lid in candidate_ids if lid not in have_real_conversion]
    skipped = len(have_real_conversion)

    with transaction.atomic(using=alias):
        cleared = Lead.objects.using(alias).filter(
            id__in=to_clear,
        ).update(converted_user_profile=None)

    return {
        'scanned': scanned,
        'cleared': cleared,
        'skipped_has_conversion': skipped,
    }


if __name__ == '__main__' or True:
    # Determine which tenant aliases to fix
    try:
        from apps.tenants.models import Tenant
        tenants = Tenant.objects.using('default').filter(status='ACTIVE').values_list('slug', flat=True)
        aliases = [f'tenant_{slug}' for slug in tenants]
    except Exception as e:
        print(f"Could not auto-discover tenants: {e}")
        # Fallback: manually specify alias
        aliases = ['tenant_sweat_demo']

    print(f"Running data fix on {len(aliases)} tenant(s): {aliases}")
    total_cleared = 0

    for alias in aliases:
        try:
            result = fix_leads_for_alias(alias)
            print(
                f"  [{alias}] scanned={result['scanned']}  "
                f"cleared={result['cleared']}  "
                f"skipped_real_conversions={result['skipped_has_conversion']}"
            )
            total_cleared += result['cleared']
        except Exception as ex:
            print(f"  [{alias}] ERROR: {ex}")

    print(f"\nDone. Total leads cleared across all tenants: {total_cleared}")

