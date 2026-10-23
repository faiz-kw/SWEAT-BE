"""
Register the Community Module in Master DB (ProductModule, ProductSubmodule, TenantPermissionCatalog)
and sync to tenant databases. Also enable it for SWEAT tenant.
"""
import os
import sys

# Ensure backend root is on sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.utils import timezone
from apps.master.models_saas import ProductModule, ProductSubmodule, TenantPermissionCatalog, Tenant, TenantModule
from apps.master.provisioning import sync_tenant_catalog_and_rbac

def register_community_module():
    print("--- Registering Community Module in Master DB ---")
    
    # 1. ProductModule
    mod, created = ProductModule.objects.using('default').update_or_create(
        code='community',
        defaults={
            'name': 'Community & Social Transformations',
            'description': 'Transformation feed, Before/After progress, coach feedback, suggestions Kanban, and content moderation.',
            'icon': 'Flame',
            'display_order': 16,
            'sort_order': 16,
            'is_core': False,
            'is_active': True,
            'status': 'ACTIVE',
        }
    )
    print(f"ProductModule 'community': {'created' if created else 'updated'}")

    # 2. ProductSubmodule
    submod, s_created = ProductSubmodule.objects.using('default').update_or_create(
        module=mod,
        code='community',
        defaults={
            'name': 'Community Hub',
            'description': 'Transformation feed, abuse moderation queue, and member suggestions.',
            'display_order': 1,
            'sort_order': 1,
            'is_active': True,
            'status': 'ACTIVE',
        }
    )
    print(f"ProductSubmodule 'community': {'created' if s_created else 'updated'}")

    # 3. Permissions in TenantPermissionCatalog
    perms = [
        ('community.view', 'Can view community hub and feeds', 'VIEW'),
        ('community.manage', 'Can moderate posts, approve transformations, and manage member requests', 'MANAGE'),
    ]
    for p_code, p_label, p_action in perms:
        p_obj, p_created = TenantPermissionCatalog.objects.using('default').update_or_create(
            code=p_code,
            defaults={
                'module': mod,
                'submodule': submod,
                'label': p_label,
                'action': p_action,
                'description': p_label,
                'is_active': True,
                'status': 'ACTIVE',
            }
        )
        print(f"Permission '{p_code}': {'created' if p_created else 'updated'}")

    # 4. Enable for existing SWEAT tenant
    sweat_tenant = Tenant.objects.using('default').filter(slug='sweat').first()
    if sweat_tenant:
        tm, tm_created = TenantModule.objects.using('default').update_or_create(
            tenant=sweat_tenant,
            module=mod,
            defaults={
                'is_enabled': True,
                'status': 'ENABLED',
                'availability_mode': 'ALL_BRANCHES',
                'configuration': {'enabled_submodules': ['/community']},
            }
        )
        print(f"TenantModule for SWEAT: {'created' if tm_created else 'updated'} (is_enabled=True)")

    # 5. Sync to tenant DB catalog
    try:
        from apps.authentication.views import _register_and_resolve_tenant
        if sweat_tenant:
            db_alias = _register_and_resolve_tenant(sweat_tenant)
            sync_res = sync_tenant_catalog_and_rbac(db_alias)
            print(f"Synced to tenant DB '{db_alias}': {sync_res}")
    except Exception as e:
        print(f"Tenant sync warning: {e}")

    print("--- Done ---")

if __name__ == '__main__':
    register_community_module()
