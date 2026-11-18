"""
Step C Verification Script:
1. UniversalMutationAuditMiddleware catch-all coverage across uninstrumented modules.
2. Zero duplicate logging when domain code already calls UnifiedAuditEngine.record_activity.
3. Secret redaction in middleware payload capture.
4. GET requests ignored (zero noise).
5. Razorpay invalid signature security alert (RAZORPAY.SIGNATURE_FAILED).
"""
import os
import sys
import uuid
import json

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()

from django.test import RequestFactory
from rest_framework.response import Response
from rest_framework.request import Request
from rest_framework.parsers import JSONParser

from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from config.tenant_middleware import _register_tenant_connection
from config.routers import set_tenant_db_alias
from apps.tenant_core.models_org import Organization, Branch
from apps.tenant_core.models_rbac import RoleAssignment
from apps.tenant_core.models_activity_audit import ActivityAuditLog
from apps.tenant_core.audit_engine import UnifiedAuditEngine
from apps.tenant_core.middleware_activity_audit import UniversalMutationAuditMiddleware
from apps.tenant_core.views_razorpay_webhook import RazorpayWebhookView


def main():
    t = Tenant.objects.using("default").get(slug="sweat")
    ds = TenantDataSource.objects.using("default").get(tenant=t)
    alias = f"tenant_{t.slug}"
    _register_tenant_connection(alias, db_name=ds.database_name, data_source=ds, tenant_id=t.id)
    set_tenant_db_alias(alias)

    branch = Branch.objects.using(alias).first()
    admin_ra = RoleAssignment.objects.using(alias).filter(
        is_active=True, role__code__in=["ADMIN", "ORG_ADMIN", "OWNER", "SUPER_ADMIN"]
    ).select_related("user").first()
    admin_user = admin_ra.user
    admin_user._auth_type = "tenant"

    factory = RequestFactory()

    # 1. Catch-all POST on uninstrumented Support Ticket route
    ticket_id = str(uuid.uuid4())
    before_count = ActivityAuditLog.objects.using(alias).count()

    def dummy_ticket_view(req):
        drf_req = Request(req, parsers=[JSONParser()])
        drf_req.user = admin_user
        resp = Response(
            {"id": ticket_id, "ticket_number": "TKT-9001", "title": "AC not cooling in Studio B"},
            status=201,
        )
        resp.renderer_context = {"request": drf_req}
        return resp

    mw = UniversalMutationAuditMiddleware(dummy_ticket_view)
    req_post = factory.post(
        "/api/v1/tenant/tickets/",
        data=json.dumps({
            "title": "AC not cooling in Studio B",
            "branch_id": str(branch.id),
            "api_key": "SHOULD_BE_REDACTED",
        }),
        content_type="application/json",
    )
    resp = mw(req_post)
    assert resp.status_code == 201
    after_count = ActivityAuditLog.objects.using(alias).count()
    assert after_count == before_count + 1, "Catch-all middleware failed to log ticket POST!"
    latest = ActivityAuditLog.objects.using(alias).order_by("-occurred_at").first()
    assert latest.module == "SUPPORT"
    assert latest.action_code == "SUPPORT.CREATED"
    assert latest.entity_id == ticket_id
    assert latest.after_state["api_key"] == "[REDACTED]"
    assert latest.result == "SUCCESS"
    print(f"[PASS] 1. Catch-all logged uninstrumented POST /tickets/: [{latest.action_code}] {latest.event_description}")

    # 2. Catch-all FAILURE on uninstrumented Inventory PATCH
    prod_id = str(uuid.uuid4())
    def dummy_inv_fail_view(req):
        drf_req = Request(req, parsers=[JSONParser()])
        drf_req.user = admin_user
        resp = Response({"detail": "Stock quantity cannot be negative.", "code": "INVALID_STOCK"}, status=400)
        resp.renderer_context = {"request": drf_req}
        return resp

    mw_fail = UniversalMutationAuditMiddleware(dummy_inv_fail_view)
    req_patch = factory.patch(
        f"/api/v1/tenant/inventory/products/{prod_id}/",
        data=json.dumps({"stock_quantity": -5}),
        content_type="application/json",
    )
    resp_fail = mw_fail(req_patch)
    assert resp_fail.status_code == 400
    latest_fail = ActivityAuditLog.objects.using(alias).order_by("-occurred_at").first()
    assert latest_fail.module == "INVENTORY"
    assert latest_fail.result == "FAILURE"
    assert latest_fail.error_code == "INVALID_STOCK"
    assert "negative" in latest_fail.error_message
    print(f"[PASS] 2. Catch-all logged failed PATCH /inventory/products/: [{latest_fail.action_code}] {latest_fail.event_description}")

    # 3. Zero Duplicate Logging when domain view already calls UnifiedAuditEngine
    before_dedup = ActivityAuditLog.objects.using(alias).count()
    def domain_logged_view(req):
        drf_req = Request(req, parsers=[JSONParser()])
        drf_req.user = admin_user
        UnifiedAuditEngine.record_activity(
            action_code="CUSTOM.DOMAIN_EXPLICIT",
            module="WORKFORCE",
            entity_type="ShiftRoster",
            entity_name="November Shift Roster",
            actor_user=admin_user,
            db_alias=alias,
        )
        resp = Response({"ok": True}, status=200)
        resp.renderer_context = {"request": drf_req}
        return resp

    mw_dedup = UniversalMutationAuditMiddleware(domain_logged_view)
    req_dedup = factory.post("/api/v1/tenant/workforce/rosters/", data="{}", content_type="application/json")
    mw_dedup(req_dedup)
    after_dedup = ActivityAuditLog.objects.using(alias).count()
    assert after_dedup == before_dedup + 1, f"Duplicate log detected! Expected +1, got +{after_dedup - before_dedup}"
    print("[PASS] 3. Zero duplicate logging verified when domain service writes explicit audit log.")

    # 4. GET requests ignored (zero noise)
    before_get = ActivityAuditLog.objects.using(alias).count()
    mw_get = UniversalMutationAuditMiddleware(lambda r: Response({"ok": True}, status=200))
    mw_get(factory.get("/api/v1/tenant/inventory/products/"))
    assert ActivityAuditLog.objects.using(alias).count() == before_get
    print("[PASS] 4. GET read-only requests safely ignored (zero noise).")

    # 5. Razorpay invalid signature security alert
    before_sig = ActivityAuditLog.objects.using(alias).filter(action_code="RAZORPAY.SIGNATURE_FAILED").count()
    req_rzp = factory.post(
        "/api/v1/webhooks/razorpay/sweat/",
        data=json.dumps({"event": "payment.captured", "payload": {}}),
        content_type="application/json",
        HTTP_X_RAZORPAY_SIGNATURE="invalid_forged_sig_123",
    )
    res_rzp = RazorpayWebhookView.as_view()(req_rzp, tenant_slug="sweat")
    assert res_rzp.status_code == 400
    after_sig = ActivityAuditLog.objects.using(alias).filter(action_code="RAZORPAY.SIGNATURE_FAILED").count()
    assert after_sig == before_sig + 1, "RAZORPAY.SIGNATURE_FAILED not recorded!"
    print("[PASS] 5. Razorpay invalid HMAC signature recorded RAZORPAY.SIGNATURE_FAILED security alert.")

    print("=== STEP C ALL CHECKS PASSED ===")


if __name__ == "__main__":
    main()
