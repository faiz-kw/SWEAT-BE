"""
Step 2 Verification Script: Unified Core Audit Engine
Validates:
1. Direct `record_activity` with 9-point contract
2. State diff calculation (`compute_field_changes`) with human labels
3. Sensitive key redaction (`sanitize_state`)
4. Simple English narrative generation (`synthesize_narrative`)
5. Dual-write and backward compatibility of `emit_audit_event`
6. Fix of TypeError in `emit_audit_event` with custom kwargs
7. Dual-write and backward compatibility of `record_business_audit`
"""

import os
import sys
import uuid
import django
from django.utils import timezone

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.tenant_core.models_activity_audit import ActivityAuditLog
from apps.tenant_core.models_privacy import TenantAuditEvent
from apps.tenant_core.models_audit_outbox import BusinessAuditEvent
from apps.tenant_core.models_org import Organization, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.audit_engine import (
    UnifiedAuditEngine,
    compute_field_changes,
    sanitize_state,
    synthesize_narrative,
)
from apps.tenant_core.audit import emit_audit_event
from apps.tenant_core.services_reliability import record_business_audit
from config.tenant_middleware import _register_tenant_connection
from config.routers import set_tenant_db_alias

print("=" * 80)
print("STEP 2: UNIFIED AUDIT ENGINE & DIFF ENGINE VERIFICATION")
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

# Test 1: Sensitive Key Redaction
print("\n[Test 1: Sensitive Data Redaction]")
raw_payload = {
    'user_id': str(uuid.uuid4()),
    'email': 'athlete@sweat.fit',
    'password': 'SuperSecretPassword123!',
    'nested': {
        'access_token': 'eyJh...secret.token',
        'otp_code': '987654',
        'payment': {
            'cvv': '123',
            'credit_card': '4111222233334444',
            'amount': 5000,
        }
    }
}
clean = sanitize_state(raw_payload)
assert clean['password'] == '[REDACTED]', "Password was not redacted!"
assert clean['nested']['access_token'] == '[REDACTED]', "Access token was not redacted!"
assert clean['nested']['otp_code'] == '[REDACTED]', "OTP code was not redacted!"
assert clean['nested']['payment']['cvv'] == '[REDACTED]', "CVV was not redacted!"
assert clean['nested']['payment']['credit_card'] == '[REDACTED]', "Credit card was not redacted!"
assert clean['nested']['payment']['amount'] == 5000, "Clean primitive was improperly altered!"
print("  [PASS] Recursive sanitizer accurately redacted all credentials, tokens, OTPs, and CVVs.")

# Test 2: Granular State Diff Calculation
print("\n[Test 2: Granular Model State Diff Calculation]")
before = {
    'first_name': 'Jane',
    'last_name': 'Doe',
    'phone_number': '+91-9876543210',
    'status': 'ACTIVE',
    'is_active': True,
    'password': 'hashed_val',
    'updated_at': '2026-11-01T10:00:00Z',
}
after = {
    'first_name': 'Jane',
    'last_name': 'Smith',
    'phone_number': '+91-9876543299',
    'status': 'FROZEN',
    'is_active': False,
    'password': 'new_hashed_val',
    'updated_at': '2026-11-18T10:00:00Z',
}
diffs = compute_field_changes(before, after)
diff_keys = {d['field_name'] for d in diffs}
assert 'last_name' in diff_keys
assert 'phone_number' in diff_keys
assert 'status' in diff_keys
assert 'is_active' in diff_keys
assert 'first_name' not in diff_keys  # Unchanged
assert 'password' not in diff_keys    # Ignored
assert 'updated_at' not in diff_keys  # Ignored
print(f"  [PASS] Diff engine calculated {len(diffs)} granular field changes, ignoring internal timestamps and passwords.")
for d in diffs:
    print(f"       - {d['field_label']}: '{d['old_value']}' -> '{d['new_value']}'")

# Test 3: Simple English Narrative Synthesis
print("\n[Test 3: Simple English Narrative Synthesis]")
narrative = synthesize_narrative(
    actor_name='John Doe',
    actor_role='Front Desk Lead',
    action_verb='UPDATED',
    module='MEMBERS',
    entity_type='Member',
    entity_name='Jane Smith',
    field_changes=diffs,
    reason='Customer requested legal surname change and phone update',
)
print(f"  Generated narrative:\n    \"{narrative}\"")
assert 'Front Desk Lead John Doe' in narrative
assert 'Member \'Jane Smith\'' in narrative
assert 'Last Name' in narrative
assert 'Reason: Customer requested legal surname change' in narrative
print("  [PASS] Narrative synthesizer generated fluent, transparent, non-technical English.")

# Test 4: Direct record_activity
print("\n[Test 4: Direct UnifiedAuditEngine.record_activity]")
initial_activity_count = ActivityAuditLog.objects.using(db_alias).count()
act_entry = UnifiedAuditEngine.record_activity(
    action_code='MEMBER.PROFILE_UPDATED',
    module='MEMBERS',
    entity_type='Member',
    entity_id=str(user.id),
    entity_name=f"{user.first_name} {user.last_name}",
    actor_user=user,
    actor_role='Admin',
    before_state=before,
    after_state=after,
    reason='User requested profile contact correction',
    branch=branch,
    organization=org,
    source_channel='WEB_ADMIN',
    db_alias=db_alias,
)
assert act_entry is not None
assert act_entry.action_code == 'MEMBER.PROFILE_UPDATED'
assert act_entry.action_verb == 'UPDATED'
assert act_entry.action_category == 'MEMBERSHIP'
assert act_entry.branch_name == branch.name
assert len(act_entry.field_changes) == len(diffs)
print(f"  [PASS] record_activity successfully created ActivityAuditLog #{act_entry.id}.")

# Test 5: emit_audit_event Dual-Write & Bug Fix (No TypeError with custom kwargs)
print("\n[Test 5: emit_audit_event Dual-Write & Kwargs Robustness]")
count_legacy_before = TenantAuditEvent.objects.using(db_alias).count()
count_act_before = ActivityAuditLog.objects.using(db_alias).count()

# Call with kwargs that previously caused fatal TypeError in authentication views
ev = emit_audit_event(
    action='UPDATE_PERMISSIONS',
    resource_type='Role',
    resource_id=str(uuid.uuid4()),
    actor=user,
    actor_type='TENANT_USER',
    description='Modified role permission matrix for Trainer',
    actor_id=str(user.id),
    actor_roles=['Manager'],
    target_type='Role',
    target_id=str(uuid.uuid4()),
    metadata={'module': 'ACCESS_CONTROL', 'scope': 'branch'},
    db_alias=db_alias,
)
assert ev is not None, "emit_audit_event failed to create TenantAuditEvent!"
count_legacy_after = TenantAuditEvent.objects.using(db_alias).count()
count_act_after = ActivityAuditLog.objects.using(db_alias).count()

assert count_legacy_after == count_legacy_before + 1, "TenantAuditEvent not created!"
assert count_act_after == count_act_before + 1, "ActivityAuditLog mirror not created!"
print("  [PASS] emit_audit_event succeeded with extra kwargs without TypeError and mirrored to ActivityAuditLog.")

# Test 6: record_business_audit Dual-Write
print("\n[Test 6: record_business_audit Dual-Write]")
count_biz_before = BusinessAuditEvent.objects.using(db_alias).count()
count_act_before = ActivityAuditLog.objects.using(db_alias).count()

biz_ev = record_business_audit(
    organization=org,
    branch=branch,
    module='CRM',
    action_code='LEAD.NOTE_ADDED',
    entity_type='Lead',
    entity_id=uuid.uuid4(),
    actor_user=user,
    event_description='Staff added note to lead: Contacted via phone',
    before_data=None,
    after_data={'note_text': 'Spoke about annual packages', 'contact_method': 'phone'},
    db_alias=db_alias,
)
assert biz_ev is not None
count_biz_after = BusinessAuditEvent.objects.using(db_alias).count()
count_act_after = ActivityAuditLog.objects.using(db_alias).count()

assert count_biz_after == count_biz_before + 1, "BusinessAuditEvent not created!"
assert count_act_after == count_act_before + 1, "ActivityAuditLog mirror not created!"
print("  [PASS] record_business_audit succeeded and mirrored to ActivityAuditLog.")

print("\n" + "=" * 80)
print("STEP 2 VERIFICATION COMPLETED SUCCESSFULLY: 6/6 CHECKS PASSED")
print("=" * 80)
