"""
Management command to migrate tenant databases using the TenantDataSource registry
and multi-tenant database routing architecture.

Usage:
    python manage.py migrate_all_tenants --all
    python manage.py migrate_all_tenants --tenant=cult-fit
    python manage.py migrate_all_tenants --all --dry-run
    python manage.py migrate_all_tenants --tenant=cult-fit --dry-run
"""

import sys
import logging
from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.management import call_command
from django.db import connections
from django.db.migrations.loader import MigrationLoader

from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from config.tenant_middleware import _register_tenant_connection
from config.routers import TenantRouter, build_tenant_db_alias

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Executes tenant_core migrations across dedicated tenant databases with Master DB isolation.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--all',
            action='store_true',
            help='Migrate all active tenant databases',
        )
        parser.add_argument(
            '--tenant',
            type=str,
            help='Slug of specific tenant to migrate',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Inspect and report pending migrations without applying schema changes',
        )
        parser.add_argument(
            '--async',
            dest='is_async',
            action='store_true',
            help='Dispatch tenant migration tasks asynchronously via Celery',
        )

    def handle(self, *args, **options):
        migrate_all = options.get('all')
        tenant_slug = options.get('tenant')
        dry_run = options.get('dry_run', False)
        is_async = options.get('is_async', False)

        if not migrate_all and not tenant_slug:
            self.stderr.write(self.style.ERROR(
                "Error: You must specify either --all or --tenant=<slug>."
            ))
            return

        router = TenantRouter()

        # MASTER DB PROTECTION ASSERTION
        if router.allow_migrate('default', 'tenant_core'):
            self.stderr.write(self.style.ERROR(
                "CRITICAL ERROR: TenantRouter erroneously allowed tenant_core migrations on Master DB ('default'). Aborting."
            ))
            sys.exit(1)

        # Resolve target tenants from Master DB
        qs = Tenant.objects.using('default').all()
        if tenant_slug:
            qs = qs.filter(slug=tenant_slug)
        else:
            qs = qs.filter(status='ACTIVE')

        tenants = list(qs)
        if not tenants:
            self.stdout.write(self.style.WARNING(
                f"No tenants found matching criteria (slug={tenant_slug}, all={migrate_all})."
            ))
            return

        if is_async:
            if dry_run:
                self.stderr.write(self.style.ERROR("Error: --dry-run is not supported with --async execution."))
                return

            self.stdout.write(f"Dispatching async migrations for {len(tenants)} tenant(s) via Celery...")
            from apps.master.tasks import migrate_tenant_async
            dispatched = []
            for tenant in tenants:
                data_source = TenantDataSource.objects.using('default').filter(tenant=tenant).first()
                if not data_source or not data_source.db_name:
                    self.stdout.write(self.style.WARNING(f"[-] SKIPPED: Tenant '{tenant.slug}' — No TenantDataSource configured."))
                    continue
                task = migrate_tenant_async.delay(str(tenant.id))
                self.stdout.write(self.style.SUCCESS(f"[DISPATCHED] Tenant '{tenant.slug}' -> Celery Task ID: {task.id}"))
                dispatched.append({'tenant': tenant.slug, 'task_id': task.id})

            self.stdout.write("\n" + "=" * 60)
            self.stdout.write("ASYNC TENANT MIGRATION DISPATCH COMPLETE")
            self.stdout.write(f"Total Dispatched: {len(dispatched)}")
            self.stdout.write("=" * 60 + "\n")
            return

        self.stdout.write(
            f"Starting multi-tenant migration run (dry_run={dry_run}, tenants_to_process={len(tenants)})..."
        )

        results = {
            'success': [],
            'failed': [],
            'skipped': [],
        }

        for tenant in tenants:
            data_source = TenantDataSource.objects.using('default').filter(tenant=tenant).first()
            if not data_source or not data_source.db_name:
                reason = "No TenantDataSource or db_name configured"
                self.stdout.write(self.style.WARNING(
                    f"[-] SKIPPED: Tenant '{tenant.slug}' — {reason}."
                ))
                results['skipped'].append({'tenant': tenant.slug, 'reason': reason})
                continue

            db_name = data_source.database_name or data_source.db_name
            if 'test' in sys.argv and 'tenant_test' in settings.DATABASES and (db_name in ('fitness_tenant', 'test_fitness_tenant', 'test')):
                db_alias = 'tenant_test'
            else:
                db_alias = build_tenant_db_alias(tenant.id)

            # Strict Master DB protection check
            if db_alias == 'default' or not db_alias.startswith('tenant_'):
                reason = f"Unsafe database alias '{db_alias}' targeting master database"
                self.stderr.write(self.style.ERROR(
                    f"[!] FAILED: Tenant '{tenant.slug}' — {reason}. Aborting tenant migration."
                ))
                results['failed'].append({'tenant': tenant.slug, 'error': reason})
                continue

            if not router.allow_migrate(db_alias, 'tenant_core'):
                reason = f"TenantRouter disallows tenant_core migration for alias '{db_alias}'"
                self.stderr.write(self.style.ERROR(
                    f"[!] FAILED: Tenant '{tenant.slug}' — {reason}."
                ))
                results['failed'].append({'tenant': tenant.slug, 'error': reason})
                continue

            # Dynamically register tenant connection
            _register_tenant_connection(db_alias, data_source.db_name, data_source=data_source, tenant_id=tenant.id)

            if dry_run:
                try:
                    loader = MigrationLoader(connections[db_alias], ignore_no_migrations=True)
                    applied = set(loader.applied_migrations.keys())
                    all_tenant_migs = [
                        node for node in loader.graph.nodes
                        if node[0] == 'tenant_core'
                    ]
                    unapplied = [m for m in all_tenant_migs if m not in applied]

                    status_desc = f"{len(unapplied)} unapplied ({[m[1] for m in unapplied]})" if unapplied else "up to date (0 unapplied)"
                    self.stdout.write(self.style.SUCCESS(
                        f"[DRY-RUN] Tenant '{tenant.slug}' (alias={db_alias}, db={data_source.db_name}): {status_desc}."
                    ))
                    results['success'].append({
                        'tenant': tenant.slug,
                        'alias': db_alias,
                        'unapplied': [m[1] for m in unapplied],
                    })
                except Exception as exc:
                    self.stderr.write(self.style.ERROR(
                        f"[!] DRY-RUN FAILED: Tenant '{tenant.slug}' (alias={db_alias}): {exc}"
                    ))
                    results['failed'].append({'tenant': tenant.slug, 'error': str(exc)})
            else:
                self.stdout.write(f"Migrating tenant '{tenant.slug}' (alias={db_alias})...")
                from config.routers import set_tenant_db_alias, get_tenant_db_alias
                old_alias = get_tenant_db_alias()
                set_tenant_db_alias(db_alias)
                try:
                    call_command(
                        'migrate',
                        'tenant_core',
                        database=db_alias,
                        interactive=False,
                        verbosity=1,
                    )
                    self.stdout.write(self.style.SUCCESS(
                        f"[SUCCESS] Tenant '{tenant.slug}' (alias={db_alias}, db={data_source.db_name}) successfully migrated."
                    ))
                    results['success'].append({'tenant': tenant.slug, 'alias': db_alias})
                except Exception as exc:
                    self.stderr.write(self.style.ERROR(
                        f"[FAILED] Tenant '{tenant.slug}' (alias={db_alias}) migration error: {exc}"
                    ))
                    results['failed'].append({'tenant': tenant.slug, 'error': str(exc)})
                finally:
                    set_tenant_db_alias(old_alias)

        # SUMMARY REPORT
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(f"MULTI-TENANT MIGRATION REPORT (dry_run={dry_run})")
        self.stdout.write(f"Total Processed : {len(tenants)}")
        self.stdout.write(f"Succeeded       : {len(results['success'])}")
        self.stdout.write(f"Failed          : {len(results['failed'])}")
        self.stdout.write(f"Skipped         : {len(results['skipped'])}")
        self.stdout.write("=" * 60 + "\n")

        if results['failed']:
            sys.exit(1)
