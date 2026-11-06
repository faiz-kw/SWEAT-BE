"""
Django Management Command: seed_cms_rbac
Seeds CMS module, submodules, permissions, and activates them for admin roles.
Also seeds initial program category visual media (Sweat Pilates, Sweat Bootcamp).

Usage:
    python manage.py seed_cms_rbac
    python manage.py seed_cms_rbac --tenant=sweat-demo
    python manage.py seed_cms_rbac --database=tenant_sweat_demo
    python manage.py seed_cms_rbac --database=default
"""

import uuid
from django.core.management.base import BaseCommand
from django.conf import settings
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from config.tenant_middleware import _register_tenant_connection
from config.routers import build_tenant_db_alias, set_tenant_db_alias

from apps.tenant_core.models_rbac import (
    ModuleCatalog, SubmoduleCatalog, Permission, Role,
    RolePermissionSet, RolePermissionSetItem, RoleModuleAccess, RoleSubmoduleAccess
)
from apps.tenant_core.models_org import Organization
from apps.tenant_core.models_classes import ClassCategory
from apps.tenant_core.models_cms import CMSBanner, ProgramCategoryMedia, CMSMediaAsset


class Command(BaseCommand):
    help = "Seeds CMS module, submodules, permissions, and admin activation into tenant database(s)."

    def add_arguments(self, parser):
        parser.add_argument(
            '--tenant',
            type=str,
            default=None,
            help='Specific tenant slug to seed (e.g. sweat-demo, sweat_demo)',
        )
        parser.add_argument(
            '--database',
            type=str,
            default=None,
            help='Direct database alias/name to target (e.g. tenant_sweat_demo, default)',
        )
        parser.add_argument(
            '--skip-sample-data',
            action='store_true',
            help='Only seed RBAC permissions and skip sample banners and media',
        )

    def handle(self, *args, **options):
        tenant_slug = options.get('tenant')
        direct_db = options.get('database')
        skip_sample = options.get('skip_sample_data', False)

        targets = []

        if direct_db:
            # Direct database alias passed
            if direct_db not in settings.DATABASES:
                settings.DATABASES[direct_db] = dict(settings.DATABASES['default'])
                settings.DATABASES[direct_db]['NAME'] = direct_db
            targets.append((direct_db, f"Direct DB '{direct_db}'"))
        elif tenant_slug:
            qs = Tenant.objects.using('default').filter(slug__iexact=tenant_slug)
            tenant = qs.first()
            if not tenant:
                # Try finding by name or fallback to direct db name
                direct_name = f"tenant_{tenant_slug.replace('-', '_')}"
                if direct_name not in settings.DATABASES:
                    settings.DATABASES[direct_name] = dict(settings.DATABASES['default'])
                    settings.DATABASES[direct_name]['NAME'] = direct_name
                targets.append((direct_name, f"Inferred DB '{direct_name}'"))
            else:
                ds = TenantDataSource.objects.using('default').filter(tenant=tenant, status='ACTIVE').first()
                db_name = ds.database_name or ds.db_name if ds else f"tenant_{tenant.slug.replace('-', '_')}"
                db_alias = build_tenant_db_alias(tenant.id)
                _register_tenant_connection(db_alias, db_name, data_source=ds, tenant_id=tenant.id)
                targets.append((db_alias, f"Tenant '{tenant.name}' ({tenant.slug})"))
        else:
            # Discover from master DB or fallback to default & known tenant
            tenants = list(Tenant.objects.using('default').filter(status='ACTIVE'))
            if tenants:
                for tenant in tenants:
                    ds = TenantDataSource.objects.using('default').filter(tenant=tenant, status='ACTIVE').first()
                    db_name = ds.database_name or ds.db_name if ds else f"tenant_{tenant.slug.replace('-', '_')}"
                    db_alias = build_tenant_db_alias(tenant.id)
                    try:
                        _register_tenant_connection(db_alias, db_name, data_source=ds, tenant_id=tenant.id)
                        targets.append((db_alias, f"Tenant '{tenant.name}' ({tenant.slug})"))
                    except Exception as e:
                        self.stdout.write(self.style.WARNING(f"Could not register db for tenant {tenant.slug}: {e}"))
            
            # If no tenants configured in master DB or 'tenant_sweat_demo' exists in settings
            if not targets:
                demo_db = 'tenant_sweat_demo'
                if demo_db not in settings.DATABASES:
                    settings.DATABASES[demo_db] = dict(settings.DATABASES['default'])
                    settings.DATABASES[demo_db]['NAME'] = demo_db
                targets.append((demo_db, f"Default Tenant DB '{demo_db}'"))

        for db_alias, label in targets:
            self.stdout.write(self.style.SUCCESS(f"\n=========================================="))
            self.stdout.write(self.style.SUCCESS(f"Seeding CMS Permissions & Content for: {label} (alias: {db_alias})"))
            self.stdout.write(self.style.SUCCESS(f"=========================================="))
            set_tenant_db_alias(db_alias)
            self._seed_rbac(db_alias)
            if not skip_sample:
                self._seed_cms_content(db_alias)

        self.stdout.write(self.style.SUCCESS("\n[SUCCESS] CMS Permissions and Content setup complete!\n"))

    def _seed_rbac(self, db_alias):
        self.stdout.write("1. Provisioning ModuleCatalog ('cms')...")
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
        self.stdout.write(f"   Module: {mod.name} (code={mod.module_code})")

        self.stdout.write("2. Provisioning SubmoduleCatalog (banners, categories, media)...")
        submodules_def = [
            ('banners', 'Home & Section Banners', 'Manage home hero sliders, promo banners, and deep links'),
            ('categories', 'Category Media & Content', 'Manage visual icons, banners, and descriptions for Pilates & Bootcamp'),
            ('media', 'Media Library', 'Upload and manage images, audio tracks, and video clips for the app'),
        ]
        submods = {}
        for sm_code, sm_name, sm_desc in submodules_def:
            sm, _ = SubmoduleCatalog.objects.using(db_alias).get_or_create(
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
            submods[sm_code] = sm
            self.stdout.write(f"   Submodule: {sm.name} (code={sm.submodule_code})")

        self.stdout.write("3. Provisioning Permissions...")
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
        for p_code, sm_code, act, lbl in perms_def:
            p_obj, _ = Permission.objects.using(db_alias).get_or_create(
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
            self.stdout.write(f"   Permission: {p_obj.permission_code}")

        self.stdout.write("4. Activating permissions for Admin roles...")
        admin_roles = Role.objects.using(db_alias).filter(
            code__in=['ORG_ADMIN', 'ADMIN', 'SUPER_ADMIN', 'TENANT_ADMIN', 'TENANT_OWNER']
        )
        if not admin_roles.exists():
            # Match any role containing ADMIN or OWNER
            admin_roles = Role.objects.using(db_alias).filter(code__icontains='ADMIN')

        self.stdout.write(f"   Found {admin_roles.count()} admin role(s) to grant access.")

        for role in admin_roles:
            perm_set = RolePermissionSet.objects.using(db_alias).filter(role=role, is_active=True).first()
            if not perm_set:
                perm_set = RolePermissionSet.objects.using(db_alias).create(
                    organization=role.organization,
                    role=role,
                    name=f"{role.name} Permission Set",
                    is_active=True
                )

            # Grant individual permissions
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

            self.stdout.write(self.style.SUCCESS(f"   [OK] Role '{role.name}' ({role.code}) updated with full CMS access."))

    def _seed_cms_content(self, db_alias):
        self.stdout.write("\n5. Provisioning Program Categories Media (Pilates & Bootcamp)...")
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
            media_obj, created = ProgramCategoryMedia.objects.using(db_alias).get_or_create(
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
            status_str = "Created" if created else "Existing"
            self.stdout.write(f"   [{status_str}] Category Media: {media_obj.category_name}")

        self.stdout.write("\n6. Provisioning Starter Banners...")
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
            banner_obj, created = CMSBanner.objects.using(db_alias).get_or_create(
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
            status_str = "Created" if created else "Existing"
            self.stdout.write(f"   [{status_str}] Banner: {banner_obj.title}")
