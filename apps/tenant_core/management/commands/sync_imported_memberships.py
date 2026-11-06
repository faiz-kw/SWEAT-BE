"""
Django Management Command: sync_imported_memberships

Scans tenant databases for imported PAID orders (NEW_MEMBERSHIP, RENEWAL, EXTENSION)
that were inserted directly via data dump without executing Django's membership
lifecycle provisioning, and safely creates their official Membership, ContractSnapshot,
and session Entitlements in bulk.

Usage:
    # 1. Preview changes (safe dry-run, no database writes)
    python manage.py sync_imported_memberships --tenant=sweat --dry-run

    # 2. Sync all paid orders for tenant 'sweat'
    python manage.py sync_imported_memberships --tenant=sweat

    # 3. Sync a specific member or order
    python manage.py sync_imported_memberships --tenant=sweat --member-number=MEM-109919
    python manage.py sync_imported_memberships --tenant=sweat --order-number=ORD-848688

    # 4. Control start date ('order-date' or 'today')
    python manage.py sync_imported_memberships --tenant=sweat --start-date=today
"""

import sys
import json
import logging
from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction

from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from config.tenant_middleware import _register_tenant_connection
from config.routers import build_tenant_db_alias, set_tenant_db_alias, get_tenant_db_alias

logger = logging.getLogger(__name__)


def calculate_expected_end_date(start_date: date, duration_value: int, duration_unit: str) -> date:
    dur_unit_norm = str(duration_unit or 'MONTH').upper().rstrip('S')
    val = int(duration_value or 1)
    if dur_unit_norm == 'DAY':
        return start_date + timedelta(days=val)
    elif dur_unit_norm == 'WEEK':
        return start_date + timedelta(weeks=val)
    elif dur_unit_norm == 'MONTH':
        return start_date + timedelta(days=val * 30)
    elif dur_unit_norm == 'YEAR':
        return start_date + timedelta(days=val * 365)
    return start_date + timedelta(days=30)


class Command(BaseCommand):
    help = 'Syncs imported paid orders from data dumps into active Memberships and Entitlements.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--tenant',
            type=str,
            default='sweat',
            help='Specific tenant slug or UUID to process (defaults to "sweat").',
        )
        parser.add_argument(
            '--all',
            action='store_true',
            help='Process across all active tenant databases.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Preview all members and packages that would be activated without saving changes.',
        )
        parser.add_argument(
            '--member-number',
            type=str,
            help='Filter to a single member number (e.g., MEM-109919).',
        )
        parser.add_argument(
            '--order-number',
            type=str,
            help='Filter to a single order number (e.g., ORD-848688).',
        )
        parser.add_argument(
            '--start-date',
            type=str,
            default='order-date',
            choices=['order-date', 'today'],
            help='Start date for new memberships: "order-date" (default) or "today".',
        )

    def handle(self, *args, **options):
        dry_run = options.get('dry_run', False)
        process_all = options.get('all', False)
        tenant_identifier = options.get('tenant')
        member_number = options.get('member_number')
        order_number = options.get('order_number')
        start_date_mode = options.get('start_date', 'order-date')

        if dry_run:
            self.stdout.write(self.style.WARNING("=== RUNNING IN DRY-RUN MODE (No database writes) ===\n"))

        # Resolve target tenants
        qs = Tenant.objects.using('default').filter(status='ACTIVE')
        if not process_all:
            if '-' in tenant_identifier and len(tenant_identifier) == 36:
                qs = qs.filter(id=tenant_identifier)
            else:
                qs = qs.filter(slug=tenant_identifier)

        tenants = list(qs)
        if not tenants:
            self.stdout.write(self.style.ERROR(f"No active tenants found matching '{tenant_identifier}'."))
            return

        total_activated_memberships = 0
        total_extended_memberships = 0
        total_skipped = 0

        for tenant in tenants:
            ds = TenantDataSource.objects.using('default').filter(tenant=tenant, status='ACTIVE').first()
            if not ds:
                self.stdout.write(self.style.WARNING(f"Tenant '{tenant.slug}' has no active TenantDataSource. Skipping."))
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
                self.stdout.write(self.style.MIGRATE_HEADING(f"\nProcessing Tenant: {tenant.name} ({tenant.slug}) [DB: {db_alias}]"))
                act_cnt, ext_cnt, skp_cnt = self._sync_tenant(
                    db_alias=db_alias,
                    tenant=tenant,
                    dry_run=dry_run,
                    member_number=member_number,
                    order_number=order_number,
                    start_date_mode=start_date_mode,
                )
                total_activated_memberships += act_cnt
                total_extended_memberships += ext_cnt
                total_skipped += skp_cnt
            finally:
                if prev_alias:
                    set_tenant_db_alias(prev_alias)

        self.stdout.write(self.style.SUCCESS("\n" + "=" * 60))
        self.stdout.write(self.style.SUCCESS(
            f"SUMMARY: Activated: {total_activated_memberships} | Extended: {total_extended_memberships} | Skipped/Already Active: {total_skipped}"
        ))
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN COMPLETE: Re-run without --dry-run to commit changes."))
        self.stdout.write(self.style.SUCCESS("=" * 60 + "\n"))

    def _sync_tenant(self, db_alias, tenant, dry_run, member_number, order_number, start_date_mode):
        from django.db.models import Q
        from apps.tenant_core.models_commerce import Order, OrderItem
        from apps.tenant_core.models_memberships import Membership, MembershipExtension, Package, PackageVersion
        from apps.tenant_core.models_workforce import UserProfile
        from apps.tenant_core.services_memberships import MembershipLifecycleService

        activated_count = 0
        extended_count = 0
        skipped_count = 0

        # Query membership/extension relevant orders with status='PAID'
        orders_qs = (
            Order.objects.using(db_alias)
            .filter(status='PAID')
            .filter(
                Q(order_type__in=['NEW_MEMBERSHIP', 'RENEWAL', 'EXTENSION'])
                | Q(items__item_type='PACKAGE')
                | Q(items__package__isnull=False)
            )
            .distinct()
            .select_related('user_profile', 'user_profile__user', 'branch')
            .prefetch_related('items', 'items__package', 'items__package_version', 'items__package_price')
            .order_by('created_at')
        )

        if order_number:
            orders_qs = orders_qs.filter(order_number=order_number)

        if member_number:
            orders_qs = orders_qs.filter(user_profile__member_number=member_number)

        orders = list(orders_qs)
        self.stdout.write(f"Found {len(orders)} relevant PAID membership/extension order(s) in {tenant.slug}.")

        for order in orders:
            profile = order.user_profile
            if not profile:
                continue

            mem_num_display = profile.member_number or str(profile.id)[:8]
            fn = getattr(profile, 'first_name_snapshot', '') or ''
            ln = getattr(profile, 'last_name_snapshot', '') or ''
            full_n = f"{fn} {ln}".strip()
            if not full_n and getattr(profile, 'user', None):
                full_n = getattr(profile.user, 'display_name', '') or getattr(profile.user, 'email', '')
            user_name_display = full_n or 'Member'

            # -------------------------------------------------------------
            # Case 1: Package Orders (NEW_MEMBERSHIP, RENEWAL, or line item with package)
            # -------------------------------------------------------------
            package_items = [item for item in order.items.all() if item.item_type == 'PACKAGE' or item.package_id]
            for p_item in package_items:
                # Check idempotency: does a membership already exist for this order item or order?
                existing_m = (
                    Membership.objects.using(db_alias)
                    .filter(source_order_item=p_item)
                    .first()
                )
                if not existing_m:
                    existing_m = (
                        Membership.objects.using(db_alias)
                        .filter(source_order=order, package_id=p_item.package_id)
                        .first()
                    )

                if existing_m and existing_m.status == 'ACTIVE':
                    skipped_count += 1
                    continue

                # Ensure package and version exist
                package = p_item.package
                package_ver = p_item.package_version
                if not package and p_item.package_id:
                    package = Package.objects.using(db_alias).filter(id=p_item.package_id).first()
                if package and not package_ver:
                    package_ver = package.versions.using(db_alias).filter(status='ACTIVE').order_by('-version_number').first()

                if not package or not package_ver:
                    self.stdout.write(self.style.WARNING(
                        f"  [SKIP] Order {order.order_number} for {mem_num_display} has missing package/version."
                    ))
                    skipped_count += 1
                    continue

                # Compute start and expected end date
                today = timezone.now().date()
                if start_date_mode == 'today':
                    calc_start_date = today
                else:
                    calc_start_date = order.created_at.date() if order.created_at else today

                pkg_name_display = package.name
                duration_val = package_ver.duration_value or 1
                duration_unit = package_ver.duration_unit or 'MONTH'
                calc_end_date = calculate_expected_end_date(calc_start_date, duration_val, duration_unit)

                # STRICT VALIDITY GUARD:
                # If package validity expired in the past (e.g. 2025 or earlier in 2026), DO NOT activate as an active plan!
                if calc_end_date < today and start_date_mode != 'today':
                    skipped_count += 1
                    continue

                days_remaining = (calc_end_date - today).days

                if dry_run:
                    self.stdout.write(self.style.SUCCESS(
                        f"  [DRY-RUN WOULD ACTIVATE] Member: {mem_num_display} ({user_name_display}) | "
                        f"Order: {order.order_number} (₹{order.total_amount}) | "
                        f"Plan: {pkg_name_display} ({duration_val} {duration_unit}) | "
                        f"Validity: {calc_start_date} to {calc_end_date} ({days_remaining} days remaining)"
                    ))
                    activated_count += 1
                else:
                    try:
                        # Backfill package & version onto item if null
                        if not p_item.package:
                            p_item.package = package
                            p_item.package_version = package_ver
                            p_item.save(using=db_alias, update_fields=['package', 'package_version'])

                        membership = MembershipLifecycleService.activate_membership_from_order(
                            order=order,
                            order_item=p_item,
                            start_date=calc_start_date,
                            db_alias=db_alias,
                        )
                        self.stdout.write(self.style.SUCCESS(
                            f"  [OK ACTIVATED] Member: {mem_num_display} ({user_name_display}) | "
                            f"Order: {order.order_number} -> Membership: {membership.membership_number} | "
                            f"Valid: {membership.start_date} to {membership.end_date} ({days_remaining} days remaining)"
                        ))
                        activated_count += 1
                    except Exception as e:
                        self.stdout.write(self.style.ERROR(
                            f"  [ERROR] Failed to activate order {order.order_number} for {mem_num_display}: {e}"
                        ))

            # -------------------------------------------------------------
            # Case 2: Extension Orders
            # -------------------------------------------------------------
            is_extension_order = order.order_type == 'EXTENSION' or any(
                item.item_type == 'EXTENSION' for item in order.items.all()
            )
            if is_extension_order:
                # Find the member's target membership
                target_m = (
                    Membership.objects.using(db_alias)
                    .filter(user_profile=profile)
                    .order_by('-created_at')
                    .first()
                )
                if not target_m:
                    self.stdout.write(self.style.WARNING(
                        f"  [SKIP EXTENSION] No membership found for member {mem_num_display} on order {order.order_number}."
                    ))
                    continue

                # Check if extension already recorded for this order
                ext_exists = (
                    MembershipExtension.objects.using(db_alias)
                    .filter(membership=target_m, created_at__gte=order.created_at - timedelta(hours=1))
                    .exists()
                )
                if ext_exists and target_m.status == 'ACTIVE':
                    skipped_count += 1
                    continue

                # Parse days from order notes or default
                ext_days = 30
                if order.notes:
                    try:
                        meta = json.loads(order.notes) if order.notes.startswith('{') else {}
                        if 'days' in meta:
                            ext_days = int(meta['days'])
                    except Exception:
                        pass

                # Check if extension would actually be valid today
                today = timezone.now().date()
                current_end = target_m.end_date or (order.created_at.date() if order.created_at else today)
                calc_new_end = current_end + timedelta(days=ext_days)
                if calc_new_end < today and start_date_mode != 'today':
                    skipped_count += 1
                    continue

                if dry_run:
                    self.stdout.write(self.style.SUCCESS(
                        f"  [DRY-RUN WOULD EXTEND] Member: {mem_num_display} ({user_name_display}) | "
                        f"Order: {order.order_number} | Membership: {target_m.membership_number} | +{ext_days} days (New Expiry: {calc_new_end})"
                    ))
                    extended_count += 1
                else:
                    try:
                        extended_m = MembershipLifecycleService.extend_membership(
                            membership=target_m,
                            days=ext_days,
                            reason_text=f"Imported extension from Order {order.order_number}",
                            db_alias=db_alias,
                        )
                        self.stdout.write(self.style.SUCCESS(
                            f"  [OK EXTENDED] Member: {mem_num_display} ({user_name_display}) | "
                            f"Order: {order.order_number} -> Membership: {target_m.membership_number} | "
                            f"New Expiry: {target_m.end_date} (Status: ACTIVE)"
                        ))
                        extended_count += 1
                    except Exception as e:
                        self.stdout.write(self.style.ERROR(
                            f"  [ERROR] Failed to extend membership for {mem_num_display}: {e}"
                        ))

        return activated_count, extended_count, skipped_count
