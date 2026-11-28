"""
Management command: update_sarvam_integration_config
====================================================
Patch the Sarvam integration row in the TENANT database (not master DB)
to add public_integration_id = "sweat" so webhook URLs always resolve correctly.

Usage:
    python manage.py update_sarvam_integration_config --tenant sweat
    python manage.py update_sarvam_integration_config --tenant sweat --dry-run
"""

import logging

from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Add public_integration_id=<slug> to Sarvam integration config in tenant DB'

    def add_arguments(self, parser):
        parser.add_argument(
            '--tenant', type=str, required=True,
            help='Tenant slug (e.g. sweat)'
        )
        parser.add_argument(
            '--dry-run', action='store_true', default=False,
            help='Print what would be changed without saving'
        )

    def handle(self, *args, **options):
        tenant_slug = options['tenant']
        dry_run = options['dry_run']

        # Resolve tenant → DB alias
        try:
            from apps.tenant_core.models_master import Tenant
            tenant = Tenant.objects.using('default').get(slug=tenant_slug)
        except Exception as e:
            raise CommandError(f'Tenant "{tenant_slug}" not found in master DB: {e}')

        db_alias = f'tenant_{tenant.db_name}' if hasattr(tenant, 'db_name') else f'tenant_{tenant.id.hex}'

        self.stdout.write(f'Tenant: {tenant.name} | DB alias: {db_alias}')

        # Find Sarvam integration in tenant DB
        try:
            from apps.tenant_core.models_integration import CRMIntegration
            integrations = CRMIntegration.objects.using(db_alias).filter(
                provider__icontains='SARVAM'
            )
        except Exception as e:
            # Try master DB as fallback
            try:
                from apps.tenant_core.models_integration import CRMIntegration
                integrations = CRMIntegration.objects.using('default').filter(
                    provider__icontains='SARVAM'
                )
                self.stdout.write(self.style.WARNING('Falling back to master DB for integration lookup'))
            except Exception as e2:
                raise CommandError(f'Cannot query CRMIntegration: {e} / {e2}')

        if not integrations.exists():
            self.stdout.write(self.style.WARNING('No SARVAM integration found — nothing to update.'))
            return

        for integ in integrations:
            config = integ.config or {}
            current_pid = config.get('public_integration_id')
            if current_pid == tenant_slug:
                self.stdout.write(f'[SKIP] {integ.id}: already has public_integration_id={current_pid}')
                continue

            self.stdout.write(
                f'[{"DRY-RUN" if dry_run else "UPDATE"}] {integ.id}: '
                f'public_integration_id: {current_pid!r} → {tenant_slug!r}'
            )
            if not dry_run:
                config['public_integration_id'] = tenant_slug
                integ.config = config
                integ.save(update_fields=['config'])
                self.stdout.write(self.style.SUCCESS(f'  ✓ Saved'))

        if dry_run:
            self.stdout.write(self.style.WARNING('Dry-run complete — no changes saved.'))
        else:
            self.stdout.write(self.style.SUCCESS('Done.'))
