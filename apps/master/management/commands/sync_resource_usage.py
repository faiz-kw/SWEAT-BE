"""
Management command to synchronize tenant resource usage into Master DB snapshot table.

Usage:
    python manage.py sync_resource_usage --all
    python manage.py sync_resource_usage --tenant=cult-fit
"""

import logging
from django.core.management.base import BaseCommand
from apps.master.models_tenant import Tenant
from apps.master.metering import sync_tenant_resource_usage

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Synchronizes live resource usage counts from tenant databases into Master DB TenantResourceUsage'

    def add_arguments(self, parser):
        parser.add_argument('--tenant', type=str, help='Slug of specific tenant to sync')
        parser.add_argument('--all', action='store_true', help='Sync all active tenants')
        parser.add_argument(
            '--metrics',
            nargs='+',
            default=['ACTIVE_USERS', 'LOCATIONS'],
            help='Specific metric codes to sync (defaults to ACTIVE_USERS and LOCATIONS)'
        )

    def handle(self, *args, **options):
        tenant_slug = options.get('tenant')
        sync_all = options.get('all')
        metrics = options.get('metrics')

        if not tenant_slug and not sync_all:
            self.stderr.write("Please specify either --tenant=<slug> or --all")
            return

        tenant_id = None
        if tenant_slug:
            tenant = Tenant.objects.using('default').filter(slug=tenant_slug).first()
            if not tenant:
                self.stderr.write(f"Tenant with slug '{tenant_slug}' not found.")
                return
            tenant_id = str(tenant.id)

        self.stdout.write(f"Starting resource usage sync (metrics={metrics})...")
        result = sync_tenant_resource_usage(tenant_id=tenant_id, metrics=metrics)

        self.stdout.write(
            f"Sync complete. Total: {result['total_tenants']}, "
            f"Succeeded: {result['succeeded']}, Failed: {result['failed']}"
        )

        for tid, details in result.get('details', {}).items():
            self.stdout.write(
                self.style.SUCCESS(f"  [OK] Tenant {details['slug']}: {details['metrics']}")
            )

        for tid, error in result.get('errors', {}).items():
            self.stdout.write(
                self.style.ERROR(f"  [ERR] Tenant {tid}: {error}")
            )
