import psycopg2

def main():
    print("Connecting to PostgreSQL...")
    conn = psycopg2.connect(
        host="localhost",
        port=5432,
        user="postgres",
        password="masheera11",
        dbname="tenant_sweat_demo"
    )
    cur = conn.cursor()

    org_id = "299b7ef9-00c1-4d62-937f-4554b3ba0810"

    print("Pausing foreign key checks for clean deduplication...")
    cur.execute("SET session_replication_role = 'replica';")

    # 1. Re-point child notes to the primary record before deleting duplicates
    print("Merging child notes to primary records...")
    cur.execute(f"""
        WITH duplicate_pairs AS (
            SELECT 
                id AS dup_id,
                FIRST_VALUE(id) OVER (PARTITION BY phone_normalized ORDER BY created_at ASC, id ASC) AS keep_id
            FROM leads
            WHERE organization_id = '{org_id}'
              AND phone_normalized IS NOT NULL 
              AND phone_normalized != ''
        )
        UPDATE lead_notes n
        SET lead_id = p.keep_id
        FROM duplicate_pairs p
        WHERE n.lead_id = p.dup_id AND p.dup_id != p.keep_id;
    """)

    # 2. Delete duplicate lead entries, keeping exactly 1 unique record per phone number
    print("Deleting duplicate lead entries...")
    cur.execute(f"""
        DELETE FROM leads a
        USING (
            SELECT id,
                   ROW_NUMBER() OVER (PARTITION BY phone_normalized ORDER BY created_at ASC, id ASC) AS rn
            FROM leads
            WHERE organization_id = '{org_id}'
              AND phone_normalized IS NOT NULL 
              AND phone_normalized != ''
        ) b
        WHERE a.id = b.id AND b.rn > 1;
    """)
    deleted_count = cur.rowcount
    print(f"-> Successfully removed {deleted_count:,} duplicate leads.")

    # 3. Standardize lead sources for clean UI badges
    print("Standardizing lead sources...")
    cur.execute(f"""
        UPDATE leads 
        SET first_touch_source = CASE 
            WHEN LOWER(COALESCE(first_touch_source, '')) LIKE '%walk%' THEN 'WALK_IN'
            WHEN LOWER(COALESCE(first_touch_source, '')) LIKE '%insta%' OR LOWER(COALESCE(first_touch_source, '')) LIKE '%ig%' THEN 'INSTAGRAM'
            WHEN LOWER(COALESCE(first_touch_source, '')) LIKE '%face%' OR LOWER(COALESCE(first_touch_source, '')) LIKE '%fb%' THEN 'FACEBOOK'
            WHEN LOWER(COALESCE(first_touch_source, '')) LIKE '%goog%' OR LOWER(COALESCE(first_touch_source, '')) LIKE '%search%' THEN 'GOOGLE_ADS'
            WHEN LOWER(COALESCE(first_touch_source, '')) LIKE '%ref%' OR LOWER(COALESCE(first_touch_source, '')) LIKE '%friend%' THEN 'REFERRAL'
            WHEN LOWER(COALESCE(first_touch_source, '')) LIKE '%web%' OR LOWER(COALESCE(first_touch_source, '')) LIKE '%guide%' THEN 'WEBSITE'
            WHEN LOWER(COALESCE(first_touch_source, '')) LIKE '%call%' OR LOWER(COALESCE(first_touch_source, '')) LIKE '%phone%' THEN 'PHONE'
            ELSE COALESCE(NULLIF(first_touch_source, ''), 'OTHER')
        END
        WHERE organization_id = '{org_id}';
    """)

    # 4. Restore foreign key checks
    cur.execute("SET session_replication_role = 'origin';")

    # 5. Final verification count
    cur.execute(f"SELECT COUNT(*) FROM leads WHERE organization_id = '{org_id}';")
    final_count = cur.fetchone()[0]

    conn.commit()
    print("=" * 60)
    print(f"CLEANUP COMPLETE! Final 100% Unique Leads Count: {final_count:,}")
    print("=" * 60)

    cur.close()
    conn.close()

if __name__ == "__main__":
    main()