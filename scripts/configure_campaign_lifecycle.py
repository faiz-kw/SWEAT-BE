import os
import sys

sys.path.insert(0, r'F:\fitness-command-center\backend')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from apps.tenant_core.models_discounts import DiscountCampaign
from apps.tenant_core.context import tenant_database_context

tenant_id = 'd778f252-6fe0-459f-86a6-b75fc395ff24'
with tenant_database_context(tenant_id) as alias:
    print('=== CONFIGURING CAMPAIGNS IN TENANT_SWEAT_DEMO ===')

    # 1. Gratitude 25% - Pilates & Bootcamp
    gratitude = DiscountCampaign.objects.using(alias).filter(codes__code='GRATITUDE25').first()
    if gratitude:
        cfg = gratitude.configuration if isinstance(gratitude.configuration, dict) else {}
        cfg['order_types'] = ['NEW_MEMBERSHIP', 'REJOIN']  # Exclude RENEWAL!
        cfg['member_type'] = 'NEW_OR_EXPIRED'
        cfg['allowed_member_types'] = ['NEW', 'EXPIRED', 'REJOIN', 'INACTIVE']
        gratitude.configuration = cfg
        gratitude.save(using=alias)
        print("Updated Gratitude 25%:")
        print("  order_types:", cfg['order_types'])
        print("  member_type:", cfg['member_type'])
        print("  allowed_member_types:", cfg['allowed_member_types'])

    # 2. Loyal 30% - Member Renewal
    loyal = DiscountCampaign.objects.using(alias).filter(codes__code='LOYAL30').first()
    if loyal:
        cfg = loyal.configuration if isinstance(loyal.configuration, dict) else {}
        cfg['order_types'] = ['RENEWAL']
        cfg['member_type'] = 'ACTIVE'
        cfg['allowed_member_types'] = ['ACTIVE']
        loyal.configuration = cfg
        loyal.save(using=alias)
        print("Updated Loyal 30%:")
        print("  order_types:", cfg['order_types'])
        print("  member_type:", cfg['member_type'])
        print("  allowed_member_types:", cfg['allowed_member_types'])

    # 3. Founding 50%
    founding50 = DiscountCampaign.objects.using(alias).filter(codes__code='FOUNDING50').first()
    if founding50:
        cfg = founding50.configuration if isinstance(founding50.configuration, dict) else {}
        cfg['order_types'] = ['NEW_MEMBERSHIP', 'NEW_PACKAGE']
        cfg['member_type'] = 'NEW'
        cfg['allowed_member_types'] = ['NEW']
        founding50.configuration = cfg
        founding50.save(using=alias)
        print("Updated Founding 50%:")

    # 4. Founding 40%
    founding40 = DiscountCampaign.objects.using(alias).filter(codes__code='FOUNDING40').first()
    if founding40:
        cfg = founding40.configuration if isinstance(founding40.configuration, dict) else {}
        cfg['order_types'] = ['NEW_MEMBERSHIP', 'NEW_PACKAGE']
        cfg['member_type'] = 'NEW'
        cfg['allowed_member_types'] = ['NEW']
        founding40.configuration = cfg
        founding40.save(using=alias)
        print("Updated Founding 40%:")

    print("\nCAMPAIGNS UPDATED SUCCESSFULLY!")
