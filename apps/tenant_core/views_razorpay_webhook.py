"""
apps/tenant_core/views_razorpay_webhook.py — Razorpay Webhook Ingestion Endpoint

Receives asynchronous event webhooks from Razorpay (e.g. payment.captured, payment.failed, order.paid).
Validates HMAC-SHA256 signature using the raw request body and tenant webhook secret.
Dispatches captured payments to RazorpayPaymentFinalizerService to ensure exactly-once
finalization even if the user closed their browser or network dropped before callback.
"""

import json
import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, permissions
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from apps.master.models_tenant import Tenant
from apps.authentication.views import _register_and_resolve_tenant
from .services_razorpay import RazorpayService, RazorpayPaymentFinalizerService, RazorpayVerificationError

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name='dispatch')
class RazorpayWebhookView(APIView):
    """
    POST /api/v1/webhooks/razorpay/
    POST /api/v1/webhooks/razorpay/<str:tenant_slug>/

    Ingests official Razorpay event webhooks with HMAC-SHA256 signature validation.
    """
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def post(self, request, tenant_slug=None):
        raw_body = request.body
        signature = (
            request.headers.get('X-Razorpay-Signature')
            or request.META.get('HTTP_X_RAZORPAY_SIGNATURE')
            or ''
        ).strip()

        if not signature:
            logger.warning("Razorpay webhook rejected: missing X-Razorpay-Signature header.")
            return Response(
                {'error': 'Missing X-Razorpay-Signature header.', 'code': 'SIGNATURE_MISSING'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 1. Resolve tenant DB alias
        db_alias = 'default'
        tenant = None

        if tenant_slug:
            tenant = Tenant.objects.using('default').filter(slug=tenant_slug).first()
            if not tenant:
                tenant = Tenant.objects.using('default').filter(id=tenant_slug).first()
            if tenant:
                db_alias = _register_and_resolve_tenant(tenant)

        # Parse payload
        try:
            data = json.loads(raw_body.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return Response(
                {'error': 'Invalid JSON payload.', 'code': 'INVALID_JSON'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # If tenant not specified in URL, attempt to infer from payment notes
        if db_alias == 'default':
            payment_entity = (
                data.get('payload', {}).get('payment', {}).get('entity', {})
                or data.get('payload', {}).get('order', {}).get('entity', {})
            )
            notes = payment_entity.get('notes') or {}
            inferred_tenant_id = notes.get('tenant_id') or notes.get('org_id')
            if inferred_tenant_id:
                tenant = Tenant.objects.using('default').filter(id=inferred_tenant_id).first()
                if tenant:
                    db_alias = _register_and_resolve_tenant(tenant)

        # 2. Verify webhook signature
        is_valid = RazorpayService.verify_webhook_signature(
            raw_body=raw_body,
            signature=signature,
            db_alias=db_alias,
        )

        if not is_valid:
            logger.warning(
                "Razorpay webhook signature verification failed for tenant=%s (db=%s)",
                tenant_slug or 'global', db_alias
            )
            return Response(
                {'error': 'Invalid webhook signature.', 'code': 'RAZORPAY_SIGNATURE_INVALID'},
                status=status.HTTP_400_BAD_REQUEST
            )

        event = data.get('event')
        logger.info("Razorpay webhook verified: event=%s tenant=%s", event, tenant_slug or 'global')

        # 3. Handle events
        if event in ('payment.captured', 'order.paid'):
            payment_entity = data.get('payload', {}).get('payment', {}).get('entity', {})
            rzp_payment_id = payment_entity.get('id')
            rzp_order_id = payment_entity.get('order_id')

            if not rzp_order_id:
                order_entity = data.get('payload', {}).get('order', {}).get('entity', {})
                rzp_order_id = order_entity.get('id')

            if not rzp_payment_id or not rzp_order_id:
                logger.warning("Webhook %s missing payment_id or order_id in payload", event)
                return Response({'error': 'Missing payment or order entity.'}, status=status.HTTP_400_BAD_REQUEST)

            try:
                result = RazorpayPaymentFinalizerService.finalize_payment(
                    razorpay_order_id=rzp_order_id,
                    razorpay_payment_id=rzp_payment_id,
                    source='WEBHOOK',
                    db_alias=db_alias,
                    raw_event=data,
                )
                return Response({
                    'status': 'ok',
                    'event': event,
                    'result': result,
                }, status=status.HTTP_200_OK)
            except RazorpayVerificationError as rve:
                logger.warning("Razorpay webhook finalization verification error: %s", rve)
                return Response({'error': str(rve), 'code': getattr(rve, 'code', 'VERIFICATION_ERROR')}, status=status.HTTP_400_BAD_REQUEST)
            except Exception as ex:
                logger.error("Error finalizing payment via webhook: %s", ex, exc_info=True)
                return Response({'error': 'Internal finalization error.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        elif event == 'payment.failed':
            payment_entity = data.get('payload', {}).get('payment', {}).get('entity', {})
            rzp_payment_id = payment_entity.get('id')
            rzp_order_id = payment_entity.get('order_id')
            logger.info("Razorpay payment %s failed for order %s", rzp_payment_id, rzp_order_id)
            # Update PaymentTransaction to FAILED if exists
            if rzp_order_id:
                from .models_commerce import PaymentTransaction
                PaymentTransaction.objects.using(db_alias).filter(
                    metadata__razorpay_order_id=rzp_order_id,
                    status='INITIATED'
                ).update(
                    status='FAILED'
                )
            return Response({'status': 'ok', 'event': event}, status=status.HTTP_200_OK)

        # Acknowledge other events
        return Response({'status': 'ok', 'event': event, 'message': 'Event ignored'}, status=status.HTTP_200_OK)
