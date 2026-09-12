"""
Tenant Onboarding Provisioning Engine — Phase 1 Layer 1
Implements the 14-step onboarding flow from Section 11 of the schema document.

Usage:
    engine = TenantProvisioningEngine(initiated_by=platform_user)
    result = engine.provision(payload)
"""

import logging
import uuid
import psycopg2
from django.conf import settings
from django.db import transaction, connections
from django.utils import timezone
from django.core.management import call_command

logger = logging.getLogger(__name__)


def _get_pg_connection(db_name='postgres'):
    """Open a raw psycopg2 connection for DDL operations (CREATE DATABASE)."""
    return psycopg2.connect(
        dbname=db_name,
        user=settings.TENANT_PROVISION_DB_USER,
        password=settings.TENANT_PROVISION_DB_PASSWORD,
        host=settings.TENANT_PROVISION_DB_HOST,
        port=settings.TENANT_PROVISION_DB_PORT,
    )


class ProvisioningError(Exception):
    pass


class TenantProvisioningEngine:
    """
    Atomically provisions a new tenant through 14 steps.
    Each step is logged to TenantProvisioning state record.
    On failure, attempts rollback and marks provisioning as FAILED.
    """

    def __init__(self, initiated_by=None):
        self.initiated_by = initiated_by

    def provision(self, payload: dict, provisioning=None, provisioning_id: str = None) -> dict:
        """
        Execute the full 14-step tenant onboarding.
        payload keys:
            brand_name, code, slug, country, currency, timezone, default_language,
            domain (optional), plan_id (optional), billing_cycle,
            enabled_modules (list of module codes), availability_mode,
            db_name (optional — auto-generated if not provided),
            org_name, org_code, org_email, org_phone,
            location_name, location_code, location_city, location_area, location_state,
            branch_name, branch_code, branch_address, branch_capacity,
            admin_email, admin_first_name, admin_last_name, admin_phone, admin_password,
            selected_branch_modules (dict: module_code -> list of branch codes)
        """
        from apps.master.models_tenant import Tenant
        from apps.master.models_infra import TenantProvisioning

        # Resolve or create provisioning record
        if provisioning is None:
            if provisioning_id:
                provisioning = TenantProvisioning.objects.using('default').get(id=provisioning_id)
            else:
                provisioning = TenantProvisioning.objects.using('default').create(
                    tenant_id=None,  # Will be set after tenant creation
                    status='IN_PROGRESS',
                    initiated_by=self.initiated_by,
                    total_steps=14,
                )

        # Idempotency check: if already completed, exit cleanly
        if provisioning.status == 'COMPLETED' and provisioning.tenant:
            tenant = provisioning.tenant
            ds = getattr(tenant, 'data_source', None)
            return {
                'success': True,
                'tenant_id': str(tenant.id),
                'tenant_slug': tenant.slug,
                'db_name': ds.db_name if ds else '',
                'admin_email': payload.get('admin_email', ''),
                'provisioning_id': str(provisioning.id),
                'message': f"Tenant '{tenant.name}' is already provisioned and active.",
            }

        if provisioning.status != 'IN_PROGRESS':
            provisioning.status = 'IN_PROGRESS'
            provisioning.save(using='default', update_fields=['status', 'updated_at'])

        tenant = provisioning.tenant
        db_alias = None

        try:
            # ── Step 1: Create Tenant in Master DB ──────────────────────────
            tenant = self._step1_create_tenant(payload, provisioning)

            # ── Step 2: Configure Domain & Branding Mode ────────────────────
            self._step2_configure_domain(tenant, payload, provisioning)

            # ── Step 3: Assign SaaS Plan ────────────────────────────────────
            self._step3_assign_plan(tenant, payload, provisioning)

            # ── Step 4: Set Module Availability Mode ─────────────────────────
            self._step4_set_module_availability(tenant, payload, provisioning)

            # ── Step 5: Configure Tenant Data Source ─────────────────────────
            data_source = self._step5_configure_data_source(tenant, payload, provisioning)

            # ── Step 6: Provision Dedicated PostgreSQL Database ───────────────
            db_name = self._step6_provision_database(tenant, data_source, provisioning)
            db_alias = f"tenant_{db_name}"

            # ── Steps 7-13: Tenant DB Operations (explicit routing context) ──
            from config.routers import set_tenant_db_alias, get_tenant_db_alias
            old_alias = get_tenant_db_alias()
            set_tenant_db_alias(db_alias)
            try:
                # ── Step 7: Create Organization ──────────────────────────────────
                org = self._step7_create_organization(db_alias, payload, provisioning)

                # ── Step 8: Create Company Entity (optional) ─────────────────────
                self._step8_create_company_entity(db_alias, org, payload, provisioning)

                # ── Step 9: Create Location ──────────────────────────────────────
                location = self._step9_create_location(db_alias, org, payload, provisioning)

                # ── Step 10: Create Branch ───────────────────────────────────────
                branch = self._step10_create_branch(db_alias, org, location, payload, provisioning)

                # ── Step 11: Apply Branch Module Mappings ────────────────────────
                self._step11_apply_branch_modules(db_alias, branch, payload, provisioning)

                # ── Step 12: Create First Org Admin ─────────────────────────────
                admin_user = self._step12_create_org_admin(db_alias, org, branch, payload, provisioning)

                # ── Step 13: Seed Permission Catalog & Create Org Admin Role ──────
                self._step13_seed_rbac(db_alias, org, admin_user, provisioning)
            finally:
                set_tenant_db_alias(old_alias)

            # ── Step 14: Activate Tenant ─────────────────────────────────────
            self._step14_activate_tenant(tenant, provisioning)

            provisioning.status = 'COMPLETED'
            provisioning.completed_at = timezone.now()
            provisioning.save(using='default', update_fields=['status', 'completed_at', 'updated_at'])

            return {
                'success': True,
                'tenant_id': str(tenant.id),
                'tenant_slug': tenant.slug,
                'db_name': db_name,
                'admin_email': payload['admin_email'],
                'provisioning_id': str(provisioning.id),
                'message': f"Tenant '{tenant.name}' successfully provisioned and activated.",
            }

        except Exception as exc:
            logger.exception(f"Provisioning failed for tenant {payload.get('brand_name')}: {exc}")
            provisioning.status = 'FAILED'
            provisioning.error_message = str(exc)
            provisioning.error_step = provisioning.current_step
            provisioning.save(using='default', update_fields=['status', 'error_message', 'error_step', 'updated_at'])

            # Mark tenant as DEACTIVATED if it was created
            if tenant:
                try:
                    tenant.status = 'DEACTIVATED'
                    tenant.deactivation_reason = f'Provisioning failed: {str(exc)}'
                    tenant.save(using='default', update_fields=['status', 'deactivation_reason', 'updated_at'])
                except Exception:
                    pass

            raise ProvisioningError(f"Provisioning failed: {exc}") from exc

    # ──────────────────────────────────────────────────────────────────────────
    # Step implementations
    # ──────────────────────────────────────────────────────────────────────────

    def _step1_create_tenant(self, payload, provisioning):
        from apps.master.models_tenant import Tenant
        slug = payload.get('slug') or payload['brand_name'].lower().replace(' ', '-')
        code = payload.get('code') or slug.upper()[:50]

        with transaction.atomic(using='default'):
            tenant = Tenant.objects.using('default').filter(slug=slug).first()
            if not tenant:
                if (
                    provisioning
                    and getattr(provisioning, 'tenant', None)
                    and provisioning.tenant.status == 'DRAFT'
                    and provisioning.tenant.slug.startswith('draft-prov-')
                ):
                    tenant = provisioning.tenant
                    tenant.code = code
                    tenant.name = payload['brand_name']
                    tenant.legal_name = payload.get('legal_name')
                    tenant.slug = slug
                    tenant.country = payload.get('country', 'IN')
                    tenant.currency = payload.get('currency', 'INR')
                    tenant.timezone = payload.get('timezone', 'Asia/Kolkata')
                    tenant.default_language = payload.get('default_language', 'en')
                    tenant.save(using='default')
                else:
                    tenant = Tenant.objects.using('default').create(
                        code=code,
                        name=payload['brand_name'],
                        legal_name=payload.get('legal_name'),
                        slug=slug,
                        status='DRAFT',
                        country=payload.get('country', 'IN'),
                        currency=payload.get('currency', 'INR'),
                        timezone=payload.get('timezone', 'Asia/Kolkata'),
                        default_language=payload.get('default_language', 'en'),
                    )
        # Update provisioning tenant FK now that we have it
        provisioning.tenant = tenant
        provisioning.save(using='default', update_fields=['tenant', 'updated_at'])
        provisioning.log_step('CREATE_TENANT', True, f"Created tenant {tenant.slug}")
        return tenant

    def _step2_configure_domain(self, tenant, payload, provisioning):
        from apps.master.models_tenant import TenantDomain, TenantBranding
        domain = payload.get('domain') or f"{tenant.slug}.performanceos.io"

        TenantDomain.objects.using('default').get_or_create(
            tenant=tenant,
            domain=domain,
            defaults={
                'domain_type': 'PLATFORM' if domain.endswith('.performanceos.io') else 'CUSTOM',
                'is_primary': True,
                'status': 'ACTIVE',
                'is_verified': True,
                'verified_at': timezone.now(),
            }
        )
        TenantBranding.objects.using('default').get_or_create(
            tenant=tenant,
            defaults={
                'branding_mode': payload.get('branding_mode', 'PLATFORM'),
                'app_name': payload['brand_name'],
                'primary_color': payload.get('primary_color', '#0f766e'),
                'accent_color': payload.get('accent_color', '#f59e0b'),
            }
        )
        provisioning.log_step('CONFIGURE_DOMAIN_BRANDING', True, f"Domain: {domain}")

    def _step3_assign_plan(self, tenant, payload, provisioning):
        from apps.master.models_saas import SaasPlan, TenantSubscription
        sub = TenantSubscription.objects.using('default').filter(tenant=tenant).first()
        if sub:
            provisioning.log_step('ASSIGN_PLAN', True, f"Plan: {sub.plan.code}")
            return sub

        plan_id = payload.get('plan_id')
        plan = None
        if plan_id:
            plan = SaasPlan.objects.using('default').filter(id=plan_id, is_active=True).first()
        if not plan:
            plan = SaasPlan.objects.using('default').filter(is_active=True).order_by('sort_order').first()

        if plan:
            sub = TenantSubscription.objects.using('default').create(
                tenant=tenant,
                plan=plan,
                billing_cycle=payload.get('billing_cycle', 'MONTHLY'),
                status='TRIALING',
                trial_ends_at=timezone.now() + timezone.timedelta(days=plan.trial_days or 14),
            )
            provisioning.log_step('ASSIGN_PLAN', True, f"Plan: {plan.code}")
        else:
            provisioning.log_step('ASSIGN_PLAN', True, 'No plan assigned (manual billing)')
        return sub

    def _step4_set_module_availability(self, tenant, payload, provisioning):
        from apps.master.models_saas import ProductModule, TenantModule
        from django.db.models import Q
        enabled_modules = payload.get('enabled_modules', [])
        availability_mode = payload.get('availability_mode', 'ALL_BRANCHES')

        # Enable all core modules + requested modules
        q = Q(is_core=True)
        if enabled_modules:
            q |= Q(code__in=enabled_modules)
        modules = ProductModule.objects.using('default').filter(q, is_active=True)

        for module in modules:
            TenantModule.objects.using('default').get_or_create(
                tenant=tenant,
                module=module,
                defaults={'availability_mode': availability_mode, 'is_enabled': True},
            )
        provisioning.log_step('SET_MODULE_AVAILABILITY', True, f"Mode: {availability_mode}, Modules: {modules.count()}")

    def _step5_configure_data_source(self, tenant, payload, provisioning):
        from apps.master.models_infra import TenantDataSource, TenantDataHostingPolicy
        db_name = payload.get('db_name') or f"tenant_{tenant.slug.replace('-', '_')}"

        data_source = TenantDataSource.objects.using('default').filter(tenant=tenant).first()
        if not data_source:
            data_source = TenantDataSource.objects.using('default').create(
                tenant=tenant,
                source_type=payload.get('source_type', 'PLATFORM_MANAGED'),
                db_name=db_name,
                db_host=settings.TENANT_PROVISION_DB_HOST,
                db_port=settings.TENANT_PROVISION_DB_PORT,
                db_user=settings.TENANT_PROVISION_DB_USER,
                status='PROVISIONING',
                is_platform_billable=True,
            )
        TenantDataHostingPolicy.objects.using('default').get_or_create(
            tenant=tenant,
            defaults={
                'data_source': data_source,
                'preferred_region': payload.get('region', 'IN-MUMBAI'),
                'backup_retention_days': 30,
                'encryption_at_rest': True,
            }
        )
        provisioning.log_step('CONFIGURE_DATA_SOURCE', True, f"DB name: {data_source.db_name}")
        return data_source

    def _step6_provision_database(self, tenant, data_source, provisioning):
        """Create the dedicated PostgreSQL database and run tenant schema migrations."""
        db_name = data_source.db_name
        db_alias = f"tenant_{db_name}"

        # Create database via raw psycopg2 (autocommit required for CREATE DATABASE)
        conn = _get_pg_connection()
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                # Check if DB already exists
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", [db_name])
                if not cur.fetchone():
                    cur.execute(f'CREATE DATABASE "{db_name}"')
                    logger.info(f"Created database: {db_name}")
        finally:
            conn.close()

        # Register the new DB in Django settings
        from config.tenant_middleware import _register_tenant_connection
        _register_tenant_connection(db_alias, db_name)

        # Run tenant_core migrations against the new database
        call_command('migrate', '--database', db_alias, 'tenant_core', verbosity=0)

        # Update data source record
        data_source.status = 'ACTIVE'
        data_source.provisioned_at = timezone.now()
        data_source.db_schema_version = '1.0'
        data_source.save(using='default', update_fields=['status', 'provisioned_at', 'db_schema_version', 'updated_at'])

        provisioning.log_step('PROVISION_DATABASE', True, f"Database '{db_name}' created and migrated")
        return db_name

    def _step7_create_organization(self, db_alias, payload, provisioning):
        from apps.tenant_core.models_org import Organization
        org_code = payload.get('org_code') or payload['brand_name'][:50].upper().replace(' ', '_')
        org = Organization.objects.using(db_alias).filter(code=org_code).first()
        if not org:
            org = Organization.objects.using(db_alias).create(
                code=org_code,
                name=payload.get('org_name') or payload['brand_name'],
                legal_name=payload.get('legal_name'),
                email=payload.get('org_email', ''),
                phone=payload.get('org_phone', ''),
                country=payload.get('country', 'IN'),
                currency=payload.get('currency', 'INR'),
                timezone=payload.get('timezone', 'Asia/Kolkata'),
                status='ACTIVE',
                activated_at=timezone.now(),
            )
        provisioning.log_step('CREATE_ORGANIZATION', True, f"Org: {org.name}")
        return org

    def _step8_create_company_entity(self, db_alias, org, payload, provisioning):
        """Optional — only create if company entity details provided."""
        if not payload.get('company_entity_name'):
            provisioning.log_step('CREATE_COMPANY_ENTITY', True, 'Skipped (optional)')
            return None

        from apps.tenant_core.models_org import CompanyEntity
        code = payload.get('company_entity_code', 'MAIN')
        entity = CompanyEntity.objects.using(db_alias).filter(organization=org, code=code).first()
        if not entity:
            entity = CompanyEntity.objects.using(db_alias).create(
                organization=org,
                code=code,
                name=payload['company_entity_name'],
                registration_number=payload.get('registration_number', ''),
                tax_registration_number=payload.get('tax_registration_number', ''),
                status='ACTIVE',
                activated_at=timezone.now(),
            )
        provisioning.log_step('CREATE_COMPANY_ENTITY', True, f"Entity: {entity.name}")
        return entity

    def _step9_create_location(self, db_alias, org, payload, provisioning):
        from apps.tenant_core.models_org import Location
        code = payload.get('location_code', 'LOC-MAIN')
        location = Location.objects.using(db_alias).filter(organization=org, code=code).first()
        if not location:
            location = Location.objects.using(db_alias).create(
                organization=org,
                code=code,
                name=payload.get('location_name', 'Main Location'),
                city=payload.get('location_city') or payload.get('city', ''),
                area=payload.get('location_area', ''),
                state=payload.get('location_state', ''),
                country=payload.get('country', 'IN'),
                status='ACTIVE',
                activated_at=timezone.now(),
            )
        provisioning.log_step('CREATE_LOCATION', True, f"Location: {location.name}")
        return location

    def _step10_create_branch(self, db_alias, org, location, payload, provisioning):
        from apps.tenant_core.models_org import Branch
        code = payload.get('branch_code', 'BR-MAIN')
        branch = Branch.objects.using(db_alias).filter(organization=org, code=code).first()
        if not branch:
            branch = Branch.objects.using(db_alias).create(
                organization=org,
                location=location,
                code=code,
                name=payload.get('branch_name') or payload.get('location_name', 'Main Studio'),
                address=payload.get('branch_address') or payload.get('address', ''),
                capacity=payload.get('branch_capacity', 0),
                status='ACTIVE',
                activated_at=timezone.now(),
            )
        provisioning.log_step('CREATE_BRANCH', True, f"Branch: {branch.name}")
        return branch

    def _step11_apply_branch_modules(self, db_alias, branch, payload, provisioning):
        """Create BranchModule rows for SELECTED_BRANCHES mode modules."""
        from apps.tenant_core.models_rbac import BranchModule
        selected = payload.get('selected_branch_modules', {})
        count = 0
        for module_code in selected:
            BranchModule.objects.using(db_alias).get_or_create(
                branch=branch,
                module_code=module_code,
                defaults={'is_enabled': True},
            )
            count += 1
        provisioning.log_step('APPLY_BRANCH_MODULES', True, f"{count} branch-module mappings created")

    def _step12_create_org_admin(self, db_alias, org, branch, payload, provisioning):
        from apps.tenant_core.models_users import TenantUser, UserBranch
        user = TenantUser.objects.using(db_alias).filter(email=payload['admin_email']).first()
        if not user:
            user = TenantUser(
                organization=org,
                email=payload['admin_email'],
                first_name=payload['admin_first_name'],
                last_name=payload.get('admin_last_name', ''),
                phone=payload.get('admin_phone', ''),
                status='ACTIVE',
                activated_at=timezone.now(),
                home_branch=branch,
            )
            user.set_password(payload['admin_password'])
            user.save(using=db_alias)

        UserBranch.objects.using(db_alias).get_or_create(
            user=user,
            branch=branch,
            defaults={'scope_type': 'HOME', 'is_active': True},
        )
        provisioning.log_step('CREATE_ORG_ADMIN', True, f"Admin: {user.email}")
        return user

    def _step13_seed_rbac(self, db_alias, org, admin_user, provisioning):
        """Seed permission catalog from master and create Org Admin role."""
        counts = sync_tenant_catalog_and_rbac(db_alias, org=org, admin_user=admin_user)
        provisioning.log_step(
            'SEED_RBAC_CATALOG', True,
            f"Seeded {counts['permissions']} permissions across {counts['modules']} modules, created Org Admin role with complete RBAC records"
        )

    def _step14_activate_tenant(self, tenant, provisioning):
        tenant.activate()
        provisioning.log_step('ACTIVATE_TENANT', True, f"Tenant {tenant.slug} is now ACTIVE")


def sync_tenant_catalog_and_rbac(db_alias: str, org=None, admin_user=None) -> dict:
    """
    Idempotently synchronize product modules, submodules, and permissions
    from Master DB into the specified tenant database.
    Also ensures any existing ORG_ADMIN system role has complete RBAC grants.
    """
    from config.routers import set_tenant_db_alias, get_tenant_db_alias
    old_alias = get_tenant_db_alias()
    set_tenant_db_alias(db_alias)
    try:
        from apps.master.models_saas import TenantPermissionCatalog
        from apps.tenant_core.models_rbac import (
            ModuleCatalog, SubmoduleCatalog, Permission, Role, RoleAssignment,
            RoleModuleAccess, RoleSubmoduleAccess, RolePermissionSet, RolePermissionSetItem
        )

        catalog_entries = (
            TenantPermissionCatalog.objects.using('default')
            .filter(is_active=True)
            .select_related('module', 'submodule')
        )

        module_map = {}
        submodule_map = {}
        perm_count = 0

        for entry in catalog_entries:
            m_code = entry.module.code
            if m_code not in module_map:
                module_obj, _ = ModuleCatalog.objects.using(db_alias).update_or_create(
                    module_code=m_code,
                    defaults={
                        'name': entry.module.name,
                        'description': entry.module.description,
                        'icon': getattr(entry.module, 'icon', ''),
                        'is_enabled': True,
                        'sort_order': entry.module.sort_order,
                        'synced_at': timezone.now(),
                        'source_module_id': entry.module.id,
                    },
                )
                module_map[m_code] = module_obj

            if entry.submodule:
                sm_code = entry.submodule.code
                key = (m_code, sm_code)
                if key not in submodule_map:
                    sm_obj, _ = SubmoduleCatalog.objects.using(db_alias).update_or_create(
                        module=module_map[m_code],
                        submodule_code=sm_code,
                        defaults={
                            'code': sm_code,
                            'name': entry.submodule.name,
                            'description': entry.submodule.description,
                            'sort_order': entry.submodule.sort_order,
                            'display_order': entry.submodule.display_order if hasattr(entry.submodule, 'display_order') else entry.submodule.sort_order,
                            'source_submodule_id': entry.submodule.id,
                            'is_enabled': True,
                            'synced_at': timezone.now(),
                        },
                    )
                    submodule_map[key] = sm_obj

            Permission.objects.using(db_alias).update_or_create(
                permission_code=entry.code,
                defaults={
                    'source_permission_id': entry.id,
                    'module': module_map[m_code],
                    'submodule': submodule_map.get((m_code, entry.submodule.code if entry.submodule else ''), None),
                    'action': entry.action,
                    'label': entry.label,
                    'description': entry.description,
                    'is_active': True,
                },
            )
            perm_count += 1

        # If org and admin_user are provided, ensure admin role and assignment
        if org and admin_user:
            admin_role, _ = Role.objects.using(db_alias).get_or_create(
                organization=org,
                code='ORG_ADMIN',
                defaults={
                    'name': 'Organization Administrator',
                    'scope': 'ORG',
                    'is_system': True,
                    'is_active': True,
                },
            )
            RoleAssignment.objects.using(db_alias).get_or_create(
                user=admin_user,
                role=admin_role,
                defaults={'is_active': True, 'branch': None, 'organization': org, 'status': 'ACTIVE'},
            )

        # For any ORG_ADMIN roles present in the tenant DB, grant all modules, submodules, and permissions
        org_admin_roles = Role.objects.using(db_alias).filter(code='ORG_ADMIN')
        for a_role in org_admin_roles:
            perm_set, _ = RolePermissionSet.objects.using(db_alias).get_or_create(
                role=a_role,
                name='Organization Administrator Full Access',
                defaults={'is_active': True, 'description': 'Full system permissions for Org Admin', 'organization': a_role.organization},
            )

            for module_obj in module_map.values():
                RoleModuleAccess.objects.using(db_alias).get_or_create(
                    role=a_role,
                    module=module_obj,
                    defaults={'permission_set': perm_set, 'can_access': True, 'is_visible': True},
                )

            for sm_obj in submodule_map.values():
                RoleSubmoduleAccess.objects.using(db_alias).get_or_create(
                    role=a_role,
                    submodule=sm_obj,
                    defaults={'permission_set': perm_set, 'can_access': True, 'is_visible': True},
                )

            for p in Permission.objects.using(db_alias).all():
                RolePermissionSetItem.objects.using(db_alias).get_or_create(
                    permission_set=perm_set,
                    permission=p,
                    defaults={'granted': True},
                )

        return {
            'modules': len(module_map),
            'submodules': len(submodule_map),
            'permissions': perm_count,
        }
    finally:
        set_tenant_db_alias(old_alias)
