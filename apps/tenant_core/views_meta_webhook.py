"""
apps/tenant_core/views_meta_webhook.py — Public Webhook Gateway for Meta Lead Ads.

Endpoints:
- GET /api/v1/webhooks/meta/leads/ — Webhook challenge verification (hub.mode, hub.challenge, hub.verify_token)
- POST /api/v1/webhooks/meta/leads/ — Inbound leadgen notification receiver (X-Hub-Signature-256 HMAC validated)
- GET/POST /api/v1/webhooks/meta/leads/<tenant_public_id>/ — Tenant-specific endpoint for dedicated Meta apps

Security:
- Strict HMAC-SHA256 signature verification over raw request body using META_APP_SECRET.
- Safe tenant resolution: Tenant is determined by verified Page ID registration in Master DB, NEVER from untrusted client parameters.
- Durable idempotency: Receipts committed to tenant DB before async task dispatch.
- Bounded processing: Returns HTTP 200 within <2s, offloading Graph API retrieval to Celery worker.
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
            # In tenant DB, check Integration model or use platform token
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
        raw_body = request.body
        signature = request.headers.get('X-Hub-Signature-256') or request.META.get('HTTP_X_HUB_SIGNATURE_256', '')

        _, app_secret, _ = get_meta_app_credentials()
        if not app_secret:
            logger.error("META_APP_SECRET is not configured on server. Rejecting webhook.")
            return Response({'error': 'Server security configuration incomplete'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        if not verify_signature(raw_body, signature, app_secret):
            logger.warning("Meta Webhook rejected: Invalid HMAC-SHA256 signature.")
            return Response({'error': 'Invalid signature'}, status=status.HTTP_403_FORBIDDEN)

        try:
            payload = json.loads(raw_body.decode('utf-8'))
        except Exception:
            return Response({'error': 'Malformed JSON payload'}, status=status.HTTP_400_BAD_REQUEST)

        if payload.get('object') != 'page':
            return Response({'status': 'ignored_non_page_object'}, status=status.HTTP_200_OK)

        entries = payload.get('entry', [])
        processed_count = 0

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

                # Safe Tenant Resolution
                tenant = self._resolve_tenant_for_page(change_page_id, tenant_public_id)
                if not tenant:
                    logger.warning("Unrouted Meta leadgen event: Page '%s' is not mapped to any active tenant. Event acknowledged.", change_page_id)
                    continue

                try:
                    with tenant_database_context(tenant.id) as alias:
                        org = Organization.objects.using(alias).filter(status='ACTIVE').order_by('-created_at').first()
                        if not org:
                            logger.error("Active organization not found for tenant %s", tenant.slug)
                            continue

                        event, created = receive_live_webhook_event(
                            organization=org,
                            page_id=change_page_id,
                            form_id=form_id,
                            leadgen_id=leadgen_id,
                            raw_payload=payload,
                            alias=alias,
                        )

                        if created or event.status == 'PENDING':
                            # Enqueue async task for Graph API retrieval & CRM ingestion
                            process_meta_lead_import_task.delay(str(tenant.id), str(event.id))
                            processed_count += 1

                except Exception as exc:
                    logger.exception("Error ingesting Meta webhook for tenant %s page %s: %s", tenant.slug, change_page_id, exc)

        return Response({'status': 'received', 'processed': processed_count}, status=status.HTTP_200_OK)

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
