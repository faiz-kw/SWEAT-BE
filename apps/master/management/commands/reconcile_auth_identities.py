"""
Management command: reconcile_auth_identities

Scans all Platform users and Tenant users across dedicated databases,
validates and reconciles the Universal Authentication Routing Directory (AuthenticationIdentity)
in the Master database, detects duplicates, stale entries, and missing records.

Usage:
    python manage.py reconcile_auth_identities
    python manage.py reconcile_auth_identities --dry-run
"""

import logging
from django.conf import settings
from django.core.management.base import BaseCommand
from apps.master.models_iam import PlatformUser, AuthenticationIdentity
from apps.master.models_infra import TenantDataSource
from apps.master.services_auth_directory import (
    compute_lookup_hash,
    normalize_identifier,
    infer_identifier_type,
    register_identity,
)
from config.routers import build_tenant_db_alias
from config.tenant_middleware import _register_tenant_connection

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Reconciles the Universal Authentication Directory against Master and Tenant databases.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report reconciliation discrepancies without writing changes to the database.',
        )

    def handle(self, *args, **options):
        dry_run = options.get('dry_run', False)

        self.stdout.write(self.style.NOTICE("=" * 60))
        self.stdout.write(self.style.NOTICE("RECONCILING UNIVERSAL AUTHENTICATION DIRECTORY"))
        if dry_run:
            self.stdout.write(self.style.WARNING("MODE: DRY RUN (Read-Only)"))
        else:
            self.stdout.write(self.style.SUCCESS("MODE: EXECUTE CHANGES"))
        self.stdout.write(self.style.NOTICE("=" * 60))

        # Expected map: (account type, tenant ID, lookup hash) -> record info
        expected = {}
        duplicates = []

        # 1. Scan Platform Users
        platform_users = PlatformUser.objects.using('default').all()
        self.stdout.write(f"Scanning Platform Users ({platform_users.count()} found)...")

        for pu in platform_users:
            pu_status = 'ACTIVE' if (pu.status == 'ACTIVE' and pu.is_active) else ('INVITED' if pu.status == 'INVITED' else 'INACTIVE')
            identifiers = []
            if pu.email:
                identifiers.append((pu.email, 'EMAIL'))
            if getattr(pu, 'username', None):
                identifiers.append((pu.username, 'USERNAME'))

            for ident, id_type in identifiers:
                norm = normalize_identifier(ident)
                h = ('PLATFORM', None, compute_lookup_hash(norm))
                rec = {
                    'identifier': norm,
                    'identifier_type': id_type,
                    'account_type': 'PLATFORM',
                    'subject_id': pu.id,
                    'tenant_id': None,
                    'status': pu_status,
                    'source': f"PlatformUser {pu.email}",
                }
                if h in expected:
                    existing = expected[h]
                    if existing['subject_id'] != pu.id or existing['account_type'] != 'PLATFORM':
                        duplicates.append((norm, existing['source'], rec['source']))
                else:
                    expected[h] = rec

        # 2. Scan Tenant Users
        data_sources = TenantDataSource.objects.using('default').filter(status='ACTIVE').select_related('tenant')
        self.stdout.write(f"Scanning Tenant DataSources ({data_sources.count()} active found)...")

        from apps.tenant_core.models_users import TenantUser

        for ds in data_sources:
            tenant = ds.tenant
            alias = build_tenant_db_alias(tenant.id)
            target_db = ds.database_name or ds.db_name
            try:
                query_alias = alias
                if 'tenant_test' in settings.DATABASES:
                    query_alias = 'tenant_test'
                else:
                    _register_tenant_connection(alias, db_name=target_db, data_source=ds, tenant_id=tenant.id)
                t_users = TenantUser.objects.using(query_alias).all()
                self.stdout.write(f"  Tenant {tenant.slug} ({target_db}): {t_users.count()} users")

                for tu in t_users:
                    tu_status = 'ACTIVE' if (tu.status == 'ACTIVE' and tu.is_login_allowed) else ('INVITED' if tu.status == 'INVITED' else 'INACTIVE')
                    identifiers = []
                    if tu.email:
                        identifiers.append((tu.email, 'EMAIL'))
                    if getattr(tu, 'username', None):
                        identifiers.append((tu.username, 'USERNAME'))

                    for ident, id_type in identifiers:
                        norm = normalize_identifier(ident)
                        h = ('TENANT', tenant.id, compute_lookup_hash(norm))
                        rec = {
                            'identifier': norm,
                            'identifier_type': id_type,
                            'account_type': 'TENANT',
                            'subject_id': tu.id,
                            'tenant_id': tenant.id,
                            'status': tu_status,
                            'source': f"Tenant {tenant.slug} user {tu.email}",
                        }
                        if h in expected:
                            existing = expected[h]
                            if existing['subject_id'] != tu.id or existing['account_type'] != 'TENANT':
                                duplicates.append((norm, existing['source'], rec['source']))
                        else:
                            expected[h] = rec

            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  Failed to query tenant {tenant.slug}: {e}"))

        # 3. Report Duplicates
        if duplicates:
            self.stdout.write(self.style.ERROR(f"\n[WARNING] Found {len(duplicates)} duplicate identifier collisions!"))
            for ident, s1, s2 in duplicates:
                self.stdout.write(self.style.ERROR(f"  Conflict on '{ident}': {s1} vs {s2}"))
        else:
            self.stdout.write(self.style.SUCCESS("\n[OK] Zero global identifier collisions detected."))

        # 4. Compare with existing AuthenticationIdentity records in Master DB
        existing_identities = {
            (ai.account_type, ai.tenant_id, ai.lookup_hash): ai
            for ai in AuthenticationIdentity.objects.using('default').all()
        }
        self.stdout.write(f"\nExisting Directory Records in Master DB: {len(existing_identities)}")

        missing_hashes = set(expected.keys()) - set(existing_identities.keys())
        stale_hashes = set(existing_identities.keys()) - set(expected.keys())

        # Discrepant status/tenant/type
        mismatched_hashes = []
        for h in set(expected.keys()) & set(existing_identities.keys()):
            exp = expected[h]
            act = existing_identities[h]
            if (
                str(act.subject_id) != str(exp['subject_id'])
                or act.account_type != exp['account_type']
                or str(act.tenant_id or '') != str(exp['tenant_id'] or '')
                or act.status != exp['status']
            ):
                mismatched_hashes.append(h)

        self.stdout.write(f"  Missing records to create : {len(missing_hashes)}")
        self.stdout.write(f"  Stale records to remove   : {len(stale_hashes)}")
        self.stdout.write(f"  Mismatched records to sync : {len(mismatched_hashes)}")

        if not dry_run:
            # Apply creations
            for h in missing_hashes:
                exp = expected[h]
                AuthenticationIdentity.objects.using('default').create(
                    lookup_hash=h[2],
                    identifier=exp['identifier'],
                    identifier_type=exp['identifier_type'],
                    account_type=exp['account_type'],
                    subject_id=exp['subject_id'],
                    tenant_id=exp['tenant_id'],
                    status=exp['status'],
                )
                self.stdout.write(f"  [CREATED] {exp['identifier']} ({exp['account_type']}) -> {exp['status']}")

            # Apply updates
            for h in mismatched_hashes:
                exp = expected[h]
                act = existing_identities[h]
                act.identifier = exp['identifier']
                act.identifier_type = exp['identifier_type']
                act.account_type = exp['account_type']
                act.subject_id = exp['subject_id']
                act.tenant_id = exp['tenant_id']
                act.status = exp['status']
                act.save(using='default')
                self.stdout.write(f"  [UPDATED] {exp['identifier']} ({exp['account_type']}) -> {exp['status']}")

            # Clean stale
            for h in stale_hashes:
                act = existing_identities[h]
                self.stdout.write(f"  [REMOVED] {act.identifier} ({act.account_type}) [stale]")
                act.delete()

            self.stdout.write(self.style.SUCCESS("\n[SUCCESS] Universal Authentication Directory is fully reconciled!"))
        else:
            self.stdout.write(self.style.NOTICE("\n[DRY RUN COMPLETE] No database modifications made."))
