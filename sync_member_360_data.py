"""
sync_member_360_data.py
Synchronizes Member 360 data from MySQL (import_db) to PostgreSQL (tenant_sweat_demo):
1. Attendance check-ins (attendance_records and access_events) with authentic historical timestamps
2. Member Origin / "Member Since" (user_profiles.joining_date) from earliest of leads.createdon and users.createdon
3. Member Date of Birth (user_profiles.date_of_birth and users.date_of_birth) from users.birthday
4. Lead Acquisition Source (user_profiles.acquisition_source) from leads.leadfrom
"""

import sys
import time
from datetime import datetime, date, timezone, timedelta
import pymysql
import psycopg2
import psycopg2.extras

MYSQL_CONFIG = {
    "host": "localhost",
    "port": 3307,
    "user": "root",
    "password": "M@$#eera11",
    "database": "import_db",
    "charset": "utf8mb4",
}

PG_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "user": "postgres",
    "password": "masheera11",
    "database": "tenant_sweat_demo",
}

IST = timezone(timedelta(hours=5, minutes=30))

def parse_dt(val):
    if not val or val in ('0000-00-00', '0000-00-00 00:00:00', 'None', 'null', '0'):
        return None
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(str(val))
    except Exception:
        return None

def parse_dob(b_str):
    if not b_str:
        return None
    b_str = str(b_str).strip()
    if not b_str or b_str in ('0000-00-00', '0000-00-00 00:00:00', 'None', 'null', '0'):
        return None
    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y', '%Y/%m/%d', '%d.%m.%Y'):
        try:
            d = datetime.strptime(b_str[:10], fmt).date()
            if 1920 <= d.year <= 2024:
                return d
        except Exception:
            continue
    return None

def clean_source(src):
    if not src:
        return 'Direct'
    src = str(src).strip()
    if not src or src in ('0', 'None', 'null'):
        return 'Direct'
    lower = src.lower()
    if 'ig' in lower or 'instagram' in lower:
        return 'Instagram'
    if 'fb' in lower or 'facebook' in lower:
        return 'Facebook'
    if 'phone' in lower or 'call' in lower:
        return 'Phone'
    if 'whatsapp' in lower:
        return 'WhatsApp'
    if 'walk' in lower:
        return 'Walk-In'
    if 'referral' in lower or 'refer' in lower:
        return 'Referral'
    if 'website' in lower or 'web' in lower:
        return 'Website'
    if 'google' in lower:
        return 'Google Ads'
    if 'influencer' in lower:
        return 'Influencer'
    return src[:30].strip().title()

def main():
    print("[*] Connecting to MySQL (import_db) and PostgreSQL (tenant_sweat_demo)...")
    mysql_conn = pymysql.connect(**MYSQL_CONFIG)
    pg_conn = psycopg2.connect(**PG_CONFIG)
    pg_conn.autocommit = False

    try:
        # =====================================================================
        # 1. ATTENDANCE & ACCESS CHECK-IN DATES
        # =====================================================================
        print("\n--- 1. Syncing Attendance Records & Access Events Timestamps ---")
        t0 = time.time()
        with pg_conn.cursor() as cur:
            print("Updating attendance_records from linked bookings and occurrences...")
            cur.execute("""
                UPDATE attendance_records a
                SET 
                    check_in_at = COALESCE(b.completed_at, b.booked_at),
                    check_out_at = COALESCE(b.completed_at + interval '1 hour', b.booked_at + interval '1 hour'),
                    created_at = COALESCE(b.completed_at, b.booked_at),
                    updated_at = COALESCE(b.completed_at, b.booked_at)
                FROM bookings b
                WHERE a.booking_id = b.id;
            """)
            print(f"attendance_records updated: {cur.rowcount} rows in {time.time() - t0:.2f}s.")

            print("Updating access_events from linked bookings...")
            t0 = time.time()
            cur.execute("""
                UPDATE access_events a
                SET 
                    event_at = COALESCE(b.completed_at, b.booked_at),
                    created_at = COALESCE(b.completed_at, b.booked_at)
                FROM bookings b
                WHERE a.booking_id = b.id;
            """)
            print(f"access_events updated: {cur.rowcount} rows in {time.time() - t0:.2f}s.")

        # =====================================================================
        # 2. MEMBER JOINING DATE, DOB & ACQUISITION SOURCE
        # =====================================================================
        print("\n--- 2. Fetching User and Lead Origin Data from MySQL ---")
        t0 = time.time()
        with mysql_conn.cursor() as cur:
            # Fetch leads first: map userid -> (min(createdon), leadfrom)
            print("Fetching leads origin...")
            cur.execute("SELECT userid, createdon, leadfrom FROM leads WHERE userid IS NOT NULL AND userid > 0;")
            leads_data = {}
            for uid, c_on, l_from in cur.fetchall():
                c_dt = parse_dt(c_on)
                if uid not in leads_data:
                    leads_data[uid] = (c_dt, l_from)
                else:
                    existing_c, existing_l = leads_data[uid]
                    if c_dt and (not existing_c or c_dt < existing_c):
                        leads_data[uid] = (c_dt, l_from or existing_l)

            print(f"Fetched {len(leads_data)} lead mappings.")

            # Fetch users
            print("Fetching users table...")
            cur.execute("SELECT uid, birthday, createdon FROM users;")
            users_rows = cur.fetchall()
            print(f"Fetched {len(users_rows)} users.")

            # Fetch users2
            cur.execute("SELECT uid, birthday, createdon FROM users2;")
            users2_rows = cur.fetchall()
            print(f"Fetched {len(users2_rows)} users2.")

        # Prepare update batch for user_profiles
        profile_records = []
        user_records = []

        for uid, birthday, createdon in users_rows:
            leg_ref = f"user:{uid}"
            dob = parse_dob(birthday)
            u_c = parse_dt(createdon)
            
            lead_c, lead_src = leads_data.get(uid, (None, None))
            
            # Joining date: earliest between lead creation and user registration
            join_dt = None
            if lead_c and u_c:
                join_dt = min(lead_c, u_c)
            elif lead_c:
                join_dt = lead_c
            elif u_c:
                join_dt = u_c
            
            join_date = join_dt.date() if isinstance(join_dt, (datetime, date)) else None
            join_tz = join_dt.replace(tzinfo=IST) if isinstance(join_dt, datetime) else None
            
            acq_src = clean_source(lead_src)
            profile_records.append((leg_ref, dob, join_date, acq_src, join_tz))
            user_records.append((leg_ref, dob, join_tz))

        for uid, birthday, createdon in users2_rows:
            leg_ref = f"users2:{uid}"
            dob = parse_dob(birthday)
            u_c = parse_dt(createdon)
            join_date = u_c.date() if isinstance(u_c, (datetime, date)) else None
            join_tz = u_c.replace(tzinfo=IST) if isinstance(u_c, datetime) else None
            acq_src = 'Direct'
            profile_records.append((leg_ref, dob, join_date, acq_src, join_tz))
            user_records.append((leg_ref, dob, join_tz))

        print(f"Prepared {len(profile_records)} records for profiles update.")

        print("Executing bulk update on PostgreSQL user_profiles and users...")
        t0 = time.time()
        with pg_conn.cursor() as cur:
            cur.execute("ALTER TABLE user_profiles ALTER COLUMN acquisition_source TYPE VARCHAR(100);")
            cur.execute("""
                CREATE TEMP TABLE tmp_profile_origin (
                    legacy_reference VARCHAR(128) PRIMARY KEY,
                    date_of_birth DATE,
                    joining_date DATE,
                    acquisition_source VARCHAR(128),
                    created_at TIMESTAMPTZ
                ) ON COMMIT DROP;
            """)
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO tmp_profile_origin (legacy_reference, date_of_birth, joining_date, acquisition_source, created_at) VALUES %s",
                profile_records,
                page_size=10000
            )

            print("Updating user_profiles...")
            cur.execute("""
                UPDATE user_profiles p
                SET 
                    date_of_birth = COALESCE(t.date_of_birth, p.date_of_birth),
                    joining_date = COALESCE(t.joining_date, p.joining_date),
                    acquisition_source = COALESCE(t.acquisition_source, p.acquisition_source),
                    created_at = COALESCE(t.created_at, p.created_at)
                FROM tmp_profile_origin t
                WHERE p.legacy_reference = t.legacy_reference;
            """)
            print(f"user_profiles updated: {cur.rowcount} rows in {time.time() - t0:.2f}s.")

            print("Updating users table date_of_birth and created_at...")
            t0 = time.time()
            cur.execute("""
                UPDATE users u
                SET 
                    date_of_birth = COALESCE(p.date_of_birth, u.date_of_birth),
                    created_at = COALESCE(p.created_at, u.created_at)
                FROM user_profiles p
                WHERE p.user_id = u.id AND p.date_of_birth IS NOT NULL;
            """)
            print(f"users updated: {cur.rowcount} rows in {time.time() - t0:.2f}s.")

        print("\n[*] Committing transaction to PostgreSQL...")
        pg_conn.commit()
        print("[DONE] All Member 360 data successfully synchronized and committed!")

    except Exception as e:
        pg_conn.rollback()
        print(f"[!] Error: {e}", file=sys.stderr)
        raise
    finally:
        mysql_conn.close()
        pg_conn.close()

if __name__ == "__main__":
    main()
