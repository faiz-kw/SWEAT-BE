"""
Management command to safely rename UAT test-user emails
from  *@sweat-uat.local  ->  *@sweat.com

Usage (dry-run first, ALWAYS):
    python manage.py rename_uat_emails --dry-run
    python manage.py rename_uat_emails

Steps:
  1. Loads all ACTIVE TenantDataSources from Master DB.
  2. Dynamically registers each tenant DB connection via the same
     _register_tenant_connection() used by TenantMiddleware.
  3. Finds users WHERE email LIKE '%@sweat-uat.local'.
  4. Renames email (and username if it also contains the old domain).
  5. Each tenant is wrapped in its own atomic transaction.
  6. Prints a clear before/after summary at the end.
"""

from django.core.management.base import BaseCommand
from django.db import connections, transaction


OLD_DOMAIN = "@sweat-uat.local"
NEW_DOMAIN = "@sweat.com"


class Command(BaseCommand):
    help = "Rename UAT test-user emails: @sweat-uat.local -> @sweat.com"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Preview changes without writing to the database.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        if dry_run:
            self.stdout.write(self.style.WARNING("WARNING: DRY-RUN mode -- no changes will be saved.\n"))
        else:
            self.stdout.write(self.style.ERROR("LIVE mode -- changes WILL be committed.\n"))

        from config.tenant_middleware import _register_tenant_connection
        from apps.master.models_infra import TenantDataSource
        from config.routers import build_tenant_db_alias

        data_sources = (
            TenantDataSource.objects
            .using("default")
            .select_related("tenant")
            .filter(status="ACTIVE")
        )

        if not data_sources.exists():
            self.stdout.write(self.style.WARNING("No ACTIVE tenant data sources found. Exiting."))
            return

        total_updated = 0
        total_skipped = 0
        tenant_summary = []

        for ds in data_sources:
            db_alias = build_tenant_db_alias(ds.tenant_id)
            tenant_name = ds.tenant.name if ds.tenant else db_alias

            self.stdout.write(f"\n-- Tenant: {tenant_name} ({db_alias})")

            try:
                # Dynamically register the tenant DB alias
                _register_tenant_connection(alias=db_alias, db_name=ds.db_name, data_source=ds)
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f"  [ERROR] Could not register DB: {exc}"))
                total_skipped += 1
                tenant_summary.append((tenant_name, "ERROR"))
                continue

            try:
                # Raw SQL bypasses the TenantRouter thread-local requirement
                with connections[db_alias].cursor() as cursor:
                    cursor.execute(
                        "SELECT id, email, username FROM users WHERE email LIKE %s",
                        [f"%{OLD_DOMAIN}"],
                    )
                    rows = cursor.fetchall()

                if not rows:
                    self.stdout.write("  No UAT emails found.")
                    tenant_summary.append((tenant_name, 0))
                    continue

                count = 0
                for row_id, old_email, old_username in rows:
                    new_email = old_email.replace(OLD_DOMAIN, NEW_DOMAIN)
                    new_username = (
                        old_username.replace(OLD_DOMAIN, NEW_DOMAIN)
                        if old_username and OLD_DOMAIN in old_username
                        else old_username
                    )

                    suffix = ""
                    if old_username and old_username != new_username:
                        suffix = f"  (username: {old_username} -> {new_username})"

                    action = "[DRY-RUN] Would update" if dry_run else "Updating"
                    self.stdout.write(f"  {action}: {old_email} -> {new_email}{suffix}")

                    if not dry_run:
                        with transaction.atomic(using=db_alias):
                            with connections[db_alias].cursor() as cursor:
                                cursor.execute(
                                    "UPDATE users SET email = %s, username = %s WHERE id = %s",
                                    [new_email, new_username, str(row_id)],
                                )
                    count += 1

                total_updated += count
                tenant_summary.append((tenant_name, count))

            except Exception as exc:
                self.stdout.write(self.style.ERROR(f"  [ERROR] Query/update failed: {exc}"))
                total_skipped += 1
                tenant_summary.append((tenant_name, "ERROR"))

        # -- Summary --------------------------------------------------------
        self.stdout.write("\n" + "=" * 60)
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY-RUN SUMMARY (nothing was changed)"))
        else:
            self.stdout.write(self.style.SUCCESS("LIVE UPDATE SUMMARY"))
        self.stdout.write("=" * 60)

        for tenant_name, count in tenant_summary:
            if count == "ERROR":
                status_str = "ERROR"
            elif count == 0:
                status_str = "0 UAT users found"
            else:
                status_str = f"{count} user(s) updated"
            self.stdout.write(f"  {tenant_name}: {status_str}")

        self.stdout.write("=" * 60)
        label = "to update" if dry_run else "updated"
        self.stdout.write(f"  Total rows {label}: {total_updated}")
        if total_skipped:
            self.stdout.write(f"  Tenants skipped/errored: {total_skipped}")

        if dry_run and total_updated > 0:
            self.stdout.write(
                self.style.SUCCESS(
                    "\nDry-run looks good. Run WITHOUT --dry-run to apply changes."
                )
            )
