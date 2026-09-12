# Generated manually for Sprint 3 Platform Authorization Hardening
from django.db import migrations


def seed_canonical_platform_permissions(apps, schema_editor):
    PlatformModule = apps.get_model('master', 'PlatformModule')
    PlatformPermission = apps.get_model('master', 'PlatformPermission')
    PlatformRole = apps.get_model('master', 'PlatformRole')
    PlatformRoleModuleAccess = apps.get_model('master', 'PlatformRoleModuleAccess')
    PlatformRolePermission = apps.get_model('master', 'PlatformRolePermission')

    db_alias = schema_editor.connection.alias

    # 1. Platform Modules
    modules_data = [
        ('tenants', 'Tenant Management', 'Tenant accounts, module entitlements, and provisioning', 'Building2', 1),
        ('billing', 'SaaS Billing & Subscriptions', 'Subscription plans, pricing, invoices, and payment tracking', 'CreditCard', 2),
        ('marketplace', 'Marketplace & Integrations', 'Partner ecosystem apps and tenant installations', 'Store', 3),
        ('modules', 'Product Module Catalog', 'Platform product module definitions and availability', 'Grid', 4),
        ('iam', 'Platform Identity & Access', 'Platform staff users, departments, and roles', 'Shield', 5),
    ]

    module_objs = {}
    for code, name, desc, icon, sort_order in modules_data:
        mod, _ = PlatformModule.objects.using(db_alias).update_or_create(
            code=code,
            defaults={
                'name': name,
                'description': desc,
                'icon': icon,
                'sort_order': sort_order,
                'is_active': True,
            }
        )
        module_objs[code] = mod

    # 2. Canonical Platform Permissions
    permissions_data = [
        # tenants
        ('tenants.view', 'tenants', 'view', 'View Tenants', 'View tenant accounts, usage metrics, and module entitlements'),
        ('tenants.create', 'tenants', 'create', 'Create Tenant', 'Create and register new tenant accounts'),
        ('tenants.edit', 'tenants', 'edit', 'Edit Tenant', 'Update tenant details, status, modules, and branch assignments'),
        ('tenants.delete', 'tenants', 'delete', 'Delete Tenant', 'Deactivate or suspend tenant accounts'),
        ('tenants.provision', 'tenants', 'provision', 'Provision Tenant', 'Execute automated tenant database and onboarding provisioning'),

        # billing
        ('billing.view', 'billing', 'view', 'View Billing', 'View subscription plans, tenant subscriptions, and invoices'),
        ('billing.create', 'billing', 'create', 'Create Billing', 'Create subscription plans and tenant subscriptions'),
        ('billing.edit', 'billing', 'edit', 'Edit Billing', 'Update plans, pricing, and change tenant subscription plans'),
        ('billing.delete', 'billing', 'delete', 'Delete Billing', 'Cancel subscriptions and deactivate plans'),

        # marketplace
        ('marketplace.view', 'marketplace', 'view', 'View Marketplace', 'View partner integrations and tenant installation status'),
        ('marketplace.create', 'marketplace', 'create', 'Create Integration', 'Publish new marketplace integrations'),
        ('marketplace.edit', 'marketplace', 'edit', 'Edit Integration', 'Toggle installations and update integration configuration'),
        ('marketplace.delete', 'marketplace', 'delete', 'Delete Integration', 'Remove partner integrations from marketplace'),

        # modules
        ('modules.view', 'modules', 'view', 'View Product Modules', 'Inspect product module catalog'),
        ('modules.edit', 'modules', 'edit', 'Edit Product Modules', 'Configure product module definitions and availability'),

        # iam
        ('iam.view', 'iam', 'view', 'View Platform IAM', 'Inspect platform staff users and roles'),
        ('iam.create', 'iam', 'create', 'Create Platform User', 'Invite and create platform staff accounts'),
        ('iam.edit', 'iam', 'edit', 'Edit Platform User', 'Update platform staff profiles and role assignments'),
        ('iam.delete', 'iam', 'delete', 'Delete Platform User', 'Deactivate or remove platform staff accounts'),
    ]

    perm_objs = {}
    for code, mod_code, action, label, desc in permissions_data:
        mod = module_objs[mod_code]
        perm, _ = PlatformPermission.objects.using(db_alias).update_or_create(
            code=code,
            defaults={
                'module': mod,
                'action': action,
                'label': label,
                'description': desc,
            }
        )
        perm_objs[code] = perm

    # 3. Existing Platform Roles — Ensure standard roles exist
    standard_roles = [
        ('SUPER_ADMIN', 'Platform Super Administrator', 'Complete system control over all tenants, billing, and infrastructure', True),
        ('BILLING_ADMIN', 'SaaS Billing Specialist', 'Subscription plans, invoices, and payment management', False),
        ('BILLING_SPECIALIST', 'SaaS Billing Specialist', 'Subscription plans, invoices, and payment management', False),
        ('SUPPORT_LEAD', 'Support Operations Lead', 'Tenant support and diagnostics access', False),
    ]

    role_objs = {}
    for r_code, r_name, r_desc, r_system in standard_roles:
        r, _ = PlatformRole.objects.using(db_alias).update_or_create(
            code=r_code,
            defaults={
                'name': r_name,
                'description': r_desc,
                'is_system': r_system,
                'is_active': True,
            }
        )
        role_objs[r_code] = r

    # 4. Role Module Access and Permission Grants
    # SUPER_ADMIN: all modules, all permissions
    super_admin = role_objs['SUPER_ADMIN']
    for mod in module_objs.values():
        PlatformRoleModuleAccess.objects.using(db_alias).update_or_create(
            role=super_admin,
            module=mod,
            defaults={'can_access': True}
        )
    for perm in perm_objs.values():
        PlatformRolePermission.objects.using(db_alias).update_or_create(
            role=super_admin,
            permission=perm,
            defaults={'granted': True}
        )

    # BILLING_ADMIN: billing.* + tenants.view
    r_admin = role_objs['BILLING_ADMIN']
    for mod_key in ['billing', 'tenants']:
        PlatformRoleModuleAccess.objects.using(db_alias).update_or_create(
            role=r_admin,
            module=module_objs[mod_key],
            defaults={'can_access': True}
        )
    for code in ['billing.view', 'billing.create', 'billing.edit', 'billing.delete', 'tenants.view']:
        PlatformRolePermission.objects.using(db_alias).update_or_create(
            role=r_admin,
            permission=perm_objs[code],
            defaults={'granted': True}
        )

    # BILLING_SPECIALIST: billing.* only
    r_spec = role_objs['BILLING_SPECIALIST']
    PlatformRoleModuleAccess.objects.using(db_alias).update_or_create(
        role=r_spec,
        module=module_objs['billing'],
        defaults={'can_access': True}
    )
    for code in ['billing.view', 'billing.create', 'billing.edit', 'billing.delete']:
        PlatformRolePermission.objects.using(db_alias).update_or_create(
            role=r_spec,
            permission=perm_objs[code],
            defaults={'granted': True}
        )

    # SUPPORT_LEAD: read-only across tenants, billing, marketplace, modules
    support_lead = role_objs['SUPPORT_LEAD']
    support_perm_codes = ['tenants.view', 'billing.view', 'marketplace.view', 'modules.view']
    for mod_key in ['tenants', 'billing', 'marketplace', 'modules']:
        PlatformRoleModuleAccess.objects.using(db_alias).update_or_create(
            role=support_lead,
            module=module_objs[mod_key],
            defaults={'can_access': True}
        )
    for code in support_perm_codes:
        PlatformRolePermission.objects.using(db_alias).update_or_create(
            role=support_lead,
            permission=perm_objs[code],
            defaults={'granted': True}
        )


class Migration(migrations.Migration):

    dependencies = [
        ('master', '0004_extend_core_settings_notifications_audit'),
    ]

    operations = [
        migrations.RunPython(seed_canonical_platform_permissions, migrations.RunPython.noop),
    ]
