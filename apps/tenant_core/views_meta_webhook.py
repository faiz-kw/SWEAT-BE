"""
apps/tenant_core/views_meta_webhook.py - Public Webhook Gateway for Meta Lead Ads.

Endpoints:
- GET /api/v1/webhooks/meta/leads/ - Webhook challenge verification (hub.mode, hub.challenge, hub.verify_token)
- POST /api/v1/webhooks/meta/leads/ - Inbound leadgen notification receiver (X-Hub-Signature-256 HMAC validated)
- GET/POST /api/v1/webhooks/meta/leads/<tenant_public_id>/ - Tenant-specific endpoint for dedicated Meta apps

Security:
- Strict HMAC-SHA256 signature verification over raw request body using META_APP_SECRET.
- Safe tenant resolution: Tenant is determined by verified Page ID registration in Master DB, NEVER from untrusted client parameters.
- Durable idempotency: Receipts committed to tenant DB before async task dispatch.
- Bounded processing: Returns HTTP 200 within <2s, offloading Graph API retrieval to Celery worker.
- Production-safe structured logging on all lifecycle events.
"""
import json
import logging
from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.db.models import Q

from apps.master.models_tenant import Tenant, MetaPageRegistry
from apps.tenant_core.context import tenant_database_context
from apps.tenant_core.models_org import Organization
from apps.tenant_core.meta_lead_rules import verify_signature
from apps.tenant_core.services_meta_graph import get_meta_app_credentials
from apps.tenant_core.services_meta_leads import receive_live_webhook_event
from apps.tenant_core.tasks_meta_leads import process_meta_lead_import_task
from apps.tenant_core.meta_logging import (
    log_meta_event,
    EVT_WEBHOOK_RECEIVED,
    EVT_SIGNATURE_FAILED,
    EVT_PAGE_RESOLVED,
    EVT_PAGE_RESOLUTION_FAILED,
    EVT_IMPORT_QUEUED,
    EVT_DUPLICATE_IGNORED,
    EVT_PROCESSING_FAILED,
)

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name='dispatch')
class MetaLeadWebhookView(APIView):
    """
    Public Meta Webhook Receiver for Facebook / Instagram Lead Ads.
    Handles verification challenges and real-time lead delivery.
    """
    authentication_classes = []
    permission_classes = []

    def get(self, request, tenant_public_id: str = None):
        """
        Meta Webhook verification handshake.
        Meta sends: hub.mode, hub.challenge, hub.verify_token
        """
        hub_mode = request.GET.get('hub.mode')
        hub_challenge = request.GET.get('hub.challenge')
        hub_token = request.GET.get('hub.verify_token')

        if hub_mode != 'subscribe' or not hub_challenge:
            return Response({'error': 'Missing required subscription parameters'}, status=status.HTTP_400_BAD_REQUEST)

        _, _, platform_verify_token = get_meta_app_credentials()

        # If tenant_public_id is provided, verify against that tenant's configured token if present
        expected_token = platform_verify_token
        if tenant_public_id:
            tenant = self._resolve_tenant(tenant_public_id)
            if not tenant:
                return Response({'error': 'Invalid tenant identifier'}, status=status.HTTP_404_NOT_FOUND)
            with tenant_database_context(tenant.id) as alias:
                from apps.tenant_core.models_infra import Integration
                integ = Integration.objects.using(alias).filter(integration_type='LEADS', provider__iexact='META').first()
                if integ and (integ.configuration or {}).get('verify_token'):
                    expected_token = integ.configuration['verify_token']

        if not expected_token:
            logger.error("Meta Webhook verification failed: No verify token configured on server.")
            return Response({'error': 'Server verification token not configured'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        if hub_token != expected_token:
            logger.warning("Meta Webhook verification token mismatch.")
            return Response({'error': 'Verification token mismatch'}, status=status.HTTP_403_FORBIDDEN)

        # Meta expects challenge returned as plain text
        return HttpResponse(hub_challenge, content_type='text/plain')

    def post(self, request, tenant_public_id: str = None):
        """
        Inbound real-time lead notification from Meta.
        Payload contains page ID, form ID, and leadgen ID.
        """
        correlation_id = getattr(request, 'correlation_id', '-') or request.headers.get('X-Request-ID', '-')
        raw_body = request.body
        signature = request.headers.get('X-Hub-Signature-256') or request.META.get('HTTP_X_HUB_SIGNATURE_256', '')

        log_meta_event(
            EVT_WEBHOOK_RECEIVED,
            "Inbound Meta leadgen webhook received",
            correlation_id=correlation_id,
            extra={'content_length': len(raw_body)},
        )

        _, app_secret, _ = get_meta_app_credentials()
        if not app_secret:
            logger.error("META_APP_SECRET is not configured on server. Rejecting webhook.")
            return Response({'error': 'Server security configuration incomplete'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        if not verify_signature(raw_body, signature, app_secret):
            log_meta_event(
                EVT_SIGNATURE_FAILED,
                "Meta webhook rejected: Invalid HMAC-SHA256 signature",
                correlation_id=correlation_id,
                level=logging.WARNING,
            )
            return Response({'error': 'Invalid signature'}, status=status.HTTP_403_FORBIDDEN)

        try:
            payload = json.loads(raw_body.decode('utf-8'))
        except Exception:
            return Response({'error': 'Malformed JSON payload'}, status=status.HTTP_400_BAD_REQUEST)

        if payload.get('object') != 'page':
            return Response({'status': 'ignored_non_page_object'}, status=status.HTTP_200_OK)

        entries = payload.get('entry', [])
        total_received = 0
        enqueued_count = 0
        pending_recovery_count = 0
        duplicate_ignored_count = 0

        for entry in entries:
            page_id = str(entry.get('id') or '')
            changes = entry.get('changes', [])

            for change in changes:
                if change.get('field') != 'leadgen':
                    continue

                value = change.get('value', {})
                leadgen_id = str(value.get('leadgen_id') or '')
                form_id = str(value.get('form_id') or '')
                change_page_id = str(value.get('page_id') or page_id)

                if not leadgen_id:
                    continue

                total_received += 1

                # Safe Tenant Resolution
                tenant = self._resolve_tenant_for_page(change_page_id, tenant_public_id)
                if not tenant:
                    log_meta_event(
                        EVT_PAGE_RESOLUTION_FAILED,
                        f"Unrouted Meta leadgen event: Page '{change_page_id}' is not mapped to any active tenant. Event acknowledged.",
                        correlation_id=correlation_id,
                        page_id=change_page_id,
                        form_id=form_id,
                        leadgen_id=leadgen_id,
                        level=logging.WARNING,
                    )
                    continue

                log_meta_event(
                    EVT_PAGE_RESOLVED,
                    f"Resolved Page '{change_page_id}' to tenant '{tenant.slug}' ({tenant.id})",
                    correlation_id=correlation_id,
                    tenant_id=str(tenant.id),
                    page_id=change_page_id,
                    form_id=form_id,
                    leadgen_id=leadgen_id,
                )

                try:
                    with tenant_database_context(tenant.id) as alias:
                        org = Organization.objects.using(alias).filter(status='ACTIVE').order_by('-created_at').first()
                        if not org:
                            logger.error("Active organization not found for tenant %s", tenant.slug)
                            continue

                        # 1. Durable PostgreSQL commit before async dispatch
                        event, created = receive_live_webhook_event(
                            organization=org,
                            page_id=change_page_id,
                            form_id=form_id,
                            leadgen_id=leadgen_id,
                            raw_payload=payload,
                            alias=alias,
                        )

                        if created:
                            # 2. Protected Celery dispatch: failure leaves record safely in PENDING
                            try:
                                process_meta_lead_import_task.delay(str(tenant.id), str(event.id))
                                enqueued_count += 1
                                log_meta_event(
                                    EVT_IMPORT_QUEUED,
                                    "Durable MetaLeadImport created and Celery task enqueued",
                                    correlation_id=correlation_id,
                                    import_id=str(event.id),
                                    tenant_id=str(tenant.id),
                                    page_id=change_page_id,
                                    form_id=form_id,
                                    leadgen_id=leadgen_id,
                                    status='PENDING',
                                )
                            except Exception as dispatch_exc:
                                pending_recovery_count += 1
                                logger.error(
                                    "Celery dispatch failed for MetaLeadImport %s (tenant %s): %s. "
                                    "Record safely committed as PENDING in PostgreSQL for background recovery.",
                                    event.id, tenant.slug, dispatch_exc,
                                )
                                log_meta_event(
                                    EVT_PROCESSING_FAILED,
                                    f"Celery task dispatch failed ({type(dispatch_exc).__name__}). Import preserved as PENDING for background recovery.",
                                    correlation_id=correlation_id,
                                    import_id=str(event.id),
                                    tenant_id=str(tenant.id),
                                    page_id=change_page_id,
                                    form_id=form_id,
                                    leadgen_id=leadgen_id,
                                    status='PENDING',
                                    error_code='DISPATCH_FAILED',
                                    extra={'dispatch_error': str(dispatch_exc)[:100]},
                                    level=logging.ERROR,
                                )
                        elif event.status in ('PENDING', 'RETRYING'):
                            # Existing pending event re-dispatched
                            try:
                                process_meta_lead_import_task.delay(str(tenant.id), str(event.id))
                                enqueued_count += 1
                                log_meta_event(
                                    EVT_IMPORT_QUEUED,
                                    "Existing pending MetaLeadImport re-enqueued for processing",
                                    correlation_id=correlation_id,
                                    import_id=str(event.id),
                                    tenant_id=str(tenant.id),
                                    page_id=change_page_id,
                                    form_id=form_id,
                                    leadgen_id=leadgen_id,
                                    status=event.status,
                                )
                            except Exception as dispatch_exc:
                                pending_recovery_count += 1
                                logger.error(
                                    "Celery re-dispatch failed for MetaLeadImport %s (tenant %s): %s. "
                                    "Record remains %s for recovery.",
                                    event.id, tenant.slug, dispatch_exc, event.status,
                                )
                                log_meta_event(
                                    EVT_PROCESSING_FAILED,
                                    f"Celery task re-dispatch failed ({type(dispatch_exc).__name__}). Import remains {event.status} for recovery.",
                                    correlation_id=correlation_id,
                                    import_id=str(event.id),
                                    tenant_id=str(tenant.id),
                                    page_id=change_page_id,
                                    form_id=form_id,
                                    leadgen_id=leadgen_id,
                                    status=event.status,
                                    error_code='DISPATCH_FAILED',
                                    extra={'dispatch_error': str(dispatch_exc)[:100]},
                                    level=logging.ERROR,
                                )
                        else:
                            duplicate_ignored_count += 1
                            log_meta_event(
                                EVT_DUPLICATE_IGNORED,
                                f"Duplicate Meta leadgen webhook received. Acknowledged idempotently (current status: {event.status}).",
                                correlation_id=correlation_id,
                                import_id=str(event.id),
                                tenant_id=str(tenant.id),
                                page_id=change_page_id,
                                form_id=form_id,
                                leadgen_id=leadgen_id,
                                status=event.status,
                            )

                except Exception as exc:
                    logger.exception("Error ingesting Meta webhook for tenant %s page %s: %s", tenant.slug, change_page_id, exc)

        return Response({
            'status': 'received',
            'processed': enqueued_count,
            'received': total_received,
            'enqueued': enqueued_count,
            'pending_recovery': pending_recovery_count,
            'duplicates_ignored': duplicate_ignored_count,
        }, status=status.HTTP_200_OK)

    def _resolve_tenant(self, public_id: str) -> Tenant | None:
        """Resolve tenant by UUID or slug in Master DB."""
        try:
            tenant = Tenant.objects.using('default').filter(id=public_id).first()
            if tenant and tenant.is_accessible:
                return tenant
        except Exception:
            pass
        tenant = Tenant.objects.using('default').filter(
            Q(slug=public_id) | Q(code=public_id)
        ).first()
        if tenant and tenant.is_accessible:
            return tenant
        return None

    def _resolve_tenant_for_page(self, page_id: str, tenant_public_id: str = None) -> Tenant | None:
        """
        Safe tenant resolution:
        1. If tenant_public_id provided in URL, verify that tenant owns the page.
        2. Otherwise, lookup page_id in MetaPageRegistry in Master DB.
        3. Fallback: check MetaPageConnection in active tenant DBs if not yet registered in Master.
        """
        # 1. Master DB MetaPageRegistry
        reg = MetaPageRegistry.objects.using('default').filter(page_id=page_id, is_active=True).select_related('tenant').first()
        if reg and reg.tenant and reg.tenant.is_accessible:
            if tenant_public_id:
                scoped_tenant = self._resolve_tenant(tenant_public_id)
                if scoped_tenant and scoped_tenant.id == reg.tenant_id:
                    return reg.tenant
                return None
            return reg.tenant

        # 2. If tenant_public_id provided, verify directly in tenant DB
        if tenant_public_id:
            tenant = self._resolve_tenant(tenant_public_id)
            if tenant:
                with tenant_database_context(tenant.id) as alias:
                    from apps.tenant_core.models_meta_leads import MetaPageConnection, MetaLeadMapping
                    has_page = MetaPageConnection.objects.using(alias).filter(page_id=page_id, is_active=True).exists()
                    has_mapping = MetaLeadMapping.objects.using(alias).filter(page_id=page_id, is_active=True).exists()
                    if has_page or has_mapping:
                        return tenant
            return None

        # 3. Fallback: Search across active tenants in Master DB
        for tenant in Tenant.objects.using('default').filter(status='ACTIVE'):
            try:
                with tenant_database_context(tenant.id) as alias:
                    from apps.tenant_core.models_meta_leads import MetaPageConnection, MetaLeadMapping
                    if MetaPageConnection.objects.using(alias).filter(page_id=page_id, is_active=True).exists() or \
                       MetaLeadMapping.objects.using(alias).filter(page_id=page_id, is_active=True).exists():
                        # Cache registration in Master DB for future O(1) lookups
                        MetaPageRegistry.objects.using('default').update_or_create(
                            page_id=page_id,
                            defaults={'tenant': tenant, 'is_active': True},
                        )
                        return tenant
            except Exception:
                continue

        return None
