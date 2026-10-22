"""
Django Management Command: sync_shop_catalog
Synchronizes live products, images, and prices dynamically from https://sweatfitwellness.com/shop/
into the tenant database(s).

Usage:
    python manage.py sync_shop_catalog
    python manage.py sync_shop_catalog --tenant=sweat
    python manage.py sync_shop_catalog --all
    python manage.py sync_shop_catalog --source-url="https://sweatfitwellness.com/wp-json/wc/store/v1/products?per_page=100"
"""

import sys
from django.conf import settings
from django.core.management.base import BaseCommand

from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from config.tenant_middleware import _register_tenant_connection
from config.routers import build_tenant_db_alias
from apps.tenant_core.services_catalog_sync import ShopCatalogSyncService


class Command(BaseCommand):
    help = 'Fetches live products from Sweat Fit Wellness store and updates the tenant Product catalog.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--tenant',
            type=str,
            default='sweat',
            help='Specific tenant slug to sync (defaults to "sweat"). Use --all for all tenants.',
        )
        parser.add_argument(
            '--all',
            action='store_true',
            help='Sync products across all active tenant databases.',
        )
        parser.add_argument(
            '--source-url',
            type=str,
            help='Custom source REST API URL to fetch products from.',
        )

    def handle(self, *args, **options):
        sync_all = options.get('all', False)
        tenant_slug = options.get('tenant')
        source_url = options.get('source_url')

        self.stdout.write(self.style.NOTICE("Connecting to live store to fetch latest product catalog..."))
        try:
            live_products = ShopCatalogSyncService.fetch_live_products(source_url=source_url)
            self.stdout.write(self.style.SUCCESS(f"Successfully fetched {len(live_products)} live products from store."))
        except Exception as exc:
            self.stderr.write(self.style.ERROR(f"Failed to fetch live products: {exc}"))
            return

        qs = Tenant.objects.using('default').filter(status='ACTIVE')
        if not sync_all:
            qs = qs.filter(slug=tenant_slug)

        tenants = list(qs)
        if not tenants:
            self.stdout.write(self.style.WARNING(f"No active tenants found matching '{tenant_slug}'."))
            return

        for tenant in tenants:
            ds = TenantDataSource.objects.using('default').filter(tenant=tenant, status='ACTIVE').first()
            if not ds:
                self.stdout.write(self.style.WARNING(f"[-] SKIPPED: Tenant '{tenant.slug}' has no active data source."))
                continue

            db_name = ds.database_name or ds.db_name
            if 'test' in sys.argv and 'tenant_test' in settings.DATABASES:
                db_alias = 'tenant_test'
            else:
                db_alias = build_tenant_db_alias(tenant.id)
            _register_tenant_connection(db_alias, db_name, data_source=ds, tenant_id=tenant.id)


            self.stdout.write(f"\n--- Syncing catalog for Tenant '{tenant.name}' ({tenant.slug}) on DB '{db_name}' ---")
            try:
                result = ShopCatalogSyncService.sync_to_database(
                    db_alias=db_alias,
                    products_data=live_products,
                )
                self.stdout.write(self.style.SUCCESS(
                    f"Sync complete! Created: {result['created']} | Updated: {result['updated']} | Total Synced: {result['total']}"
                ))
                for p in result['products']:
                    self.stdout.write(f"  * [{p['category']}] {p['name']} - INR {p['price']:.2f} (Stock: {p['stock']})")
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"Error syncing tenant '{tenant.slug}': {e}"))

        self.stdout.write(self.style.SUCCESS("\nAll catalog synchronization tasks completed successfully!"))
