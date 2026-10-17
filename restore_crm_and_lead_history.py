"""
restore_crm_and_lead_history.py
High-Performance ETL to restore all original CRM, Lead, Call, Task, and Trial data from MySQL to PostgreSQL:
1. Restore authentic Lead creation dates (leads.created_at & updated_at) from leads & archive_leads.
2. Restore archived member joining dates (user_profiles.joining_date) from archive_users.createdon.
3. Synchronize 67,600+ Sales Followup Tasks (sales_followup_tasks) from lcomments with scheduled due dates.
4. Synchronize 2,740 Trial notes & interactions from lcomments into trial_bookings.
5. Synchronize 104,740 Call logs from call_logs into crm_call_sessions.
6. Synchronize full authentic Lead Notes from lcomments into lead_notes with authentic created_at timestamps.
"""

import sys
import time
import uuid
from datetime import datetime, date, time as dt_time, timezone, timedelta
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

def parse_dt(v):
    if not v:
        return None
    s = str(v).strip()
    if not s or s.startswith("0000"):
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=IST) if v.tzinfo is None else v
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19], fmt).replace(tzinfo=IST)
        except Exception:
            pass
    return None

def parse_d(v):
    if not v:
        return None
    s = str(v).strip()
    if not s or s.startswith("0000"):
        return None
    if isinstance(v, date):
        return v
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except Exception:
            pass
    return None

def run_migration():
    print("=" * 75)
    print("STARTING COMPREHENSIVE CRM & LEAD HISTORY RESTORATION")
    print("=" * 75)

    my_conn = pymysql.connect(**MYSQL_CONFIG)
    pg_conn = psycopg2.connect(**PG_CONFIG)
    pg_conn.autocommit = False

    try:
        pg_cur = pg_conn.cursor()
        my_cur = my_conn.cursor()

        # Fetch Organization ID and Default Branch
        pg_cur.execute("SELECT id FROM organizations LIMIT 1;")
        org_id = pg_cur.fetchone()[0]
        pg_cur.execute("SELECT id FROM branches LIMIT 1;")
        default_branch_id = pg_cur.fetchone()[0]

        # Fetch Agent / Staff User ID Map
        pg_cur.execute("""
            SELECT split_part(legacy_reference, ':', 2)::int, user_id 
            FROM user_profiles 
            WHERE legacy_reference LIKE 'agent:%';
        """)
        agent_user_map = dict(pg_cur.fetchall())
        print(f"Loaded {len(agent_user_map)} agent user mappings.")

        # Default fallback staff user
        pg_cur.execute("SELECT id FROM users WHERE user_type = 'STAFF' LIMIT 1;")
        default_staff_user_id = pg_cur.fetchone()[0]

        # ---------------------------------------------------------------------
        # 1. RESTORE AUTHENTIC LEAD CREATION DATES
        # ---------------------------------------------------------------------
        print("\n--- [Step 1] Restoring Authentic Lead Creation Dates ---")
        t0 = time.time()

        # Temporary table for lead dates
        pg_cur.execute("""
            CREATE TEMP TABLE tmp_lead_dates (
                lead_id uuid PRIMARY KEY,
                created_at timestamptz,
                updated_at timestamptz
            ) ON COMMIT DROP;
        """)

        # A. Map leads converted to members via legacy_reference
        pg_cur.execute("""
            SELECT l.id, split_part(u.legacy_reference, ':', 2)::int
            FROM leads l
            JOIN user_profiles u ON l.converted_user_profile_id = u.id;
        """)
        pg_conv_leads = pg_cur.fetchall()
        print(f"Loaded {len(pg_conv_leads)} converted leads from PG.")

        my_cur.execute("SELECT userid, MIN(createdon), MAX(updatedon) FROM leads WHERE userid > 0 GROUP BY userid;")
        my_lead_dates = {}
        for r in my_cur.fetchall():
            c_dt = parse_dt(r[1])
            u_dt = parse_dt(r[2]) or c_dt
            if c_dt:
                my_lead_dates[r[0]] = (c_dt, u_dt)

        lead_date_batch = []
        for l_id, u_id in pg_conv_leads:
            if u_id in my_lead_dates:
                c_dt, u_dt = my_lead_dates[u_id]
                lead_date_batch.append((l_id, c_dt, u_dt))

        print(f"Prepared {len(lead_date_batch)} converted lead date updates.")

        # B. Map ARCHIVED leads via email_normalized
        pg_cur.execute("""
            SELECT l.id, l.email_normalized
            FROM leads l
            WHERE l.current_status = 'ARCHIVED' AND l.email_normalized IS NOT NULL AND l.email_normalized != '';
        """)
        pg_arc_leads = pg_cur.fetchall()

        my_cur.execute("""
            SELECT LOWER(TRIM(email)), MIN(createdon), MAX(updatedon) 
            FROM archive_leads 
            WHERE email IS NOT NULL AND email != '' 
            GROUP BY LOWER(TRIM(email));
        """)
        my_arc_dates = {}
        for r in my_cur.fetchall():
            c_dt = parse_dt(r[1])
            u_dt = parse_dt(r[2]) or c_dt
            if c_dt:
                my_arc_dates[r[0]] = (c_dt, u_dt)

        arc_matched = 0
        for l_id, email in pg_arc_leads:
            if email in my_arc_dates:
                c_dt, u_dt = my_arc_dates[email]
                lead_date_batch.append((l_id, c_dt, u_dt))
                arc_matched += 1

        print(f"Prepared {arc_matched} archived lead date updates. Total batch: {len(lead_date_batch)}")

        # Execute bulk update
        psycopg2.extras.execute_values(
            pg_cur,
            "INSERT INTO tmp_lead_dates (lead_id, created_at, updated_at) VALUES %s;",
            lead_date_batch,
            page_size=5000,
        )
        pg_cur.execute("""
            UPDATE leads l
            SET 
                created_at = t.created_at,
                updated_at = t.updated_at
            FROM tmp_lead_dates t
            WHERE l.id = t.lead_id;
        """)
        print(f"Updated {pg_cur.rowcount} leads created_at in {time.time() - t0:.2f}s.")

        # ---------------------------------------------------------------------
        # 2. RESTORE ARCHIVED MEMBERS JOINING DATES
        # ---------------------------------------------------------------------
        print("\n--- [Step 2] Restoring Archived Member Joining Dates ---")
        t0 = time.time()
        pg_cur.execute("""
            CREATE TEMP TABLE tmp_arc_member_dates (
                legacy_ref varchar(128) PRIMARY KEY,
                joining_date date,
                created_at timestamptz
            ) ON COMMIT DROP;
        """)

        my_cur.execute("SELECT uid, createdon FROM archive_users WHERE createdon IS NOT NULL;")
        arc_member_rows = []
        for uid, createdon in my_cur.fetchall():
            c_dt = parse_dt(createdon)
            if c_dt:
                arc_member_rows.append((f"user:{uid}", c_dt.date(), c_dt))

        print(f"Loaded {len(arc_member_rows)} valid archive_users registration dates.")
        psycopg2.extras.execute_values(
            pg_cur,
            "INSERT INTO tmp_arc_member_dates (legacy_ref, joining_date, created_at) VALUES %s;",
            arc_member_rows,
            page_size=5000,
        )
        pg_cur.execute("""
            UPDATE user_profiles u
            SET 
                joining_date = t.joining_date
            FROM tmp_arc_member_dates t
            WHERE u.legacy_reference = t.legacy_ref;
        """)
        print(f"Updated {pg_cur.rowcount} user_profiles joining_date for archived users in {time.time() - t0:.2f}s.")

        # ---------------------------------------------------------------------
        # 3. BUILD COMPLETE LEAD LOOKUP MAPS FOR HISTORICAL LOGS
        # ---------------------------------------------------------------------
        print("\n--- [Step 3] Building Lead Lookup Index ---")
        t0 = time.time()
        # Lead lookup by email
        pg_cur.execute("SELECT email_normalized, id, branch_id FROM leads WHERE email_normalized IS NOT NULL AND email_normalized != '';")
        email_to_lead_data = {r[0]: (r[1], r[2]) for r in pg_cur.fetchall()}
        print(f"Indexed {len(email_to_lead_data)} leads by email.")

        # MySQL leadid -> email mapping
        my_cur.execute("SELECT uid, LOWER(TRIM(email)) FROM leads WHERE email IS NOT NULL AND email != '';")
        leadid_to_email = dict(my_cur.fetchall())
        my_cur.execute("SELECT uid, LOWER(TRIM(email)) FROM archive_leads WHERE email IS NOT NULL AND email != '';")
        for uid, em in my_cur.fetchall():
            if uid not in leadid_to_email:
                leadid_to_email[uid] = em

        print(f"Indexed {len(leadid_to_email)} MySQL leads/archive_leads to emails in {time.time() - t0:.2f}s.")

        # ---------------------------------------------------------------------
        # 4. SYNCHRONIZE SALES FOLLOWUP TASKS
        # ---------------------------------------------------------------------
        print("\n--- [Step 4] Synchronizing Sales Followup Tasks (`sales_followup_tasks`) ---")
        t0 = time.time()
        my_cur.execute("""
            SELECT uid, leadid, agentid, followup, followuptime, response, subject, comment, createdon 
            FROM lcomments 
            WHERE followup IS NOT NULL AND CAST(followup AS CHAR) NOT LIKE '0000%';
        """)
        lcomment_rows = my_cur.fetchall()
        print(f"Fetched {len(lcomment_rows)} follow-up records from lcomments.")

        now_dt = datetime.now(IST)
        tasks_batch = []
        for row in lcomment_rows:
            lc_uid, lc_leadid, lc_agentid, lc_followup, lc_followuptime, lc_resp, lc_subj, lc_comment, lc_createdon = row
            email = leadid_to_email.get(lc_leadid)
            if not email or email not in email_to_lead_data:
                continue
            lead_id, _ = email_to_lead_data[email]

            assigned_user_id = agent_user_map.get(lc_agentid, default_staff_user_id)
            c_dt = parse_dt(lc_createdon) or now_dt
            f_d = parse_d(lc_followup)
            if not f_d:
                continue
            
            # Followup time
            f_t = dt_time(11, 0)
            if lc_followuptime:
                try:
                    if isinstance(lc_followuptime, timedelta):
                        total_secs = int(lc_followuptime.total_seconds())
                        hours = (total_secs // 3600) % 24
                        mins = (total_secs % 3600) // 60
                        f_t = dt_time(hours, mins)
                    elif isinstance(lc_followuptime, dt_time):
                        f_t = lc_followuptime
                except Exception:
                    pass

            due_dt = datetime.combine(f_d, f_t).replace(tzinfo=IST)
            status = "COMPLETED" if due_dt < now_dt else "PENDING"
            priority = "HIGH" if "urgent" in str(lc_comment).lower() or "high" in str(lc_comment).lower() else "NORMAL"
            outcome = (lc_comment or lc_resp or "")[:500] or None

            tasks_batch.append((
                str(uuid.uuid4()),
                "CALL",
                priority,
                due_dt,
                status,
                outcome,
                due_dt,
                f"lcomment:{lc_uid}",
                assigned_user_id,
                assigned_user_id,
                lead_id,
                c_dt,
                c_dt
            ))

        print(f"Inserting {len(tasks_batch)} Sales Followup Tasks into PostgreSQL...")
        psycopg2.extras.execute_values(
            pg_cur,
            """
            INSERT INTO sales_followup_tasks (
                id, task_type, priority, due_at, status, outcome, next_followup_at,
                external_reference, assigned_to_user_id, created_by_user_id, lead_id,
                created_at, updated_at
            ) VALUES %s
            ON CONFLICT (id) DO NOTHING;
            """,
            tasks_batch,
            page_size=5000
        )
        print(f"sales_followup_tasks inserted: {len(tasks_batch)} in {time.time() - t0:.2f}s.")

        # ---------------------------------------------------------------------
        # 5. SYNCHRONIZE TRIAL INTERACTIONS INTO TRIAL_BOOKINGS
        # ---------------------------------------------------------------------
        print("\n--- [Step 5] Synchronizing Trial Bookings from `lcomments` (subject = 'Trial') ---")
        t0 = time.time()
        my_cur.execute("""
            SELECT uid, leadid, agentid, followup, followuptime, response, comment, createdon 
            FROM lcomments 
            WHERE subject = 'Trial';
        """)
        trial_rows = my_cur.fetchall()
        print(f"Fetched {len(trial_rows)} trial interactions from lcomments.")

        trials_batch = []
        for row in trial_rows:
            lc_uid, lc_leadid, lc_agentid, lc_followup, lc_followuptime, lc_resp, lc_comment, lc_createdon = row
            email = leadid_to_email.get(lc_leadid)
            if not email or email not in email_to_lead_data:
                continue
            lead_id, branch_id = email_to_lead_data[email]
            branch_id = branch_id or default_branch_id

            c_dt = parse_dt(lc_createdon) or now_dt
            f_d = parse_d(lc_followup)
            start_dt = datetime.combine(f_d, dt_time(10, 30)).replace(tzinfo=IST) if f_d else c_dt
            end_dt = start_dt + timedelta(minutes=50)

            # Determine trial status from notes
            text_lower = (str(lc_comment) + " " + str(lc_resp)).lower()
            if "attended" in text_lower or "came" in text_lower:
                status = "ATTENDED"
            elif "didnt come" in text_lower or "didn't come" in text_lower or "not answered" in text_lower or "no response" in text_lower:
                status = "NO_SHOW"
            elif "cancel" in text_lower:
                status = "CANCELLED"
            else:
                status = "BOOKED" if start_dt > now_dt else "ATTENDED"

            staff_user = agent_user_map.get(lc_agentid, default_staff_user_id)

            trials_batch.append((
                str(uuid.uuid4()),
                lead_id,
                branch_id,
                "GROUP_CLASS",
                start_dt,
                end_dt,
                status,
                "CONFIRMED" if status in ("ATTENDED", "CONFIRMED") else "PENDING",
                "PHONE",
                (lc_comment or "")[:500],
                "SALES",
                staff_user,
                c_dt,
                c_dt
            ))

        print(f"Inserting {len(trials_batch)} Trial Bookings into PostgreSQL...")
        psycopg2.extras.execute_values(
            pg_cur,
            """
            INSERT INTO trial_bookings (
                id, lead_id, branch_id, trial_type, scheduled_start, scheduled_end,
                status, confirmation_status, confirmation_channel, notes, booking_source,
                created_by_user_id, created_at, updated_at
            ) VALUES %s
            ON CONFLICT (id) DO NOTHING;
            """,
            trials_batch,
            page_size=3000
        )
        print(f"trial_bookings inserted: {len(trials_batch)} in {time.time() - t0:.2f}s.")

        # ---------------------------------------------------------------------
        # 6. SYNCHRONIZE CALL LOGS INTO CRM_CALL_SESSIONS
        # ---------------------------------------------------------------------
        print("\n--- [Step 6] Synchronizing Call Logs into `crm_call_sessions` ---")
        t0 = time.time()
        my_cur.execute("""
            SELECT uid, user_id, agent_id, lead_id, call_id, createdon, client_number, agent_number, PhoneNumber, Status, StartTime, EndTime, Duration, RecordingUrl 
            FROM call_logs;
        """)
        call_rows = my_cur.fetchall()
        print(f"Fetched {len(call_rows)} records from call_logs.")

        calls_batch = []
        for row in call_rows:
            (cl_uid, cl_uid_user, cl_aid, cl_lid, cl_cid, cl_createdon, cl_client_no, cl_agent_no, cl_phone_no,
             cl_status, cl_start, cl_end, cl_dur, cl_rec) = row

            c_dt = parse_dt(cl_createdon) or parse_dt(cl_start) or now_dt
            st_dt = parse_dt(cl_start) or c_dt
            end_dt = parse_dt(cl_end)

            email = leadid_to_email.get(cl_lid)
            lead_id = None
            if email and email in email_to_lead_data:
                lead_id = email_to_lead_data[email][0]

            duration = 0.0
            if cl_dur:
                try:
                    duration = float(cl_dur)
                except Exception:
                    pass

            status_str = (cl_status or "COMPLETED").strip().upper()
            if status_str not in ("QUEUED", "INITIATED", "RINGING", "IN_PROGRESS", "COMPLETED", "BUSY", "NO_ANSWER", "FAILED", "CANCELLED"):
                status_str = "COMPLETED"

            prov_attempt = f"{cl_cid}_{cl_uid}" if cl_cid else f"legacy_call_{cl_uid}"

            calls_batch.append((
                str(uuid.uuid4()),
                "EXOTEL",
                prov_attempt,
                status_str,
                "OUTBOUND",
                str(cl_agent_no or "")[:50],
                str(cl_client_no or cl_phone_no or "")[:50],
                duration,
                st_dt,
                end_dt,
                "[]",
                "{}",
                f'{{"recording_url": "{cl_rec or ""}", "legacy_uid": {cl_uid}, "call_id": "{cl_cid or ""}"}}',
                "",
                "",
                f"call_log_{cl_uid}",
                lead_id,
                org_id,
                c_dt,
                c_dt
            ))

        print(f"Inserting {len(calls_batch)} Call Sessions into PostgreSQL...")
        psycopg2.extras.execute_values(
            pg_cur,
            """
            INSERT INTO crm_call_sessions (
                id, provider, provider_attempt_id, status, direction, agent_phone_number,
                user_phone_number, duration_seconds, started_at, ended_at, transcript,
                final_agent_variables, metadata, error_code, error_message, idempotency_key,
                lead_id, organization_id, created_at, updated_at
            ) VALUES %s
            ON CONFLICT (id) DO NOTHING;
            """,
            calls_batch,
            page_size=5000
        )
        print(f"crm_call_sessions inserted: {len(calls_batch)} in {time.time() - t0:.2f}s.")

        # ---------------------------------------------------------------------
        # 7. RESTORE LEAD NOTES HISTORICAL TIMESTAMPS
        # ---------------------------------------------------------------------
        print("\n--- [Step 7] Restoring Lead Notes with Authentic Timestamps ---")
        t0 = time.time()
        # Clean up empty notes created on Oct 8 migration
        pg_cur.execute("DELETE FROM lead_notes WHERE note_text = '';")
        print(f"Purged {pg_cur.rowcount} blank placeholder notes.")

        # Insert all valid non-empty comments from lcomments
        my_cur.execute("""
            SELECT uid, leadid, agentid, comment, response, subject, createdon 
            FROM lcomments 
            WHERE comment IS NOT NULL AND comment != '';
        """)
        comment_rows = my_cur.fetchall()
        print(f"Fetched {len(comment_rows)} meaningful comments from lcomments.")

        notes_batch = []
        for row in comment_rows:
            lc_uid, lc_leadid, lc_agentid, lc_comment, lc_resp, lc_subj, lc_createdon = row
            email = leadid_to_email.get(lc_leadid)
            if not email or email not in email_to_lead_data:
                continue
            lead_id, _ = email_to_lead_data[email]

            c_dt = parse_dt(lc_createdon) or now_dt
            author_id = agent_user_map.get(lc_agentid, default_staff_user_id)
            n_type = (lc_subj or "GENERAL").strip().upper()[:45] or "GENERAL"

            notes_batch.append((
                str(uuid.uuid4()),
                lead_id,
                lc_comment.strip(),
                n_type,
                author_id,
                c_dt,
                c_dt
            ))

        print(f"Inserting {len(notes_batch)} authentic Lead Notes into PostgreSQL...")
        psycopg2.extras.execute_values(
            pg_cur,
            """
            INSERT INTO lead_notes (
                id, lead_id, note_text, note_type, created_by_user_id, created_at, updated_at
            ) VALUES %s
            ON CONFLICT (id) DO NOTHING;
            """,
            notes_batch,
            page_size=5000
        )
        print(f"lead_notes inserted: {len(notes_batch)} in {time.time() - t0:.2f}s.")

        # Commit entire transaction
        pg_conn.commit()
        print("\n" + "=" * 75)
        print("MIGRATION COMPLETED & COMMITTED SUCCESSFULLY!")
        print("=" * 75)

    except Exception as e:
        pg_conn.rollback()
        print(f"\n[ERROR] Migration failed, rolling back changes: {e}")
        import traceback
        traceback.print_exc()
        raise e
    finally:
        my_conn.close()
        pg_conn.close()

if __name__ == "__main__":
    run_migration()
