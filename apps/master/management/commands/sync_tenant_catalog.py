"""
Management command to synchronize Master DB Product/Permission catalog into tenant databases.
Usage:
    python manage.py sync_tenant_catalog --all
    python manage.py sync_tenant_catalog --tenant=cult-fit
"""

import logging
from django.core.management.base import BaseCommand
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.provisioning import sync_tenant_catalog_and_rbac
from config.tenant_middleware import _register_tenant_connection

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Synchronizes Master DB product modules, submodules, and permissions into tenant DBs'

    def add_arguments(self, parser):
        parser.add_argument('--tenant', type=str, help='Slug of specific tenant to sync')
        parser.add_argument('--all', action='store_true', help='Sync all active tenants')

    def handle(self, *args, **options):
        tenant_slug = options.get('tenant')
        sync_all = options.get('all')

        if not tenant_slug and not sync_all:
            self.stderr.write("Please specify either --tenant=<slug> or --all")
            return

        tenants = Tenant.objects.using('default').filter(status='ACTIVE')
        if tenant_slug:
            tenants = tenants.filter(slug=tenant_slug)

        if not tenants.exists():
            self.stderr.write(f"No active tenants found matching criteria.")
            return

        for tenant in tenants:
            data_source = TenantDataSource.objects.using('default').filter(tenant=tenant).first()
            if not data_source:
                self.stderr.write(f"Skipping {tenant.slug}: No TenantDataSource found.")
                continue

            db_alias = f"tenant_{data_source.db_name}"
            _register_tenant_connection(db_alias, data_source.db_name)

            self.stdout.write(f"Syncing catalog for tenant '{tenant.slug}' (alias={db_alias})...")
            try:
                res = sync_tenant_catalog_and_rbac(db_alias)
                self.stdout.write(self.style.SUCCESS(
                    f"  Successfully synced {tenant.slug}: "
                    f"{res['modules']} modules, {res['submodules']} submodules, {res['permissions']} permissions."
                ))
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"  Failed syncing {tenant.slug}: {e}"))
