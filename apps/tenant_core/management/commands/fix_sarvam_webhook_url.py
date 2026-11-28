"""
Management command: fix_sarvam_webhook_url
==========================================
One-time command to patch the Sarvam CRM integration config so the webhook URL
is built with the tenant slug ("sweat") rather than the org UUID.

Run once on the production server after deploy:

    python manage.py fix_sarvam_webhook_url

Safe to run multiple times (idempotent).
"""

import json
import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Patch Sarvam CRM integration to add public_integration_id=sweat in config'

    def handle(self, *args, **options):
        # Import here so Django is fully initialised
        from apps.tenant_core.models_integration import CRMIntegration  # noqa: adjust import path if needed

        updated = 0
        for integration in CRMIntegration.objects.using('default').filter(provider__icontains='SARVAM'):
            config = integration.config or {}
            if config.get('public_integration_id'):
                self.stdout.write(
                    f'[SKIP] Integration {integration.id} already has '
                    f'public_integration_id={config["public_integration_id"]}'
                )
                continue

            config['public_integration_id'] = 'sweat'
            integration.config = config
            integration.save(update_fields=['config'])
            self.stdout.write(
                self.style.SUCCESS(
                    f'[OK]   Integration {integration.id} updated: public_integration_id=sweat'
                )
            )
            updated += 1

        if updated == 0:
            self.stdout.write('No integrations needed updating (already correct or none found).')
        else:
            self.stdout.write(self.style.SUCCESS(f'Done — {updated} integration(s) updated.'))
