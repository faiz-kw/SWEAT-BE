"""
backfill_historical_audit_logs.py — Idempotent Additive Historical Backfill

Migrates existing historical audit records from legacy tables:
  1. `audit_events` (TenantAuditEvent)
  2. `business_audit_events` (BusinessAuditEvent)
into the canonical `activity_audit_logs` ledger across all active tenants.

Safety Guarantees:
- Purely additive (INSERT only; never updates or deletes any existing record).
- 100% idempotent via deterministic UUID5 IDs + bulk_create(ignore_conflicts=True).
- Preserves original `occurred_at` timestamps, actors, branches, and before/after states.
- Runs recursive secret redaction and computes granular `field_changes` diffs.
"""

import os
import sys
import uuid

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()

from django.utils import timezone
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from config.tenant_middleware import _register_tenant_connection
from config.routers import set_tenant_db_alias
from apps.tenant_core.models_org import Organization, Branch
from apps.tenant_core.models_privacy import TenantAuditEvent
from apps.tenant_core.models_audit_outbox import BusinessAuditEvent
from apps.tenant_core.models_activity_audit import ActivityAuditLog
from apps.tenant_core.audit_engine import (
    UnifiedAuditEngine,
    sanitize_state,
    compute_field_changes,
    synthesize_narrative,
)

BACKFILL_NAMESPACE = uuid.UUID("c7f49080-5d91-4d8e-9a77-94c8e2a00001")


def _map_actor_type(raw_type: str) -> str:
    t = (raw_type or "").upper()
    if t in ("SUPER_ADMIN", "ADMIN", "ORG_ADMIN"):
        return "ADMIN"
    if t in ("TENANT_USER", "USER", "EMPLOYEE", "STAFF"):
        return "STAFF"
    if t == "MEMBER":
        return "MEMBER"
    if t in ("SYSTEM_JOB", "SYSTEM"):
        return "SYSTEM"
    if t in ("INTEGRATION", "WEBHOOK", "API"):
        return "WEBHOOK"
    return "STAFF"


def _map_source_channel(raw_channel: str) -> str:
    c = (raw_channel or "").upper()
    if c in ("ADMIN_PANEL", "WEB_ADMIN", "WEB", "FRONT_DESK", "TRAINER_PANEL", "SALES_PANEL"):
        return "WEB_ADMIN"
    if c in ("MOBILE_APP", "MOBILE"):
        return "MOBILE_APP"
    if c in ("MEMBER_PORTAL", "PORTAL"):
        return "MEMBER_PORTAL"
    if c in ("INTEGRATION", "WEBHOOK", "API"):
        return "WEBHOOK"
    if c in ("SYSTEM", "MIGRATION", "BACKGROUND_WORKER"):
        return "BACKGROUND_WORKER"
    return "WEB_ADMIN"


def backfill_tenant(tenant: Tenant) -> dict:
    ds = TenantDataSource.objects.using("default").filter(tenant=tenant, status="ACTIVE").first()
    if not ds or not (ds.database_name or ds.db_name):
        return {"tenant": tenant.slug, "skipped": True, "reason": "No active data source"}

    db_alias = f"tenant_{tenant.slug}"
    db_name = ds.database_name or ds.db_name
    _register_tenant_connection(db_alias, db_name=db_name, data_source=ds, tenant_id=tenant.id)
    set_tenant_db_alias(db_alias)

    default_org = Organization.objects.using(db_alias).first()
    branch_map = {str(b.id): b for b in Branch.objects.using(db_alias).all()}

    existing_ids = set(
        str(x) for x in ActivityAuditLog.objects.using(db_alias).values_list("id", flat=True)
    )

    to_insert = []
    stats = {"tenant": tenant.slug, "audit_events_added": 0, "business_audit_added": 0, "skipped_existing": 0}

    # 1. Backfill TenantAuditEvent (audit_events)
    legacy_events = TenantAuditEvent.objects.using(db_alias).select_related("actor").all().order_by("created_at")
    for ev in legacy_events:
        det_id = uuid.uuid5(BACKFILL_NAMESPACE, f"audit_events:{ev.id}")
        if str(det_id) in existing_ids:
            stats["skipped_existing"] += 1
            continue

        actor_user = ev.actor
        actor_email = ev.actor_email or (actor_user.email if actor_user else "")
        actor_name = ""
        if actor_user:
            actor_name = f"{actor_user.first_name or ''} {actor_user.last_name or ''}".strip() or actor_email
        elif actor_email:
            actor_name = actor_email.split("@")[0].replace(".", " ").title()
        else:
            actor_name = "System"

        actor_role = ev.actor_role or ("Super Admin" if ev.actor_type == "SUPER_ADMIN" else "Staff")
        actor_type = _map_actor_type(ev.actor_type)
        action_code = (ev.event_name or f"{ev.resource_type}.{ev.action}").upper()[:100]
        action_verb = (ev.action or "modified").lower()
        module = (ev.resource_type or "ADMIN").upper()[:50]
        category = UnifiedAuditEngine._infer_category(action_code, module)

        before_clean = sanitize_state(ev.before_state) if ev.before_state else None
        after_clean = sanitize_state(ev.after_state) if ev.after_state else None
        changes = compute_field_changes(before_clean, after_clean) if (before_clean or after_clean) else []

        desc = (ev.description or "").strip()
        if not desc:
            desc = synthesize_narrative(
                actor_name=actor_name,
                actor_role=actor_role,
                action_verb=action_verb,
                module=module,
                entity_type=ev.resource_type or "Record",
                entity_name=str(ev.resource_id or ev.resource_type or "Record"),
                field_changes=changes,
                result="SUCCESS",
                reason=ev.reason or "",
            )

        br_obj = branch_map.get(str(ev.branch_id)) if ev.branch_id else None
        org_id = ev.organization_id or (default_org.id if default_org else None)

        to_insert.append(ActivityAuditLog(
            id=det_id,
            organization_id=org_id,
            branch=br_obj,
            branch_name=br_obj.name if br_obj else "",
            actor_type=actor_type,
            actor_user=actor_user,
            actor_id=str(actor_user.id) if actor_user else None,
            actor_email=actor_email[:255],
            actor_name=actor_name[:255],
            actor_role=actor_role[:100],
            action_code=action_code,
            action_category=category,
            action_verb=action_verb[:50],
            module=module,
            submodule="LEGACY_AUDIT",
            entity_type=(ev.resource_type or "Record")[:100],
            entity_id=str(ev.resource_id)[:64] if ev.resource_id else None,
            entity_name=str(ev.resource_id or ev.resource_type or "Record")[:255],
            before_state=before_clean,
            after_state=after_clean,
            field_changes=changes,
            event_description=desc,
            reason=ev.reason or "",
            remarks="",
            result="SUCCESS",
            error_code="",
            error_message="",
            metadata={"backfilled_from": "audit_events", "legacy_id": str(ev.id)},
            ip_address=ev.ip_address,
            user_agent=ev.user_agent or "",
            request_id=str(ev.request_id or "")[:64],
            correlation_id=str(ev.correlation_id or "")[:64],
            source_channel=_map_source_channel(ev.source_application),
            occurred_at=ev.created_at or timezone.now(),
        ))
        existing_ids.add(str(det_id))
        stats["audit_events_added"] += 1

    # 2. Backfill BusinessAuditEvent (business_audit_events)
    biz_qs = BusinessAuditEvent.objects.using(db_alias).select_related("actor_user", "branch").all().order_by("occurred_at")
    for bev in biz_qs.iterator(chunk_size=500):
        det_id = uuid.uuid5(BACKFILL_NAMESPACE, f"business_audit_events:{bev.id}")
        if str(det_id) in existing_ids:
            stats["skipped_existing"] += 1
            continue

        actor_user = bev.actor_user
        actor_email = actor_user.email if actor_user else ""
        if actor_user:
            actor_name = f"{actor_user.first_name or ''} {actor_user.last_name or ''}".strip() or actor_email
        else:
            actor_name = "System Automation" if bev.actor_type == "SYSTEM" else (bev.actor_type or "System").title()

        actor_type = _map_actor_type(bev.actor_type)
        actor_role = (actor_user.user_type if actor_user and getattr(actor_user, "user_type", None) else actor_type).title()

        module = (bev.module or "CRM").upper()[:50]
        action_code = (bev.action_code or f"{module}.EVENT").upper()[:100]
        action_verb = action_code.split(".")[-1].replace("_", " ").lower()[:50]
        category = UnifiedAuditEngine._infer_category(action_code, module)

        before_clean = sanitize_state(bev.before_data) if bev.before_data else None
        after_clean = sanitize_state(bev.after_data) if bev.after_data else None
        changes = compute_field_changes(before_clean, after_clean) if (before_clean or after_clean) else []

        entity_name = ""
        if isinstance(after_clean, dict):
            for k in ("name", "full_name", "title", "order_number", "membership_number", "email"):
                if after_clean.get(k) and isinstance(after_clean[k], str):
                    entity_name = after_clean[k][:255]
                    break
        if not entity_name:
            entity_name = f"{bev.entity_type} #{str(bev.entity_id)[:8]}" if bev.entity_id else (bev.entity_type or "Record")

        desc = (bev.event_description or "").strip()
        if not desc:
            desc = synthesize_narrative(
                actor_name=actor_name,
                actor_role=actor_role,
                action_verb=action_verb,
                module=module,
                entity_type=bev.entity_type or "Record",
                entity_name=entity_name,
                field_changes=changes,
                result="SUCCESS",
            )

        meta_clean = sanitize_state(bev.metadata) if isinstance(bev.metadata, dict) else {}
        if not isinstance(meta_clean, dict):
            meta_clean = {}
        meta_clean["backfilled_from"] = "business_audit_events"
        meta_clean["legacy_id"] = str(bev.id)
        if bev.parent_entity_type:
            meta_clean["parent_entity_type"] = bev.parent_entity_type
        if bev.parent_entity_id:
            meta_clean["parent_entity_id"] = str(bev.parent_entity_id)

        br_obj = bev.branch
        to_insert.append(ActivityAuditLog(
            id=det_id,
            organization_id=bev.organization_id or (default_org.id if default_org else None),
            branch=br_obj,
            branch_name=br_obj.name if br_obj else "",
            actor_type=actor_type,
            actor_user=actor_user,
            actor_id=str(actor_user.id) if actor_user else None,
            actor_email=actor_email[:255],
            actor_name=actor_name[:255],
            actor_role=actor_role[:100],
            action_code=action_code,
            action_category=category,
            action_verb=action_verb,
            module=module,
            submodule=(bev.entity_type or "").upper()[:50],
            entity_type=(bev.entity_type or "Record")[:100],
            entity_id=str(bev.entity_id)[:64] if bev.entity_id else None,
            entity_name=entity_name[:255],
            before_state=before_clean,
            after_state=after_clean,
            field_changes=changes,
            event_description=desc,
            reason="",
            remarks="",
            result="SUCCESS",
            error_code="",
            error_message="",
            metadata=meta_clean,
            ip_address=bev.ip_address,
            user_agent=bev.user_agent or "",
            request_id=str(bev.request_id or "")[:64],
            correlation_id="",
            source_channel=_map_source_channel(bev.source_channel),
            occurred_at=bev.occurred_at or bev.created_at or timezone.now(),
        ))
        existing_ids.add(str(det_id))
        stats["business_audit_added"] += 1

        if len(to_insert) >= 500:
            ActivityAuditLog.objects.using(db_alias).bulk_create(to_insert, batch_size=500, ignore_conflicts=True)
            to_insert.clear()

    if to_insert:
        ActivityAuditLog.objects.using(db_alias).bulk_create(to_insert, batch_size=500, ignore_conflicts=True)

    stats["final_activity_audit_count"] = ActivityAuditLog.objects.using(db_alias).count()
    return stats


def main():
    tenants = Tenant.objects.using("default").filter(status="ACTIVE")
    print(f"Found {tenants.count()} ACTIVE tenant(s) for historical audit backfill.")
    for t in tenants:
        res = backfill_tenant(t)
        print(" ->", res)


if __name__ == "__main__":
    main()
