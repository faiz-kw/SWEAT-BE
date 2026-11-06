"""
Django Management Command: import_delta_dump

Parses sweat_oct15_to_nov05_complete_delta.sql and imports the exact 472 memberships,
their session entitlements (allocated & consumed), and validity dates directly into
the tenant database, resolving any subquery mismatches (e.g. ORD-TXN- vs ORD-).

Usage:
    # Dry run (preview):
    python manage.py import_delta_dump --dry-run
    python manage.py import_delta_dump --member-number=MEM-109919 --dry-run

    # Live run:
    python manage.py import_delta_dump
    python manage.py import_delta_dump --file=/var/www/sweat_oct15_to_nov05_complete_delta.sql
"""

import os
import re
import sys
import logging
from datetime import datetime, date
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from django.db.models import Q

from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from config.tenant_middleware import _register_tenant_connection
from config.routers import build_tenant_db_alias, set_tenant_db_alias, get_tenant_db_alias

logger = logging.getLogger(__name__)


def _clean_val(v):
    v = v.strip()
    if v.upper() == 'NULL':
        return None
    if v.startswith("'") and v.endswith("'"):
        return v[1:-1]
    return v


class Command(BaseCommand):
    help = 'Directly imports exact memberships and entitlements from delta SQL dump with real-world dates.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--file',
            type=str,
            default=None,
            help='Path to the delta SQL file (defaults to finding sweat_oct15_to_nov05_complete_delta.sql).',
        )
        parser.add_argument(
            '--tenant',
            type=str,
            default='sweat',
            help='Tenant slug or UUID (defaults to "sweat").',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Preview rows that will be imported without saving to the database.',
        )
        parser.add_argument(
            '--member-number',
            type=str,
            help='Filter import to a specific member number (e.g. MEM-109919).',
        )

    def handle(self, *args, **options):
        dry_run = options.get('dry_run', False)
        tenant_identifier = options.get('tenant', 'sweat')
        sql_file = options.get('file')
        target_member_number = options.get('member_number')

        # Find SQL file
        file_path = self._locate_file(sql_file)
        if not file_path:
            self.stdout.write(self.style.ERROR(
                "Could not find 'sweat_oct15_to_nov05_complete_delta.sql'.\n"
                "Please specify path via: python manage.py import_delta_dump --file=/path/to/file.sql"
            ))
            return

        self.stdout.write(self.style.SUCCESS(f"Using delta dump file: {file_path}"))
        if dry_run:
            self.stdout.write(self.style.WARNING("=== RUNNING IN DRY-RUN MODE (No database writes) ===\n"))

        # Resolve tenant & DB connection
        tenant = Tenant.objects.using('default').filter(status='ACTIVE')
        if '-' in tenant_identifier and len(tenant_identifier) == 36:
            tenant = tenant.filter(id=tenant_identifier).first()
        else:
            tenant = tenant.filter(slug=tenant_identifier).first()

        if not tenant:
            self.stdout.write(self.style.ERROR(f"Tenant '{tenant_identifier}' not found."))
            return

        ds = TenantDataSource.objects.using('default').filter(tenant=tenant, status='ACTIVE').first()
        if not ds:
            self.stdout.write(self.style.ERROR(f"No active DataSource for tenant '{tenant.slug}'."))
            return

        db_name = ds.database_name or ds.db_name
        if 'test' in sys.argv and 'tenant_test' in settings.DATABASES:
            db_alias = 'tenant_test'
        else:
            db_alias = build_tenant_db_alias(tenant.id)

        _register_tenant_connection(db_alias, db_name, data_source=ds, tenant_id=tenant.id)
        prev_alias = get_tenant_db_alias()
        set_tenant_db_alias(db_alias)

        try:
            self._import_from_sql(
                file_path=file_path,
                db_alias=db_alias,
                tenant=tenant,
                dry_run=dry_run,
                target_member_number=target_member_number,
            )
        finally:
            if prev_alias:
                set_tenant_db_alias(prev_alias)

    def _locate_file(self, specified_path):
        candidates = []
        if specified_path:
            candidates.append(specified_path)
        candidates.extend([
            'sweat_oct15_to_nov05_complete_delta.sql',
            '../sweat_oct15_to_nov05_complete_delta.sql',
            'Downloads/sweat_oct15_to_nov05_complete_delta.sql',
            '/var/www/sweat_oct15_to_nov05_complete_delta.sql',
            '/var/www/SWEAT-BE/sweat_oct15_to_nov05_complete_delta.sql',
            '/root/Downloads/sweat_oct15_to_nov05_complete_delta.sql',
            os.path.expanduser('~/Downloads/sweat_oct15_to_nov05_complete_delta.sql'),
            r'C:\Users\Masheera\Downloads\sweat_oct15_to_nov05_complete_delta.sql',
        ])
        for c in candidates:
            if os.path.exists(c):
                return os.path.abspath(c)
        return None

    def _import_from_sql(self, file_path, db_alias, tenant, dry_run, target_member_number):
        from apps.tenant_core.models_workforce import UserProfile
        from apps.tenant_core.models_commerce import Order, OrderItem
        from apps.tenant_core.models_memberships import (
            Membership, MembershipEntitlement, MembershipContractSnapshot,
            Package, PackageVersion, PackagePrice
        )
        from apps.tenant_core.models_org import Branch

        self.stdout.write("Reading SQL file and extracting membership & entitlement rows...")
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()

        # Build local lookup caches
        user_profiles_cache = {}
        for p in UserProfile.objects.using(db_alias).all():
            if p.legacy_reference:
                user_profiles_cache[p.legacy_reference.strip().lower()] = p
            if p.member_number:
                user_profiles_cache[p.member_number.strip().lower()] = p
                # Handle MEM-109919 -> 109919
                num_only = p.member_number.replace('MEM-', '').strip().lower()
                user_profiles_cache[f"user:{num_only}"] = p
                user_profiles_cache[num_only] = p

        orders_cache = {}
        for o in Order.objects.using(db_alias).all():
            orders_cache[o.order_number.strip().upper()] = o
            # Also map ORD-848688 -> ORD-TXN-48688 or ending digits
            digits = re.sub(r'\D', '', o.order_number)
            if digits:
                orders_cache[digits] = o

        branches_cache = {str(b.id): b for b in Branch.objects.using(db_alias).all()}

        # -------------------------------------------------------------
        # STEP 1: Parse and insert Memberships
        # -------------------------------------------------------------
        membership_lines = []
        in_m = False
        for line in lines:
            if 'INSERT INTO memberships (' in line:
                in_m = True
                continue
            if in_m:
                st = line.strip()
                if st.startswith('('):
                    membership_lines.append(st.rstrip(',;'))
                if st.endswith(';'):
                    in_m = False

        self.stdout.write(f"Found {len(membership_lines)} membership rows in SQL file.")
        created_memberships = 0
        updated_memberships = 0
        skipped_memberships = 0
        legacy_to_membership = {}

        for row_str in membership_lines:
            row_inner = row_str[1:-1] if row_str.startswith('(') and row_str.endswith(')') else row_str.lstrip('(').rstrip(')')

            # Extract user_profile legacy_reference
            user_ref_m = re.search(r"legacy_reference\s*=\s*'([^']+)'", row_inner)
            user_ref = user_ref_m.group(1).strip().lower() if user_ref_m else None
            profile = user_profiles_cache.get(user_ref) if user_ref else None

            if not profile and user_ref:
                # Try suffix only
                num_only = user_ref.split(':')[-1]
                profile = user_profiles_cache.get(f"user:{num_only}") or user_profiles_cache.get(f"mem-{num_only}") or user_profiles_cache.get(num_only)

            # Filter if target_member_number specified
            if target_member_number:
                if not profile or profile.member_number != target_member_number:
                    continue

            # Extract row parts (handling strings and subqueries)
            # Match fields before the subqueries
            fields_match = re.match(
                r"^'([^']+)',\s*'([^']+)',\s*'([^']+)',\s*'([^']+)',\s*'([^']+)',\s*'([^']+)',\s*([^,]+),\s*'([^']+)',\s*'([^']+)',\s*'([^']+)',\s*'([^']+)',\s*'([^']+)',\s*'([^']+)',\s*'([^']+)',\s*'([^']+)',\s*'([^']+)'",
                row_inner
            )
            if not fields_match:
                continue

            (
                m_id, mem_num, start_date_str, end_date_str, status_val, activated_at_str,
                cancelled_at_raw, legacy_ref, created_at_str, updated_at_str, home_branch_id,
                package_id, package_price_id, package_version_id, program_id, purchase_branch_id
            ) = fields_match.groups()

            start_d = datetime.strptime(start_date_str[:10], '%Y-%m-%d').date()
            end_d = datetime.strptime(end_date_str[:10], '%Y-%m-%d').date()

            # Extract order number from subquery
            order_m = re.search(r"order_number\s*=\s*'([^']+)'", row_inner)
            order_num_str = order_m.group(1).strip() if order_m else None
            matched_order = None
            if order_num_str:
                matched_order = orders_cache.get(order_num_str.upper())
                if not matched_order:
                    # Match digits: e.g. ORD-TXN-48688 -> 48688 -> match ORD-848688
                    digits = re.sub(r'\D', '', order_num_str)
                    matched_order = orders_cache.get(digits)

            matched_item = None
            if matched_order:
                matched_item = matched_order.items.using(db_alias).filter(item_type='PACKAGE').first()

            if not profile:
                self.stdout.write(self.style.WARNING(f"  [SKIP] Could not resolve member for legacy_ref: {user_ref} (Plan: {mem_num})"))
                skipped_memberships += 1
                continue

            if dry_run:
                self.stdout.write(self.style.SUCCESS(
                    f"  [DRY-RUN WOULD IMPORT] Member: {profile.member_number} ({profile.first_name_snapshot or ''}) | "
                    f"Plan #{mem_num} | Dates: {start_d} to {end_d} | Status: {status_val}"
                ))
                created_memberships += 1
                legacy_to_membership[legacy_ref] = profile
                legacy_to_membership[mem_num] = profile
            else:
                home_br = branches_cache.get(home_branch_id) or profile.preferred_branch
                purch_br = branches_cache.get(purchase_branch_id) or home_br

                # Insert or update
                existing = Membership.objects.using(db_alias).filter(Q(id=m_id) | Q(membership_number=mem_num)).first()
                target_m = None
                if existing:
                    existing.user_profile = profile
                    existing.legacy_reference = legacy_ref
                    existing.start_date = start_d
                    existing.end_date = end_d
                    existing.status = status_val
                    up_fields = ['user_profile', 'legacy_reference', 'start_date', 'end_date', 'status', 'updated_at']

                    if matched_order:
                        existing.source_order = matched_order
                        existing.source_order_item = matched_item
                        up_fields.extend(['source_order', 'source_order_item'])
                    if not existing.home_branch_id and home_br:
                        existing.home_branch = home_br
                        up_fields.append('home_branch')
                    if not existing.purchase_branch_id and purch_br:
                        existing.purchase_branch = purch_br
                        up_fields.append('purchase_branch')
                    if not existing.package_id and package_id:
                        existing.package_id = package_id
                        up_fields.append('package_id')
                    if not existing.package_version_id and package_version_id:
                        existing.package_version_id = package_version_id
                        up_fields.append('package_version_id')
                    if not existing.program_id and program_id:
                        existing.program_id = program_id
                        up_fields.append('program_id')

                    existing.save(using=db_alias, update_fields=up_fields)
                    legacy_to_membership[legacy_ref] = existing
                    legacy_to_membership[mem_num] = existing
                    target_m = existing
                    updated_memberships += 1
                else:
                    m_obj = Membership(
                        id=m_id,
                        membership_number=mem_num,
                        user_profile=profile,
                        program_id=program_id,
                        package_id=package_id,
                        package_version_id=package_version_id,
                        package_price_id=package_price_id,
                        source_order=matched_order,
                        source_order_item=matched_item,
                        home_branch=home_br,
                        purchase_branch=purch_br,
                        start_date=start_d,
                        end_date=end_d,
                        status=status_val,
                        legacy_reference=legacy_ref,
                        activated_at=datetime.strptime(activated_at_str[:19], '%Y-%m-%d %H:%M:%S') if activated_at_str else timezone.now(),
                    )
                    m_obj.save(using=db_alias)
                    legacy_to_membership[legacy_ref] = m_obj
                    legacy_to_membership[mem_num] = m_obj
                    target_m = m_obj
                    created_memberships += 1

                if target_m:
                    order_amt = matched_order.paid_amount if (matched_order and matched_order.paid_amount) else (matched_order.total_amount if matched_order else Decimal('11000'))
                    disc_amt = matched_order.discount_amount if (matched_order and matched_order.discount_amount) else Decimal('0')
                    cat_amt = matched_order.subtotal if (matched_order and matched_order.subtotal) else order_amt
                    try:
                        MembershipContractSnapshot.objects.using(db_alias).update_or_create(
                            membership=target_m,
                            defaults={
                                'package_name_snapshot': target_m.package.name if target_m.package else 'Sweat Pilates',
                                'purchase_price': order_amt,
                                'catalog_price': cat_amt,
                                'final_amount': order_amt,
                                'discount_amount': disc_amt,
                                'start_date': start_d,
                                'end_date': end_d,
                                'source_order': matched_order,
                            }
                        )
                    except Exception as e:
                        logger.warning(f"Failed to create contract snapshot: {e}")

                self.stdout.write(self.style.SUCCESS(
                    f"  [OK IMPORTED] Member: {profile.member_number} | {mem_num} | {start_d} to {end_d} | Status: {status_val}"
                ))

        # -------------------------------------------------------------
        # STEP 2: Parse and insert Entitlements
        # -------------------------------------------------------------
        entitlement_lines = []
        in_e = False
        for line in lines:
            if 'INSERT INTO membership_entitlements (' in line:
                in_e = True
                continue
            if in_e:
                st = line.strip()
                if st.startswith('('):
                    entitlement_lines.append(st.rstrip(',;'))
                if st.endswith(';'):
                    in_e = False

        self.stdout.write(f"\nFound {len(entitlement_lines)} entitlement rows in SQL file.")
        created_entitlements = 0

        for row_str in entitlement_lines:
            row_inner = row_str[1:-1] if row_str.startswith('(') and row_str.endswith(')') else row_str.lstrip('(').rstrip(')')

            # Extract target membership legacy_ref
            m_ref_match = re.search(r"legacy_reference\s*=\s*'([^']+)'", row_inner)
            m_ref = m_ref_match.group(1).strip() if m_ref_match else None

            # Extract fields
            m_match = re.match(
                r"^'([^']+)',\s*'([^']+)',\s*([^,]+),\s*([^,]+),\s*(\d+),\s*(\d+),\s*([^,]+),\s*'([^']+)',\s*'([^']+)',\s*'([^']+)'",
                row_inner
            )
            if not m_match:
                continue

            (
                ent_id, ent_type, ref_type_raw, ref_id_raw,
                alloc_units, consumed_units, is_unlimited_raw,
                valid_from_str, valid_until_str, ent_status
            ) = m_match.groups()

            # Find target membership
            target_membership = None
            if m_ref:
                target_membership = legacy_to_membership.get(m_ref)
                if not target_membership or isinstance(target_membership, str):
                    target_membership = Membership.objects.using(db_alias).filter(legacy_reference=m_ref).first()
                if not target_membership and 'userplan:' in m_ref:
                    num_str = m_ref.split(':')[-1]
                    try:
                        mem_num_cand = f"MEMSHIP-{int(num_str):06d}"
                        target_membership = Membership.objects.using(db_alias).filter(membership_number=mem_num_cand).first()
                    except ValueError:
                        pass

            if target_member_number:
                if not target_membership:
                    continue
                # Check member number
                mem_num_val = None
                if hasattr(target_membership, 'member_number'):
                    mem_num_val = target_membership.member_number
                elif hasattr(target_membership, 'user_profile') and target_membership.user_profile:
                    mem_num_val = target_membership.user_profile.member_number
                if mem_num_val != target_member_number:
                    continue

            alloc_num = Decimal(str(alloc_units))
            consumed_num = Decimal(str(consumed_units))
            rem_num = max(Decimal('0'), alloc_num - consumed_num)

            if dry_run:
                plan_name_disp = getattr(target_membership, 'membership_number', m_ref or 'Plan')
                self.stdout.write(self.style.SUCCESS(
                    f"  [DRY-RUN WOULD PROVISION SESSIONS] Plan: {plan_name_disp} | Allocated: {alloc_num} | Consumed: {consumed_num} | Remaining: {rem_num}"
                ))
                created_entitlements += 1
            elif target_membership and hasattr(target_membership, 'id'):
                # Upsert entitlement
                ent_obj = MembershipEntitlement.objects.using(db_alias).filter(id=ent_id).first()
                if not ent_obj:
                    ent_obj = MembershipEntitlement(
                        id=ent_id,
                        membership=target_membership,
                        entitlement_type=ent_type,
                        allocated_units=alloc_num,
                        consumed_units=consumed_num,
                        is_unlimited=is_unlimited_raw.upper() == 'TRUE',
                        valid_from=datetime.strptime(valid_from_str[:10], '%Y-%m-%d'),
                        valid_until=datetime.strptime(valid_until_str[:19], '%Y-%m-%d %H:%M:%S') if len(valid_until_str) >= 19 else datetime.strptime(valid_until_str[:10], '%Y-%m-%d'),
                        status=ent_status,
                    )
                else:
                    ent_obj.membership = target_membership
                    ent_obj.allocated_units = alloc_num
                    ent_obj.consumed_units = consumed_num
                    ent_obj.status = ent_status

                ent_obj.save(using=db_alias)
                created_entitlements += 1

        self.stdout.write(self.style.SUCCESS("\n" + "=" * 60))
        self.stdout.write(self.style.SUCCESS(
            f"IMPORT COMPLETE: Created Memberships: {created_memberships} | Updated: {updated_memberships} | Entitlements: {created_entitlements}"
        ))
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN COMPLETE: Re-run without --dry-run to commit changes."))
        self.stdout.write(self.style.SUCCESS("=" * 60 + "\n"))
