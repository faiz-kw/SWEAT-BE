"""
Django Management Command: cleanup_expired_retail_reservations
Finds and cancels abandoned retail orders whose 15-minute stock reservations have expired.

Usage:
    python manage.py cleanup_expired_retail_reservations
    python manage.py cleanup_expired_retail_reservations --tenant=sweat
    python manage.py cleanup_expired_retail_reservations --all
"""

import sys
import logging
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from config.tenant_middleware import _register_tenant_connection
from config.routers import build_tenant_db_alias, set_tenant_db_alias, get_tenant_db_alias
from apps.tenant_core.models_inventory import StockReservation
from apps.tenant_core.services_retail import RetailCommerceService

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Cancels abandoned retail orders and releases expired stock reservations.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--tenant',
            type=str,
            default='sweat',
            help='Specific tenant slug to process (defaults to "sweat"). Use --all for all tenants.',
        )
        parser.add_argument(
            '--all',
            action='store_true',
            help='Clean up expired reservations across all active tenant databases.',
        )

    def handle(self, *args, **options):
        process_all = options.get('all', False)
        tenant_slug = options.get('tenant')

        qs = Tenant.objects.using('default').filter(status='ACTIVE')
        if not process_all:
            qs = qs.filter(slug=tenant_slug)

        tenants = list(qs)
        if not tenants:
            self.stdout.write(self.style.WARNING(f"No active tenants found matching '{tenant_slug}'."))
            return

        now = timezone.now()
        total_orders_cancelled = 0
        total_reservations_released = 0

        for tenant in tenants:
            ds = TenantDataSource.objects.using('default').filter(tenant=tenant, status='ACTIVE').first()
            if not ds:
                continue

            db_name = ds.database_name or ds.db_name
            if 'test' in sys.argv and 'tenant_test' in settings.DATABASES:
                db_alias = 'tenant_test'
            else:
                db_alias = build_tenant_db_alias(tenant.id)
            _register_tenant_connection(db_alias, db_name, data_source=ds, tenant_id=tenant.id)


            prev_alias = get_tenant_db_alias()
            set_tenant_db_alias(db_alias)
            try:
                # Find expired pending reservations
                expired_reservations = list(
                    StockReservation.objects.using(db_alias)
                    .filter(status='PENDING', expires_at__lte=now)
                    .select_related('order')
                )

                if not expired_reservations:
                    self.stdout.write(f"Tenant '{tenant.slug}': 0 expired reservations found.")
                    continue

                # Distinct order IDs that have expired reservations
                order_ids = list({res.order_id for res in expired_reservations if res.order_id})
                self.stdout.write(
                    f"Tenant '{tenant.slug}': Found {len(expired_reservations)} expired reservation(s) "
                    f"across {len(order_ids)} order(s). Releasing..."
                )

                for order_id in order_ids:
                    try:
                        res_dict = RetailCommerceService.release_retail_reservation(
                            order_id=order_id,
                            reason='EXPIRED',
                            db_alias=db_alias,
                        )
                        total_orders_cancelled += 1
                        total_reservations_released += res_dict.get('released_count', 0)
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"  [CANCELLED] Order {res_dict.get('order_number')} — released {res_dict.get('released_count')} items."
                            )
                        )
                    except Exception as err:
                        self.stderr.write(
                            self.style.ERROR(f"  [ERROR] Failed to release order {order_id}: {err}")
                        )
            finally:
                set_tenant_db_alias(prev_alias)

        self.stdout.write(
            self.style.SUCCESS(
                f"\nCleanup complete: {total_orders_cancelled} abandoned orders cancelled, "
                f"{total_reservations_released} stock reservations released."
            )
        )
