import uuid
import logging
from django.db import transaction

from apps.tenant_core.models_rbac import (
    ModuleCatalog, SubmoduleCatalog, Permission, Role,
    RolePermissionSet, RolePermissionSetItem, RoleModuleAccess, RoleSubmoduleAccess
)
from apps.tenant_core.context import get_current_tenant_db_alias

logger = logging.getLogger(__name__)


def seed_cms_rbac(db_alias=None):
    """
    Ensure the CMS module, its submodules, and all CMS permissions are registered in the
    tenant database catalog, and grant full access to administrator roles.
    """
    alias = db_alias or get_current_tenant_db_alias() or 'default'
    
    with transaction.atomic(using=alias):
        # 1. Module Catalog
        mod, _ = ModuleCatalog.objects.using(alias).get_or_create(
            module_code='cms',
            defaults={
                'code': 'cms',
                'name': 'Content Management System',
                'source_module_id': uuid.uuid4(),
                'is_enabled': True,
                'status': 'ACTIVE',
            }
        )
        if not mod.is_enabled:
            mod.is_enabled = True
            mod.save(using=alias)

        # 2. Submodule Catalog
        submodules_def = [
            ('banners', 'Home & Section Banners', 'Manage home hero sliders, promo banners, and deep links'),
            ('categories', 'Category Media & Content', 'Manage visual icons, banners, and descriptions for Pilates & Bootcamp'),
            ('media', 'Media Library', 'Upload and manage images, audio tracks, and video clips for the app'),
        ]
        submods = {}
        for sm_code, sm_name, sm_desc in submodules_def:
            sm, _ = SubmoduleCatalog.objects.using(alias).get_or_create(
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

        # 3. Permissions
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
            p_obj, _ = Permission.objects.using(alias).get_or_create(
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

        # 4. Activate for Administrator Roles
        admin_roles = Role.objects.using(alias).filter(code__in=['ORG_ADMIN', 'ADMIN', 'SUPER_ADMIN'])
        for role in admin_roles:
            perm_set = RolePermissionSet.objects.using(alias).filter(role=role, is_active=True).first()
            if not perm_set:
                perm_set = RolePermissionSet.objects.using(alias).create(
                    organization=role.organization,
                    role=role,
                    name=f"{role.name} Permission Set",
                    is_active=True
                )

            for p in created_perms:
                RolePermissionSetItem.objects.using(alias).get_or_create(
                    permission_set=perm_set,
                    permission=p,
                    defaults={'is_allowed': True, 'granted': True}
                )

            RoleModuleAccess.objects.using(alias).update_or_create(
                role=role,
                module=mod,
                defaults={
                    'permission_set': perm_set,
                    'can_access': True,
                    'is_visible': True,
                }
            )

            for sm in submods.values():
                RoleSubmoduleAccess.objects.using(alias).update_or_create(
                    role=role,
                    submodule=sm,
                    defaults={
                        'permission_set': perm_set,
                        'can_access': True,
                        'is_visible': True,
                    }
                )

    return True
