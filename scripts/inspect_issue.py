import os, sys
backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from apps.master.models_tenant import Tenant
from apps.tenant_core.models_catalog import Program, Package, ProgramCategory
from apps.tenant_core.models_discounts import DiscountCampaign, DiscountCode
from apps.tenant_core.models_crm import Member
from apps.tenant_core.models_memberships import Membership
from apps.tenant_core.services_discounts import (
    get_member_lifecycle_state,
    matches_member_type,
    matches_order_type,
    ORDER_TYPE_FAMILIES
)

tenant = Tenant.objects.filter(slug='tenant_sweat_demo').first() or Tenant.objects.first()
print(f"=== Tenant: {tenant.name} ({tenant.id}) ===")

print("\n--- ACTIVE CAMPAIGNS ---")
campaigns = DiscountCampaign.objects.filter(tenant=tenant, is_active=True)
for c in campaigns:
    codes = list(c.codes.filter(is_active=True).values_list('code', flat=True))
    print(f"ID: {c.id} | Name: {c.name} | Codes: {codes}")
    print(f"  Target Criteria: {c.target_criteria}")
    print(f"  Min Order: {c.min_order_value} | Max Disc: {c.max_discount_amount} | Value: {c.discount_value} ({c.discount_type})")

print("\n--- PROGRAMS & PACKAGES ---")
programs = Program.objects.filter(tenant=tenant)
for p in programs:
    print(f"\nProgram: {p.name} (ID: {p.id})")
    pkgs = Package.objects.filter(tenant=tenant, program=p)
    for pkg in pkgs:
        print(f"  Package: {pkg.name} | ID: {pkg.id} | Price: {pkg.price} | IsActive: {pkg.is_active}")

print("\n--- MEMBER: Devanshi Shah ---")
devanshi = Member.objects.filter(tenant=tenant, id='837e5f72-b7de-4b2e-ade8-15baba664a7d').first()
if not devanshi:
    devanshi = Member.objects.filter(tenant=tenant, first_name__icontains='Devanshi').first()

if devanshi:
    print(f"Devanshi Shah: ID {devanshi.id}, Name: {devanshi.first_name} {devanshi.last_name}")
    lifecycle = get_member_lifecycle_state(tenant, devanshi.id)
    print(f"Lifecycle state: {lifecycle}")
    mems = Membership.objects.filter(tenant=tenant, member=devanshi)
    for m in mems:
        print(f"  Membership: {m.id} | Status: {m.status} | Package: {getattr(m.package, 'name', 'None')} ({getattr(m.package, 'id', 'None')}) | Program: {getattr(m.package.program, 'name', 'None') if m.package else 'None'} | End Date: {m.end_date}")
else:
    print("Devanshi Shah not found in DB")

print("\n--- TEST FOUNDING50 CAMPAIGN TARGET CRITERIA ---")
f50 = DiscountCampaign.objects.filter(tenant=tenant, codes__code='FOUNDING50').first()
if f50:
    print(f"FOUNDING50 campaign: {f50.name}")
    print(f"Target criteria: {f50.target_criteria}")
    order_types = f50.target_criteria.get('order_types', [])
    print(f"Order types configured: {order_types}")
    print(f"Does it match NEW_MEMBERSHIP? {matches_order_type(order_types, 'NEW_MEMBERSHIP')}")
    print(f"Does it match CONVERT_LEAD? {matches_order_type(order_types, 'CONVERT_LEAD')}")
    print(f"Does it match LEAD_CONVERSION? {matches_order_type(order_types, 'LEAD_CONVERSION')}")
