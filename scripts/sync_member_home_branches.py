"""
Script: Sync Member Home Branches with Package Purchase Branches
Usage:
    python manage.py shell < scripts/sync_member_home_branches.py
"""
import os
import django
from django.db import transaction, connection
from django.utils import timezone

def run_branch_sync():
    print("=== Starting Member Home Branch Synchronization ===")
    
    with connection.cursor() as cursor:
        # 1. Sync memberships home_branch_id to purchase_branch_id
        cursor.execute("""
            UPDATE memberships
            SET home_branch_id = purchase_branch_id,
                updated_at = NOW()
            WHERE purchase_branch_id IS NOT NULL
              AND (home_branch_id IS NULL OR home_branch_id != purchase_branch_id);
        """)
        mems_updated = cursor.rowcount
        print(f"[1/3] Updated memberships home_branch_id: {mems_updated} records")

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
        print(f"[2/3] Updated user_profiles preferred_branch_id: {profiles_updated} records")

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
        print(f"[3/3] Updated users home_branch_id: {users_updated} records")

    print("=== Member Home Branch Synchronization Completed Successfully ===")

run_branch_sync()
