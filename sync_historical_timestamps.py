"""
sync_historical_timestamps.py
Synchronizes original historical timestamps from MySQL (import_db) to PostgreSQL (tenant_sweat_demo).
Updates:
1. bookings: booked_at, created_at, completed_at, cancelled_at
2. orders: created_at, updated_at
3. payment_transactions: created_at, paid_at, updated_at
4. member_invoices: created_at, updated_at
5. memberships: created_at, activated_at
"""

import sys
import time
from datetime import datetime, timezone, timedelta
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

def parse_ist(dt):
    if not dt or dt == "0000-00-00 00:00:00" or dt == "0000-00-00":
        return None
    if isinstance(dt, datetime):
        return dt.replace(tzinfo=IST)
    try:
        parsed = datetime.fromisoformat(str(dt))
        return parsed.replace(tzinfo=IST)
    except Exception:
        return None

def main():
    print("[*] Connecting to MySQL (import_db on 3307) and PostgreSQL (tenant_sweat_demo on 5432)...")
    mysql_conn = pymysql.connect(**MYSQL_CONFIG)
    pg_conn = psycopg2.connect(**PG_CONFIG)
    pg_conn.autocommit = False

    try:
        # =====================================================================
        # 1. BOOKINGS TIMESTAMPS
        # =====================================================================
        print("\n--- 1. Syncing Bookings Timestamps ---")
        t0 = time.time()
        with mysql_conn.cursor() as cur:
            print("Fetching bookings from MySQL import_db...")
            cur.execute("""
                SELECT uid, createdon, booking_time, attendance_tIme, cancellation_tIme 
                FROM bookings
            """)
            booking_rows = cur.fetchall()
            print(f"Fetched {len(booking_rows)} bookings from MySQL.")

        # Prepare records for staging
        booking_records = []
        for uid, createdon, booking_time, attendance_time, cancellation_time in booking_rows:
            b_num = f"BK-{int(uid):07d}"
            # Choose booking_time or createdon
            created_dt = parse_ist(createdon) or parse_ist(booking_time)
            booked_dt = parse_ist(booking_time) or created_dt
            attended_dt = parse_ist(attendance_time)
            cancelled_dt = parse_ist(cancellation_time)
            if created_dt:
                booking_records.append((b_num, booked_dt, created_dt, attended_dt, cancelled_dt))

        print(f"Prepared {len(booking_records)} booking records to update.")

        with pg_conn.cursor() as cur:
            cur.execute("""
                CREATE TEMP TABLE tmp_booking_dates (
                    booking_number VARCHAR(64) PRIMARY KEY,
                    booked_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ,
                    completed_at TIMESTAMPTZ,
                    cancelled_at TIMESTAMPTZ
                ) ON COMMIT DROP;
            """)
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO tmp_booking_dates (booking_number, booked_at, created_at, completed_at, cancelled_at) VALUES %s",
                booking_records,
                page_size=10000
            )
            print("Loaded staging table. Executing UPDATE on bookings...")
            cur.execute("""
                UPDATE bookings b
                SET 
                    booked_at = t.booked_at,
                    created_at = t.created_at,
                    completed_at = COALESCE(t.completed_at, b.completed_at),
                    cancelled_at = COALESCE(t.cancelled_at, b.cancelled_at)
                FROM tmp_booking_dates t
                WHERE b.booking_number = t.booking_number;
            """)
            print(f"Bookings updated: {cur.rowcount} rows in {time.time() - t0:.2f}s.")

        # =====================================================================
        # 2. ORDERS & TRANSACTIONS TIMESTAMPS
        # =====================================================================
        print("\n--- 2. Syncing Orders & Transactions Timestamps ---")
        t0 = time.time()
        with mysql_conn.cursor() as cur:
            print("Fetching transactions from MySQL import_db...")
            cur.execute("SELECT uid, createdon, updatedon FROM transactions;")
            tx_rows = cur.fetchall()
            print(f"Fetched {len(tx_rows)} transactions from MySQL.")

        order_records = []
        for uid, createdon, updatedon in tx_rows:
            ord_num = f"ORD-{int(uid):06d}"
            c_dt = parse_ist(createdon)
            u_dt = parse_ist(updatedon) or c_dt
            if c_dt:
                order_records.append((ord_num, c_dt, u_dt))

        with pg_conn.cursor() as cur:
            cur.execute("""
                CREATE TEMP TABLE tmp_order_dates (
                    order_number VARCHAR(64) PRIMARY KEY,
                    created_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ
                ) ON COMMIT DROP;
            """)
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO tmp_order_dates (order_number, created_at, updated_at) VALUES %s",
                order_records,
                page_size=10000
            )
            print("Loaded staging table. Executing UPDATE on orders...")
            cur.execute("""
                UPDATE orders o
                SET 
                    created_at = t.created_at,
                    updated_at = t.updated_at
                FROM tmp_order_dates t
                WHERE o.order_number = t.order_number;
            """)
            print(f"Orders updated: {cur.rowcount} rows.")

            # Update payment_transactions
            print("Updating payment_transactions timestamps from orders...")
            cur.execute("""
                UPDATE payment_transactions p
                SET 
                    created_at = o.created_at,
                    paid_at = CASE WHEN p.status = 'COMPLETED' THEN o.created_at ELSE p.paid_at END,
                    updated_at = o.updated_at
                FROM orders o
                WHERE p.order_id = o.id;
            """)
            print(f"Payment transactions updated: {cur.rowcount} rows.")

            # Update member_invoices if linked to orders
            print("Updating member_invoices timestamps from orders...")
            cur.execute("""
                UPDATE member_invoices i
                SET 
                    created_at = o.created_at,
                    updated_at = o.updated_at
                FROM orders o
                WHERE i.order_id = o.id;
            """)
            print(f"Member invoices updated: {cur.rowcount} rows in {time.time() - t0:.2f}s.")

        # =====================================================================
        # 3. MEMBERSHIPS TIMESTAMPS
        # =====================================================================
        print("\n--- 3. Syncing Memberships Timestamps ---")
        t0 = time.time()
        with mysql_conn.cursor() as cur:
            print("Fetching userplans from MySQL import_db...")
            cur.execute("SELECT uid, createdon, startdate FROM userplans;")
            up_rows = cur.fetchall()
            print(f"Fetched {len(up_rows)} userplans from MySQL.")

        membership_records = []
        for uid, createdon, startdate in up_rows:
            mem_num = f"MEMSHIP-{int(uid):06d}"
            c_dt = parse_ist(createdon)
            if c_dt:
                membership_records.append((mem_num, c_dt))

        with pg_conn.cursor() as cur:
            cur.execute("""
                CREATE TEMP TABLE tmp_membership_dates (
                    membership_number VARCHAR(64) PRIMARY KEY,
                    created_at TIMESTAMPTZ
                ) ON COMMIT DROP;
            """)
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO tmp_membership_dates (membership_number, created_at) VALUES %s",
                membership_records,
                page_size=10000
            )
            print("Loaded staging table. Executing UPDATE on memberships...")
            cur.execute("""
                UPDATE memberships m
                SET 
                    created_at = t.created_at,
                    activated_at = COALESCE(m.activated_at, t.created_at)
                FROM tmp_membership_dates t
                WHERE m.membership_number = t.membership_number;
            """)
            print(f"Memberships updated: {cur.rowcount} rows in {time.time() - t0:.2f}s.")

        # Commit all updates
        print("\n[*] Committing transaction to PostgreSQL...")
        pg_conn.commit()
        print("[✓] All historical timestamps successfully synced and committed!")

    except Exception as e:
        pg_conn.rollback()
        print(f"[!] Error occurred: {e}", file=sys.stderr)
        raise
    finally:
        mysql_conn.close()
        pg_conn.close()

if __name__ == "__main__":
    main()
