"""
apps/tenant_core/views_pos.py — POS Machine REST API Endpoints

Provides API routes for:
1. Listing available terminals for cashier selection per branch.
2. Initiating a card/UPI payment push to a physical terminal.
3. Polling real-time transaction status (with automatic finalization).
4. Cancelling an active payment session on the terminal screen.
5. Admin configuration of POS providers and physical terminals.
"""

import logging
from decimal import Decimal
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, permissions
from django.core.exceptions import ValidationError

from config.routers import get_tenant_db_alias
from .models_org import Branch
from .models_pos import POSTerminal, POSTransaction, POSProviderConfig
from .services_pos import POSOrchestratorService

logger = logging.getLogger(__name__)


def _get_active_db_alias(request) -> str:
    return getattr(request, "tenant_db_alias", None) or get_tenant_db_alias() or "default"


class BranchPOSTerminalsView(APIView):
    """
    GET /api/v1/pos/terminals/?branch_id=<branch_uuid>
    Returns active physical card machines available at a specific gym branch.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        db_alias = _get_active_db_alias(request)
        branch_id = request.query_params.get("branch_id")

        query = POSTerminal.objects.using(db_alias).filter(status="ACTIVE").select_related("provider_config", "branch")
        if branch_id:
            query = query.filter(branch_id=branch_id)

        terminals = [
            {
                "id": str(t.id),
                "name": t.name,
                "terminal_id": t.terminal_id,
                "device_serial_no": t.device_serial_no,
                "model_name": t.model_name,
                "status": t.status,
                "is_default": t.is_default_for_branch,
                "provider_code": t.provider_config.provider_code,
                "provider_name": t.provider_config.display_name,
                "branch_id": str(t.branch_id),
                "branch_name": t.branch.name,
            }
            for t in query
        ]
        return Response({"terminals": terminals}, status=status.HTTP_200_OK)


class InitiatePOSPaymentView(APIView):
    """
    POST /api/v1/pos/initiate/
    Body:
    {
        "order_id": "<uuid>",
        "terminal_id": "<uuid or TID>" (optional, defaults to branch machine)
    }
    Pushes an in-person charge to the physical card machine.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        db_alias = _get_active_db_alias(request)
        order_id = request.data.get("order_id")
        terminal_id = request.data.get("terminal_id")

        if not order_id:
            return Response(
                {"error": "order_id is required.", "code": "ORDER_ID_REQUIRED"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            pos_tx = POSOrchestratorService.initiate_pos_payment(
                order_id=order_id,
                terminal_id=terminal_id,
                actor=request.user if hasattr(request, "user") else None,
                db_alias=db_alias,
            )
            return Response(
                {
                    "success": pos_tx.status in ["INITIATED", "PENDING"],
                    "transaction_id": str(pos_tx.id),
                    "external_ref_id": pos_tx.external_ref_id,
                    "status": pos_tx.status,
                    "amount": str(pos_tx.amount),
                    "currency": pos_tx.currency,
                    "terminal_name": pos_tx.terminal.name,
                    "terminal_id": pos_tx.terminal.terminal_id,
                    "error_message": pos_tx.error_message,
                },
                status=status.HTTP_201_CREATED if pos_tx.status in ["INITIATED", "PENDING"] else status.HTTP_400_BAD_REQUEST,
            )
        except ValidationError as e:
            return Response(
                {"error": str(e), "code": "POS_INITIATE_FAILED"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as e:
            logger.exception("Unexpected error initiating POS payment: %s", e)
            return Response(
                {"error": "Failed to initiate payment on POS machine.", "details": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class POSPaymentStatusView(APIView):
    """
    GET /api/v1/pos/status/<str:external_ref_id>/
    Cashier's browser polls this endpoint every 2-3 seconds while waiting for swipe/tap.
    Polls the cloud status and completes the order atomically once payment is approved.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, external_ref_id):
        db_alias = _get_active_db_alias(request)
        try:
            pos_tx, pay_tx = POSOrchestratorService.poll_and_finalize_status(
                external_ref_id=external_ref_id,
                actor=request.user if hasattr(request, "user") else None,
                db_alias=db_alias,
            )
            return Response(
                {
                    "external_ref_id": pos_tx.external_ref_id,
                    "status": pos_tx.status,
                    "amount": str(pos_tx.amount),
                    "currency": pos_tx.currency,
                    "payment_mode": pos_tx.payment_mode,
                    "rrn": pos_tx.rrn,
                    "auth_code": pos_tx.auth_code,
                    "card_network": pos_tx.card_network,
                    "card_last4": pos_tx.card_last4,
                    "order_status": pos_tx.order.status,
                    "error_message": pos_tx.error_message,
                    "is_completed": pos_tx.status == "SUCCESS",
                },
                status=status.HTTP_200_OK,
            )
        except ValidationError as e:
            return Response(
                {"error": str(e), "code": "POS_STATUS_ERROR"},
                status=status.HTTP_400_BAD_REQUEST,
            )


class CancelPOSPaymentView(APIView):
    """
    POST /api/v1/pos/cancel/<str:external_ref_id>/
    Cashier clicks 'Cancel' on the screen to abort the transaction on the physical machine.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, external_ref_id):
        db_alias = _get_active_db_alias(request)
        reason = request.data.get("reason", "Cancelled by cashier from CRM")

        try:
            pos_tx = POSOrchestratorService.cancel_payment(
                external_ref_id=external_ref_id,
                reason=reason,
                db_alias=db_alias,
            )
            return Response(
                {
                    "external_ref_id": pos_tx.external_ref_id,
                    "status": pos_tx.status,
                    "message": "Payment session cancelled successfully.",
                },
                status=status.HTTP_200_OK,
            )
        except ValidationError as e:
            return Response(
                {"error": str(e), "code": "POS_CANCEL_FAILED"},
                status=status.HTTP_400_BAD_REQUEST,
            )

