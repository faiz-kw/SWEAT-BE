"""
apps/tenant_core/management/commands/recover_meta_imports.py

Django management command to find and recover any live Meta imports that were persisted
in PostgreSQL as PENDING but were never processed (due to worker queue/broker failure).
"""

from django.core.management.base import BaseCommand
from apps.master.models_tenant import Tenant
from apps.tenant_core.services_meta_recovery import recover_unprocessed_meta_imports


class Command(BaseCommand):
    help = 'Recovers stuck PENDING Meta Lead Ads imports across active tenants'

    def add_arguments(self, parser):
        parser.add_argument('--tenant', type=str, default='sweat', help='Tenant slug or ID to recover')
        parser.add_argument('--all-tenants', action='store_true', help='Recover across all active tenants')
        parser.add_argument('--min-age-seconds', type=int, default=60, help='Minimum age in seconds of pending import')
        parser.add_argument('--async-dispatch', action='store_true', help='Dispatch to Celery queue instead of synchronous processing')

    def handle(self, *args, **options):
        min_age = options['min_age_seconds']
        dispatch_async = options['async_dispatch']

        if options['all_tenants']:
            tenants = list(Tenant.objects.using('default').filter(status='ACTIVE'))
        else:
            slug = options['tenant']
            tenants = list(Tenant.objects.using('default').filter(slug=slug, status='ACTIVE'))

        if not tenants:
            self.stdout.write(self.style.WARNING("No active tenants found matching criteria."))
            return

        total_recovered = 0
        total_failed = 0

        for t in tenants:
            self.stdout.write(f"Checking tenant '{t.slug}' (ID: {t.id})...")
            res = recover_unprocessed_meta_imports(
                tenant_id=str(t.id),
                min_age_seconds=min_age,
                dispatch_async=dispatch_async,
            )
            rec = res['recovered_count']
            fl = res['failed_count']
            total_recovered += rec
            total_failed += fl
            if rec > 0 or fl > 0:
                self.stdout.write(self.style.SUCCESS(f"  Tenant '{t.slug}': Recovered {rec}, Failed {fl}"))
            else:
                self.stdout.write(f"  Tenant '{t.slug}': No stuck imports found.")

        self.stdout.write(self.style.SUCCESS(
            f"Meta imports recovery completed: total_recovered={total_recovered}, total_failed={total_failed}"
        ))
