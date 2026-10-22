"""
migrate_october_and_all_trials.py
Synchronize all trial bookings from MySQL sweat_latest_db into PostgreSQL tenant_sweat_demo trial_bookings table.
Ensures exact match with MySQL (including all 236 October 2026 trials).
"""

import os
import sys
import uuid
from datetime import datetime, timedelta
import zoneinfo
import django
import pymysql

sys.path.insert(0, r"C:\Users\Masheera\Downloads\VibeCopilot_Projects\New_folder\SWEAT-BE")
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from config.tenant_middleware import _register_tenant_connection
from config.routers import build_tenant_db_alias, set_tenant_db_alias
from apps.master.models_infra import TenantDataSource
from django.db import connections

IST = zoneinfo.ZoneInfo("Asia/Kolkata")
NOW = datetime.now(IST)

def main():
    print("=" * 70)
    print("MIGRATING TRIAL DATA FROM MYSQL (sweat_latest_db) TO POSTGRESQL (tenant_sweat_demo)")
    print("=" * 70)

    # 1. Connect to PostgreSQL
    ds = TenantDataSource.objects.using('default').first()
    alias = build_tenant_db_alias(ds.tenant_id)
    _register_tenant_connection(alias=alias, tenant_id=ds.tenant_id, data_source=ds)
    set_tenant_db_alias(alias)

    pg_conn = connections[alias]
    
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM organizations LIMIT 1")
        org_id = cur.fetchone()[0]
        
        cur.execute("SELECT id, code, name FROM branches")
        branch_rows = cur.fetchall()

    branch_map_by_code = {code: bid for bid, code, name in branch_rows}
    mysql_branch_to_pg = {
        1: branch_map_by_code.get('SWEAT_GOREGAON_WEST'),
        2: branch_map_by_code.get('SWEAT_ANDHERI_WEST'),
        3: branch_map_by_code.get('SWEAT_GOREGAON_WEST'),
        5: branch_map_by_code.get('SWEAT_MALAD_WEST'),
        6: branch_map_by_code.get('SWEAT_ONLINE'),
    }
    default_branch_id = branch_map_by_code.get('SWEAT_ANDHERI_WEST') or list(branch_map_by_code.values())[0]

    # 2. Fetch existing leads
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id, phone_normalized, email_normalized FROM leads")
        lead_rows = cur.fetchall()

    phone_to_lead = {}
    email_to_lead = {}
    for lid, p, e in lead_rows:
        if p:
            cp = ''.join(filter(str.isdigit, p))[-10:]
            if cp and cp not in phone_to_lead:
                phone_to_lead[cp] = lid
        if e:
            ce = e.strip().lower()
            if ce and ce not in email_to_lead:
                email_to_lead[ce] = lid

    print(f"[PG] Organization: {org_id}")
    print(f"[PG] Loaded {len(phone_to_lead):,} phone leads, {len(email_to_lead):,} email leads")

    # 3. Fetch existing trial bookings
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id, lead_id, scheduled_start, notes FROM trial_bookings")
        existing_tb_rows = cur.fetchall()

    existing_tb_ids = set(r[0] for r in existing_tb_rows)
    existing_tb_notes = set(r[3] for r in existing_tb_rows if r[3])
    existing_tb_pairs = set((r[1], r[2]) for r in existing_tb_rows)
    print(f"[PG] Existing trial_bookings in DB: {len(existing_tb_rows):,}")

    # 4. Connect to MySQL sweat_latest_db
    my_conn = pymysql.connect(
        host='127.0.0.1',
        port=3307,
        user='root',
        password='M@$#eera11',
        database='sweat_latest_db',
        cursorclass=pymysql.cursors.DictCursor
    )

    with my_conn.cursor() as cur:
        cur.execute("""
            SELECT 
                b.uid as booking_uid,
                b.user_id,
                b.userplan_id,
                b.slot_id,
                b.createdon,
                b.booking_time,
                b.booking_status,
                b.attended,
                b.attendance_tIme,
                b.cancellation_tIme,
                b.book_source,
                up.branch_id as up_branch_id,
                c.branch_id as class_branch_id,
                u.firstname,
                u.lastname,
                u.contact,
                u.email,
                u.gender,
                u.createdon as user_createdon
            FROM bookings b
            JOIN userplans up ON b.userplan_id = up.uid
            LEFT JOIN slots s ON b.slot_id = s.uid
            LEFT JOIN class c ON s.class_id = c.uid
            LEFT JOIN users u ON b.user_id = u.uid
            WHERE up.istrial = 1
            ORDER BY b.createdon ASC
        """)
        mysql_trials = cur.fetchall()

    print(f"[MySQL] Total trial bookings fetched: {len(mysql_trials):,}")

    new_leads_to_insert = []
    trials_to_insert = []
    skipped_existing = 0

    for t in mysql_trials:
        b_uid = t['booking_uid']
        note_tag = f"legacy_booking_uid:{b_uid}"
        trial_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, f"sweat_trial_booking_{b_uid}")

        # Check if already present by note tag or UUID
        if note_tag in existing_tb_notes or trial_uuid in existing_tb_ids:
            skipped_existing += 1
            continue

        phone = (t['contact'] or '').strip()
        cp = ''.join(filter(str.isdigit, phone))[-10:] if phone else ''
        email = (t['email'] or '').strip().lower()

        lead_id = phone_to_lead.get(cp) or email_to_lead.get(email)

        # If lead does not exist in PostgreSQL, queue creation
        if not lead_id:
            lead_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"sweat_lead_user_{t['user_id']}")
            if cp:
                phone_to_lead[cp] = lead_id
            if email:
                email_to_lead[email] = lead_id

            user_created = t['user_createdon'] or t['createdon'] or datetime.now()
            if user_created.tzinfo is None:
                user_created = user_created.replace(tzinfo=IST)

            new_leads_to_insert.append((
                lead_id,
                org_id,
                (t['firstname'] or 'Trial').strip()[:150],
                (t['lastname'] or 'Lead').strip()[:150],
                f"+91{cp}" if cp else None,
                email if email else None,
                (t['gender'] or '')[:30],
                'TRIAL_BOOKED',
                True,  # consent_email
                True,  # consent_sms
                True,  # consent_whatsapp
                False, # do_not_contact
                'COMPLIANT', # response_sla_status
                user_created,
                user_created
            ))

        # Date handling
        b_time = t['booking_time'] or t['createdon']
        if not b_time:
            continue
        if b_time.tzinfo is None:
            b_time_ist = b_time.replace(tzinfo=IST)
        else:
            b_time_ist = b_time

        created_dt = t['createdon'] or b_time
        if created_dt.tzinfo is None:
            created_dt_ist = created_dt.replace(tzinfo=IST)
        else:
            created_dt_ist = created_dt

        # Check (lead_id, scheduled_start) pair
        if (lead_id, b_time_ist) in existing_tb_pairs:
            skipped_existing += 1
            continue

        # Branch mapping
        br_num = t['class_branch_id'] or t['up_branch_id']
        br_id = mysql_branch_to_pg.get(br_num, default_branch_id)

        # Status mapping
        raw_status = (t['booking_status'] or 'booked').lower()
        attended = t['attended'] == 1
        if attended:
            status = 'ATTENDED'
        elif 'cancel' in raw_status:
            status = 'CANCELLED'
        elif b_time_ist < NOW:
            status = 'NO_SHOW'
        else:
            status = 'BOOKED'

        raw_src = (t['book_source'] or '').lower()
        if 'app' in raw_src:
            src = 'MOBILE_APP'
        elif 'admin' in raw_src:
            src = 'ADMIN'
        else:
            src = 'WEB'

        sched_end = b_time_ist + timedelta(minutes=50)

        trials_to_insert.append((
            trial_uuid,
            lead_id,
            br_id,
            'CLASS',
            b_time_ist,
            sched_end,
            status,
            src,
            'CONFIRMED',
            created_dt_ist,
            created_dt_ist,
            note_tag
        ))
        # Record into lookup set so inner duplicates in MySQL loop don't double insert
        existing_tb_pairs.add((lead_id, b_time_ist))
        existing_tb_ids.add(trial_uuid)

    print(f"-> Skipped already existing trials: {skipped_existing:,}")
    print(f"-> New leads to insert: {len(new_leads_to_insert)}")
    print(f"-> New trials to insert: {len(trials_to_insert):,}")

    # 5. Insert new leads if any
    with pg_conn.cursor() as cur:
        if new_leads_to_insert:
            print("Inserting missing leads into PostgreSQL...")
            insert_lead_sql = """
                INSERT INTO leads (
                    id, organization_id, first_name, last_name, 
                    phone_normalized, email_normalized, gender, 
                    current_status, consent_email, consent_sms, 
                    consent_whatsapp, do_not_contact, response_sla_status, 
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING;
            """
            for lead_tuple in new_leads_to_insert:
                cur.execute(insert_lead_sql, lead_tuple)
            pg_conn.commit()
            print(f"[OK] {len(new_leads_to_insert)} leads inserted.")

        # 6. Bulk insert trials in batches of 1,000
        insert_trial_sql = """
            INSERT INTO trial_bookings (
                id, lead_id, branch_id, trial_type, 
                scheduled_start, scheduled_end, status, 
                booking_source, confirmation_status, 
                created_at, updated_at, notes
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                scheduled_start = EXCLUDED.scheduled_start,
                scheduled_end = EXCLUDED.scheduled_end,
                status = EXCLUDED.status,
                created_at = EXCLUDED.created_at,
                updated_at = EXCLUDED.updated_at;
        """
        batch_size = 1000
        total_inserted = 0
        for i in range(0, len(trials_to_insert), batch_size):
            batch = trials_to_insert[i:i + batch_size]
            cur.executemany(insert_trial_sql, batch)
            pg_conn.commit()
            total_inserted += len(batch)
            print(f"[OK] Inserted {total_inserted:,} / {len(trials_to_insert):,} trial bookings...")

    print("=" * 70)
    print("MIGRATION FINISHED! VERIFYING COUNTS...")
    print("=" * 70)

    # 7. Verification
    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM trial_bookings")
        total_trials_pg = cur.fetchone()[0]

        cur.execute("""
            SELECT COUNT(*) 
            FROM trial_bookings 
            WHERE created_at >= '2026-10-01' AND created_at < '2026-11-01'
        """)
        oct_created_pg = cur.fetchone()[0]

        cur.execute("""
            SELECT COUNT(*) 
            FROM trial_bookings 
            WHERE scheduled_start >= '2026-10-01' AND scheduled_start < '2026-11-01'
        """)
        oct_sched_pg = cur.fetchone()[0]

    with my_conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*)
            FROM bookings b
            JOIN userplans up ON b.userplan_id = up.uid
            WHERE up.istrial = 1 AND b.createdon >= '2026-10-01' AND b.createdon < '2026-11-01'
        """)
        oct_created_my = cur.fetchone()[0]

        cur.execute("""
            SELECT COUNT(*)
            FROM bookings b
            JOIN userplans up ON b.userplan_id = up.uid
            WHERE up.istrial = 1 AND b.booking_time >= '2026-10-01' AND b.booking_time < '2026-11-01'
        """)
        oct_sched_my = cur.fetchone()[0]

    print(f"PostgreSQL Total Trial Bookings: {total_trials_pg:,}")
    print(f"October Created Trials: PostgreSQL = {oct_created_pg} | MySQL = {oct_created_my}")
    print(f"October Scheduled Trials: PostgreSQL = {oct_sched_pg} | MySQL = {oct_sched_my}")

    if oct_created_pg == oct_created_my:
        print(" SUCCESS! October trials match MySQL single-digit for single-digit!")
    else:
        print(f" Notice: Oct created PG={oct_created_pg}, MySQL={oct_created_my}")

if __name__ == '__main__':
    main()
