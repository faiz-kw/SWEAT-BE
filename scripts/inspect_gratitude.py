import os
import sys

sys.path.insert(0, r'F:\fitness-command-center\backend')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from apps.tenant_core.models_discounts import DiscountCampaign, DiscountCode
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_memberships import Membership
from apps.tenant_core.context import tenant_database_context

tenant_id = 'd778f252-6fe0-459f-86a6-b75fc395ff24'
with tenant_database_context(tenant_id) as alias:
    print('=== ALL CAMPAIGNS & THEIR CONFIGURATION ===')
    for c in DiscountCampaign.objects.using(alias).filter(status='ACTIVE'):
        codes = list(c.codes.values_list('code', flat=True))
        print(f"Name: {c.name}")
        print(f"Codes: {codes}")
        print(f"Config: {c.configuration}")
        print("-" * 50)

    print('\n=== MEMBER: Aarav Sharma ===')
    mems = Membership.objects.using(alias).filter(user_profile__id='fb1a6dea-417a-4321-a016-aa26629c0518')
    for m in mems:
        print(f"Membership {m.membership_number}: status={m.status}, start={m.start_date}, end={m.end_date}, package={m.package.name}")
