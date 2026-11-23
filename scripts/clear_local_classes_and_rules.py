import os
import sys
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.db import connections
from apps.master.models import Tenant, TenantDataSource
from config.tenant_middleware import _register_tenant_connection
from config.routers import build_tenant_db_alias, set_tenant_db_alias

def run_clear():
    t = Tenant.objects.filter(slug='sweat').first() or Tenant.objects.first()
    ds = TenantDataSource.objects.filter(tenant=t).first()
    db_alias = build_tenant_db_alias(t.id)
    _register_tenant_connection(db_alias, db_name=ds.db_name, data_source=ds, tenant_id=t.id)
    set_tenant_db_alias(db_alias)

    print(f"=== Clearing Classes & Recurrence Rules from Local Database: {db_alias} ({ds.db_name}) ===")

    cursor = connections[db_alias].cursor()
    # Check tables
    sql_find_fks = """
    SELECT tc.table_name, ccu.table_name AS foreign_table_name
    FROM information_schema.table_constraints AS tc
    JOIN information_schema.key_column_usage AS kcu
      ON tc.constraint_name = kcu.constraint_name
    JOIN information_schema.constraint_column_usage AS ccu
      ON ccu.constraint_name = tc.constraint_name
    WHERE constraint_type = 'FOREIGN KEY'
      AND ccu.table_name IN ('tenant_core_classoccurrence', 'tenant_core_classschedulerule');
    """
    cursor.execute(sql_find_fks)
    fks = cursor.fetchall()
    print("Dependent tables referencing classoccurrence / classschedulerule:")
    for table_name, foreign_table_name in fks:
        print(f"  {table_name} -> {foreign_table_name}")

    from apps.tenant_core.models_classes import ClassOccurrence, ClassScheduleRule

    table_occ = ClassOccurrence._meta.db_table
    table_rule = ClassScheduleRule._meta.db_table

    print(f"\nExecuting TRUNCATE on {table_occ} and {table_rule} CASCADE...")
    cursor.execute(f"TRUNCATE TABLE {table_occ}, {table_rule} CASCADE;")

    cursor.execute(f"SELECT COUNT(*) FROM {table_occ};")
    occ_count = cursor.fetchone()[0]
    cursor.execute(f"SELECT COUNT(*) FROM {table_rule};")
    rule_count = cursor.fetchone()[0]

    print(f"\nResult:")
    print(f"  tenant_core_classoccurrence count: {occ_count}")
    print(f"  tenant_core_classschedulerule count: {rule_count}")
    print("Local database classes and recurrence rules successfully wiped clean!")

if __name__ == '__main__':
    run_clear()
