#!/usr/bin/env python
"""
Standalone Seed Script for CMS Module, RBAC Permissions, and Initial Assets.
Can be executed directly on the server without needing migrations to be imported manually.

Usage:
    # Run against tenant_sweat_demo (default on this deployment):
    python scripts/seed_cms_server.py

    # Or specify database directly:
    python scripts/seed_cms_server.py --database=tenant_sweat_demo

    # Or specify tenant slug:
    python scripts/seed_cms_server.py --tenant=sweat_demo
"""

import os
import sys
import uuid
import argparse

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from django.conf import settings
from config.routers import set_tenant_db_alias, build_tenant_db_alias
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from config.tenant_middleware import _register_tenant_connection

from apps.tenant_core.models_rbac import (
    ModuleCatalog, SubmoduleCatalog, Permission, Role,
    RolePermissionSet, RolePermissionSetItem, RoleModuleAccess, RoleSubmoduleAccess
)
from apps.tenant_core.models_org import Organization
from apps.tenant_core.models_classes import ClassCategory
from apps.tenant_core.models_cms import CMSBanner, ProgramCategoryMedia, CMSMediaAsset


def run_seed(db_alias='tenant_sweat_demo', skip_sample=False):
    print("=" * 60)
    print(f"  SEEDING CMS RBAC & ASSETS ON DATABASE: {db_alias}")
    print("=" * 60)

    # 1. Ensure dynamic DB alias is configured in django.conf.settings
    if db_alias not in settings.DATABASES:
        settings.DATABASES[db_alias] = dict(settings.DATABASES['default'])
        settings.DATABASES[db_alias]['NAME'] = db_alias
    
    # 2. Activate tenant routing
    set_tenant_db_alias(db_alias)

    # 3. Create or enable ModuleCatalog
    mod, created = ModuleCatalog.objects.using(db_alias).get_or_create(
        module_code='cms',
        defaults={
            'code': 'cms',
            'name': 'Content Management System',
            'source_module_id': uuid.uuid4(),
            'is_enabled': True,
            'status': 'ACTIVE',
        }
    )
    if not created and not mod.is_enabled:
        mod.is_enabled = True
        mod.save(using=db_alias)
    print(f"[1/5] Module Catalog: '{mod.name}' (code={mod.module_code}, is_enabled={mod.is_enabled})")

    # 4. Create Submodules
    submodules_def = [
        ('banners', 'Home & Section Banners', 'Manage home hero sliders, promo banners, and deep links'),
        ('categories', 'Category Media & Content', 'Manage visual icons, banners, and descriptions for Pilates & Bootcamp'),
        ('media', 'Media Library', 'Upload and manage images, audio tracks, and video clips for the app'),
    ]
    submods = {}
    for sm_code, sm_name, sm_desc in submodules_def:
        sm, sm_created = SubmoduleCatalog.objects.using(db_alias).get_or_create(
            module=mod,
            submodule_code=sm_code,
            defaults={
                'code': sm_code,
                'name': sm_name,
                'description': sm_desc,
                'source_submodule_id': uuid.uuid4(),
                'is_enabled': True,
                'status': 'ACTIVE',
            }
        )
        if not sm_created and not sm.is_enabled:
            sm.is_enabled = True
            sm.save(using=db_alias)
        submods[sm_code] = sm
        status_tag = "Created" if sm_created else "Active"
        print(f"      - Submodule: {sm.name} [{status_tag}]")

    # 5. Create Permissions
    perms_def = [
        ('cms.banners.view', 'banners', 'view', 'View CMS Banners'),
        ('cms.banners.manage', 'banners', 'manage', 'Manage CMS Banners'),
        ('cms.categories.view', 'categories', 'view', 'View Category Media'),
        ('cms.categories.manage', 'categories', 'manage', 'Manage Category Media'),
        ('cms.media.view', 'media', 'view', 'View Media Library'),
        ('cms.media.upload', 'media', 'upload', 'Upload Media Assets'),
        ('cms.media.delete', 'media', 'delete', 'Delete Media Assets'),
    ]
    created_perms = []
    print("[2/5] Registering CMS Permissions:")
    for p_code, sm_code, act, lbl in perms_def:
        p_obj, p_created = Permission.objects.using(db_alias).get_or_create(
            permission_code=p_code,
            defaults={
                'module': mod,
                'submodule': submods.get(sm_code),
                'code': p_code,
                'label': lbl,
                'action': act,
            }
        )
        created_perms.append(p_obj)
        print(f"      - {p_obj.permission_code} ({p_obj.label})")

    # 6. Grant to Admin Roles
    print("[3/5] Granting & Activating CMS Permissions for Admin Roles:")
    admin_roles = list(Role.objects.using(db_alias).filter(
        code__in=['ORG_ADMIN', 'ADMIN', 'SUPER_ADMIN', 'TENANT_ADMIN', 'TENANT_OWNER']
    ))
    if not admin_roles:
        admin_roles = list(Role.objects.using(db_alias).filter(code__icontains='ADMIN'))

    for role in admin_roles:
        perm_set = RolePermissionSet.objects.using(db_alias).filter(role=role, is_active=True).first()
        if not perm_set:
            perm_set = RolePermissionSet.objects.using(db_alias).create(
                organization=role.organization,
                role=role,
                name=f"{role.name} Permission Set",
                is_active=True
            )

        # Grant items
        for p in created_perms:
            RolePermissionSetItem.objects.using(db_alias).get_or_create(
                permission_set=perm_set,
                permission=p,
                defaults={'is_allowed': True, 'granted': True}
            )

        # Open Module gate
        RoleModuleAccess.objects.using(db_alias).update_or_create(
            role=role,
            module=mod,
            defaults={
                'permission_set': perm_set,
                'can_access': True,
                'is_visible': True,
            }
        )

        # Open Submodule gates
        for sm in submods.values():
            RoleSubmoduleAccess.objects.using(db_alias).update_or_create(
                role=role,
                submodule=sm,
                defaults={
                    'permission_set': perm_set,
                    'can_access': True,
                    'is_visible': True,
                }
            )
        print(f"      [OK] Activated for role: {role.name} ({role.code})")

    if not skip_sample:
        # 7. Seed Program Categories Media (Pilates & Bootcamp)
        print("[4/5] Provisioning Program Categories Media (Sweat Pilates & Sweat Bootcamp):")
        org = Organization.objects.using(db_alias).filter(status='ACTIVE').first()

        categories = [
            {
                'name': 'Sweat Pilates',
                'code': 'SWEAT_PILATES',
                'tagline': 'Dynamic Reformer & Mat Pilates for Core Strength and Flexibility',
                'desc': 'Sweat Pilates combines classical and contemporary reformer techniques designed to tone, sculpt, and build deep core stability with low-impact, high-intensity precision.',
                'icon_url': 'https://images.unsplash.com/photo-1518611012118-696072aa579a?w=128&auto=format&fit=crop&q=80',
                'banner_url': 'https://images.unsplash.com/photo-1518611012118-696072aa579a?w=1200&auto=format&fit=crop&q=80',
                'video_url': 'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4',
                'order': 1,
            },
            {
                'name': 'Sweat Bootcamp',
                'code': 'SWEAT_BOOTCAMP',
                'tagline': 'High-Intensity Functional Strength and Athletic Conditioning',
                'desc': 'Sweat Bootcamp delivers athletic interval conditioning, metabolic conditioning, and functional strength designed to build endurance, burn fat, and push your limits.',
                'icon_url': 'https://images.unsplash.com/photo-1534438327276-14e5300c3a48?w=128&auto=format&fit=crop&q=80',
                'banner_url': 'https://images.unsplash.com/photo-1534438327276-14e5300c3a48?w=1200&auto=format&fit=crop&q=80',
                'video_url': 'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerEscapes.mp4',
                'order': 2,
            },
        ]

        for cat_data in categories:
            class_cat = ClassCategory.objects.using(db_alias).filter(name__iexact=cat_data['name']).first()
            media_obj, c_created = ProgramCategoryMedia.objects.using(db_alias).get_or_create(
                organization=org,
                category_name=cat_data['name'],
                defaults={
                    'class_category': class_cat,
                    'category_code': cat_data['code'],
                    'tagline': cat_data['tagline'],
                    'content_description': cat_data['desc'],
                    'icon_url': cat_data['icon_url'],
                    'banner_url': cat_data['banner_url'],
                    'video_url': cat_data['video_url'],
                    'display_order': cat_data['order'],
                    'is_active': True,
                }
            )
            print(f"      - Category Media: {media_obj.category_name} (Created: {c_created})")

        # 8. Starter Banners
        print("[5/5] Provisioning Starter Banners:")
        banners = [
            {
                'title': 'Transform Your Fitness at SWEAT',
                'subtitle': 'Discover World-Class Reformer Pilates & High-Energy Bootcamp',
                'placement': 'HOME_HERO',
                'media_type': 'IMAGE',
                'media_url': 'https://images.unsplash.com/photo-1518611012118-696072aa579a?w=1600&auto=format&fit=crop&q=80',
                'link_type': 'CATEGORY',
                'link_target': 'SWEAT_PILATES',
                'order': 1,
            },
            {
                'title': 'Sweat Bootcamp: Push Your Limits',
                'subtitle': 'Dynamic group conditioning sessions now live in Goregaon West',
                'placement': 'HOME_HERO',
                'media_type': 'IMAGE',
                'media_url': 'https://images.unsplash.com/photo-1534438327276-14e5300c3a48?w=1600&auto=format&fit=crop&q=80',
                'link_type': 'CATEGORY',
                'link_target': 'SWEAT_BOOTCAMP',
                'order': 2,
            },
            {
                'title': 'New Season Memberships Open',
                'subtitle': 'Unlock unlimited sessions and premium coaching access',
                'placement': 'OFFERS_SLIDER',
                'media_type': 'IMAGE',
                'media_url': 'https://images.unsplash.com/photo-1574680096145-d05b474e2155?w=1200&auto=format&fit=crop&q=80',
                'link_type': 'PACKAGE',
                'link_target': 'ALL',
                'order': 1,
            }
        ]
        for b in banners:
            banner_obj, b_created = CMSBanner.objects.using(db_alias).get_or_create(
                organization=org,
                title=b['title'],
                placement=b['placement'],
                defaults={
                    'subtitle': b['subtitle'],
                    'media_type': b['media_type'],
                    'media_url': b['media_url'],
                    'link_type': b['link_type'],
                    'link_target': b['link_target'],
                    'display_order': b['order'],
                    'is_active': True,
                }
            )
            print(f"      - Banner: {banner_obj.title} (Created: {b_created})")

    print("\n" + "=" * 60)
    print("  CMS PERMISSIONS & SEED COMPLETED SUCCESSFULLY!")
    print("=" * 60 + "\n")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Seed CMS permissions and initial assets on server.")
    parser.add_argument('--database', type=str, default='tenant_sweat_demo', help='Target DB alias')
    parser.add_argument('--tenant', type=str, default=None, help='Tenant slug')
    parser.add_argument('--skip-sample', action='store_true', help='Skip sample banners and category media')

    args = parser.parse_args()

    target_db = args.database
    if args.tenant:
        target_db = f"tenant_{args.tenant.replace('-', '_')}"

    run_seed(target_db, skip_sample=args.skip_sample)
