import psycopg2
import pymysql

pg_conn = psycopg2.connect(
    host="localhost", port=5432, user="postgres", password="masheera11", dbname="tenant_sweat_demo"
)
pg_cur = pg_conn.cursor()

# Get lead columns
pg_cur.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'leads'")
cols = pg_cur.fetchall()
print("Postgres leads columns:", [c[0] for c in cols])

# Get total leads count
pg_cur.execute("SELECT COUNT(*) FROM leads")
print("Postgres total leads count:", pg_cur.fetchone()[0])

# Sample leads
pg_cur.execute("SELECT * FROM leads LIMIT 2")
print("Postgres sample lead:", pg_cur.fetchone())

# Check tables matching lead_source
pg_cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name LIKE '%source%'")
print("Source tables in PG:", pg_cur.fetchall())

# Check lead_sources table if exists
try:
    pg_cur.execute("SELECT * FROM lead_sources")
    print("lead_sources records:", pg_cur.fetchall())
except Exception as e:
    pg_conn.rollback()
    print("lead_sources error:", e)

# Status distribution
pg_cur.execute("SELECT current_status, COUNT(*) FROM leads GROUP BY current_status ORDER BY COUNT(*) DESC")
print("current_status distribution:", pg_cur.fetchall())

# Check how many leads have lead_source_id
pg_cur.execute("SELECT COUNT(*), COUNT(lead_source_id), COUNT(first_touch_source), COUNT(latest_touch_source) FROM leads")
print("Source counts:", pg_cur.fetchone())

