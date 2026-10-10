"""
migrate_mysql_to_postgres.py
Bulletproof Bulk Data Transfer: MySQL (sweat_tenant_db) -> PostgreSQL (tenant_sweat_demo & sweat_db)
"""

import json
import sys
import uuid
from datetime import datetime, timedelta
import psycopg2
import psycopg2.extras
import pymysql

# ============================================================================
# 1. DATABASE CONFIGURATION
# ============================================================================
MYSQL_CONFIG = {
    "host": "localhost",
    "port": 3307,
    "user": "root",
    "password": "M@$#eera11",
    "database": "sweat_tenant_db",
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
}

PG_TENANT_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "user": "postgres",
    "password": "masheera11",
    "database": "tenant_sweat_demo",
}

PG_MASTER_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "user": "postgres",
    "password": "masheera11",
    "database": "sweat_db",
}

BATCH_SIZE = 5000

SPECIFIC_DEFAULTS = {
    "organizations": {
        "code": "SWEAT",
        "country": "IN",
        "currency": "INR",
        "timezone": "Asia/Kolkata",
        "deactivation_reason": "",
    },
    "locations": {
        "postal_code": "400001",
        "deactivation_reason": "",
    },
    "branches": {
        "geofence_enforcement": "STRICT",
        "timezone": "Asia/Kolkata",
        "deactivation_reason": "",
    },
    "users": {
        "avatar_url": "",
        "mfa_secret": "",
        "phone": "",
    },
    "attendance_records": {
        "liveness_method": "",
        "trainer_selfie_url": "",
        "liveness_challenges_passed": [],
    },
}

TABLES_TO_MIGRATE = [
    "organizations",
    "locations",
    "branches",
    "departments",
    "roles",
    "users",
    "user_profiles",
    "employee_profiles",
    "trainer_profiles",
    "sales_profiles",
    "program_categories",
    "program_types",
    "programs",
    "packages",
    "package_versions",
    "package_prices",
    "package_entitlement_definitions",
    "discount_campaigns",
    "discount_codes",
    "discount_redemptions",
    "intake_forms",
    "intake_questions",
    "intake_submissions",
    "intake_answers",
    "leads",
    "lead_notes",
    "orders",
    "order_items",
    "payment_transactions",
    "member_invoices",
    "memberships",
    "membership_entitlements",
    "class_categories",
    "class_templates",
    "class_schedule_rules",
    "class_occurrences",
    "class_occurrence_trainers",
    "bookings",
    "booking_reschedules",
    "attendance_records",
    "referral_programs",
    "reward_accounts",
    "reward_ledger",
    "access_events",
    "communication_messages",
    "meta_lead_mappings",
    "meta_lead_imports",
    "files",
    "organization_settings",
    "integrations",
    "tenant_audit_events",
    "legacy_entity_map",
]

TABLE_NAME_MAP = {
    "crm_lead_activities": "lead_activities",
}


def fix_schedule_time(s_time_raw, e_time_raw):
  """Ensure end_time is strictly greater than start_time."""
  try:
    s_str = str(s_time_raw)[:8]
    e_str = str(e_time_raw)[:8]

    # Handle standard HH:MM or HH:MM:SS
    if len(s_str) == 5:
      s_str += ":00"
    if len(e_str) == 5:
      e_str += ":00"

    st = datetime.strptime(s_str, "%H:%M:%S")
    et = datetime.strptime(e_str, "%H:%M:%S")

    if et <= st:
      # If 12-hour format bug (e.g. 12:00 to 01:00 PM)
      if et.hour < 12 and (et.hour + 12) > st.hour:
        et = et.replace(hour=et.hour + 12)
      else:
        # Advance by 50 mins
        et = st + timedelta(minutes=50)

    # Cap before midnight
    if et.hour < st.hour:
      return "23:59:59"
    return et.strftime("%H:%M:%S")
  except Exception:
    return "23:59:59"


def get_safe_default(col_name, data_type, udt_type):
  if udt_type == "uuid" or data_type == "uuid":
    return str(uuid.uuid4())
  if col_name in ("postal_code", "zip", "pincode", "postalcode"):
    return "400001"
  if col_name == "currency":
    return "INR"
  if col_name == "country":
    return "IN"
  if col_name == "timezone":
    return "Asia/Kolkata"
  if col_name in ("total_days", "duration_value", "validity_days"):
    return 30
  if col_name in (
      "version_number",
      "allocated_units",
      "display_order",
      "priority",
  ):
    return 1
  if col_name in ("capacity", "max_booking_capacity", "default_capacity"):
    return 15
  if data_type in ("character varying", "text", "character"):
    return ""
  if data_type == "boolean":
    return False
  if data_type in (
      "integer",
      "bigint",
      "smallint",
      "numeric",
      "decimal",
      "double precision",
      "real",
  ):
    return 0
  if "timestamp" in data_type:
    return datetime.now()
  if data_type == "date":
    return "1970-01-01"
  if data_type in ("json", "jsonb"):
    return psycopg2.extras.Json({})
  return ""


def get_pg_columns_and_types(pg_cur, table_name):
  pg_cur.execute(
      """
        SELECT column_name, data_type, udt_name, is_nullable, column_default 
        FROM information_schema.columns 
        WHERE table_schema = 'public' AND table_name = %s
    """,
      (table_name,),
  )
  res = pg_cur.fetchall()
  return {
      r[0]: {
          "data_type": r[1],
          "udt_name": r[2],
          "is_nullable": r[3],
          "column_default": r[4],
      }
      for r in res
  }


def get_mysql_columns(my_cur, table_name):
  my_cur.execute(f"SHOW COLUMNS FROM `{table_name}`")
  return {r["Field"] for r in my_cur.fetchall()}


def main():
  print("=" * 70)
  print(
      "Starting Migration: MySQL (sweat_tenant_db) -> PostgreSQL"
      " (tenant_sweat_demo)"
  )
  print("=" * 70)

  try:
    my_conn = pymysql.connect(**MYSQL_CONFIG)
    my_cur = my_conn.cursor()
    print(" Connected to MySQL (sweat_tenant_db)")
  except Exception as e:
    print(f" Failed to connect to MySQL: {e}")
    sys.exit(1)

  try:
    pg_conn = psycopg2.connect(**PG_TENANT_CONFIG)
    pg_cur = pg_conn.cursor()
    print(" Connected to PostgreSQL (tenant_sweat_demo)")
  except Exception as e:
    print(f" Failed to connect to PostgreSQL: {e}")
    sys.exit(1)

  print(
      "\n Pausing PostgreSQL foreign key checks (session_replication_role ="
      " 'replica')..."
  )
  pg_cur.execute("SET session_replication_role = 'replica';")
  pg_conn.commit()

  total_rows_migrated = 0

  for my_table in TABLES_TO_MIGRATE:
    pg_table = TABLE_NAME_MAP.get(my_table, my_table)

    my_cur.execute(f"SHOW TABLES LIKE '{my_table}'")
    if not my_cur.fetchone():
      continue

    pg_col_info = get_pg_columns_and_types(pg_cur, pg_table)
    if not pg_col_info:
      continue

    my_cols = get_mysql_columns(my_cur, my_table)
    common_cols = [c for c in my_cols if c in pg_col_info]

    table_defaults = SPECIFIC_DEFAULTS.get(pg_table, {})

    missing_required_cols = {}
    for col, meta in pg_col_info.items():
      if col not in common_cols and meta["is_nullable"] == "NO":
        if meta["column_default"] is None:
          fallback = table_defaults.get(
              col,
              get_safe_default(col, meta["data_type"], meta["udt_name"]),
          )
          if isinstance(fallback, (dict, list)):
            fallback = psycopg2.extras.Json(fallback)
          missing_required_cols[col] = fallback

    all_target_cols = common_cols + list(missing_required_cols.keys())
    if not all_target_cols:
      continue

    my_cur.execute(f"SELECT COUNT(*) AS cnt FROM `{my_table}`")
    total_in_table = my_cur.fetchone()["cnt"]
    if total_in_table == 0:
      continue

    col_list_str = ", ".join([f"`{c}`" for c in common_cols])
    my_cur.execute(f"SELECT {col_list_str} FROM `{my_table}`")

    pg_cols_str = ", ".join([f'"{c}"' for c in all_target_cols])

    insert_sql = f"""
            INSERT INTO "{pg_table}" ({pg_cols_str})
            VALUES %s
            ON CONFLICT DO NOTHING
        """

    rows_done = 0
    while True:
      batch = my_cur.fetchmany(BATCH_SIZE)
      if not batch:
        break

      cleaned_batch = []
      for row in batch:
        tuple_vals = []
        for col in common_cols:
          val = row[col]
          meta = pg_col_info[col]
          col_type = meta["data_type"]
          udt_type = meta["udt_name"]

          # 1. Convert MySQL zero-dates ('0000-00-00')
          if isinstance(val, str) and val.startswith("0000-"):
            val = (
                None
                if meta["is_nullable"] == "YES"
                else (
                    "1970-01-01"
                    if "date" in col_type
                    else "1970-01-01 00:00:00"
                )
            )

          # 2. Fix class_schedule_rules end_time > start_time constraint
          elif pg_table == "class_schedule_rules" and col == "end_time":
            val = fix_schedule_time(row.get("start_time"), val)

          # 3. Fix class_occurrences end_at > start_at constraint
          elif pg_table == "class_occurrences" and col == "end_at":
            s_at = row.get("start_at")
            if s_at and val and val <= s_at:
              if isinstance(s_at, datetime):
                val = s_at + timedelta(minutes=50)

          # 4. Convert booleans
          elif col_type == "boolean":
            val = bool(val) if val is not None else None

          # 5. Convert dict/list/json
          elif isinstance(val, (dict, list)):
            val = psycopg2.extras.Json(val)
          elif col_type in ("json", "jsonb"):
            if isinstance(val, str):
              try:
                val = psycopg2.extras.Json(json.loads(val))
              except Exception:
                val = psycopg2.extras.Json({})
            elif val is None and meta["is_nullable"] == "NO":
              val = psycopg2.extras.Json({})

          # 6. Convert empty strings on UUID fields
          elif udt_type == "uuid" or col_type == "uuid":
            if (
                val == ""
                or val is None
                or (isinstance(val, str) and not val.strip())
            ):
              val = None if meta["is_nullable"] == "YES" else str(uuid.uuid4())

          # 7. Handle check constraints on days/counts (must be > 0)
          if col in ("total_days", "duration_value", "validity_days"):
            if val is None or val == 0:
              dur = row.get("duration_value")
              val = int(dur) if dur and int(dur) > 0 else 30

          # 8. If NOT NULL in Postgres and value is None/null, fill safe default
          if val is None and meta["is_nullable"] == "NO":
            val = table_defaults.get(
                col, get_safe_default(col, col_type, udt_type)
            )
            if isinstance(val, (dict, list)):
              val = psycopg2.extras.Json(val)

          tuple_vals.append(val)

        # Append missing required columns
        for col, default_val in missing_required_cols.items():
          actual_val = default_val
          if col in ("total_days", "duration_value", "validity_days"):
            dur = row.get("duration_value")
            actual_val = int(dur) if dur and int(dur) > 0 else 30
          tuple_vals.append(actual_val)

        cleaned_batch.append(tuple(tuple_vals))

      psycopg2.extras.execute_values(
          pg_cur, insert_sql, cleaned_batch, page_size=BATCH_SIZE
      )
      pg_conn.commit()
      rows_done += len(cleaned_batch)

    print(f"✓ {pg_table:<32}: {rows_done:>7,} / {total_in_table:,} rows")
    total_rows_migrated += rows_done

  print(
      "\n Restoring PostgreSQL foreign keys (session_replication_role ="
      " 'origin')..."
  )
  pg_cur.execute("SET session_replication_role = 'origin';")
  pg_conn.commit()

  pg_cur.close()
  pg_conn.close()
  my_cur.close()
  my_conn.close()

  # 4. Register Tenant in Master Database (sweat_db)
  print("\n Registering Tenant in Master DB (sweat_db)...")
  try:
    master_conn = psycopg2.connect(**PG_MASTER_CONFIG)
    master_cur = master_conn.cursor()

    master_cur.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'tenants'
            );
        """)
    if master_cur.fetchone()[0]:
      master_cur.execute("""
                INSERT INTO tenants (id, name, slug, status, created_at, updated_at)
                VALUES (
                    '00000000-0000-4000-8000-000000000001',
                    'SWEAT Fitness & Wellness',
                    'sweat-demo',
                    'ACTIVE',
                    NOW(),
                    NOW()
                )
                ON CONFLICT (id) DO UPDATE SET status = 'ACTIVE';
            """)
      master_conn.commit()
      print("✓ Tenant registered in Master DB!")
    master_cur.close()
    master_conn.close()
  except Exception as e:
    print(f"⚠️  Master DB check: {e}")

  print("\n" + "=" * 70)
  print(f"MIGRATION COMPLETE: {total_rows_migrated:,} total rows migrated!")
  print("=" * 70)


if __name__ == "__main__":
  main()