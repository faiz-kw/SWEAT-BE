"""
Django Management Command: sync_default_role_permissions
Seeds or backfills canonical system role default permissions into tenant database(s).
Usage:
    python manage.py sync_default_role_permissions [--tenant=SLUG] [--overwrite] [--dry-run]
"""

from django.core.management.base import BaseCommand
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from config.tenant_middleware import _register_tenant_connection
from config.routers import build_tenant_db_alias, set_tenant_db_alias
from apps.tenant_core.rbac_defaults import sync_default_role_permissions, DEFAULT_ROLES_CONFIG


class Command(BaseCommand):
    help = 'Synchronizes canonical role default permission templates into tenant database(s).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--tenant',
            type=str,
            help='Specific tenant slug to sync (defaults to all active tenants)',
        )
        parser.add_argument(
            '--overwrite',
            action='store_true',
            help='Overwrite existing permission grants with default values',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Simulate sync without committing changes',
        )

    def handle(self, *args, **options):
        tenant_slug = options.get('tenant')
        overwrite = options.get('overwrite', False)
        dry_run = options.get('dry_run', False)

        qs = Tenant.objects.using('default').filter(status='ACTIVE')
        if tenant_slug:
            qs = qs.filter(slug=tenant_slug)

        tenants = list(qs)
        if not tenants:
            self.stdout.write(self.style.WARNING(f"No active tenants found matching query."))
            return

        self.stdout.write(f"Starting default role permission sync for {len(tenants)} tenant(s)...")

        for tenant in tenants:
            ds = TenantDataSource.objects.using('default').filter(tenant=tenant, status='ACTIVE').first()
            if not ds:
                self.stdout.write(self.style.WARNING(f"  [SKIPPED] Tenant '{tenant.slug}' has no active data source."))
                continue

            db_name = ds.database_name or ds.db_name
            db_alias = build_tenant_db_alias(tenant.id)
            _register_tenant_connection(db_alias, db_name, data_source=ds, tenant_id=tenant.id)
            set_tenant_db_alias(db_alias)

            if dry_run:
                self.stdout.write(self.style.NOTICE(f"  [DRY RUN] Would sync {len(DEFAULT_ROLES_CONFIG)} canonical roles for tenant '{tenant.slug}' ({db_name})."))
                continue

            try:
                results = sync_default_role_permissions(db_alias, overwrite_custom=overwrite)
                self.stdout.write(self.style.SUCCESS(f"  [OK] Tenant '{tenant.slug}' synchronized:"))
                for role_code, res in results.items():
                    self.stdout.write(
                        f"    - {res['role_name']} ({role_code}): {res['granted_permissions']} perms, "
                        f"{res['modules_enabled']} modules, {res['submodules_enabled']} submodules"
                    )
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f"  [ERROR] Failed to sync tenant '{tenant.slug}': {exc}"))

        self.stdout.write(self.style.SUCCESS("Default role permission synchronization complete."))
