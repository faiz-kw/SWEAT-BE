"""
Step 1 Verification Script: Canonical Database Model & Migration
Validates:
- ActivityAuditLog table creation in tenant database
- All required columns matching the 9-point contract
- Insert capability with full payload
- Append-only immutability at Django ORM level (update/delete blocked)
- Append-only immutability at PostgreSQL trigger level (raw SQL update/delete blocked)
"""

import os
import sys
import uuid
import django
from django.utils import timezone
from django.core.exceptions import PermissionDenied
from django.db import connections, DatabaseError

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.tenant_core.models_activity_audit import ActivityAuditLog
from apps.tenant_core.models_org import Organization, Branch
from apps.tenant_core.models_users import TenantUser
from config.tenant_middleware import _register_tenant_connection
from config.routers import set_tenant_db_alias

print("=" * 80)
print("STEP 1: CANONICAL DATABASE MODEL & IMMUTABILITY VERIFICATION")
print("=" * 80)

# Connect to tenant database
t = Tenant.objects.using('default').get(slug='sweat')
ds = TenantDataSource.objects.using('default').get(tenant=t)
db_alias = f"tenant_{t.slug}"
_register_tenant_connection(db_alias, db_name=ds.database_name, data_source=ds, tenant_id=t.id)
set_tenant_db_alias(db_alias)

org = Organization.objects.using(db_alias).first()
branch = Branch.objects.using(db_alias).first()
user = TenantUser.objects.using(db_alias).first()

print(f"Connected to DB alias: {db_alias}")
print(f"Organization: {org.name if org else 'None'} ({org.id if org else ''})")
print(f"Branch: {branch.name if branch else 'None'} ({branch.id if branch else ''})")
print(f"User: {user.email if user else 'None'} ({user.id if user else ''})")

# Test 1: Insert record
print("\n[Test 1: Model Creation & Persistence]")
test_id = uuid.uuid4()
log_entry = ActivityAuditLog(
    id=test_id,
    organization=org,
    branch=branch,
    actor_type='STAFF',
    actor_user=user,
    actor_role='Operations Manager',
    action_code='SYSTEM.INIT_TEST',
    action_category='SYSTEM',
    action_verb='INITIALIZED',
    module='SETTINGS',
    submodule='AUDIT_ENGINE',
    entity_type='SystemConfig',
    entity_id=str(uuid.uuid4()),
    entity_name='Audit Engine V2 Core',
    before_state={'status': 'legacy', 'tables': 2},
    after_state={'status': 'canonical_v2', 'tables': 3},
    field_changes=[
        {'field_name': 'status', 'field_label': 'System Status', 'old_value': 'legacy', 'new_value': 'canonical_v2'},
        {'field_name': 'tables', 'field_label': 'Audited Tables', 'old_value': 2, 'new_value': 3},
    ],
    event_description='Operations Manager initialized the Audit Engine V2 Canonical System for branch.',
    reason='System upgrade to comprehensive 18-module audit trail',
    result='SUCCESS',
    metadata={'environment': 'production_demo', 'version': '2.0.0'},
    ip_address='127.0.0.1',
    user_agent='Mozilla/5.0 AuditVerification/1.0',
    request_id=str(uuid.uuid4()),
    correlation_id=str(uuid.uuid4()),
    source_channel='WEB_ADMIN',
)
log_entry.save(using=db_alias)

retrieved = ActivityAuditLog.objects.using(db_alias).get(id=test_id)
assert retrieved is not None
assert retrieved.action_code == 'SYSTEM.INIT_TEST'
assert retrieved.branch_name == branch.name
assert len(retrieved.field_changes) == 2
print("  [PASS] Inserted and re-queried ActivityAuditLog with full 9-point contract payload.")

# Test 2: Django ORM immutability on save()
print("\n[Test 2: Django ORM Immutability on save()]")
orm_update_blocked = False
try:
    retrieved.reason = "Tampered reason string"
    retrieved.save(using=db_alias)
except PermissionDenied as e:
    orm_update_blocked = True
    print(f"  [PASS] save() modification blocked as expected: {e}")

assert orm_update_blocked, "Django ORM failed to block save() update on existing record!"

# Test 3: Django ORM immutability on delete()
print("\n[Test 3: Django ORM Immutability on delete()]")
orm_delete_blocked = False
try:
    retrieved.delete(using=db_alias)
except PermissionDenied as e:
    orm_delete_blocked = True
    print(f"  [PASS] delete() modification blocked as expected: {e}")

assert orm_delete_blocked, "Django ORM failed to block delete() on existing record!"

# Test 4: PostgreSQL Trigger Immutability on raw SQL UPDATE
print("\n[Test 4: PostgreSQL Trigger Immutability on raw SQL UPDATE]")
db_update_blocked = False
with connections[db_alias].cursor() as cursor:
    try:
        cursor.execute(
            "UPDATE activity_audit_logs SET reason = 'Malicious SQL update' WHERE id = %s",
            [str(test_id)]
        )
    except DatabaseError as e:
        db_update_blocked = True
        print(f"  [PASS] PostgreSQL trigger blocked raw SQL UPDATE: {e}".strip())

assert db_update_blocked, "PostgreSQL trigger failed to block raw SQL UPDATE!"

# Test 5: PostgreSQL Trigger Immutability on raw SQL DELETE
print("\n[Test 5: PostgreSQL Trigger Immutability on raw SQL DELETE]")
db_delete_blocked = False
with connections[db_alias].cursor() as cursor:
    try:
        cursor.execute(
            "DELETE FROM activity_audit_logs WHERE id = %s",
            [str(test_id)]
        )
    except DatabaseError as e:
        db_delete_blocked = True
        print(f"  [PASS] PostgreSQL trigger blocked raw SQL DELETE: {e}".strip())

assert db_delete_blocked, "PostgreSQL trigger failed to block raw SQL DELETE!"

print("\n" + "=" * 80)
print("STEP 1 VERIFICATION COMPLETED SUCCESSFULLY: 5/5 CHECKS PASSED")
print("=" * 80)
