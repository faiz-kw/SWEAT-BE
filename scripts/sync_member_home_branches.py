"""
Script: Sync Member Home Branches with Package Purchase Branches
Usage:
    python manage.py shell < scripts/sync_member_home_branches.py
"""
import os
import sys
import django
from django.conf import settings
from django.db import connections, transaction
from config.routers import build_tenant_db_alias, set_tenant_db_alias
from config.tenant_middleware import _register_tenant_connection


def sync_branch_for_alias(alias: str, label: str):
    print(f"\n--- Checking Database: {alias} ({label}) ---")
    try:
        conn = connections[alias]
        with conn.cursor() as cursor:
            # Check if memberships table exists in this database
            cursor.execute("""
                SELECT COUNT(*) FROM information_schema.tables 
                WHERE table_name = 'memberships';
            """)
            if cursor.fetchone()[0] == 0:
                print(f"Skipping {alias}: 'memberships' table not present in this database.")
                return

            print(f"Executing branch alignment on tenant database '{alias}'...")

            # 1. Sync memberships home_branch_id to purchase_branch_id
            cursor.execute("""
                UPDATE memberships
                SET home_branch_id = purchase_branch_id,
                    updated_at = NOW()
                WHERE purchase_branch_id IS NOT NULL
                  AND (home_branch_id IS NULL OR home_branch_id != purchase_branch_id);
            """)
            mems_updated = cursor.rowcount
            print(f"  [1/3] Updated memberships home_branch_id: {mems_updated} records")

            # 2. Update user_profiles.preferred_branch_id to match active/latest package branch
            cursor.execute("""
                WITH latest_member_branch AS (
                    SELECT DISTINCT ON (user_profile_id)
                        user_profile_id,
                        COALESCE(purchase_branch_id, home_branch_id) AS target_branch_id
                    FROM memberships
                    WHERE COALESCE(purchase_branch_id, home_branch_id) IS NOT NULL
                    ORDER BY
                        user_profile_id,
                        (status = 'ACTIVE' AND (end_date >= CURRENT_DATE OR end_date IS NULL)) DESC,
                        (status = 'ACTIVE') DESC,
                        (status = 'FROZEN') DESC,
                        end_date DESC NULLS LAST,
                        created_at DESC
                )
                UPDATE user_profiles up
                SET preferred_branch_id = lmb.target_branch_id,
                    updated_at = NOW()
                FROM latest_member_branch lmb
                WHERE up.id = lmb.user_profile_id
                  AND (up.preferred_branch_id IS NULL OR up.preferred_branch_id != lmb.target_branch_id);
            """)
            profiles_updated = cursor.rowcount
            print(f"  [2/3] Updated user_profiles preferred_branch_id: {profiles_updated} records")

            # 3. Update users.home_branch_id to match user_profiles.preferred_branch_id
            cursor.execute("""
                UPDATE users u
                SET home_branch_id = up.preferred_branch_id,
                    updated_at = NOW()
                FROM user_profiles up
                WHERE up.user_id = u.id
                  AND up.preferred_branch_id IS NOT NULL
                  AND (u.home_branch_id IS NULL OR u.home_branch_id != up.preferred_branch_id);
            """)
            users_updated = cursor.rowcount
            print(f"  [3/3] Updated users home_branch_id: {users_updated} records")

            print(f"Successfully synchronized {alias} ({label})")

    except Exception as e:
        print(f"Error executing on {alias} ({label}): {e}")


def main():
    print("==========================================================")
    print("  Sync Member Home Branches with Package Purchase Branch  ")
    print("==========================================================")

    # 1. Discover tenants from Master DB
    aliases_to_process = []
    try:
        from apps.master.models_tenant import Tenant
        from apps.master.models_infra import TenantDataSource

        tenants = Tenant.objects.using('default').all()
        for t in tenants:
            alias = build_tenant_db_alias(t.id)
            ds = TenantDataSource.objects.using('default').filter(tenant=t).first()
            if ds:
                _register_tenant_connection(alias, db_name=ds.db_name, data_source=ds, tenant_id=t.id)
                aliases_to_process.append((alias, f"Tenant: {t.slug} (DB: {ds.db_name})"))
            else:
                db_name = f"tenant_{t.slug.replace('-', '_')}"
                _register_tenant_connection(alias, db_name=db_name, tenant_id=t.id)
                aliases_to_process.append((alias, f"Tenant: {t.slug} (DB: {db_name})"))
    except Exception as e:
        print(f"Note: Master tenant registry lookup encountered: {e}")

    # 2. Check for configured aliases in settings.DATABASES
    for db_alias in list(settings.DATABASES.keys()):
        if db_alias.startswith('tenant_') and not any(a[0] == db_alias for a in aliases_to_process):
            db_name = settings.DATABASES[db_alias].get('NAME', db_alias)
            aliases_to_process.append((db_alias, f"Configured alias: {db_alias} (DB: {db_name})"))

    # Fallback common default if list is empty
    if not aliases_to_process:
        fallback_alias = 'tenant_sweat_demo'
        if fallback_alias not in settings.DATABASES:
            settings.DATABASES[fallback_alias] = dict(settings.DATABASES['default'])
            settings.DATABASES[fallback_alias]['NAME'] = fallback_alias
        aliases_to_process.append((fallback_alias, f"Fallback alias: {fallback_alias}"))

    print(f"Found {len(aliases_to_process)} tenant database target(s) to process.")

    for alias, label in aliases_to_process:
        set_tenant_db_alias(alias)
        sync_branch_for_alias(alias, label)

    print("\n==========================================================")
    print("  All tenant database branch sync operations finished!    ")
    print("==========================================================")


main()
